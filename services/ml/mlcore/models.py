"""Наборы признаков и обёртки над моделями ансамбля."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier, CatBoostRegressor, Pool

from transit_core.features import CAT_FEATURES

PLAN = ["lead", "gap_prev_plan", "gap_next_plan", "tgt_role", "n_plan_between", "plan_since_lp"]
TRACK = ["est_now", "dev_last", "dev_last_interp", "since_last", "n_between", "plan_run", "lb_next", "lp_dev",
         "lp_lb", "dev_mean5", "dev_slope5", "dev_max15", "prev_loop_delta", "prev_loop_age"]
GPS = ["gps_age", "valid_share5", "spd2", "spd5", "stop5", "path5", "dwell", "at_stop", "dist_tgt", "dist_next"]

OFFLINE_COLS = ["cur_dev"] + PLAN + ["tgt_stop", "route"]
ONLINE_COLS = PLAN + TRACK + GPS + ["tgt_stop", "route"]

LATE_S = 120.0
EARLY_S = -60.0
QUANTILES = (0.1, 0.5, 0.9)
PRED_CLIP = (-600.0, 900.0)


def _cat(cols: list[str]) -> list[str]:
    return [c for c in cols if c in CAT_FEATURES]


def prepare(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Привести таблицу к нужному набору колонок: числовые → float, категориальные → str."""
    X = pd.DataFrame(index=df.index)
    for c in cols:
        if c in CAT_FEATURES:
            X[c] = df[c].astype(str).fillna("NA") if c in df else "NA"
        else:
            X[c] = pd.to_numeric(df[c], errors="coerce").astype(float) if c in df else np.nan
    return X


@dataclass
class CatBag:
    """Бэггинг CatBoost-регрессоров с MAE по нескольким сидам."""

    cols: list[str]
    seeds: int = 5
    params: dict = field(default_factory=lambda: dict(iterations=1500, learning_rate=0.03, depth=6))
    models: list[CatBoostRegressor] = field(default_factory=list)

    def fit(self, df: pd.DataFrame, y: np.ndarray) -> "CatBag":
        X = prepare(df, self.cols)
        self.models = []
        for sd in range(self.seeds):
            m = CatBoostRegressor(loss_function="MAE", random_seed=sd, verbose=0, cat_features=_cat(self.cols),
                                  thread_count=-1, **self.params)
            m.fit(X, y)
            self.models.append(m)
        return self

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        X = prepare(df, self.cols)
        return np.clip(np.mean([m.predict(X) for m in self.models], axis=0), *PRED_CLIP)

    def shap(self, df: pd.DataFrame) -> np.ndarray:
        """SHAP-вклады признаков (последняя колонка — базовое значение), усреднённые по сидам."""
        X = prepare(df, self.cols)
        pool = Pool(X, cat_features=_cat(self.cols))
        return np.mean([m.get_feature_importance(pool, type="ShapValues") for m in self.models], axis=0)

    def save(self, path: Path, name: str) -> None:
        path.mkdir(parents=True, exist_ok=True)
        for k, m in enumerate(self.models):
            m.save_model(str(path / f"{name}_{k}.cbm"))
        (path / f"{name}.json").write_text(json.dumps({"cols": self.cols, "seeds": len(self.models)}))

    @classmethod
    def load(cls, path: Path, name: str) -> "CatBag":
        meta = json.loads((path / f"{name}.json").read_text())
        bag = cls(cols=meta["cols"], seeds=meta["seeds"])
        for k in range(meta["seeds"]):
            m = CatBoostRegressor()
            m.load_model(str(path / f"{name}_{k}.cbm"))
            bag.models.append(m)
        return bag


@dataclass
class RiskModels:
    """Интервал прогноза (квантили) и вероятности классов early/ontime/late."""

    cols: list[str]
    quant: CatBoostRegressor | None = None
    cls: CatBoostClassifier | None = None

    def fit(self, df: pd.DataFrame, y: np.ndarray) -> "RiskModels":
        X = prepare(df, self.cols)
        alpha = ",".join(str(q) for q in QUANTILES)
        self.quant = CatBoostRegressor(loss_function=f"MultiQuantile:alpha={alpha}", iterations=1200,
                                       learning_rate=0.03, depth=6, verbose=0, random_seed=0,
                                       cat_features=_cat(self.cols), thread_count=4)
        self.quant.fit(X, y)
        lab = np.where(y > LATE_S, 2, np.where(y < EARLY_S, 0, 1))
        self.cls = CatBoostClassifier(loss_function="MultiClass", iterations=1000, learning_rate=0.03, depth=6,
                                      verbose=0, random_seed=0, cat_features=_cat(self.cols), thread_count=4)
        self.cls.fit(X, lab)
        return self

    def predict(self, df: pd.DataFrame) -> dict[str, np.ndarray]:
        X = prepare(df, self.cols)
        q = np.sort(np.atleast_2d(self.quant.predict(X)), axis=1)
        p = self.cls.predict_proba(X)
        return {"q10": q[:, 0], "q50": q[:, 1], "q90": q[:, 2], "p_early": p[:, 0], "p_late": p[:, 2]}

    def save(self, path: Path, name: str) -> None:
        self.quant.save_model(str(path / f"{name}_quant.cbm"))
        self.cls.save_model(str(path / f"{name}_cls.cbm"))
        (path / f"{name}.json").write_text(json.dumps({"cols": self.cols}))

    @classmethod
    def load(cls, path: Path, name: str) -> "RiskModels":
        meta = json.loads((path / f"{name}.json").read_text())
        rm = cls(cols=meta["cols"])
        rm.quant = CatBoostRegressor()
        rm.quant.load_model(str(path / f"{name}_quant.cbm"))
        rm.cls = CatBoostClassifier()
        rm.cls.load_model(str(path / f"{name}_cls.cbm"))
        return rm
