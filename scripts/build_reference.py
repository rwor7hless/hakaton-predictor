"""Собрать справочники для backend: соответствие unit_id ↔ tr_id и fallback-профиль остановок."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/ml"))

from mlcore.data import build_all  # noqa: E402
from transit_core.schedule import build_vehicle_schedules, load_schedule  # noqa: E402
from transit_core.tracker import KX, KY, VehicleTracker, stop_roles  # noqa: E402

D = ROOT / "data/dataset"
OUT = ROOT / "artifacts/reference"


def _rdp(pts: np.ndarray, eps: float) -> np.ndarray:
    """Упрощение ломаной Рамера — Дугласа — Пекера с допуском ``eps`` метров."""
    if len(pts) < 3:
        return pts
    a, b = pts[0], pts[-1]
    ab = (b - a) * [KX, KY]
    q = (pts - a) * [KX, KY]
    d = np.abs(ab[0] * q[:, 1] - ab[1] * q[:, 0]) / (np.hypot(*ab) + 1e-9)
    i = int(np.argmax(d))
    if d[i] > eps:
        return np.vstack([_rdp(pts[: i + 1], eps)[:-1], _rdp(pts[i:], eps)])
    return np.vstack([a, b])


def _drop_jumps(pts: np.ndarray, max_m: float = 300.0) -> np.ndarray:
    """Убрать одиночные GPS-скачки: точку, далёкую от обоих соседей, при близких соседях."""
    if len(pts) < 3:
        return pts
    keep = np.ones(len(pts), dtype=bool)
    d = lambda a, b: np.hypot((a[:, 0] - b[:, 0]) * KX, (a[:, 1] - b[:, 1]) * KY)  # noqa: E731
    prev_d, next_d, skip = d(pts[1:-1], pts[:-2]), d(pts[1:-1], pts[2:]), d(pts[:-2], pts[2:])
    keep[1:-1] = ~((prev_d > max_m) & (next_d > max_m) & (skip < max_m))
    return pts[keep]


def route_paths() -> dict[str, list[list[float]]]:
    """Геометрия маршрута по реальному GPS-треку самого полного рейса каждого ТС."""
    VS = build_vehicle_schedules(load_schedule(str(D / "validate/schedule_plan.csv")))
    t = pd.read_csv(D / "validate/traffic.csv", usecols=["tr_id", "event_time", "location_valid", "lon", "lat",
                                                         "speed", "heading"])
    t["t"] = (pd.to_datetime(t.event_time) - pd.Timestamp("1970-01-01")).dt.total_seconds()
    out = {}
    for tr, v in VS.items():
        g = t[t.tr_id == tr].sort_values("t")
        tk = VehicleTracker(v)
        for r in zip(g.t, g.lon, g.lat, g.speed, g.heading, g.location_valid):
            tk.update(r[0], r[1], r[2], r[3], r[4], bool(r[5]))
        roles = stop_roles(v)
        starts = [i for i in range(len(v)) if roles[i] == 1] + [len(v)]
        best, best_cov = None, 0
        for a, b in zip(starts[:-1], starts[1:]):
            seg = tk.arr[a:b]
            cov = np.isfinite(seg).sum() - tk.arr_interp[a:b].sum()
            if cov > best_cov and np.isfinite(seg[0]) and np.isfinite(seg[-1]):
                best, best_cov = (a, b - 1), cov
        if best is None:
            continue
        t0, t1 = tk.arr[best[0]], tk.arr[best[1]]
        w = g[(g.t >= t0) & (g.t <= t1) & g.location_valid]
        pts = _drop_jumps(w[["lon", "lat"]].to_numpy())
        stops = np.c_[v.lon[best[0]:best[1] + 1], v.lat[best[0]:best[1] + 1]]
        plen = lambda a: float(np.hypot(np.diff(a[:, 0]) * KX, np.diff(a[:, 1]) * KY).sum())  # noqa: E731
        if len(pts) > 10 and plen(pts) <= min(3 * max(plen(stops), 1000.0), 60000.0):
            out[str(tr)] = np.round(_rdp(pts, 8.0), 6).tolist()
    return out


def main() -> None:
    sys.setrecursionlimit(20000)
    OUT.mkdir(parents=True, exist_ok=True)
    frames = [pd.read_csv(D / f, usecols=["tr_id", "unit_id"]).drop_duplicates()
              for f in ("train/traffic.csv", "validate/traffic.csv")]
    units = pd.concat(frames).drop_duplicates().sort_values("tr_id")
    units.to_csv(OUT / "units.csv", index=False)
    t = build_all(ROOT / "artifacts/cache")
    tr = pd.concat([t["train"][~t["train"]["leak_val"]], t["test"]])
    k = 0.6
    resid = tr["target_delay_s"] - k * tr["cur_dev"]
    bias = resid.groupby(tr["tgt_stop"]).median()
    cnt = tr.groupby("tgt_stop").size()
    bias = (bias * cnt / (cnt + 5)).round(1)
    (OUT / "fallback.json").write_text(json.dumps({"k": k, "global_bias": float(np.median(resid)),
                                                   "stop_bias": bias.to_dict()}, ensure_ascii=False))
    paths = route_paths()
    (OUT / "route_paths.json").write_text(json.dumps(paths))
    print(len(units), "units;", len(bias), "stops in fallback profile;", len(paths), "route paths",
          {k: len(v) for k, v in paths.items()})


if __name__ == "__main__":
    main()
