"""Настройки backend из переменных окружения."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    """Параметры сервиса (см. README, раздел «Конфигурация»)."""

    schedule_csv: Path = Path(_env("SCHEDULE_CSV", str(ROOT / "data/dataset/validate/schedule_plan.csv")))
    units_csv: Path = Path(_env("UNITS_CSV", str(ROOT / "artifacts/reference/units.csv")))
    fallback_json: Path = Path(_env("FALLBACK_JSON", str(ROOT / "artifacts/reference/fallback.json")))
    stop_names_json: Path = Path(_env("STOP_NAMES_JSON", str(ROOT / "artifacts/reference/stop_names.json")))
    route_geometry_json: Path = Path(_env("ROUTE_GEOMETRY_JSON", str(ROOT / "artifacts/reference/route_geometry.json")))
    route_paths_json: Path = Path(_env("ROUTE_PATHS_JSON", str(ROOT / "artifacts/reference/route_paths.json")))
    ml_url: str = _env("ML_URL", "http://localhost:8001")
    ml_timeout_s: float = float(_env("ML_TIMEOUT_S", "2.0"))
    ndtp_host: str = _env("NDTP_HOST", "0.0.0.0")
    ndtp_port: int = int(_env("NDTP_PORT", "9201"))
    predict_every_s: float = float(_env("PREDICT_EVERY_S", "30"))
    stale_gps_s: float = float(_env("STALE_GPS_S", "120"))
    offline_s: float = float(_env("OFFLINE_S", "1800"))
    live_wall_s: float = float(_env("LIVE_WALL_S", "10"))
    stale_wall_s: float = float(_env("STALE_WALL_S", "60"))
    ws_period_s: float = float(_env("WS_PERIOD_S", "1.0"))
    history_h: float = float(_env("HISTORY_H", "3"))


settings = Settings()
