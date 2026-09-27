"""Backend: приём NDTP, сопоставление с расписанием, прогнозы, алерты, REST/WebSocket API.

Swagger: ``/docs``, OpenAPI: ``/openapi.json``. NDTP принимается по TCP на
``NDTP_PORT`` (по умолчанию 9201).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ndtp import ConnRequest, NavPacket
from ndtp.server import ConnInfo, NdtpServer
from transit_core.schedule import to_unix

from .config import settings
from .fleet import Fleet, _clean
from .reference import Reference
from .whatif import apply_post, scenario_features, summarize

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("backend")
logging.getLogger("httpx").setLevel(logging.WARNING)

fleet: Fleet | None = None
ndtp_server: NdtpServer | None = None


def _on_message(conn: ConnInfo, msg: Any) -> None:
    if isinstance(msg, ConnRequest):
        fleet.on_connect(msg.unit_id)
    elif isinstance(msg, NavPacket) and msg.nav is not None:
        n = msg.nav
        fleet.submit(msg.unit_id, float(n.timestamp), n.lon if n.valid else None, n.lat if n.valid else None,
                     float(n.speed_avg), float(n.course), bool(n.valid))


def _on_disconnect(conn: ConnInfo) -> None:
    if conn.unit_id is not None:
        fleet.on_disconnect(conn.unit_id)


async def _stats_loop() -> None:
    while True:
        if ndtp_server is not None:
            fleet.ndtp_stats = ndtp_server.stats
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global fleet, ndtp_server
    ref = Reference(settings.schedule_csv, settings.units_csv, settings.fallback_json, settings.route_paths_json,
                    settings.route_geometry_json, settings.stop_names_json)
    fleet = Fleet(ref, settings)
    await fleet.start()
    ndtp_server = NdtpServer(settings.ndtp_host, settings.ndtp_port, _on_message, _on_disconnect,
                             idle_timeout=settings.stale_wall_s)
    await ndtp_server.start()
    task = asyncio.create_task(_stats_loop())
    log.info("backend ready: %d vehicles with schedule, NDTP on %s", len(fleet.vehicles), settings.ndtp_port)
    yield
    task.cancel()
    await ndtp_server.stop()
    await fleet.stop()


app = FastAPI(
    title="Transit Delay Backend",
    version="1.0.0",
    description="Приём телеметрии NDTP, наложение на нитку графика, прогноз отклонения за 10–15 минут, "
                "алерты и API диспетчерского дашборда.",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _fleet() -> Fleet:
    if fleet is None:
        raise HTTPException(503, "сервис запускается")
    return fleet


class TelemetryPoint(BaseModel):
    """Точка телеметрии в JSON (альтернатива NDTP для подачи исторических данных)."""

    unit_id: int
    event_time: datetime = Field(description="время точки (время потока)")
    lon: float | None = None
    lat: float | None = None
    speed: float | None = None
    heading: float | None = None
    location_valid: bool = True


class WhatIfRequest(BaseModel):
    tr_id: int
    scenario: Literal["extra_vehicle", "dwell_cut", "signal_priority"]
    value: float | None = None


@app.get("/api/health", tags=["service"], summary="Состояние потока, ML и сервиса")
def health() -> dict:
    return _fleet().health()


@app.get("/api/state", tags=["dashboard"], summary="Снимок для дашборда (то же приходит по WebSocket /ws)")
def state() -> dict:
    return _fleet().state()


@app.get("/api/vehicles", tags=["dashboard"], summary="Все ТС с текущим прогнозом")
def vehicles() -> list[dict]:
    f = _fleet()
    return [f.vehicle_view(v) for v in list(f.vehicles.values()) + list(f.unknown.values())]


@app.get("/api/vehicles/{tr_id}", tags=["dashboard"], summary="Детали ТС: трек, ближайшие остановки, история отклонения")
def vehicle(tr_id: int) -> dict:
    d = _fleet().vehicle_detail(tr_id)
    if d is None:
        raise HTTPException(404, "ТС не найдено")
    return d


@app.get("/api/routes", tags=["dashboard"], summary="Маршрутная сеть с риском по участкам")
def routes(geometry: bool = Query(True, description="false — только участки под риском, без геометрии")) -> list[dict]:
    return _fleet().routes_view(geometry)


@app.get("/api/alerts", tags=["dashboard"], summary="Инциденты (алерты)")
def alerts(status: Literal["active", "resolved", "all"] = Query("active")) -> list[dict]:
    return _fleet().alerts_view(status)


@app.post("/api/alerts/{alert_id}/ack", tags=["dashboard"], summary="Взять инцидент в работу")
def alert_ack(alert_id: str) -> dict:
    a = _fleet().ack_alert(alert_id, True)
    if a is None:
        raise HTTPException(404, "активный инцидент не найден")
    return a


@app.delete("/api/alerts/{alert_id}/ack", tags=["dashboard"], summary="Снять отметку «в работе»")
def alert_unack(alert_id: str) -> dict:
    a = _fleet().ack_alert(alert_id, False)
    if a is None:
        raise HTTPException(404, "активный инцидент не найден")
    return a


@app.get("/api/hotspots", tags=["dashboard"],
         summary="Проблемные участки: доля рискованных прогнозов, средняя задержка, типичные причины")
def hotspots(top: int = Query(15, ge=1, le=100)) -> list[dict]:
    return _fleet().hotspots(top)


@app.get("/api/metrics", tags=["service"], summary="Онлайн-MAE, соблюдение горизонта, задержки, пропускная способность")
def metrics() -> dict:
    return _fleet().metrics()


@app.post("/api/whatif", tags=["dashboard"], summary="What-if: эффект управляющего воздействия на прогноз")
async def whatif(req: WhatIfRequest) -> dict:
    f = _fleet()
    v = f.vehicles.get(req.tr_id)
    if v is None or v.prediction is None or v.features is None:
        raise HTTPException(409, "для ТС нет актуального прогноза")
    try:
        feats, text = scenario_features(v.features, req.scenario, req.value)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    r = (await f._ml_predict([(str(v.tr_id), feats)]))[0]
    after = apply_post(req.scenario, req.value, float(r["delay_s"]), feats, float(v.prediction["delay_s"]))
    base = {k: v.prediction[k] for k in v.prediction if k != "contributions"}
    return summarize(base, after, r["p_late"] if req.scenario != "signal_priority" else min(r["p_late"], v.prediction["p_late"]),
                     r["p_early"], text)


@app.post("/api/telemetry", tags=["ingest"], summary="Приём телеметрии в JSON (исторический датасет без NDTP)")
def telemetry(points: list[TelemetryPoint]) -> dict:
    f = _fleet()
    for p in points:
        f.submit(p.unit_id, to_unix(p.event_time.replace(tzinfo=None)), p.lon, p.lat, p.speed, p.heading,
                 p.location_valid and p.lon is not None and not math.isnan(p.lon))
    return {"accepted": len(points), "queue_len": f.queue.qsize()}


@app.post("/api/reset", tags=["service"], summary="Сбросить оперативное состояние (новая сессия потока)")
def reset() -> dict:
    _fleet().reset()
    return {"status": "reset"}


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            await websocket.send_json({"type": "state", "data": _fleet().state()})
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(websocket.receive_text(), timeout=settings.ws_period_s)
    except (WebSocketDisconnect, RuntimeError):
        return


__all__ = ["app", "_clean"]
