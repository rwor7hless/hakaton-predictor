"""Состояние парка и конвейер «телеметрия → сопоставление → признаки → прогноз → алерты».

Время внутри — «время потока» (метки телеметрии, unix-секунды). Прогнозы
строятся по часам потока каждые ``PREDICT_EVERY_S`` секунд для каждого ТС на
первую остановку с плановым временем в окне ``(T+10, T+15]`` мин — строго
заранее: в признаки попадает только телеметрия с меткой ``<= T``.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field

import httpx
import numpy as np

from transit_core.features import gps_sequence, snapshot
from transit_core.schedule import from_unix
from transit_core.tracker import VehicleTracker

from .causes import detect_causes, recommendations, risk_level, risk_reason
from .config import Settings
from .reference import Reference

log = logging.getLogger("backend.fleet")

RESET_BACKJUMP_S = 600.0

# Жизненный цикл инцидента. Порог закрытия ниже порога открытия, поэтому инцидент
# не мигает, когда прогноз колеблется у границы светофора.
OPEN_AFTER = 2           # прогнозов «Внимание» подряд, чтобы завести инцидент («Критично» — сразу)
LEVEL_CONFIRM = 2        # прогнозов подряд, чтобы открытый инцидент сменил уровень «Внимание» ↔ «Критично»
CALM_BAND_S = 60.0       # прогноз в норме: зелёный и отклонение не больше минуты
CLOSE_AFTER_S = 180.0    # столько потока прогноз должен быть в норме подряд, чтобы закрыть инцидент
REOPEN_S = 600.0         # риск у той же машины в течение 10 мин после закрытия продолжает прежний инцидент
HISTORY_MAX = 120        # точек истории прогнозов в инциденте (час потока при шаге 30 с)
TRIP_GAP_S = 600.0       # перерыв между плановыми остановками дольше 10 мин — стоянка между рейсами
HOLD_MAX_S = 1800.0      # удержанный прогноз снимается, если цель на 30 мин позже плана так и не пройдена


def _clean(v):
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, (np.floating,)):
        v = float(v)
        return v if math.isfinite(v) else None
    if isinstance(v, np.integer):
        return int(v)
    return v


def reliable_delay(f: dict) -> float | None:
    """Текущее отклонение, если трекинг надёжен (свежий GPS и недавно пройденная остановка)."""
    est, since, age = _clean(f.get("est_now")), _clean(f.get("since_last")), _clean(f.get("gps_age"))
    if est is None or since is None or since > 600 or age is None or age > 180 or abs(est) >= 900:
        return None
    return round(float(est), 1)


def _pct(xs, q) -> float | None:
    return float(np.percentile(list(xs), q)) if xs else None


@dataclass
class Vehicle:
    """Оперативное состояние одного ТС."""

    tr_id: int
    unit_id: int | None
    tracker: VehicleTracker | None
    route_name: str
    last_wall: float = 0.0
    last_t: float | None = None
    lon: float | None = None
    lat: float | None = None
    speed: float | None = None
    heading: float | None = None
    connected: bool = False
    prediction: dict | None = None
    features: dict | None = None
    risk: str = "grey"
    degraded: bool = False
    delay_hist: deque = field(default_factory=lambda: deque(maxlen=2000))
    route_m: float | None = None
    route_m_t: float | None = None
    pos_t: float | None = None
    target_idx: int | None = None
    risk_streak: int = 0
    held: dict | None = None
    outcomes: dict = field(default_factory=dict)


@dataclass
class Issued:
    """Выданный прогноз, ожидающий факта для онлайн-оценки."""

    tr_id: int
    idx: int
    issued_at: float
    planned: float
    delay: float
    naive: float | None


class Fleet:
    """Весь парк: ingest телеметрии, прогнозный цикл, алерты, метрики.

    Args:
        ref: справочные данные.
        cfg: настройки.
    """

    def __init__(self, ref: Reference, cfg: Settings):
        self.ref, self.cfg = ref, cfg
        self.started = time.time()
        self.vehicles: dict[int, Vehicle] = {}
        self.unknown: dict[int, Vehicle] = {}
        self.alerts: dict[int, dict] = {}
        self._seq = 0
        self.hot: dict[tuple, dict] = {}
        self.resolved: deque[dict] = deque(maxlen=300)
        self.pending: dict[tuple[int, int], list[Issued]] = {}
        self.errors: deque[tuple[float, float | None, bool, bool]] = deque(maxlen=5000)
        self.leads: deque[float] = deque(maxlen=5000)
        self.clock: float | None = None
        self.next_tick: float | None = None
        self.packets = 0
        self.pkt_times: deque[float] = deque(maxlen=5000)
        self.last_packet_wall: float | None = None
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=100_000)
        self.ml_state = "unknown"
        self.ml_version: str | None = None
        self.ml_lat: deque[float] = deque(maxlen=500)
        self.e2e_lat: deque[float] = deque(maxlen=500)
        self.predictions_total = 0
        self.retro = 0
        self.ndtp_stats: dict = {}
        self._http: httpx.AsyncClient | None = None
        self._ml_fail_until = 0.0
        self._trigger_wall = time.time()
        self.model_metrics: dict = {}
        self._model_checked = 0.0
        self.fresh_lat: deque[float] = deque(maxlen=500)
        self._init_vehicles()

    def _init_vehicles(self) -> None:
        self.vehicles = {}
        for tr, s in self.ref.schedules.items():
            self.vehicles[tr] = Vehicle(tr, self.ref.tr_to_unit.get(tr), VehicleTracker(s, relocate=True),
                                        self.ref.routes[tr].name)

    def reset(self) -> None:
        """Полный сброс состояния (перемотка потока назад)."""
        log.warning("stream time jumped back — resetting fleet state")
        self._init_vehicles()
        self.alerts.clear()
        self.resolved.clear()
        self.hot.clear()
        self.pending.clear()
        self.errors.clear()
        self.leads.clear()
        self.clock = None
        self.next_tick = None

    async def start(self) -> None:
        """Запустить фоновые задачи обработки очереди."""
        self._http = httpx.AsyncClient(base_url=self.cfg.ml_url, timeout=self.cfg.ml_timeout_s)
        asyncio.create_task(self._consumer())
        asyncio.create_task(self._probe_ml())

    async def stop(self) -> None:
        if self._http:
            await self._http.aclose()

    # ---------- ingest ----------
    def submit(self, unit_id: int, t: float, lon: float | None, lat: float | None, speed: float | None,
               course: float | None, valid: bool) -> None:
        """Положить точку телеметрии в очередь обработки (вызывается из NDTP-сервера)."""
        try:
            self.queue.put_nowait((time.time(), unit_id, t, lon, lat, speed, course, valid))
        except asyncio.QueueFull:
            log.error("ingest queue overflow, dropping packet")

    def on_connect(self, unit_id: int) -> None:
        v = self._vehicle_by_unit(unit_id)
        v.connected = True

    def on_disconnect(self, unit_id: int) -> None:
        v = self._vehicle_by_unit(unit_id, create=False)
        if v is not None:
            v.connected = False

    def _vehicle_by_unit(self, unit_id: int, create: bool = True) -> Vehicle | None:
        tr = self.ref.unit_to_tr.get(unit_id)
        if tr is not None and tr in self.vehicles:
            return self.vehicles[tr]
        if unit_id not in self.unknown and create:
            self.unknown[unit_id] = Vehicle(tr if tr is not None else unit_id, unit_id, None,
                                            f"Без расписания (терминал {unit_id})")
        return self.unknown.get(unit_id)

    async def _consumer(self) -> None:
        while True:
            item = await self.queue.get()
            try:
                self._ingest(*item)
                while not self.queue.empty():
                    item = self.queue.get_nowait()
                    self._ingest(*item)
                self._trigger_wall = item[0]
                await self._maybe_predict()
            except Exception:
                log.exception("ingest failure")

    def _ingest(self, wall, unit_id, t, lon, lat, speed, course, valid) -> None:
        self.packets += 1
        self.pkt_times.append(wall)
        self.last_packet_wall = wall
        v = self._vehicle_by_unit(unit_id)
        v.connected = True
        v.last_wall = wall
        if v.tracker is None:
            v.last_t = t
            if valid and lon is not None:
                v.lon, v.lat, v.speed, v.heading = lon, lat, speed, course
            return
        t = self.ref.project_time(t)
        if v.last_t is not None and t < v.last_t - RESET_BACKJUMP_S:
            self.reset()
            v = self._vehicle_by_unit(unit_id)
        v.tracker.update(t, lon, lat, speed, course, valid)
        v.last_t = t
        # Точку сбоя GPS (далеко от маршрута) не показываем: маркер остаётся на последнем честном месте.
        if valid and lon is not None and math.isfinite(lon) and lon != 0 and not v.tracker.off_route:
            v.lon, v.lat, v.speed, v.heading = lon, lat, speed, course
            v.pos_t = t
        for idx, at, interp in v.tracker.pop_events():
            self._on_arrival(v, idx, at, interp)
        if self.clock is None or t > self.clock:
            self.clock = t

    # ---------- evaluation ----------
    def _on_arrival(self, v: Vehicle, idx: int, at: float, interp: bool) -> None:
        s = v.tracker.s
        dev = at - s.plan[idx]
        if not interp:
            v.delay_hist.append((at, float(dev), "actual", None, None, idx))
            v.outcomes[idx] = float(dev)
        for it in self.pending.pop((v.tr_id, idx), []):
            if interp:
                continue
            retro = it.issued_at >= at
            self.retro += int(retro)
            self.errors.append((abs(it.delay - dev), None if it.naive is None else abs(it.naive - dev),
                                600 <= it.planned - it.issued_at <= 900, retro))
        if interp:
            return
        for a in list(self.alerts.values()) + list(self.resolved)[:50]:
            if a["tr_id"] == v.tr_id and idx in a.get("_targets", {}):
                a["actual_delay_s"] = round(float(dev), 1)
                a["error_s"] = round(abs(a["_targets"][idx] - dev), 1)

    # ---------- prediction loop ----------
    async def _maybe_predict(self) -> None:
        if self.clock is None:
            return
        step = self.cfg.predict_every_s
        if self.next_tick is None:
            self.next_tick = math.floor(self.clock / step) * step
        if self.clock < self.next_tick:
            return
        T = self.next_tick
        self.next_tick = math.floor(self.clock / step) * step + step
        await self.predict_at(T)

    def _build(self, v: Vehicle, T: float):
        tk = v.tracker
        s = tk.s
        if v.last_t is None or T - v.last_t > self.cfg.offline_s:
            return None
        ti = s.target_index(T)
        if ti is None:
            return None
        f = snapshot(tk, T, ti, self.ref.routes[v.tr_id].key)
        f["seq"] = gps_sequence(tk, T, ti).ravel().tolist()
        return ti, f

    def _trip_ends_before(self, v: Vehicle, t: float) -> bool:
        """До момента ``t`` рейс ТС заканчивается: следующая плановая остановка — уже после стоянки."""
        s = v.tracker.s
        k = int(np.searchsorted(s.plan, t, side="right"))
        return k >= len(s) or (k > 0 and s.plan[k] - s.plan[k - 1] > TRIP_GAP_S)

    def _hold(self, v: Vehicle, T: float) -> bool:
        """Сохранить последний прогноз, пока ТС не проехало его цель.

        В окне 10–15 мин может не оказаться плановой остановки: в конце рейса (до конечной
        меньше 10 мин) или на длинном перегоне. Прогноз, выданный раньше за 10–15 мин до
        цели, остаётся в силе; новых прогнозов модель в это время не выдаёт.
        """
        p, ti = v.prediction, v.target_idx
        if p is None or ti is None or v.tracker is None or v.last_t is None or T - v.last_t > self.cfg.offline_s:
            return False
        s = v.tracker.s
        idx, _ = v.tracker.arrivals_upto(T)
        if (len(idx) and int(idx[-1]) >= ti) or T > s.plan[ti] + HOLD_MAX_S:
            return False
        if v.held is None:
            trip_end = self._trip_ends_before(v, T + 600)
            k = int(np.searchsorted(s.plan, T + 600, side="right"))
            v.held = {"since": from_unix(T), "reason": "trip_end" if trip_end else "long_segment",
                      "resume_at": None if trip_end or k >= len(s) else from_unix(s.plan[k] - 900)}
        return True

    async def predict_at(self, T: float) -> None:
        """Построить прогнозы для всех активных ТС на момент потока ``T``."""
        t0 = time.time()
        batch, held = [], set()
        for v in self.vehicles.values():
            b = self._build(v, T)
            if b is None:
                if self._hold(v, T):
                    held.add(v.tr_id)
                    continue
                v.prediction, v.features, v.risk, v.held = None, None, "grey", None
                continue
            v.held = None
            batch.append((v, *b))
        if not batch:
            self._expire_alerts(T, held)
            return
        results = await self._ml_predict([(str(v.tr_id), f) for v, _, f in batch])
        for (v, ti, f), r in zip(batch, results):
            self._apply(v, T, ti, f, r)
            if v.last_wall:
                self.fresh_lat.append((time.time() - v.last_wall) * 1000)
        self._expire_alerts(T, {v.tr_id for v, _, _ in batch} | held)
        self.e2e_lat.append((time.time() - self._trigger_wall) * 1000)
        self.predictions_total += len(batch)
        log.debug("predicted %d vehicles at %s in %.1f ms", len(batch), from_unix(T), (time.time() - t0) * 1000)

    async def _ml_predict(self, items: list[tuple[str, dict]]) -> list[dict]:
        payload = {"items": [{"id": i, "features": {k: _clean(v) for k, v in f.items()}} for i, f in items],
                   "explain": True}
        if time.time() >= self._ml_fail_until and self._http is not None:
            try:
                t0 = time.perf_counter()
                r = await self._http.post("/v1/predict", json=payload)
                r.raise_for_status()
                j = r.json()
                self.ml_lat.append((time.perf_counter() - t0) * 1000)
                self.ml_state, self.ml_version = "ok", j.get("model_version")
                return [dict(x, model="ensemble") for x in j["items"]]
            except Exception as e:
                log.warning("ML unavailable (%s) — fallback mode", e)
                self.ml_state = "fallback"
                self._ml_fail_until = time.time() + 10
        self.ml_state = "fallback"
        out = []
        for _, f in items:
            d = self.ref.fallback_delay(f.get("tgt_stop"), _clean(f.get("est_now")))
            out.append({"delay_s": d, "q10_s": d - 120, "q90_s": d + 120,
                        "p_late": float(d > 120), "p_early": float(d < -60), "contributions": [], "model": "fallback"})
        return out

    async def _probe_ml(self) -> None:
        while True:
            try:
                if self._http is not None:
                    r = await self._http.get("/health")
                    if r.status_code == 200 and self.ml_state != "ok" and time.time() >= self._ml_fail_until:
                        self.ml_state = "ok"
                        self.ml_version = r.json().get("model_version")
                    if r.status_code == 200 and (not self.model_metrics or time.time() - self._model_checked > 60):
                        info = (await self._http.get("/v1/model")).json()
                        self.model_metrics = info.get("metrics", {})
                        self._model_checked = time.time()
            except Exception:
                if self.ml_state != "fallback":
                    self.ml_state = "fallback"
            await asyncio.sleep(5)

    def _apply(self, v: Vehicle, T: float, ti: int, f: dict, r: dict) -> None:
        s = v.tracker.s
        delay = float(r["delay_s"])
        risk = risk_level(delay, r["p_late"], r["p_early"])
        idx, arr = v.tracker.arrivals_upto(T)
        li = int(idx[idx < ti][-1]) if len(idx[idx < ti]) else None
        seg = None
        if li is not None:
            seg = {"from_stop": s.name[li], "to_stop": s.name[ti], "from_lat": float(s.lat[li]),
                   "from_lon": float(s.lon[li]), "to_lat": float(s.lat[ti]), "to_lon": float(s.lon[ti]),
                   "from_idx": li, "to_idx": ti}
        causes = detect_causes(f, delay, r.get("contributions", []))
        gps_age = _clean(f.get("gps_age"))
        degraded = r.get("model") == "fallback" or gps_age is None or gps_age > self.cfg.stale_gps_s
        pred = {
            "issued_at": from_unix(T), "target_stop_id": str(s.item_id[ti]), "target_stop_name": s.name[ti],
            "target_planned": from_unix(s.plan[ti]), "lead_min": round((s.plan[ti] - T) / 60, 1),
            "target_lat": float(s.lat[ti]), "target_lon": float(s.lon[ti]),
            "delay_s": round(delay, 1), "delay_q10_s": round(float(r["q10_s"]), 1),
            "delay_q90_s": round(float(r["q90_s"]), 1), "p_late": round(float(r["p_late"]), 3),
            "p_early": round(float(r["p_early"]), 3), "risk": risk, "causes": causes,
            "segment": seg, "recommendations": recommendations(risk, delay, causes, s.name[ti], seg),
            "model": r.get("model", "ensemble"), "contributions": r.get("contributions", [])[:5],
        }
        v.prediction, v.features, v.risk, v.degraded, v.target_idx = pred, f, risk, degraded, ti
        est = _clean(f.get("est_now"))
        v.delay_hist.append((float(s.plan[ti]), delay, "predicted", T, est, ti))
        if est is not None:
            v.delay_hist.append((T, float(est), "current", None, None, None))
        self.leads.append(s.plan[ti] - T)
        self.pending.setdefault((v.tr_id, ti), []).append(Issued(v.tr_id, ti, T, float(s.plan[ti]), delay, est))
        if seg is not None:
            self._hotspot(v, seg, delay, risk, causes)
        v.risk = self._track_incident(v, T, pred, delay, risk, ti)

    def _track_incident(self, v: Vehicle, T: float, pred: dict, delay: float, raw: str, ti: int) -> str:
        """Обновить инцидент ТС по новому прогнозу; вернуть статус ТС для карты и сводки.

        Инцидент заводится, когда прогноз «Критично» или «Внимание» дважды подряд, и
        закрывается, только когда прогноз ``CLOSE_AFTER_S`` подряд в норме. Пока инцидент
        открыт, статус ТС не опускается ниже «Внимание».
        """
        now = from_unix(T)
        v.risk_streak = v.risk_streak + 1 if raw in ("red", "yellow") else 0
        a = self.alerts.get(v.tr_id)
        if a is None:
            if raw == "red" or (raw == "yellow" and v.risk_streak >= OPEN_AFTER):
                a = self._open_alert(v, T, pred)
            else:
                return "green"
        # Уровень открытого инцидента меняется, только если новый подтверждён LEVEL_CONFIRM прогнозами подряд.
        level = a["risk"]
        a["_red"] = a["_red"] + 1 if raw == "red" else 0
        a["_nonred"] = 0 if raw == "red" else a["_nonred"] + 1
        if level == "yellow" and a["_red"] >= LEVEL_CONFIRM:
            level = "red"
        elif level == "red" and a["_nonred"] >= LEVEL_CONFIRM:
            level = "yellow"
        calm = raw == "green" and abs(delay) <= CALM_BAND_S
        a["_calm_since"] = (a["_calm_since"] if a["_calm_since"] is not None else T) if calm else None
        peak = a.get("peak")
        if peak is None or abs(delay) > abs(peak["delay_s"]):
            a["peak"] = {"at": now, "delay_s": pred["delay_s"], "target_stop_name": pred["target_stop_name"]}
        a["history"].append([now, pred["delay_s"], raw])
        del a["history"][:-HISTORY_MAX]
        a.update({"updated_at": now, "risk": level, "kind": "late" if delay > 0 else "early", "prediction": pred,
                  "reason": risk_reason(delay, pred["p_late"], pred["p_early"]),
                  "closing": {"calm_since": from_unix(a["_calm_since"]) if a["_calm_since"] is not None else None,
                              "calm_s": 0.0 if a["_calm_since"] is None else round(T - a["_calm_since"], 1),
                              "need_s": CLOSE_AFTER_S, "band_s": CALM_BAND_S}})
        a["_targets"][ti] = delay
        if a["_calm_since"] is not None and T - a["_calm_since"] >= CLOSE_AFTER_S:
            self._resolve(v.tr_id, T, "calm")
            return "green"
        return level

    def _open_alert(self, v: Vehicle, T: float, pred: dict) -> dict:
        now = from_unix(T)
        reason = risk_reason(pred["delay_s"], pred["p_late"], pred["p_early"])
        prev = next((x for x in self.resolved if x["tr_id"] == v.tr_id), None)
        if prev is not None and prev.get("resolved_reason") == "calm" and T - prev["_closed_t"] <= REOPEN_S:
            self.resolved.remove(prev)
            a = prev
            a.update({"status": "active", "reopened_at": now, "reopen_count": a.get("reopen_count", 0) + 1,
                      "resolved_at": None, "resolved_reason": None})
        else:
            self._seq += 1
            a = {"id": f"{v.tr_id}-{self._seq}", "tr_id": v.tr_id, "route_name": v.route_name, "created_at": now,
                 "status": "active", "actual_delay_s": None, "error_s": None, "ack_at": None, "_targets": {},
                 "opened": {"at": now, "delay_s": pred["delay_s"], "target_stop_name": pred["target_stop_name"],
                            "target_planned": pred["target_planned"], "reason": reason},
                 "peak": None, "history": [], "reopen_count": 0, "reopened_at": None,
                 "resolved_at": None, "resolved_reason": None}
        a.update({"risk": reason["risk"] if reason else "yellow", "_red": 0, "_nonred": 0, "_calm_since": None})
        self.alerts[v.tr_id] = a
        return a

    def _hotspot(self, v: Vehicle, seg: dict, delay: float, risk: str, causes: list[dict]) -> None:
        key = (v.tr_id, seg["from_stop"], seg["to_stop"])
        h = self.hot.setdefault(key, {"tr_id": v.tr_id, "route_name": v.route_name, "from_stop": seg["from_stop"],
                                      "to_stop": seg["to_stop"], "from_lat": seg["from_lat"], "from_lon": seg["from_lon"],
                                      "to_lat": seg["to_lat"], "to_lon": seg["to_lon"], "n": 0, "risky": 0,
                                      "sum_delay": 0.0, "max_delay": -1e9, "causes": {}})
        h["n"] += 1
        h["sum_delay"] += delay
        h["max_delay"] = max(h["max_delay"], delay)
        if risk in ("red", "yellow"):
            h["risky"] += 1
            for c in causes:
                h["causes"][c["code"]] = h["causes"].get(c["code"], 0) + 1

    def hotspots(self, top: int = 15) -> list[dict]:
        """Проблемные участки: где прогноз чаще всего даёт риск, средняя задержка и типичные причины."""
        texts = {"dwell_off_stop": "простои вне остановок", "long_boarding": "долгая посадка",
                 "slow_segment": "низкая скорость", "accumulated": "накопленное опоздание", "ahead": "опережение",
                 "prev_loop": "повторяется от рейса к рейсу", "schedule_profile": "ошибка времени хода в расписании",
                 "gps_lost": "потери GPS", "model": "прочие факторы"}
        rows = []
        for h in self.hot.values():
            if h["n"] < 3 or h["risky"] == 0:
                continue
            causes = sorted(h["causes"].items(), key=lambda x: -x[1])[:3]
            rows.append({**{k: h[k] for k in ("tr_id", "route_name", "from_stop", "to_stop", "from_lat", "from_lon",
                                               "to_lat", "to_lon", "n")},
                         "risk_share": round(h["risky"] / h["n"], 2), "mean_delay_s": round(h["sum_delay"] / h["n"], 1),
                         "max_delay_s": round(h["max_delay"], 1),
                         "top_causes": [{"code": c, "text": texts.get(c, c), "count": k} for c, k in causes]})
        rows.sort(key=lambda r: (-r["risk_share"] * r["n"], -r["mean_delay_s"]))
        return rows[:top]

    def _resolve(self, tr_id: int, T: float, reason: str) -> None:
        """Закрыть инцидент: ``calm`` — прогноз вернулся в норму, ``trip_end`` — ТС вышло из рейса."""
        a = self.alerts.pop(tr_id, None)
        if a is None:
            return
        now = from_unix(T)
        a.update({"status": "resolved", "updated_at": now, "resolved_at": now, "resolved_reason": reason, "_closed_t": T})
        self.resolved.appendleft(a)

    def ack_alert(self, alert_id: str, on: bool) -> dict | None:
        """Отметить активный инцидент «в работе» диспетчером (``on=False`` снимает отметку).

        Отметка ставится по времени потока и сохраняется, когда инцидент закрывается.
        Возвращает инцидент или ``None``, если активного инцидента с таким id нет.
        """
        a = next((x for x in self.alerts.values() if x["id"] == alert_id), None)
        if a is None:
            return None
        a["ack_at"] = (from_unix(self.clock) if self.clock is not None else a["updated_at"]) if on else None
        return self._public(a)

    def _expire_alerts(self, T: float, active: set[int]) -> None:
        for tr in [tr for tr in self.alerts if tr not in active]:
            v = self.vehicles.get(tr)
            if v is None or v.last_t is None or T - v.last_t > self.cfg.offline_s:
                reason = "no_data"
            elif v.tracker is not None and self._trip_ends_before(v, T + 600):
                reason = "trip_end"
            else:
                reason = "no_forecast"
            self._resolve(tr, T, reason)
        for k in [k for k, lst in self.pending.items() if lst and T - lst[0].planned > 3600]:
            self.pending.pop(k, None)

    # ---------- views ----------
    LAYOVER_MAX_S = 2 * 3600

    def vehicle_status(self, v: Vehicle) -> str:
        """Статус ТС: в рейсе (moving/stopped), на отстое между рейсами, вне смены, нет данных."""
        if v.tracker is None:
            return "no_schedule"
        T = self.clock
        if T is None:
            return "offline"
        s = v.tracker.s
        k = int(np.searchsorted(s.plan, T))
        if not (k < len(s) and s.plan[k] - T <= 900):
            return "layover" if k < len(s) and s.plan[k] - T <= self.LAYOVER_MAX_S else "out_of_service"
        if v.last_t is None or T - v.last_t > self.cfg.offline_s:
            return "offline"
        if self.stream_state() != "live" or T - v.last_t > self.cfg.stale_gps_s:
            return "stale"
        return "stopped" if (v.speed is not None and v.speed < 3) else "moving"

    def _next_trip(self, v: Vehicle) -> dict | None:
        s, T = v.tracker.s, self.clock
        k = int(np.searchsorted(s.plan, T))
        if k >= len(s):
            return None
        return {"planned": from_unix(s.plan[k]), "stop_name": s.name[k], "in_min": round((s.plan[k] - T) / 60, 1)}

    def _route_position(self, v: Vehicle) -> float | None:
        geo = self.ref.geometry.get(v.tr_id)
        if geo is None or v.lon is None or v.lat is None or self.clock is None:
            return None
        if v.route_m_t == v.last_t:
            return v.route_m
        s = v.tracker.s
        idx, _ = v.tracker.arrivals_upto(self.clock)
        span = None
        if len(idx):
            li = int(idx[-1])
            nx = li + 1
            while nx < len(s) and s.stop_key[nx] == s.stop_key[li]:
                nx += 1
            if nx < len(s) and self.clock - s.plan[nx] < 1800:
                span = geo.span(s.stop_key[li], s.stop_key[nx])
        moving = v.speed is not None and v.speed >= 5 and v.heading is not None and math.isfinite(v.heading)
        v.route_m = geo.locate(v.lon, v.lat, span, v.route_m, float(v.heading) if moving else None)
        v.route_m_t = v.last_t
        return v.route_m

    def vehicle_view(self, v: Vehicle) -> dict:
        T = self.clock
        status = self.vehicle_status(v)
        live = status in ("moving", "stopped", "stale")
        out = {"tr_id": v.tr_id, "unit_id": v.unit_id, "route_name": v.route_name, "lat": v.lat, "lon": v.lon,
               "heading": _clean(v.heading), "speed_kmh": _clean(v.speed), "last_seen": from_unix(v.last_t),
               "pos_time": from_unix(v.pos_t) if v.pos_t is not None else None,
               "gps_age_s": None if (T is None or v.last_t is None or v.tracker is None) else round(T - v.last_t, 1),
               "status": status, "scheduled": v.tracker is not None, "route_m": None, "next_trip": None,
               "current_delay_s": None, "last_stop": None, "next_stop": None,
               "prediction": v.prediction if live else None,
               "risk": v.risk if live else "grey",
               # Прогноз уже в зоне риска, но инцидент ещё не заведён: сколько раз подряд из OPEN_AFTER.
               "prediction_held": v.held if live else None,
               "risk_pending": ({"count": v.risk_streak, "need": OPEN_AFTER}
                                if live and v.risk_streak and v.tr_id not in self.alerts else None),
               "degraded": v.degraded or status == "stale"}
        if v.tracker is None or T is None:
            return out
        out["route_m"] = self._route_position(v)
        if status in ("layover", "out_of_service"):
            out["next_trip"] = self._next_trip(v)
        s = v.tracker.s
        idx, arr = v.tracker.arrivals_upto(T)
        if len(idx):
            li = int(idx[-1])
            if T - arr[li] < 3600:
                out["last_stop"] = {"stop_id": str(s.item_id[li]), "name": s.name[li], "planned": from_unix(s.plan[li]),
                                    "actual": from_unix(arr[li]), "delay_s": round(float(arr[li] - s.plan[li]), 1)}
        if v.features is not None:
            out["current_delay_s"] = reliable_delay(v.features)
        nx = (int(idx[-1]) + 1) if len(idx) else v.tracker.next_idx
        if nx is not None and nx < len(s):
            eta = s.plan[nx] + (out["current_delay_s"] or 0.0)
            out["next_stop"] = {"stop_id": str(s.item_id[nx]), "name": s.name[nx], "planned": from_unix(s.plan[nx]),
                                "eta": from_unix(max(eta, T))}
        return out

    def vehicle_detail(self, tr_id: int) -> dict | None:
        v = self.vehicles.get(tr_id) or self.unknown.get(tr_id) or next(
            (u for u in self.unknown.values() if u.tr_id == tr_id), None)
        if v is None:
            return None
        out = self.vehicle_view(v)
        T = self.clock
        if v.tracker is not None:
            out["track"] = [[p.lon, p.lat, from_unix(p.t)] for p in v.tracker.gps if p.valid]
            s = v.tracker.s
            idx, _ = v.tracker.arrivals_upto(T) if T is not None else (np.array([], int), None)
            nx = (int(idx[-1]) + 1) if len(idx) else (v.tracker.next_idx or 0)
            cur = out["current_delay_s"] or 0.0
            end = nx + 10
            if v.target_idx is not None and nx <= v.target_idx < nx + 40:
                end = max(end, v.target_idx + 1)  # схеме рейса нужны остановки вплоть до цели прогноза
            out["upcoming"] = [{"stop_id": str(s.item_id[k]), "name": s.name[k], "planned": from_unix(s.plan[k]),
                                "eta": from_unix(s.plan[k] + cur), "lat": float(s.lat[k]), "lon": float(s.lon[k])}
                               for k in range(nx, min(len(s), end))]
            horizon = (T or 0) - self.cfg.history_h * 3600
            out["delay_history"] = [self._history_row(v, h) for h in v.delay_hist if h[0] >= horizon]
        else:
            out["track"], out["upcoming"], out["delay_history"] = [], [], []
        return out

    HISTORY_KIND = {"actual": "actual", "current": "estimate", "predicted": "predicted"}

    def _history_row(self, v: Vehicle, h: tuple) -> dict:
        """Точка истории: факт на остановке, оценка между остановками или прогноз.

        У прогноза ``t`` — плановое время цели; ``issued_at`` — когда выдан, отклонение в
        тот момент и фактическое отклонение на цели, если ТС её уже прошло.
        """
        t, d, k, issued, est, idx = h
        row = {"t": from_unix(t), "delay_s": round(d, 1), "kind": self.HISTORY_KIND[k]}
        if k == "predicted":
            fact = v.outcomes.get(idx)
            row.update({"issued_at": from_unix(issued), "lead_min": round((t - issued) / 60, 1),
                        "current_at_issue_s": None if est is None else round(est, 1),
                        "actual_s": None if fact is None else round(fact, 1)})
        return row

    def routes_view(self, geometry: bool = True) -> list[dict]:
        """Маршрутная сеть: геометрия оборота по дорогам, остановки с положением вдоль неё, риск участков.

        Args:
            geometry: включать ли ломаную и остановки (без них — только риск участков, для частого опроса).
        """
        out = []
        for tr, r in self.ref.routes.items():
            s = self.ref.schedules[tr]
            geo = self.ref.geometry.get(tr)
            stops = [{"stop_id": str(s.item_id[i]), "name": s.name[i], "lat": float(s.lat[i]), "lon": float(s.lon[i])}
                     for i in r.loop_idx]
            if geo is not None:
                for st, m in zip(stops, geo.stop_m):
                    st["m"] = m
            n = len(stops)
            nseg = n if geo is not None and geo.closed else max(0, n - 1)
            seg_risk = ["green"] * nseg
            v = self.vehicles[tr]
            p = v.prediction
            if p and p.get("segment") and v.risk in ("red", "yellow") and self.vehicle_status(v) in ("moving", "stopped", "stale"):
                kf, kt = s.stop_key[p["segment"]["from_idx"]], s.stop_key[p["segment"]["to_idx"]]
                keys = [s.stop_key[i] for i in r.loop_idx]
                if geo is not None:
                    sp = geo.span(kf, kt)
                    if sp is None:
                        a = next((k for k, x in enumerate(keys) if x == kf), None)
                        b = next((k for k, x in enumerate(keys) if x == kt), None)
                        sp = (a, b) if a is not None and b is not None else None
                else:
                    sp = (keys.index(kf), keys.index(kt)) if kf in keys and kt in keys else None
                if sp is not None:
                    a, b = sp
                    k = a
                    for _ in range(nseg):
                        if k == b:
                            break
                        seg_risk[k % nseg] = v.risk
                        k = (k + 1) % n
            segs = [{"from_idx": k, "to_idx": (k + 1) % n, "risk": seg_risk[k]} for k in range(nseg)]
            if not geometry:
                out.append({"tr_id": tr, "segments": [x for x in segs if x["risk"] != "green"], "n_segments": nseg})
                continue
            path = geo.path if geo is not None else (self.ref.paths.get(tr) or [[x["lon"], x["lat"]] for x in stops])
            out.append({"tr_id": tr, "route_name": r.name, "stops": stops, "path": path,
                        "length_m": geo.length if geo is not None else None,
                        "closed": geo.closed if geo is not None else False, "segments": segs,
                        "approx_m": geo.approx if geo is not None else []})
        return out

    @staticmethod
    def _public(a: dict) -> dict:
        return {k: v for k, v in a.items() if not k.startswith("_")}

    RECENT_RESOLVED_S = 300.0

    def recent_resolved(self) -> list[dict]:
        """Инциденты, закрытые за последние 5 мин потока: дашборд показывает, что и почему закрылось."""
        if self.clock is None:
            return []
        return [self._public(a) for a in self.resolved
                if a.get("_closed_t") is not None and self.clock - a["_closed_t"] <= self.RECENT_RESOLVED_S
                and a["tr_id"] not in self.alerts]

    def alerts_view(self, status: str = "active") -> list[dict]:
        order = {"red": 0, "yellow": 1}
        act = sorted(self.alerts.values(), key=lambda a: (order.get(a["risk"], 2), -abs(a["prediction"]["delay_s"])))
        act = [self._public(a) for a in act]
        res = [self._public(a) for a in self.resolved]
        if status == "active":
            return act
        if status == "resolved":
            return res
        return act + res

    def stream_state(self) -> str:
        if self.last_packet_wall is None:
            return "no_data"
        age = time.time() - self.last_packet_wall
        return "live" if age <= self.cfg.live_wall_s else "stale" if age <= self.cfg.stale_wall_s else "no_data"

    def health(self) -> dict:
        now = time.time()
        recent = [t for t in self.pkt_times if now - t <= 10]
        st = self.stream_state()
        connected = sum(1 for v in list(self.vehicles.values()) + list(self.unknown.values()) if v.connected)
        return {
            "status": "ok" if st == "live" and self.ml_state == "ok" else "degraded",
            "stream": {"state": st, "connected_units": connected,
                       "known_units": len(self.vehicles) + len(self.unknown),
                       "last_packet_at": from_unix(max((v.last_t or 0) for v in self.vehicles.values()) or None)
                       if self.packets else None,
                       "stream_time": from_unix(self.clock), "packets_total": self.packets,
                       "packets_per_s": round(len(recent) / 10, 1), "crc_errors": self.ndtp_stats.get("crc_errors", 0),
                       "queue_len": self.queue.qsize()},
            "ml": {"state": self.ml_state if self.ml_state != "unknown" else "ok", "model_version": self.ml_version,
                   "last_latency_ms": round(self.ml_lat[-1], 1) if self.ml_lat else None},
            "uptime_s": round(now - self.started, 1),
        }

    def metrics(self) -> dict:
        errs = list(self.errors)
        mae = float(np.mean([e[0] for e in errs])) if errs else None
        naive = [e[1] for e in errs if e[1] is not None]
        return {
            "online": {"mae_s": None if mae is None else round(mae, 1), "n": len(errs),
                       "naive_mae_s": round(float(np.mean(naive)), 1) if naive else None,
                       "horizon_ok_share": round(float(np.mean([e[2] for e in errs])), 3) if errs else None,
                       "mean_lead_min": round(float(np.mean(self.leads)) / 60, 2) if self.leads else None,
                       "retro_predictions": self.retro},
            "latency": {"ingest_to_prediction_ms_p50": _pct(self.e2e_lat, 50),
                        "ingest_to_prediction_ms_p95": _pct(self.e2e_lat, 95),
                        "data_age_at_prediction_ms_p50": _pct(self.fresh_lat, 50),
                        "ml_predict_ms_p50": _pct(self.ml_lat, 50), "ml_predict_ms_p95": _pct(self.ml_lat, 95)},
            "throughput": {"packets_per_s": self.health()["stream"]["packets_per_s"], "queue_len": self.queue.qsize(),
                           "predictions_total": self.predictions_total, "packets_total": self.packets},
            "offline": self._offline(),
        }

    def _offline(self) -> dict:
        m = self.model_metrics
        pick = {"test_mae_s": "offline_mae_s", "baseline_mae_s": "baseline_mae_s", "zero_mae_s": "zero_mae_s",
                "online_test_mae_s": "online_ensemble_mae_s", "interval_coverage": "online_q10_q90_coverage"}
        return {k: round(float(m[v]), 3 if k == "interval_coverage" else 1) for k, v in pick.items() if m.get(v) is not None}

    def state(self) -> dict:
        views = [self.vehicle_view(v) for v in self.vehicles.values()] + \
                [self.vehicle_view(v) for v in self.unknown.values()]
        sched = [x for x in views if x["scheduled"]]
        live = [x for x in sched if x["status"] in ("moving", "stopped", "stale")]
        cnt = {k: sum(1 for x in live if x["risk"] == k) for k in ("red", "yellow", "green")}
        m = self.metrics()
        return {
            "stream_time": from_unix(self.clock), "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "summary": {"vehicles_total": len(sched), "vehicles_active": len(live), **cnt,
                        "grey": sum(1 for x in live if x["risk"] == "grey"),
                        "layover": sum(1 for x in sched if x["status"] == "layover"),
                        "out_of_service": sum(1 for x in sched if x["status"] == "out_of_service"),
                        "offline": sum(1 for x in sched if x["status"] == "offline"),
                        "unscheduled": len(views) - len(sched),
                        "online_mae_s": m["online"]["mae_s"], "online_n": m["online"]["n"],
                        "mean_lead_min": m["online"]["mean_lead_min"]},
            "vehicles": views, "alerts": self.alerts_view("active"), "recent_resolved": self.recent_resolved(),
            "health": self.health(),
        }
