"""Жизненный цикл инцидента: открытие по устойчивому риску, закрытие с гистерезисом, продолжение."""
import os
from collections import deque

import pytest

# Как в test_backend_api: при любом порядке запуска backend не должен занимать порт NDTP.
os.environ.setdefault("NDTP_PORT", "0")
os.environ.setdefault("ML_URL", "http://127.0.0.1:9")
os.environ.setdefault("PREDICT_EVERY_S", "60")

from backend import fleet as F
from backend.causes import risk_level, risk_reason

T0 = 1767682800.0  # 2026-01-06 07:00 по времени потока
STEP = 30.0


@pytest.fixture
def fl():
    f = F.Fleet.__new__(F.Fleet)
    f.alerts, f.resolved, f._seq = {}, deque(maxlen=300), 0
    return f


@pytest.fixture
def veh():
    return F.Vehicle(tr_id=900001, unit_id=None, tracker=None, route_name="тест")


def feed(fl, v, delays, t0=T0, p_late=0.0):
    """Подать прогнозы с шагом 30 с; вернуть статусы ТС после каждого."""
    out = []
    for k, d in enumerate(delays):
        pred = {"delay_s": d, "p_late": p_late, "p_early": 0.0, "target_stop_name": "Остановка",
                "target_planned": "2026-01-06T07:15:00"}
        raw = risk_level(d, p_late, 0.0)
        out.append(fl._track_incident(v, t0 + k * STEP, pred, d, raw, k))
    return out


def test_single_yellow_does_not_open(fl, veh):
    assert feed(fl, veh, [100, 20, 110, 30]) == ["green"] * 4
    assert not fl.alerts


def test_two_yellow_in_a_row_open(fl, veh):
    st = feed(fl, veh, [100, 110])
    assert st == ["green", "yellow"] and veh.tr_id in fl.alerts
    a = fl.alerts[veh.tr_id]
    assert a["opened"]["reason"] == {"risk": "yellow", "rule": "delay_late", "value": 110.0, "threshold": 90.0}


def test_red_opens_immediately(fl, veh):
    assert feed(fl, veh, [200]) == ["red"]
    assert fl.alerts[veh.tr_id]["risk"] == "red"


def test_no_flicker_until_calm_for_three_minutes(fl, veh):
    # Открыт, потом прогноз колеблется у границы: инцидент держится, статус не ниже «Внимание».
    st = feed(fl, veh, [100, 110, 40, 95, 70, 30, 20, 10, 0, 10])
    assert "green" not in st[1:9]
    # 30,20,10,0,10,... — в норме с 5-го шага; закрытие через 180 с (6 шагов) подряд.
    st = feed(fl, veh, [5, 5, 5], t0=T0 + 10 * STEP)
    assert st[-1] == "green" and veh.tr_id not in fl.alerts
    assert fl.resolved[0]["resolved_reason"] == "calm"


def test_green_but_outside_band_keeps_incident(fl, veh):
    feed(fl, veh, [200])
    st = feed(fl, veh, [80] * 10, t0=T0 + STEP)  # зелёный, но +80 с > ±60 с
    assert st[-1] == "yellow" and veh.tr_id in fl.alerts


def test_red_deescalates_after_two_non_red(fl, veh):
    assert feed(fl, veh, [200, 120, 120]) == ["red", "red", "yellow"]


def test_level_does_not_flip_on_alternating_forecasts(fl, veh):
    st = feed(fl, veh, [100, 110, 200, 120, 200, 120, 200, 200])
    assert st == ["green", "yellow", "yellow", "yellow", "yellow", "yellow", "yellow", "red"]


def test_reopen_within_ten_minutes_continues_same_incident(fl, veh):
    feed(fl, veh, [200] + [0] * 8)
    first = fl.resolved[0]["id"]
    feed(fl, veh, [200], t0=T0 + 9 * STEP + 300)
    a = fl.alerts[veh.tr_id]
    assert a["id"] == first and a["reopen_count"] == 1 and a["status"] == "active"


def test_risk_reason_order():
    assert risk_reason(10, 0.7, 0.0)["rule"] == "p_late"
    assert risk_reason(-100, 0.0, 0.0) == {"risk": "yellow", "rule": "delay_early", "value": -100.0, "threshold": -90.0}
    assert risk_reason(10, 0.1, 0.1) is None
