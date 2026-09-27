"""Замеры производительности: латентность ML, скорость трекера, пропускная способность backend.

Запуск (сервисы должны быть подняты)::

    python scripts/benchmark.py --ml http://localhost:8001 --backend http://localhost:8000
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for p in ("libs/transit_core",):
    sys.path.insert(0, str(ROOT / p))


def _items(n: int) -> list[dict]:
    df = pd.read_parquet(ROOT / "artifacts/cache/features_test.parquet")
    df = pd.concat([df] * (n // len(df) + 1)).head(n)
    items = []
    for r in df.to_dict("records"):
        f = {k: (None if isinstance(v, float) and v != v else v) for k, v in r.items() if k != "seq"}
        f["seq"] = [float(x) for x in r["seq"]]
        items.append({"id": str(r["sample_id"]), "features": f})
    return items


def bench_ml(url: str) -> dict:
    out = {}
    with httpx.Client(base_url=url, timeout=30) as c:
        for n in (1, 13, 30, 100):
            items = _items(n)
            for explain in (False, True):
                lat = []
                for _ in range(15):
                    t0 = time.perf_counter()
                    c.post("/v1/predict", json={"items": items, "explain": explain}).raise_for_status()
                    lat.append((time.perf_counter() - t0) * 1000)
                lat = lat[3:]
                out[f"batch{n}_{'shap' if explain else 'plain'}"] = {
                    "p50_ms": round(statistics.median(lat), 1), "p95_ms": round(float(np.percentile(lat, 95)), 1)}
    return out


def bench_tracker() -> dict:
    from transit_core.schedule import build_vehicle_schedules, load_schedule
    from transit_core.tracker import VehicleTracker
    D = ROOT / "data/dataset"
    VS = build_vehicle_schedules(load_schedule(str(D / "validate/schedule_plan.csv")))
    t = pd.read_csv(D / "validate/traffic.csv", usecols=["tr_id", "event_time", "location_valid", "lon", "lat",
                                                         "speed", "heading"])
    t["t"] = (pd.to_datetime(t.event_time) - pd.Timestamp("1970-01-01")).dt.total_seconds()
    t = t[t.tr_id.isin(VS)].sort_values("t")
    trk = {tr: VehicleTracker(v) for tr, v in VS.items()}
    t0 = time.perf_counter()
    for r in zip(t.tr_id, t.t, t.lon, t.lat, t.speed, t.heading, t.location_valid):
        trk[r[0]].update(r[1], r[2], r[3], r[4], r[5], bool(r[6]))
    dt = time.perf_counter() - t0
    return {"points": len(t), "total_s": round(dt, 2), "us_per_point": round(dt / len(t) * 1e6, 1),
            "points_per_s": int(len(t) / dt)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ml", default="http://localhost:8001")
    ap.add_argument("--backend", default="http://localhost:8000")
    a = ap.parse_args()
    res = {"ml": bench_ml(a.ml), "tracker": bench_tracker()}
    try:
        res["backend_metrics"] = httpx.get(f"{a.backend}/api/metrics", timeout=5).json()
    except Exception as e:
        res["backend_metrics"] = str(e)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    (ROOT / "docs/_static/benchmark.json").write_text(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
