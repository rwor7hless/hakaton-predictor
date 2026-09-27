"""HTTP API ML-ядра (FastAPI). Swagger: ``/docs``, OpenAPI: ``/openapi.json``."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import Body, FastAPI, HTTPException
from pydantic import BaseModel, Field

from .predictor import Predictor

ARTIFACTS = Path(os.environ.get("ARTIFACTS_DIR", Path(__file__).resolve().parents[3] / "artifacts"))
MODEL_DIR = Path(os.environ.get("MODEL_DIR", ARTIFACTS / "models" / "current"))

@asynccontextmanager
async def lifespan(app: FastAPI):
    global predictor
    predictor = Predictor(MODEL_DIR)
    yield


app = FastAPI(
    lifespan=lifespan,
    title="Transit Delay ML Core",
    version="1.0.0",
    description="ML-ядро предиктора задержек: ансамбль CatBoost + PyTorch (ONNX), интервалы, вероятности, "
                "объяснения (SHAP), дообучение.",
)
predictor: Predictor | None = None
latencies: deque[float] = deque(maxlen=2000)
counters = {"requests": 0, "items": 0, "errors": 0}
jobs: dict[str, dict] = {}


class Item(BaseModel):
    """Одна прогнозная точка: идентификатор и словарь признаков (см. ``GET /v1/model``)."""

    id: str
    features: dict[str, Any]


class PredictRequest(BaseModel):
    items: list[Item] = Field(default_factory=list)
    explain: bool = True


ONLINE_EXAMPLE = {"items": [{"id": "vehicle-131672", "features": {
    "lead": 660, "gap_prev_plan": 60, "gap_next_plan": 60, "tgt_role": 0,
    "n_plan_between": 5, "plan_since_lp": 300, "est_now": 42, "dev_last": 30,
    "tgt_stop": "37.857186,55.730929", "route": "R9e0c5496",
}}], "explain": True}

OFFLINE_EXAMPLE = {"items": [{"id": "131672_1767670800", "features": {
    "cur_dev": 274, "lead": 660, "gap_prev_plan": 60, "gap_next_plan": 60,
    "tgt_role": 0, "n_plan_between": 5, "plan_since_lp": 300,
    "tgt_stop": "37.857186,55.730929", "route": "R9e0c5496",
}}], "explain": False}


class Contribution(BaseModel):
    feature: str
    text: str
    value: Any = None
    shap: float


class PredictItem(BaseModel):
    id: str
    delay_s: float
    q10_s: float
    q90_s: float
    p_late: float
    p_early: float
    contributions: list[Contribution] = Field(default_factory=list)


class PredictResponse(BaseModel):
    model_version: str
    latency_ms: float
    items: list[PredictItem]


class OfflineItem(BaseModel):
    id: str
    delay_s: float


class OfflineResponse(BaseModel):
    model_version: str
    latency_ms: float
    items: list[OfflineItem]


class TrainJob(BaseModel):
    job_id: str
    state: str
    started_at: float
    finished_at: float | None = None
    model_dir: str | None = None
    log_tail: str = ""


def _pred() -> Predictor:
    if predictor is None:
        raise HTTPException(503, "модель не загружена")
    return predictor


@app.get("/health", summary="Проверка живости")
def health() -> dict:
    return {"status": "ok" if predictor is not None else "loading",
            "model_version": predictor.meta.get("version") if predictor else None}


@app.get("/v1/model", summary="Версия, признаки, метрики и состав ансамбля")
def model_info() -> dict:
    return _pred().info()


@app.post("/v1/predict", response_model=PredictResponse, summary="Онлайн-прогноз задержки (пакетный)")
def predict(req: PredictRequest = Body(openapi_examples={"online": {
    "summary": "Пример онлайн-прогноза", "value": ONLINE_EXAMPLE,
}})) -> PredictResponse:
    p = _pred()
    t0 = time.perf_counter()
    try:
        out = p.predict([i.model_dump() for i in req.items], explain=req.explain)
    except Exception as e:
        counters["errors"] += 1
        raise HTTPException(422, f"ошибка инференса: {e}") from e
    ms = (time.perf_counter() - t0) * 1000
    latencies.append(ms)
    counters["requests"] += 1
    counters["items"] += len(req.items)
    return PredictResponse(model_version=p.meta.get("version", "?"), latency_ms=ms, items=out)


@app.post("/v1/predict/offline", response_model=OfflineResponse,
          summary="Прогноз в режиме сабмита (с подсказкой cur_dev, как в validate/points.csv)")
def predict_offline(req: PredictRequest = Body(openapi_examples={"offline": {
    "summary": "Пример прогноза с подсказкой cur_dev", "value": OFFLINE_EXAMPLE,
}})) -> OfflineResponse:
    p = _pred()
    t0 = time.perf_counter()
    try:
        out = p.predict_offline([i.model_dump() for i in req.items])
    except Exception as e:
        counters["errors"] += 1
        raise HTTPException(422, f"ошибка инференса: {e}") from e
    return OfflineResponse(model_version=p.meta.get("version", "?"), latency_ms=(time.perf_counter() - t0) * 1000,
                           items=out)


@app.get("/metrics", summary="Задержка инференса и счётчики")
def metrics() -> dict:
    lat = np.array(latencies) if latencies else np.array([np.nan])
    return {**counters, "latency_ms_p50": float(np.nanpercentile(lat, 50)),
            "latency_ms_p95": float(np.nanpercentile(lat, 95)), "latency_ms_max": float(np.nanmax(lat))}


def _run_train(job_id: str) -> None:
    job = jobs[job_id]
    out = ARTIFACTS / "models" / f"v-{job_id}"
    cmd = [sys.executable, "-m", "mlcore.train", "--out", str(out),
           "--submission", str(ARTIFACTS / f"submission-{job_id}.csv"), "--cache", str(ARTIFACTS / "cache")]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[1]))
    job["log_tail"] = (proc.stdout + proc.stderr)[-3000:]
    job["finished_at"] = time.time()
    if proc.returncode != 0:
        job["state"] = "failed"
        return
    cur = MODEL_DIR
    if cur.is_symlink() or not cur.exists():
        tmp = cur.with_name(cur.name + ".new")
        if tmp.exists() or tmp.is_symlink():
            tmp.unlink()
        tmp.symlink_to(out, target_is_directory=True)
        tmp.replace(cur)
    else:
        shutil.copytree(out, cur, dirs_exist_ok=True)
    _pred().load()
    job["state"] = "done"
    job["model_dir"] = str(out)


@app.post("/v1/train", response_model=TrainJob, summary="Запустить дообучение в фоне (горячая замена модели)")
def train() -> TrainJob:
    if any(j["state"] == "running" for j in jobs.values()):
        raise HTTPException(409, "обучение уже идёт")
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {"job_id": job_id, "state": "running", "started_at": time.time()}
    threading.Thread(target=_run_train, args=(job_id,), daemon=True).start()
    return TrainJob(**jobs[job_id])


@app.get("/v1/train/{job_id}", response_model=TrainJob, summary="Статус задачи дообучения")
def train_status(job_id: str) -> TrainJob:
    if job_id not in jobs:
        raise HTTPException(404, "нет такой задачи")
    return TrainJob(**jobs[job_id])
