"""Точность восстановления фактов прохождения остановок из GPS и отсутствие заглядывания в будущее."""
import numpy as np
import pandas as pd

from conftest import DATASET, needs_dataset
from transit_core.features import snapshot
from transit_core.schedule import build_vehicle_schedules, load_schedule, to_unix
from transit_core.tracker import VehicleTracker


def _replay(tr_id, until=None):
    s = build_vehicle_schedules(load_schedule(str(DATASET / "test/schedule.csv")))[tr_id]
    t = pd.read_csv(DATASET / "test/traffic.csv", usecols=["tr_id", "event_time", "location_valid", "lon", "lat",
                                                           "speed", "heading"])
    t = t[t.tr_id == tr_id]
    t["t"] = (pd.to_datetime(t.event_time) - pd.Timestamp("1970-01-01")).dt.total_seconds()
    t = t.sort_values("t")
    if until is not None:
        t = t[t.t <= until]
    tk = VehicleTracker(s)
    for r in zip(t.t, t.lon, t.lat, t.speed, t.heading, t.location_valid):
        tk.update(r[0], r[1], r[2], r[3], r[4], bool(r[5]))
    return s, tk


@needs_dataset
def test_arrivals_match_facts():
    s, tk = _replay(131672)
    ok = np.isfinite(tk.arr) & ~s.manual_fill & ~tk.arr_interp
    err = np.abs(tk.arr[ok] - s.fact[ok])
    assert ok.mean() > 0.8
    assert np.median(err) < 5
    assert np.mean(err < 30) > 0.9


@needs_dataset
def test_snapshot_uses_only_past():
    T = to_unix("2026-01-06 07:30:00")
    s, tk = _replay(131672, until=T)
    ti = s.target_index(T)
    f = snapshot(tk, T, ti)
    assert all(p.t <= T for p in tk.gps)
    idx, arr = tk.arrivals_upto(T)
    assert np.all(arr[idx] <= T)
    assert 600 < f["lead"] <= 900


@needs_dataset
def test_relocate_after_gps_fault():
    """122658 в validate: 11:20–11:35 блок шлёт координаты за 18 км от маршрута. Онлайн-трекер
    отбрасывает их и после сбоя находит ТС заново; офлайн-режим (как при обучении) — нет."""
    s = build_vehicle_schedules(load_schedule(str(DATASET / "validate/schedule_plan.csv")))[122658]
    t = pd.read_csv(DATASET / "validate/traffic.csv")
    t = t[(t.tr_id == 122658) & (t.event_time <= "2026-01-06 12:00:00")].sort_values("event_time")
    lo, hi = to_unix("2026-01-06 11:40:00"), to_unix("2026-01-06 12:00:00")
    found = {}
    for relocate in (False, True):
        tk = VehicleTracker(s, relocate=relocate)
        for r in t.itertuples():
            tk.update(to_unix(r.event_time), r.lon, r.lat, r.speed, r.heading, bool(r.location_valid))
        real = np.isfinite(tk.arr) & ~tk.arr_interp & (tk.arr >= lo) & (tk.arr <= hi)
        found[relocate] = int(real.sum())
    assert found[False] == 0 and found[True] >= 5
