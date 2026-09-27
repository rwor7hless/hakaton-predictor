"""Загрузка артефактов ансамбля и инференс для API."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np
import pandas as pd

from .models import CatBag, RiskModels

FEATURE_TEXT = {
    "cur_dev": "отклонение на последней остановке",
    "est_now": "текущее отклонение от графика",
    "dev_last": "отклонение на последней пройденной остановке",
    "dev_max15": "максимальное отклонение за 15 мин",
    "dev_mean5": "среднее отклонение на 5 последних остановках",
    "dev_slope5": "тренд отклонения",
    "lb_next": "опоздание к следующей остановке",
    "lp_lb": "опоздание к плановой остановке",
    "lp_dev": "отклонение на плановой остановке",
    "prev_loop_delta": "изменение отклонения на этом участке в прошлом рейсе",
    "prev_loop_age": "давность прошлого рейса",
    "tgt_stop": "профиль целевой остановки (систематическое отклонение расписания)",
    "route": "профиль маршрута",
    "n_between": "число остановок до цели",
    "n_plan_between": "число плановых остановок до цели",
    "plan_run": "плановое время хода до цели",
    "plan_since_lp": "время с плановой остановки",
    "gap_prev_plan": "плановый интервал перед целевой остановкой",
    "gap_next_plan": "плановый интервал после целевой остановки",
    "tgt_role": "тип целевой остановки (конечная/промежуточная)",
    "lead": "горизонт прогноза",
    "dwell": "длительность текущей стоянки",
    "stop5": "доля времени в простое за 5 мин",
    "spd5": "средняя скорость за 5 мин",
    "spd2": "средняя скорость за 2 мин",
    "path5": "путь за 5 мин",
    "dist_tgt": "расстояние до целевой остановки",
    "dist_next": "расстояние до следующей остановки",
    "gps_age": "давность последней GPS-точки",
    "valid_share5": "доля валидных GPS-точек",
    "since_last": "время с последней пройденной остановки",
    "at_stop": "ТС на остановке",
    "dev_last_interp": "последняя остановка восстановлена интерполяцией",
}


class Predictor:
    """Ансамбль: CatBoost (бэггинг) + PyTorch/ONNX GRU, квантильный интервал и вероятности классов.

    Args:
        model_dir: каталог с артефактами (``meta.json``, ``*.cbm``, ``*.onnx``).
    """

    def __init__(self, model_dir: Path):
        self.dir = Path(model_dir)
        self.lock = threading.RLock()
        self.load()

    def load(self) -> None:
        """(Пере)загрузить артефакты из каталога — используется и для горячей замены модели."""
        meta = json.loads((self.dir / "meta.json").read_text())
        online = CatBag.load(self.dir, "online")
        offline = CatBag.load(self.dir, "offline")
        risk = RiskModels.load(self.dir, "risk")
        seq_on = seq_off = None
        w = float(meta.get("ensemble", {}).get("seq", 0.0))
        if w > 0:
            from .seq import SeqModel
            seq_on = SeqModel.load(self.dir, "seq_online")
            seq_off = SeqModel.load(self.dir, "seq_offline")
        with self.lock:
            self.meta, self.online, self.offline, self.risk = meta, online, offline, risk
            self.seq_on, self.seq_off, self.w = seq_on, seq_off, w
            self.scale = float(meta.get("interval_scale", 1.0))

    @staticmethod
    def _frame(items: list[dict]) -> pd.DataFrame:
        rows = []
        for it in items:
            f = dict(it["features"])
            f.setdefault("tgt_stop", "NA")
            f.setdefault("route", "NA")
            f.setdefault("seq", [0.0] * 120)
            if f["seq"] is None or len(f["seq"]) != 120:
                f["seq"] = [0.0] * 120
            rows.append(f)
        return pd.DataFrame(rows)

    def predict(self, items: list[dict], explain: bool = True, top: int = 5) -> list[dict]:
        """Онлайн-прогноз: задержка, интервал q10–q90, p_late/p_early и вклады признаков."""
        if not items:
            return []
        df = self._frame(items)
        with self.lock:
            cat = self.online.predict(df)
            delay = (1 - self.w) * cat + self.w * self.seq_on.predict_frame(df) if self.seq_on is not None else cat
            r = self.risk.predict(df)
            k = self.scale
            lo = delay - k * np.maximum(r["q50"] - r["q10"], 0)
            hi = delay + k * np.maximum(r["q90"] - r["q50"], 0)
            contrib = self._contrib(df, top) if explain else [[] for _ in items]
        return [{"id": it["id"], "delay_s": float(delay[i]), "q10_s": float(lo[i]), "q90_s": float(hi[i]),
                 "p_late": float(r["p_late"][i]), "p_early": float(r["p_early"][i]), "contributions": contrib[i]}
                for i, it in enumerate(items)]

    def predict_offline(self, items: list[dict]) -> list[dict]:
        """Прогноз по точкам с подсказкой ``cur_dev`` (режим сабмита validate)."""
        if not items:
            return []
        df = self._frame(items)
        with self.lock:
            p = self.offline.predict(df)
            if self.seq_off is not None:
                p = (1 - self.w) * p + self.w * self.seq_off.predict_frame(df)
        return [{"id": it["id"], "delay_s": float(p[i])} for i, it in enumerate(items)]

    def _contrib(self, df: pd.DataFrame, top: int) -> list[list[dict]]:
        m = self.online.models[0]
        from catboost import Pool

        from .models import _cat, prepare
        X = prepare(df, self.online.cols)
        sv = m.get_feature_importance(Pool(X, cat_features=_cat(self.online.cols)), type="ShapValues")[:, :-1]
        out = []
        for i in range(len(df)):
            order = np.argsort(-np.abs(sv[i]))[:top]
            row = []
            for j in order:
                name = self.online.cols[j]
                val = X.iloc[i, j]
                row.append({"feature": name, "text": FEATURE_TEXT.get(name, name),
                            "value": None if isinstance(val, float) and not np.isfinite(val) else
                            (val if isinstance(val, str) else float(val)),
                            "shap": float(sv[i, j])})
            out.append(row)
        return out

    def info(self) -> dict:
        """Метаданные модели для ``GET /v1/model``."""
        with self.lock:
            return {**self.meta, "model_dir": str(self.dir)}
