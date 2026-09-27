"""API backend: запуск без ML (деградация на fallback), приём телеметрии, прогнозы, what-if."""
import os

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from conftest import DATASET, needs_dataset

os.environ["NDTP_PORT"] = "0"
os.environ["ML_URL"] = "http://127.0.0.1:9"
os.environ["PREDICT_EVERY_S"] = "60"


@pytest.fixture(scope="module")
def client():
    from backend.api import app
    with TestClient(app) as c:
        yield c


@needs_dataset
def test_health_and_openapi(client):
    from backend import api

    h = client.get("/api/health").json()
    assert h["stream"]["state"] == "no_data"
    assert api.ndtp_server.idle_timeout == api.settings.stale_wall_s
    spec = client.get("/openapi.json").json()
    assert "/api/state" in spec["paths"] and "/api/whatif" in spec["paths"]


@needs_dataset
def test_telemetry_to_prediction_fallback(client):
    t = pd.read_csv(DATASET / "validate/traffic.csv")
    t = t[(t.tr_id == 131672) & (t.event_time >= "2026-01-06 07:00") & (t.event_time <= "2026-01-06 07:40")]
    pts = [{"unit_id": int(r.unit_id), "event_time": r.event_time, "lon": None if pd.isna(r.lon) else r.lon,
            "lat": None if pd.isna(r.lat) else r.lat, "speed": None if pd.isna(r.speed) else r.speed,
            "heading": None if pd.isna(r.heading) else r.heading, "location_valid": bool(r.location_valid)}
           for r in t.sort_values("event_time").itertuples()]
    assert client.post("/api/telemetry", json=pts).status_code == 200
    import time
    for _ in range(50):
        st = client.get("/api/state").json()
        v = next(x for x in st["vehicles"] if x["tr_id"] == 131672)
        if v["prediction"]:
            break
        time.sleep(0.1)
    p = v["prediction"]
    assert p is not None and p["model"] == "fallback"
    assert 10 <= p["lead_min"] <= 15
    assert st["health"]["ml"]["state"] == "fallback"
    d = client.get("/api/vehicles/131672").json()
    assert d["track"] and d["upcoming"]
    assert any(u["stop_id"] == p["target_stop_id"] for u in d["upcoming"]), "цель прогноза вне списка ближайших остановок"
    w = client.post("/api/whatif", json={"tr_id": 131672, "scenario": "signal_priority", "value": 20})
    assert w.status_code == 200 and "delay_s_after" in w.json()
    assert client.get("/api/routes").json()
    m = client.get("/api/metrics").json()
    assert m["online"]["retro_predictions"] == 0


@needs_dataset
def test_route_geometry_and_position(client):
    routes = client.get("/api/routes").json()
    geo = [r for r in routes if r.get("length_m")]
    assert geo, "нет геометрии маршрутов"
    r = geo[0]
    assert len(r["path"]) > 10 and all("m" in s for s in r["stops"])
    ms = [s["m"] for s in r["stops"]]
    assert ms == sorted(ms)
    light = client.get("/api/routes?geometry=false").json()
    assert all("path" not in x for x in light)
    st = client.get("/api/state").json()
    v = next(x for x in st["vehicles"] if x["tr_id"] == 131672)
    assert v["scheduled"] is True
    assert v["route_m"] is None or 0 <= v["route_m"] <= next(x for x in routes if x["tr_id"] == 131672)["length_m"]
    assert {"layover", "out_of_service", "unscheduled"} <= set(st["summary"])


@needs_dataset
def test_alert_ack(client):
    from backend import api
    f = api.fleet
    tr = next(iter(f.vehicles))
    aid = f"{tr}-ack-test"
    f.alerts[tr] = {"id": aid, "tr_id": tr, "route_name": "", "created_at": "2026-01-06T07:00:00", "status": "active",
                    "actual_delay_s": None, "error_s": None, "ack_at": None, "updated_at": "2026-01-06T07:00:00",
                    "risk": "red", "kind": "late", "prediction": {"delay_s": 200.0}, "_targets": {}}
    try:
        def find(status="active"):
            return next(x for x in client.get(f"/api/alerts?status={status}").json() if x["id"] == aid)

        assert find()["ack_at"] is None
        r = client.post(f"/api/alerts/{aid}/ack")
        assert r.status_code == 200 and r.json()["ack_at"]
        assert find()["ack_at"] == r.json()["ack_at"]
        assert any(x["id"] == aid and x["ack_at"] for x in client.get("/api/state").json()["alerts"])

        r = client.delete(f"/api/alerts/{aid}/ack")
        assert r.status_code == 200 and r.json()["ack_at"] is None
        assert find()["ack_at"] is None

        assert client.post("/api/alerts/no-such/ack").status_code == 404
        assert client.delete("/api/alerts/no-such/ack").status_code == 404

        client.post(f"/api/alerts/{aid}/ack")
        f._resolve(tr, 1767684600.0, "calm")
        assert find("resolved")["ack_at"]
        assert client.post(f"/api/alerts/{aid}/ack").status_code == 404
    finally:
        f.alerts.pop(tr, None)
        f.resolved = type(f.resolved)((a for a in f.resolved if a["id"] != aid), maxlen=f.resolved.maxlen)


@needs_dataset
def test_upcoming_reaches_far_target(client):
    from backend import api
    v = api.fleet.vehicles[131672]
    d = client.get("/api/vehicles/131672").json()
    s = v.tracker.s
    u0 = d["upcoming"][0]
    k0 = next(k for k in range(len(s)) if str(s.item_id[k]) == u0["stop_id"] and s.name[k] == u0["name"])
    old = v.target_idx
    v.target_idx = min(len(s) - 1, k0 + 16)
    try:
        up = client.get("/api/vehicles/131672").json()["upcoming"]
        assert up[-1]["stop_id"] == str(s.item_id[v.target_idx]) and len(up) == v.target_idx - k0 + 1
    finally:
        v.target_idx = old


@needs_dataset
def test_history_rows_explain_forecasts(client):
    d = client.get("/api/vehicles/131672").json()
    rows = d["delay_history"]
    assert rows and {r["kind"] for r in rows} <= {"actual", "estimate", "predicted"}
    pred = [r for r in rows if r["kind"] == "predicted"]
    assert pred and all(r["issued_at"] < r["t"] and 10 <= r["lead_min"] <= 15 for r in pred)


@needs_dataset
def test_hold_keeps_last_forecast_until_target(client):
    import numpy as np
    from backend import api
    f = api.fleet
    v = f.vehicles[131672]
    s = v.tracker.s
    idx, _ = v.tracker.arrivals_upto(1e12)
    start = s.plan[int(idx[-1]) + 1] if len(idx) else s.plan[0]
    # Момент, когда в окне 10–15 мин нет плановой остановки (конец рейса или длинный перегон).
    T = next(t for t in np.arange(start, start + 6 * 3600, 30) if s.target_index(t) is None)
    ti = int(np.searchsorted(s.plan, T, side="right"))
    saved = (v.prediction, v.target_idx, v.last_t, v.held)
    try:
        v.prediction, v.target_idx, v.last_t, v.held = {"delay_s": 60.0}, ti, T, None
        assert f._hold(v, T) and v.held["reason"] in ("trip_end", "long_segment")
        late = s.plan[ti] + 1801
        v.last_t, v.held = late, None
        assert not f._hold(v, late)
    finally:
        v.prediction, v.target_idx, v.last_t, v.held = saved
