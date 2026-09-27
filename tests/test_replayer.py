from __future__ import annotations

import asyncio
import csv
import math
import socket
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
try:
    import ndtp  # noqa: F401
    import replayer  # noqa: F401
except ImportError:
    sys.path.insert(0, str(ROOT / "libs" / "ndtp"))
    sys.path.insert(0, str(ROOT / "services" / "replayer"))

import httpx
from ndtp import ConnRequest, NavPacket
from ndtp.server import ConnInfo, NdtpServer
from replayer.app import create_app
from replayer.engine import (
    ReplayEngine,
    ReplaySettings,
    StreamClock,
    load_traffic_csv,
    parse_start_time,
    record_from_row,
)

REAL_CSV = ROOT / "data" / "dataset" / "validate" / "traffic.csv"
COLUMNS = [
    "packet_id",
    "tr_id",
    "unit_id",
    "event_time",
    "device_event_id",
    "location_valid",
    "gps_time",
    "lon",
    "lat",
    "alt",
    "speed",
    "heading",
    "receive_time",
    "is_hist_data",
]
WINDOW = ("2026-01-06 06:05:00", "2026-01-06 06:40:00")
WARMUP_FROM = "2026-01-06 06:10:00"
LIVE_FROM = "2026-01-06 06:30:00"


def _synthetic_rows() -> list[dict[str, str]]:
    rows = []
    t0 = datetime(2026, 1, 6, 6, 5)
    for u, unit in enumerate((664030, 794446, 1112060, 663271, 890371)):
        for k in range(0, 35 * 6):
            t = t0 + timedelta(seconds=10 * k + u)
            valid = k % 7 != 0
            rows.append(
                {
                    "packet_id": str(k),
                    "tr_id": str(100000 + u),
                    "unit_id": str(unit),
                    "event_time": t.strftime("%Y-%m-%d %H:%M:%S.%f"),
                    "device_event_id": "0",
                    "location_valid": str(valid),
                    "gps_time": t.strftime("%Y-%m-%d %H:%M:%S.%f") if valid else "",
                    "lon": f"{37.5 + k * 1e-4:.6f}" if valid else "",
                    "lat": f"{55.7 + u * 1e-2:.6f}" if valid else "",
                    "alt": "65505" if k % 11 == 0 else "150",
                    "speed": str(k % 60) if valid else "",
                    "heading": str((k * 7) % 360) if valid else "",
                    "receive_time": t.strftime("%Y-%m-%d %H:%M:%S.%f"),
                    "is_hist_data": str(k % 13 == 0),
                }
            )
    return rows


def _real_rows(units: int = 5) -> list[dict[str, str]]:
    with REAL_CSV.open(newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if WINDOW[0] <= r["event_time"] < WINDOW[1]]
    per_unit: dict[str, int] = defaultdict(int)
    for r in rows:
        per_unit[r["unit_id"]] += 1
    chosen = set(sorted(per_unit, key=lambda u: -per_unit[u])[:units])
    return [r for r in rows if r["unit_id"] in chosen]


@pytest.fixture(scope="module")
def traffic_rows() -> list[dict[str, str]]:
    rows = _real_rows() if REAL_CSV.exists() else _synthetic_rows()
    assert 300 <= len(rows) <= 3000
    return rows


@pytest.fixture
def traffic_csv(tmp_path: Path, traffic_rows: list[dict[str, str]]) -> Path:
    path = tmp_path / "traffic.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(traffic_rows)
    return path


def _expected(rows: list[dict[str, str]]) -> dict[int, list[dict[str, str]]]:
    out: dict[int, list[dict[str, str]]] = defaultdict(list)
    for r in sorted(rows, key=lambda r: r["event_time"]):
        if r["event_time"] >= WARMUP_FROM:
            out[int(r["unit_id"])].append(r)
    return out


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Collector:
    def __init__(self) -> None:
        self.by_unit: dict[int, list[object]] = defaultdict(list)
        self.disconnects = 0

    def on_message(self, conn: ConnInfo, msg: object) -> None:
        assert conn.unit_id is not None
        self.by_unit[conn.unit_id].append(msg)

    def on_disconnect(self, conn: ConnInfo) -> None:
        self.disconnects += 1

    def navs(self, unit: int) -> list[NavPacket]:
        return [m for m in self.by_unit[unit] if isinstance(m, NavPacket)]

    def total_navs(self) -> int:
        return sum(len(self.navs(u)) for u in self.by_unit)


def _settings(csv_path: Path, port: int, **kw: object) -> ReplaySettings:
    base: dict[str, object] = dict(
        csv_path=str(csv_path),
        host="127.0.0.1",
        port=port,
        speed=3000.0,
        start_time="06:30",
        warmup_min=20.0,
        autostart=False,
        connect_wait_s=5.0,
        handshake_delay_s=0.0,
        backoff_initial_s=0.05,
        backoff_max_s=0.2,
    )
    base.update(kw)
    return ReplaySettings(**base)  # type: ignore[arg-type]


async def _until(predicate, timeout: float = 10.0) -> None:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def test_record_from_row_rules() -> None:
    base = dict(zip(COLUMNS, [""] * len(COLUMNS), strict=True))
    row = base | {
        "unit_id": "664030",
        "event_time": "2026-01-06 12:30:31.462764",
        "location_valid": "True",
        "lon": "37.774517",
        "lat": "55.813473",
        "alt": "65505",
        "speed": "368.0",
        "heading": "159.0",
        "is_hist_data": "True",
    }
    rec = record_from_row(row)
    assert rec is not None
    assert rec.nav.timestamp == int((datetime(2026, 1, 6, 12, 30, 31) - datetime(1970, 1, 1)).total_seconds())
    assert rec.ts == pytest.approx(rec.nav.timestamp + 0.462764)
    assert (rec.nav.lon, rec.nav.lat, rec.nav.valid) == (37.774517, 55.813473, True)
    assert (rec.nav.altitude, rec.nav.speed_avg, rec.nav.speed_max, rec.nav.course) == (65505, 368, 368, 159)
    assert rec.history and rec.unit_id == 664030

    invalid = record_from_row(row | {"location_valid": "False", "speed": "", "alt": "70000", "is_hist_data": "False"})
    assert invalid is not None
    assert (invalid.nav.lon, invalid.nav.lat, invalid.nav.valid) == (0.0, 0.0, False)
    assert (invalid.nav.speed_avg, invalid.nav.altitude) == (0, 65535)
    assert not invalid.history

    nan_speed = record_from_row(row | {"speed": "nan", "alt": "-3", "heading": ""})
    assert nan_speed is not None
    assert (nan_speed.nav.speed_avg, nan_speed.nav.altitude, nan_speed.nav.course) == (0, 0, 0)
    no_coords = record_from_row(row | {"lon": "", "lat": ""})
    assert no_coords is not None and not no_coords.nav.valid
    assert record_from_row(row | {"event_time": ""}) is None


def test_parse_start_time_and_clock() -> None:
    day = datetime(2026, 1, 6).date()
    assert parse_start_time("06:30", day) == datetime(2026, 1, 6, 6, 30)
    assert parse_start_time("06:30:15", day) == datetime(2026, 1, 6, 6, 30, 15)
    assert parse_start_time("2026-01-06T07:00:00", day) == datetime(2026, 1, 6, 7)
    with pytest.raises(ValueError):
        parse_start_time("half past six", day)

    clock = StreamClock(10.0)
    clock.anchor(1000.0)
    clock.pause()
    frozen = clock.now()
    clock.set_speed(100.0)
    assert clock.now() == frozen and clock.speed == 100.0
    clock.resume()
    assert clock.now() >= frozen


def test_load_sorts_by_event_time(traffic_csv: Path, traffic_rows: list[dict[str, str]]) -> None:
    data = load_traffic_csv(traffic_csv)
    assert len(data.records) == len(traffic_rows)
    assert data.times == sorted(data.times)
    assert data.units == sorted({int(r["unit_id"]) for r in traffic_rows})
    assert str(data.day) == "2026-01-06"


@pytest.mark.asyncio
async def test_short_replay_delivers_everything(traffic_csv: Path, traffic_rows: list[dict[str, str]]) -> None:
    expected = _expected(traffic_rows)
    n_expected = sum(len(v) for v in expected.values())
    col = Collector()
    server = NdtpServer("127.0.0.1", 0, col.on_message, col.on_disconnect)
    await server.start()
    engine = ReplayEngine(_settings(traffic_csv, server.port))
    try:
        n_units = len({r["unit_id"] for r in traffic_rows})
        status = await engine.start()
        assert status["state"] == "running" and status["units"] == n_units
        assert await engine.wait_finished(timeout=30)
        await _until(lambda: col.total_navs() == n_expected)
        await _until(lambda: engine.status()["sent"] == n_expected)
        await _until(lambda: engine.status()["acks"] == n_expected + n_units)

        status = engine.status()
        assert status["state"] == "finished"
        assert status["dispatched"] == status["total"] == n_expected
        assert status["connected"] == n_units
        assert status["stream_time"] >= "2026-01-06T06:39:"

        for unit, rows in expected.items():
            msgs = col.by_unit[unit]
            assert isinstance(msgs[0], ConnRequest) and msgs[0].unit_id == unit
            assert sum(isinstance(m, ConnRequest) for m in msgs) == 1
            navs = col.navs(unit)
            assert len(navs) == len(rows)
            for pkt, row in zip(navs, rows, strict=True):
                rec = record_from_row(row)
                assert rec is not None and pkt.nav is not None
                assert pkt.nav.timestamp == rec.nav.timestamp
                assert pkt.nav.valid == rec.nav.valid
                assert math.isclose(pkt.nav.lon, rec.nav.lon, abs_tol=1e-7)
                assert math.isclose(pkt.nav.lat, rec.nav.lat, abs_tol=1e-7)
                assert (pkt.nav.speed_avg, pkt.nav.course, pkt.nav.altitude) == (
                    rec.nav.speed_avg,
                    rec.nav.course,
                    rec.nav.altitude,
                )
                if row["event_time"] < LIVE_FROM:
                    assert pkt.history
                else:
                    assert pkt.history == rec.history
            assert [p.request_id for p in navs] == list(range(2, len(navs) + 2))
        assert server.stats["crc_errors"] == 0
    finally:
        await engine.stop()
        await server.stop()
    assert engine.status()["state"] == "idle"
    assert engine.status()["connected"] == 0


@pytest.mark.asyncio
async def test_replayer_waits_for_server_and_reconnects(traffic_csv: Path, traffic_rows: list[dict[str, str]]) -> None:
    expected = _expected(traffic_rows)
    n_expected = sum(len(v) for v in expected.values())
    n_units = len({r["unit_id"] for r in traffic_rows})
    port = _free_port()
    col = Collector()
    engine = ReplayEngine(_settings(traffic_csv, port, connect_wait_s=0.0, speed=100.0))
    await engine.start()
    await asyncio.sleep(0.4)
    assert engine.status()["connected"] == 0

    server = NdtpServer("127.0.0.1", port, col.on_message, col.on_disconnect)
    await server.start()
    try:
        await _until(lambda: engine.status()["connected"] == n_units)
        await _until(lambda: col.total_navs() > 0 and engine.status()["phase"] == "live")
        before = col.total_navs()

        await server.stop()
        await _until(lambda: engine.status()["connected"] == 0)
        await asyncio.sleep(0.3)
        assert engine.status()["state"] == "running"

        server = NdtpServer("127.0.0.1", port, col.on_message, col.on_disconnect)
        await server.start()
        engine.set_speed(5000)
        assert await engine.wait_finished(timeout=30)
        await _until(lambda: engine.status()["queued"] == 0 and engine.status()["connected"] == n_units)

        assert col.total_navs() > before
        assert col.total_navs() <= n_expected
        status = engine.status()
        assert status["reconnects"] >= n_units
        for unit in expected:
            handshakes = [m for m in col.by_unit[unit] if isinstance(m, ConnRequest)]
            assert len(handshakes) >= 2
            assert isinstance(col.by_unit[unit][0], ConnRequest)
    finally:
        await engine.stop()
        await server.stop()


@pytest.mark.asyncio
async def test_api_contract(traffic_csv: Path) -> None:
    col = Collector()
    server = NdtpServer("127.0.0.1", 0, col.on_message)
    await server.start()
    app = create_app(_settings(traffic_csv, server.port, speed=20.0))
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://replayer") as client:
            assert (await client.get("/health")).json()["status"] == "ok"
            status = (await client.get("/api/status")).json()
            assert status["state"] == "idle" and status["stream_time"] is None
            for key in ("state", "speed", "stream_time", "start_time", "units", "sent", "connected"):
                assert key in status

            r = await client.post("/api/start", json={"speed": 1, "start_time": "06:30", "warmup_min": 20})
            assert r.status_code == 200
            status = r.json()
            assert status["state"] == "running" and status["speed"] == 1 and status["start_time"] == "06:30"
            await _until(lambda: app.state.engine.status()["phase"] == "live")
            await _until(lambda: app.state.engine.status()["connected"] == status["units"])

            paused = (await client.post("/api/pause")).json()
            assert paused["state"] == "paused"
            t1 = paused["stream_time"]
            assert t1.startswith("2026-01-06T06:3")
            await asyncio.sleep(1.1)
            assert (await client.get("/api/status")).json()["stream_time"] == t1

            r = await client.post("/api/speed", json={"speed": 60})
            assert r.json()["speed"] == 60 and r.json()["state"] == "paused"
            assert (await client.post("/api/speed", json={"speed": 0})).status_code == 422
            assert (await client.post("/api/start", json={"start_time": "bogus"})).status_code == 400

            assert (await client.get("/api/status")).json()["state"] == "paused"
            resumed = (await client.post("/api/resume")).json()
            assert resumed["state"] == "running"
            await asyncio.sleep(0.6)
            t2 = (await client.get("/api/status")).json()["stream_time"]
            assert t2 > t1

            stopped = (await client.post("/api/stop")).json()
            assert stopped["state"] == "idle" and stopped["connected"] == 0

            restarted = (await client.post("/api/start")).json()
            assert restarted["state"] == "running" and restarted["speed"] == 60
            await client.post("/api/stop")
    finally:
        await app.state.engine.stop()
        await server.stop()
