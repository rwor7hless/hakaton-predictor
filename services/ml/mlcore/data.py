"""Подготовка обучающих/прогнозных таблиц.

Признаки считаются проигрыванием телеметрии через тот же трекер, что работает
в backend, поэтому офлайн- и онлайн-признаки совпадают. В трекер подаются
только точки с ``event_time <= T``; в признаках не используются факты
расписания — только план и GPS.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd

from transit_core.features import OFFLINE_FEATURES, gps_sequence, route_key, snapshot
from transit_core.schedule import EPOCH, build_vehicle_schedules, load_schedule
from transit_core.tracker import VehicleTracker

DATASET_DIR = Path(os.environ.get("DATASET_DIR", Path(__file__).resolve().parents[3] / "data" / "dataset"))
SYNTH_MIN_ID = 9_000_000
LEAK_WINDOW_S = 40 * 60


def load_points(path: Path) -> pd.DataFrame:
    """Прочитать labels_*.csv или points.csv; добавить ``T_s`` (unix-секунды)."""
    p = pd.read_csv(path)
    p["T_s"] = (pd.to_datetime(p["T"]) - EPOCH).dt.total_seconds()
    return p


def load_traffic(path: Path) -> pd.DataFrame:
    """Прочитать traffic.csv и отсортировать по ТС и времени события."""
    t = pd.read_csv(path, usecols=["tr_id", "event_time", "location_valid", "lon", "lat", "speed", "heading"])
    t["t"] = (pd.to_datetime(t["event_time"]) - EPOCH).dt.total_seconds()
    return t.sort_values(["tr_id", "t"], kind="stable").reset_index(drop=True)


def replay_features(points: pd.DataFrame, traffic: pd.DataFrame, schedule_path: Path) -> pd.DataFrame:
    """Посчитать признаки для прогнозных точек проигрыванием телеметрии до каждого ``T``.

    Последовательность GPS за 10 минут (для нейросети) кладётся в колонку ``seq``
    как плоский список float32.
    """
    VS = build_vehicle_schedules(load_schedule(str(schedule_path)))
    routes = {tr: route_key(v) for tr, v in VS.items()}
    rows = []
    groups = dict(tuple(traffic.groupby("tr_id")))
    for tr, P in points.groupby("tr_id"):
        v = VS[int(tr)]
        tk = VehicleTracker(v)
        g = groups.get(tr)
        cols = [g[c].to_numpy() for c in ("t", "lon", "lat", "speed", "heading", "location_valid")] if g is not None else None
        j, m = 0, (len(g) if g is not None else 0)
        for r in P.sort_values("T_s").itertuples():
            while j < m and cols[0][j] <= r.T_s:
                tk.update(cols[0][j], cols[1][j], cols[2][j], cols[3][j], cols[4][j], bool(cols[5][j]))
                j += 1
            ti = v.index_of[int(r.target_stop_id)]
            f = snapshot(tk, r.T_s, ti, routes[int(tr)])
            f["seq"] = gps_sequence(tk, r.T_s, ti).ravel().tolist()
            f["sample_id"] = r.sample_id
            rows.append(f)
    F = pd.DataFrame(rows)
    out = points.merge(F, on="sample_id", how="left")
    out["cur_dev"] = out["cur_dev_s"]
    return out


def synthetic_shifts(schedule_path: Path) -> dict[int, tuple[int, float]]:
    """Для синтетических ТС найти исходное реальное ТС и сдвиг времени (с)."""
    VS = build_vehicle_schedules(load_schedule(str(schedule_path)))
    real = {route_key(v): tr for tr, v in VS.items() if tr < SYNTH_MIN_ID}
    out = {}
    for tr, v in VS.items():
        if tr >= SYNTH_MIN_ID and route_key(v) in real:
            src = real[route_key(v)]
            a, b = VS[src].plan, v.plan
            if len(a) == len(b):
                out[tr] = (src, float(np.median(b - a)))
    return out


def leak_mask(train: pd.DataFrame, holdout: pd.DataFrame, shifts: dict[int, tuple[int, float]]) -> np.ndarray:
    """Маска синтетических точек, которые во «время оригинала» перекрывают отложенные периоды.

    Синтетические ТС — сдвинутые по времени копии реальных, поэтому их точки
    рядом с периодами test/validate фактически содержат ответы.
    """
    hold = {tr: g["T_s"].to_numpy() for tr, g in holdout.groupby("tr_id")}
    mask = np.zeros(len(train), dtype=bool)
    for k, (tr, T) in enumerate(zip(train["tr_id"].to_numpy(), train["T_s"].to_numpy())):
        if tr in shifts:
            src, sh = shifts[tr]
            h = hold.get(src)
            if h is not None and np.any(np.abs(h - (T - sh)) < LEAK_WINDOW_S):
                mask[k] = True
    return mask


def build_all(cache_dir: Path) -> dict[str, pd.DataFrame]:
    """Собрать (или взять из кэша) таблицы признаков train / test / validate."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    D = DATASET_DIR
    specs = {
        "train": (D / "labels/labels_train.csv", D / "train/traffic.csv", D / "train/schedule.csv"),
        "test": (D / "labels/labels_test.csv", D / "test/traffic.csv", D / "test/schedule.csv"),
        "val": (D / "validate/points.csv", D / "validate/traffic.csv", D / "validate/schedule_plan.csv"),
    }
    out = {}
    for name, (pts, trf, sch) in specs.items():
        f = cache_dir / f"features_{name}.parquet"
        if f.exists():
            out[name] = pd.read_parquet(f)
            continue
        df = replay_features(load_points(pts), load_traffic(trf), sch)
        df.to_parquet(f)
        out[name] = df
    shifts = synthetic_shifts(D / "train/schedule.csv")
    tr = out["train"]
    tr["synthetic"] = tr["tr_id"] >= SYNTH_MIN_ID
    tr["leak_val"] = leak_mask(tr, out["val"], shifts)
    tr["leak_test_val"] = tr["leak_val"] | leak_mask(tr, out["test"], shifts)
    return out


__all__ = ["build_all", "replay_features", "leak_mask", "synthetic_shifts", "OFFLINE_FEATURES"]
