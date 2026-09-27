"""HTTP API replayer'а (FastAPI, порт 8090).

Эндпоинты по контракту ``docs/contracts.md`` §3: ``GET /api/status``,
``POST /api/start``, ``/api/pause``, ``/api/resume``, ``/api/stop``,
``/api/speed``, а также ``GET /health``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .engine import MAX_SPEED, ReplayEngine, ReplaySettings

__all__ = ["SpeedRequest", "StartRequest", "app", "create_app"]

log = logging.getLogger("replayer")


class StartRequest(BaseModel):
    """Тело ``POST /api/start``; незаданные поля берутся из последнего запуска/окружения."""

    speed: float | None = Field(default=None, gt=0, le=MAX_SPEED, description="Скорость потока")
    start_time: str | None = Field(default=None, description="Начало live-фазы, HH:MM")
    warmup_min: float | None = Field(default=None, ge=0, le=24 * 60, description="Прогрев, минут")


class SpeedRequest(BaseModel):
    """Тело ``POST /api/speed``."""

    speed: float = Field(gt=0, le=MAX_SPEED, description="Новая скорость потока")


def create_app(settings: ReplaySettings | None = None) -> FastAPI:
    """Создаёт FastAPI-приложение replayer'а.

    Args:
        settings: Настройки; по умолчанию читаются из окружения.

    Returns:
        Приложение; движок доступен как ``app.state.engine``.
    """
    settings = settings or ReplaySettings.from_env()
    engine = ReplayEngine(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if settings.autostart:
            try:
                await engine.start()
            except Exception as exc:
                engine.error = f"{type(exc).__name__}: {exc}"
                log.exception("autostart failed")
        yield
        await engine.stop()

    app = FastAPI(title="NDTP Replayer", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.state.engine = engine

    @app.get("/health")
    async def health() -> dict[str, Any]:
        """Проверка живости сервиса."""
        return {"status": "ok", "state": engine.state}

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        """Текущее состояние проигрывания."""
        return engine.status()

    @app.post("/api/start")
    async def start(body: StartRequest | None = None) -> dict[str, Any]:
        """Запускает проигрывание с начала."""
        body = body or StartRequest()
        try:
            return await engine.start(body.speed, body.start_time, body.warmup_min)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=503, detail=f"traffic CSV not found: {exc.filename}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/pause")
    async def pause() -> dict[str, Any]:
        """Пауза потока."""
        return engine.pause()

    @app.post("/api/resume")
    async def resume() -> dict[str, Any]:
        """Продолжение после паузы."""
        return engine.resume()

    @app.post("/api/stop")
    async def stop() -> dict[str, Any]:
        """Остановка и закрытие соединений."""
        return await engine.stop()

    @app.post("/api/speed")
    async def speed(body: SpeedRequest) -> dict[str, Any]:
        """Смена скорости на лету."""
        try:
            return engine.set_speed(body.speed)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = create_app()
