from __future__ import annotations

import asyncio
import json
import random
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
try:
    import ndtp
except ImportError:
    sys.path.insert(0, str(ROOT / "libs" / "ndtp"))
    import ndtp

from ndtp import (
    CELL_NAMES,
    CELL_SIZES,
    Cell,
    ConnRequest,
    Frame,
    FrameDecoder,
    NavCell,
    NavPacket,
    crc16_modbus,
    decode_frame,
    encode_conn_request,
    encode_nav_packet,
    encode_result,
)
from ndtp.server import ConnInfo, NdtpServer

FIXTURES = ROOT / "tests" / "fixtures" / "ndtp"


def _nav(**kw: object) -> NavCell:
    base: dict[str, object] = dict(
        timestamp=1767702631,
        lon=37.774517,
        lat=55.813473,
        valid=True,
        speed_avg=23,
        speed_max=31,
        course=159,
        track=1234,
        altitude=165,
        nsat=11,
        pdop=2,
        bat_voltage=200,
    )
    base.update(kw)
    return NavCell(**base)  # type: ignore[arg-type]


def _stream(n: int = 6) -> tuple[bytes, list[bytes]]:
    frames = [encode_conn_request(664030, 1)]
    for i in range(n - 1):
        frames.append(encode_nav_packet(664030, i + 2, _nav(timestamp=1767702631 + i, speed_avg=i), history=i % 2 == 0))
    return b"".join(frames), frames


def test_crc_known_vectors() -> None:
    assert crc16_modbus(b"123456789") == 0x4B37
    assert crc16_modbus(b"") == 0xFFFF
    assert crc16_modbus(b"456789", init=crc16_modbus(b"123")) == 0x4B37


def test_crc_is_stored_byte_swapped() -> None:
    frame = encode_conn_request(1166336, 1)
    crc = crc16_modbus(frame[15:])
    assert frame[6:8] == struct.pack(">H", crc)
    capture = (FIXTURES / "emulator_capture.bin").read_bytes()
    size = struct.unpack_from("<H", capture, 2)[0]
    assert capture[6:8] == struct.pack(">H", crc16_modbus(capture[15 : 15 + size]))


def test_spec_example_coordinates() -> None:
    payload = struct.pack("<IIIBBHHHHHBB", 1767702631, 376173210, 557551234, 0b01100000, 0, 0, 0, 0, 0, 0, 0, 0)
    nav = NavCell.from_bytes(payload)
    assert nav.lon == pytest.approx(37.6173210, abs=1e-9)
    assert nav.lat == pytest.approx(55.7551234, abs=1e-9)
    assert not nav.valid


@pytest.mark.parametrize(
    ("lon", "lat", "bits"),
    [
        (37.6173210, 55.7551234, 0b01100000),
        (-73.9857, 40.7484, 0b00100000),
        (151.2093, -33.8688, 0b01000000),
        (-58.3816, -34.6037, 0b00000000),
    ],
)
@pytest.mark.parametrize("history", [False, True])
def test_nav_roundtrip_signs(lon: float, lat: float, bits: int, history: bool) -> None:
    nav = _nav(lon=lon, lat=lat)
    frame = encode_nav_packet(794446, 42, nav, history=history)
    body = frame[25:]
    assert body[0] == 0 and body[1] == 0
    extra_dop = body[2 + 12]
    assert extra_dop & 0b01100000 == bits
    assert extra_dop & 0b10000000
    assert struct.unpack_from("<HH", frame, 15) == (1, 100 if history else 101)
    msg = decode_frame(frame)
    assert isinstance(msg, NavPacket)
    assert (msg.unit_id, msg.request_id, msg.history) == (794446, 42, history)
    assert msg.nav == nav
    assert msg.cells == []
    assert msg.frame is not None and msg.frame.needs_reply


def test_nav_extra_dop_bits_roundtrip() -> None:
    for bit, name in enumerate(["call_request", "alarm", "sos", "first_power_on", "internal_battery"]):
        nav = _nav(**{name: True})
        assert nav.to_bytes()[12] & (1 << bit)
        assert decode_frame(encode_nav_packet(1, 1, nav)).nav == nav  # type: ignore[union-attr]
    invalid = _nav(valid=False, lon=0.0, lat=0.0)
    decoded = decode_frame(encode_nav_packet(1, 1, invalid)).nav  # type: ignore[union-attr]
    assert decoded == invalid and not decoded.valid


def test_nav_clamping() -> None:
    nav = _nav(speed_avg=70000, speed_max=-5, altitude=65505, course=float("nan"), track=70000, nsat=300, pdop=-1)
    out = NavCell.from_bytes(nav.to_bytes())
    assert (out.speed_avg, out.speed_max, out.altitude, out.course) == (65535, 0, 65505, 0)
    assert out.track == 70000 & 0xFFFF
    assert (out.nsat, out.pdop) == (255, 0)
    assert len(nav.to_bytes()) == 26


def test_conn_request_roundtrip_and_emulator_bytes() -> None:
    msg = decode_frame(encode_conn_request(1166336, 7))
    assert isinstance(msg, ConnRequest)
    assert (msg.unit_id, msg.proto_high, msg.proto_low, msg.max_packet_size, msg.request_id) == (
        1166336,
        6,
        2,
        65535,
        7,
    )
    capture = (FIXTURES / "emulator_capture.bin").read_bytes()
    first = decode_frame(capture[:43])
    assert isinstance(first, ConnRequest)
    assert encode_conn_request(first.unit_id, first.request_id) == capture[:43]


def test_result_encoding() -> None:
    msg = decode_frame(encode_result(664030, 17, 1, error=0))
    assert isinstance(msg, Frame)
    assert (msg.unit_id, msg.service_id, msg.nph_type, msg.request_id) == (664030, 1, 0, 17)
    assert msg.result_code == 0
    assert not msg.needs_reply
    assert decode_frame(encode_result(1, 2, 0, error=3)).result_code == 3  # type: ignore[union-attr]


def test_extra_cells_roundtrip() -> None:
    usi0 = Cell(8, 0, fields={"det_status": 1, "level_mm": 644, "level_l": 143, "temperature": 18})
    usi1 = Cell(8, 1, fields={"det_status": 2, "level_mm": 10, "level_l": 5, "temperature": 20})
    can = Cell(10, 0, fields={"speed": 30, "tEngine": -7, "pressureAxis": [1, 2, 3, 4, 5]})
    termo = Cell(16, 0, fields={"status": 0, "temp": -12})
    opaque = Cell(9, 0, raw=bytes(range(40)))
    msg = decode_frame(encode_nav_packet(5, 9, _nav(), extra_cells=[usi0, usi1, can, termo, opaque]))
    assert isinstance(msg, NavPacket)
    assert [(c.name, c.number) for c in msg.cells] == [
        ("G6CellUsi08", 0),
        ("G6CellUsi08", 1),
        ("G6CellCan10", 0),
        ("G6CellTermo16", 0),
        ("G6CellReg09", 0),
    ]
    assert msg.cells[0].fields == usi0.fields
    assert msg.cells[2].fields is not None and msg.cells[2].fields["tEngine"] == -7
    assert msg.cells[2].fields["pressureAxis"] == [1, 2, 3, 4, 5]
    assert msg.cells[3].fields == {"status": 0, "temp": -12}
    assert msg.cells[4].raw == bytes(range(40)) and msg.cells[4].fields is None


def test_unknown_cell_type_keeps_rest_raw() -> None:
    body = _nav().to_cell_bytes() + bytes([250, 0]) + b"\x01\x02\x03"
    frame = ndtp.encode_frame(3, 1, 101, body, 1)
    dec = FrameDecoder()
    (msg,) = dec.feed(frame)
    assert isinstance(msg, NavPacket) and msg.nav == _nav()
    assert msg.cells == [Cell(250, 0, b"\x01\x02\x03", None)]
    assert dec.stats["unknown_cells"] == 1


def test_truncated_cell_counts_bad_payload() -> None:
    body = _nav().to_cell_bytes() + bytes([8, 0]) + b"\x01\x02"
    dec = FrameDecoder()
    (msg,) = dec.feed(ndtp.encode_frame(3, 1, 101, body, 1))
    assert isinstance(msg, NavPacket) and msg.nav == _nav() and msg.cells == []
    assert dec.stats["bad_payload"] == 1


def test_feed_one_byte_at_a_time() -> None:
    data, frames = _stream(8)
    dec = FrameDecoder()
    msgs = []
    for b in data:
        msgs.extend(dec.feed(bytes([b])))
    assert len(msgs) == len(frames)
    assert isinstance(msgs[0], ConnRequest)
    assert all(isinstance(m, NavPacket) for m in msgs[1:])
    assert [m.nav.speed_avg for m in msgs[1:]] == list(range(7))  # type: ignore[union-attr]
    assert dec.stats["frames_ok"] == 8
    assert dec.stats["crc_errors"] == dec.stats["resyncs"] == dec.stats["bytes_skipped"] == 0
    assert dec.buffered == 0


def test_garbage_between_frames() -> None:
    _, frames = _stream(5)
    fake_header = b"\x7e\x7e\xff\xff\x00\x00\x00\x00\x02\x01\x00\x00\x00\x00\x00"
    garbage = [b"hello", b"\x7e", b"\x00\x7e\x7e\x01", fake_header, b"\x7e\x7e\x0a"]
    data = garbage[0] + frames[0] + garbage[1] + frames[1] + garbage[2] + frames[2] + garbage[3] + frames[3]
    data += garbage[4] + frames[4]
    dec = FrameDecoder()
    msgs = dec.feed(data)
    assert len(msgs) == 5
    assert isinstance(msgs[0], ConnRequest)
    assert [m.request_id for m in msgs] == [1, 2, 3, 4, 5]
    assert dec.stats["frames_ok"] == 5
    assert dec.stats["resyncs"] >= 4
    assert dec.stats["bytes_skipped"] == sum(len(g) for g in garbage)


def test_fake_header_does_not_stall_stream() -> None:
    _, frames = _stream(3)
    fake = b"\x7e\x7e\xf0\xff\x00\x00\x00\x00\x02\x01\x00\x00\x00\x00\x00"
    dec = FrameDecoder()
    assert dec.feed(fake) == []
    assert dec.feed(frames[0][:10]) == []
    msgs = dec.feed(frames[0][10:])
    assert len(msgs) == 1 and isinstance(msgs[0], ConnRequest)
    assert len(dec.feed(frames[1] + frames[2])) == 2


@pytest.mark.parametrize("seed", range(20))
def test_random_garbage_and_chunking(seed: int) -> None:
    rng = random.Random(seed)
    _, frames = _stream(12)
    data = b""
    for f in frames:
        data += bytes(rng.randrange(256) for _ in range(rng.randrange(0, 40)))
        if rng.random() < 0.3:
            data += b"\x7e\x7e"
        data += f
    dec = FrameDecoder()
    msgs = []
    pos = 0
    while pos < len(data):
        step = rng.randrange(1, 64)
        msgs.extend(dec.feed(data[pos : pos + step]))
        pos += step
    assert [m.request_id for m in msgs] == list(range(1, 13))


def test_bad_crc_frame_is_skipped() -> None:
    _, frames = _stream(4)
    broken_body = bytearray(frames[1])
    broken_body[30] ^= 0xFF
    broken_crc = bytearray(frames[2])
    broken_crc[6] ^= 0x01
    dec = FrameDecoder()
    msgs = dec.feed(frames[0] + bytes(broken_body) + bytes(broken_crc) + frames[3])
    assert [m.request_id for m in msgs] == [1, 4]
    assert dec.stats["crc_errors"] == 2
    assert dec.stats["frames_ok"] == 2
    more = dec.feed(frames[1])
    assert [m.request_id for m in more] == [2]


def test_verify_crc_disabled() -> None:
    frame = bytearray(encode_conn_request(9, 1))
    frame[6] ^= 0xFF
    dec = FrameDecoder(verify_crc=False)
    (msg,) = dec.feed(bytes(frame))
    assert isinstance(msg, ConnRequest) and msg.unit_id == 9


def test_emulator_capture() -> None:
    data = (FIXTURES / "emulator_capture.bin").read_bytes()
    dec = FrameDecoder()
    msgs = dec.feed(data)
    assert dec.stats["crc_errors"] == 0 and dec.stats["bytes_skipped"] == 0 and dec.buffered == 0
    handshakes = [m for m in msgs if isinstance(m, ConnRequest)]
    navs = [m for m in msgs if isinstance(m, NavPacket)]
    assert len(handshakes) == 2 and len(navs) == 8
    assert {h.unit_id for h in handshakes} == {n.unit_id for n in navs}
    for h in handshakes:
        assert (h.proto_high, h.proto_low, h.max_packet_size, h.request_id) == (6, 2, 65535, 1)
    for p in navs:
        assert not p.history and p.nav is not None and p.nav.valid
        assert 55.0 < p.nav.lat < 56.5 and 37.0 < p.nav.lon < 38.5
        assert [c.name for c in p.cells] == ["G6CellUsi08", "G6CellTermo16", "G6CellIntSensor02", "G6CellCan10"]
        assert all(c.fields is not None for c in p.cells)
        assert p.cells[3].fields is not None and len(p.cells[3].fields["pressureAxis"]) == 5
        assert p.frame is not None
        assert encode_nav_packet(p.unit_id, p.request_id, p.nav, extra_cells=p.cells) == p.frame.raw


def test_emulator_cell_sizes_and_layouts() -> None:
    reference = json.loads((FIXTURES / "cells.json").read_text())
    assert {c["typeId"] for c in reference} == set(CELL_SIZES)
    assert {c["typeId"]: c["type"] for c in reference} == CELL_NAMES

    config = json.loads((FIXTURES / "emulator_cells_probe_config.json").read_text())
    expected = {u["unitId"]: u["cells"] for u in config["units"]}

    dec = FrameDecoder()
    msgs = dec.feed((FIXTURES / "emulator_cells_probe.bin").read_bytes())
    assert dec.stats["crc_errors"] == dec.stats["bad_payload"] == dec.stats["unknown_cells"] == 0
    seen_types: set[int] = set()
    for p in msgs:
        if not isinstance(p, NavPacket):
            continue
        assert p.nav is not None
        for c in p.cells:
            assert len(c.raw) == CELL_SIZES[c.type]
            seen_types.add(c.type)
        if 5000 <= p.unit_id < 5200:
            assert [c.type for c in p.cells] == [p.unit_id - 5000]
        elif p.unit_id == 6000:
            assert p.nav.lon == pytest.approx(37.617321) and p.nav.lat == pytest.approx(-55.7551234)
            assert p.nav.call_request and not p.nav.alarm and p.nav.valid
            assert (p.nav.speed_avg, p.nav.speed_max, p.nav.course, p.nav.track) == (0x2222, 0x3333, 0x144, 0x5555)
            assert (p.nav.altitude, p.nav.nsat, p.nav.pdop, p.nav.bat_voltage) == (0x6666, 0x77, 0x88, 0x11)
        elif p.unit_id in expected:
            for got, want in zip(p.cells, expected[p.unit_id], strict=True):
                assert got.name == want["type"]
                assert got.fields == want["fields"]
    assert seen_types == set(CELL_SIZES) - {0}


async def _read_results(reader: asyncio.StreamReader, n: int) -> list[Frame]:
    dec = FrameDecoder()
    out: list[Frame] = []
    while len(out) < n:
        data = await asyncio.wait_for(reader.read(4096), 5)
        assert data
        out.extend(m for m in dec.feed(data) if isinstance(m, Frame))
    return out


@pytest.mark.asyncio
async def test_server_replies_and_survives_garbage() -> None:
    received: list[tuple[int | None, object]] = []
    disconnected: list[ConnInfo] = []

    async def on_message(conn: ConnInfo, msg: object) -> None:
        received.append((conn.unit_id, msg))
        if isinstance(msg, NavPacket) and msg.request_id == 3:
            raise RuntimeError("callback failure must not kill the connection")

    server = NdtpServer("127.0.0.1", 0, on_message, disconnected.append)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        _, frames = _stream(5)
        payload = b"junk\x7e" + frames[0] + b"\x00\x01" + b"".join(frames[1:])
        for i in range(0, len(payload), 7):
            writer.write(payload[i : i + 7])
            await writer.drain()
        results = await _read_results(reader, 5)
        assert [(r.service_id, r.request_id, r.result_code) for r in results] == [
            (0, 1, 0),
            (1, 2, 0),
            (1, 3, 0),
            (1, 4, 0),
            (1, 5, 0),
        ]
        writer.write(frames[1][:20])
        await writer.drain()
        writer.close()
        await writer.wait_closed()

        reader2, writer2 = await asyncio.open_connection("127.0.0.1", server.port)
        writer2.write(encode_conn_request(111, 1))
        await writer2.drain()
        assert len(await _read_results(reader2, 1)) == 1

        for _ in range(100):
            if disconnected:
                break
            await asyncio.sleep(0.02)
        assert len(disconnected) == 1 and disconnected[0].unit_id == 664030
        assert disconnected[0].stats["resyncs"] >= 1
        assert len(received) == 6
        assert isinstance(received[0][1], ConnRequest)
        assert received[-1][0] == 111
        stats = server.stats
        assert stats["callback_errors"] == 1
        assert stats["connections_total"] == 2 and stats["connections_active"] == 1
        assert stats["frames_ok"] == 6 and stats["crc_errors"] == 0
        writer2.close()
    finally:
        await server.stop()
    assert not server.is_serving
    assert len(disconnected) == 2


@pytest.mark.asyncio
async def test_server_restart_on_same_port() -> None:
    server = NdtpServer("127.0.0.1", 0)
    await server.start()
    port = server.port
    _, w = await asyncio.open_connection("127.0.0.1", port)
    await server.stop()
    w.close()
    server2 = NdtpServer("127.0.0.1", port)
    await server2.start()
    try:
        r, w2 = await asyncio.open_connection("127.0.0.1", port)
        w2.write(encode_conn_request(5, 1))
        await w2.drain()
        assert len(await _read_results(r, 1)) == 1
        w2.close()
    finally:
        await server2.stop()
