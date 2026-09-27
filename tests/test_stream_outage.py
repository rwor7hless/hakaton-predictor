"""При обрыве потока ТС не должно выглядеть как продолжающее движение."""

import time

from conftest import needs_dataset

from backend.config import Settings
from backend.fleet import Fleet
from backend.reference import Reference


@needs_dataset
def test_vehicle_becomes_stale_when_stream_stops():
    cfg = Settings()
    ref = Reference(cfg.schedule_csv, cfg.units_csv, cfg.fallback_json)
    fleet = Fleet(ref, cfg)
    vehicle = next(iter(fleet.vehicles.values()))
    t = float(vehicle.tracker.s.plan[5] - 30)
    fleet.clock = vehicle.last_t = t
    vehicle.speed = 20.0
    fleet.last_packet_wall = time.time()
    assert fleet.vehicle_view(vehicle)["status"] == "moving"

    fleet.last_packet_wall = time.time() - cfg.live_wall_s - 1
    view = fleet.vehicle_view(vehicle)
    assert fleet.stream_state() == "stale"
    assert view["status"] == "stale" and view["degraded"]

    fleet.last_packet_wall = time.time() - cfg.stale_wall_s - 1
    view = fleet.vehicle_view(vehicle)
    assert fleet.stream_state() == "no_data"
    assert view["status"] == "stale" and view["degraded"]

    fleet.last_packet_wall = time.time()
    view = fleet.vehicle_view(vehicle)
    assert view["status"] == "moving" and not view["degraded"]
