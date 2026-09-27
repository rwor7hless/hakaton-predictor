"""Обучение ансамбля, честная валидация и сборка сабмита.

Запуск::

    python -m mlcore.train --out ../../artifacts/models/current --submission ../../artifacts/submission.csv

Схема валидации: обучение на train без синтетических точек, перекрывающих
периоды test/validate (синтетика — сдвинутые по времени копии реальных ТС),
проверка на labels_test. Финальные модели учатся на train + test, из train
исключается только синтетика, перекрывающая validate.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .data import DATASET_DIR, build_all
from .models import OFFLINE_COLS, ONLINE_COLS, CatBag, RiskModels

ROOT = Path(__file__).resolve().parents[3]
SEQ_WEIGHT = 0.5


def mae(y, p) -> float:
    """Средняя абсолютная ошибка, с."""
    return float(np.mean(np.abs(np.asarray(y) - np.asarray(p))))


def interval_scale(y: np.ndarray, r: dict, target: float = 0.8) -> float:
    """Конформная калибровка: во сколько раз расширить квантильный интервал до покрытия ``target``."""
    for k in np.arange(1.0, 6.01, 0.05):
        lo, hi = r["q50"] - k * (r["q50"] - r["q10"]), r["q50"] + k * (r["q90"] - r["q50"])
        if np.mean((y >= lo) & (y <= hi)) >= target:
            return float(round(k, 2))
    return 6.0


def evaluate(tables: dict[str, pd.DataFrame], seeds: int, use_seq: bool) -> dict:
    """Честная оценка на labels_test: возвращает метрики всех компонентов."""
    tr = tables["train"][~tables["train"]["leak_test_val"]]
    te = tables["test"]
    y, yt = tr["target_delay_s"].to_numpy(), te["target_delay_s"].to_numpy()
    res = {"n_train": int(len(tr)), "n_test": int(len(te)),
           "zero_mae_s": mae(yt, 0), "baseline_mae_s": mae(yt, te["cur_dev"])}
    off = CatBag(OFFLINE_COLS, seeds=seeds).fit(tr, y)
    on = CatBag(ONLINE_COLS, seeds=seeds).fit(tr, y)
    p_off, p_on = off.predict(te), on.predict(te)
    res["offline_mae_s"] = mae(yt, p_off)
    res["online_catboost_mae_s"] = mae(yt, p_on)
    risk = RiskModels(ONLINE_COLS).fit(tr, y)
    r = risk.predict(te)
    res["online_q10_q90_coverage_raw"] = float(np.mean((yt >= r["q10"]) & (yt <= r["q90"])))
    k = interval_scale(yt, r)
    res["interval_scale"] = k
    lo, hi = r["q50"] - k * (r["q50"] - r["q10"]), r["q50"] + k * (r["q90"] - r["q50"])
    res["online_q10_q90_coverage"] = float(np.mean((yt >= lo) & (yt <= hi)))
    lab = np.where(yt > 120, 2, np.where(yt < -60, 0, 1))
    pred_lab = np.where(r["p_late"] > 0.5, 2, np.where(r["p_early"] > 0.5, 0, 1))
    res["online_class_accuracy"] = float(np.mean(lab == pred_lab))
    if use_seq:
        from .seq import NUM_COLS, SeqModel
        p_seq = SeqModel.fit_frame(tr, y).predict_frame(te)
        res["online_seq_mae_s"] = mae(yt, p_seq)
        res["online_seq_weight"] = SEQ_WEIGHT
        res["online_ensemble_mae_s"] = mae(yt, (1 - SEQ_WEIGHT) * p_on + SEQ_WEIGHT * p_seq)
        p_seq_off = SeqModel.fit_frame(tr, y, num_cols=["cur_dev"] + NUM_COLS).predict_frame(te)
        res["offline_seq_mae_s"] = mae(yt, p_seq_off)
        res["offline_catboost_mae_s"] = res["offline_mae_s"]
        res["offline_mae_s"] = mae(yt, (1 - SEQ_WEIGHT) * p_off + SEQ_WEIGHT * p_seq_off)
    else:
        res["online_seq_weight"], res["online_ensemble_mae_s"] = 0.0, res["online_catboost_mae_s"]
    mt = res["zero_mae_s"] - (res["zero_mae_s"] - res["baseline_mae_s"]) / 0.40
    res["implied_score_offline"] = float(np.clip((res["zero_mae_s"] - res["offline_mae_s"]) / (res["zero_mae_s"] - mt), 0, 1))
    return res


def train_final(tables: dict[str, pd.DataFrame], out: Path, seeds: int, seq_weight: float, metrics: dict) -> None:
    """Обучить финальные модели на train + test и сохранить артефакты."""
    tr = pd.concat([tables["train"][~tables["train"]["leak_val"]], tables["test"]], ignore_index=True)
    y = tr["target_delay_s"].to_numpy()
    out.mkdir(parents=True, exist_ok=True)
    CatBag(OFFLINE_COLS, seeds=seeds).fit(tr, y).save(out, "offline")
    CatBag(ONLINE_COLS, seeds=seeds).fit(tr, y).save(out, "online")
    RiskModels(ONLINE_COLS).fit(tr, y).save(out, "risk")
    if seq_weight > 0:
        from .seq import NUM_COLS, SeqModel
        SeqModel.fit_frame(tr, y).save(out, "seq_online")
        SeqModel.fit_frame(tr, y, num_cols=["cur_dev"] + NUM_COLS).save(out, "seq_offline")
    meta = {
        "version": datetime.now().strftime("%Y%m%d-%H%M"),
        "trained_at": datetime.now().isoformat(timespec="seconds"),
        "n_train": int(len(tr)),
        "offline_cols": OFFLINE_COLS,
        "online_cols": ONLINE_COLS,
        "ensemble": {"catboost": 1 - seq_weight, "seq": seq_weight},
        "interval_scale": metrics.get("interval_scale", 1.0),
        "metrics": metrics,
    }
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))


def make_submission(tables: dict[str, pd.DataFrame], models: Path, path: Path) -> pd.DataFrame:
    """Прогноз по validate офлайн-моделью в формате ``sample_id;prediction``."""
    va = tables["val"]
    meta = json.loads((models / "meta.json").read_text())
    w = meta["ensemble"]["seq"]
    pred = CatBag.load(models, "offline").predict(va)
    if w > 0:
        from .seq import SeqModel
        pred = (1 - w) * pred + w * SeqModel.load(models, "seq_offline").predict_frame(va)
    sub = pd.DataFrame({"sample_id": va["sample_id"], "prediction": np.round(pred, 1)})
    tmpl = pd.read_csv(DATASET_DIR / "sample_submission.csv", sep=";")
    sub = tmpl[["sample_id"]].merge(sub, on="sample_id", how="left")
    assert sub["prediction"].notna().all() and len(sub) == len(tmpl) and sub["sample_id"].is_unique
    path.parent.mkdir(parents=True, exist_ok=True)
    sub.to_csv(path, sep=";", index=False)
    return sub


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache", type=Path, default=ROOT / "artifacts/cache")
    ap.add_argument("--out", type=Path, default=ROOT / "artifacts/models/current")
    ap.add_argument("--submission", type=Path, default=ROOT / "artifacts/submission.csv")
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--no-seq", action="store_true", help="не обучать PyTorch-модель последовательностей")
    ap.add_argument("--skip-eval", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    tables = build_all(a.cache)
    metrics = {} if a.skip_eval else evaluate(tables, a.seeds, not a.no_seq)
    print(json.dumps(metrics, indent=2))
    train_final(tables, a.out, a.seeds, 0.0 if a.no_seq else SEQ_WEIGHT, metrics)
    sub = make_submission(tables, a.out, a.submission)
    print(f"submission: {a.submission} ({len(sub)} rows), total {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
