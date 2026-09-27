"""Запоздавшие точки одного ТС не должны очищать состояние всего парка."""

import pytest

from conftest import needs_dataset

from backend.config import Settings
from backend.fleet import Fleet
from backend.reference import Reference


@pytest.fixture
def fleet():
    cfg = Settings()
    ref = Reference(cfg.schedule_csv, cfg.units_csv, cfg.fallback_json)
    return Fleet(ref, cfg)


def ingest(fleet, unit_id, t):
    fleet._ingest(t, unit_id, t, None, None, None, None, False)


def mapped_vehicles(fleet):
    return [tr for tr in fleet.ref.tr_to_unit if tr in fleet.vehicles]


@needs_dataset
def test_other_vehicle_catching_up_does_not_reset_fleet(fleet):
    tr1, tr2 = mapped_vehicles(fleet)[:2]
    t = fleet.ref.day_start + 7 * 3600
    ingest(fleet, fleet.ref.tr_to_unit[tr1], t + 1200)
    first = fleet.vehicles[tr1]
    fleet.alerts[tr1] = {"id": "kept"}
    fleet.errors.append((10.0, None, True, False))

    ingest(fleet, fleet.ref.tr_to_unit[tr2], t)

    assert fleet.vehicles[tr1] is first
    assert fleet.alerts[tr1]["id"] == "kept"
    assert len(fleet.errors) == 1
    assert fleet.clock == t + 1200
    assert fleet.vehicles[tr2].last_t == t


@needs_dataset
def test_same_vehicle_rewind_resets_fleet(fleet):
    tr = mapped_vehicles(fleet)[0]
    unit_id = fleet.ref.tr_to_unit[tr]
    t = fleet.ref.day_start + 7 * 3600
    ingest(fleet, unit_id, t + 1200)
    first = fleet.vehicles[tr]
    fleet.alerts[tr] = {"id": "cleared"}

    ingest(fleet, unit_id, t)

    assert fleet.vehicles[tr] is not first
    assert not fleet.alerts
    assert fleet.clock == t
