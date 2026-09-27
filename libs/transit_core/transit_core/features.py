"""Признаки прогнозной точки ``(ТС, T, целевая остановка)``.

Один и тот же код считает признаки в backend (онлайн, поток NDTP) и при
обучении (проигрывание CSV через :class:`~transit_core.tracker.VehicleTracker`).
Используются только данные, известные к моменту ``T``.
"""
from __future__ import annotations

import hashlib
import math

import numpy as np

from .schedule import VehicleSchedule
from .tracker import KX, KY, VehicleTracker

NUM_FEATURES = [
    "lead", "gap_prev_plan", "gap_next_plan", "tgt_role", "n_plan_between", "plan_since_lp",
    "est_now", "dev_last", "dev_last_interp", "since_last", "n_between", "plan_run", "lb_next",
    "lp_dev", "lp_lb", "dev_mean5", "dev_slope5", "dev_max15",
    "prev_loop_delta", "prev_loop_age",
    "gps_age", "valid_share5", "spd2", "spd5", "stop5", "path5", "dwell", "at_stop", "dist_tgt", "dist_next",
]
CAT_FEATURES = ["tgt_stop", "route"]
ONLINE_FEATURES = NUM_FEATURES + CAT_FEATURES
OFFLINE_FEATURES = ["cur_dev"] + ONLINE_FEATURES
LOST_S = 15 * 60
DEV_CLIP = 900.0


def route_key(s: VehicleSchedule) -> str:
    """Стабильный ключ маршрута по набору остановок (копии одного маршрута получают один ключ)."""
    keys = "|".join(sorted(set(s.stop_key)))
    return "R" + hashlib.md5(keys.encode()).hexdigest()[:8]


def _nan() -> float:
    return float("nan")


def snapshot(tk: VehicleTracker, T: float, ti: int, route: str | None = None) -> dict:
    """Посчитать признаки для прогноза задержки на остановке ``ti`` в момент ``T``.

    Args:
        tk: трекер ТС, в который уже поданы все точки с временем ``<= T``.
        T: момент прогноза, unix-секунды потока.
        ti: индекс целевой остановки в расписании ТС.
        route: ключ маршрута (если не задан — считается по расписанию).

    Returns:
        Словарь ``признак -> значение`` (NaN для неизвестных).
    """
    s = tk.s
    n = len(s)
    plan = s.plan
    f: dict = {k: _nan() for k in NUM_FEATURES}
    f["tgt_stop"] = s.stop_key[ti]
    f["route"] = route or route_key(s)
    f["lead"] = plan[ti] - T
    f["gap_prev_plan"] = plan[ti] - plan[ti - 1] if ti > 0 else _nan()
    f["gap_next_plan"] = plan[ti + 1] - plan[ti] if ti + 1 < n else _nan()
    f["tgt_role"] = float(tk.role[ti])
    lp0 = s.last_planned_index(T)
    if lp0 is not None:
        f["n_plan_between"] = float(ti - lp0)
        f["plan_since_lp"] = T - plan[lp0]

    idx, arr = tk.arrivals_upto(T)
    idx = idx[idx < ti]
    if len(idx) and T - arr[int(idx[-1])] > LOST_S:
        idx = idx[:0]
    if len(idx):
        li = int(idx[-1])
        dev = np.clip(arr - plan, -DEV_CLIP, DEV_CLIP)
        f["dev_last"] = dev[li]
        f["dev_last_interp"] = float(tk.arr_interp[li])
        f["since_last"] = T - arr[li]
        f["n_between"] = ti - li
        f["plan_run"] = plan[ti] - plan[li]
        nx = li + 1
        f["lb_next"] = min(T - plan[nx], DEV_CLIP) if nx < n else _nan()
        f["est_now"] = max(dev[li], f["lb_next"]) if nx < n else dev[li]
        lp = s.last_planned_index(T)
        if lp is not None:
            if np.isfinite(arr[lp]) and arr[lp] <= T:
                f["lp_dev"] = dev[lp]
            else:
                f["lp_lb"] = min(T - plan[lp], DEV_CLIP)
        direct = idx[~tk.arr_interp[idx]]
        last5 = direct[-5:]
        if len(last5):
            f["dev_mean5"] = float(dev[last5].mean())
            f["dev_slope5"] = float((dev[last5[-1]] - dev[last5[0]]) / max(1, len(last5) - 1))
        rec = idx[arr[idx] >= T - 900]
        if len(rec):
            f["dev_max15"] = float(dev[rec].max())
        pts = tk._by_key.get(s.stop_key[ti])
        if pts is not None:
            prev_t = pts[(pts < li) & np.isfinite(arr[pts]) & (arr[pts] <= T)]
            if len(prev_t):
                pt = int(prev_t[-1])
                pls = tk._by_key.get(s.stop_key[li])
                prev_l = pls[(pls < pt) & np.isfinite(arr[pls])] if pls is not None else []
                if len(prev_l) and pt - int(prev_l[-1]) < 60:
                    pl = int(prev_l[-1])
                    f["prev_loop_delta"] = dev[pt] - dev[pl]
                    f["prev_loop_age"] = T - arr[pt]
    else:
        f["n_between"] = float(ti - (tk.next_idx or 0))

    pts = [p for p in tk.gps if p.t <= T]
    if pts:
        valid = [p for p in pts if p.valid]
        w5 = [p for p in pts if p.t > T - 300]
        f["valid_share5"] = (sum(p.valid for p in w5) / len(w5)) if w5 else _nan()
        if valid:
            last = valid[-1]
            f["gps_age"] = T - last.t
            v5 = [p for p in valid if p.t > T - 300]
            v2 = [p for p in valid if p.t > T - 120]
            sp5 = [p.speed for p in v5 if math.isfinite(p.speed)]
            sp2 = [p.speed for p in v2 if math.isfinite(p.speed)]
            f["spd5"] = float(np.mean(sp5)) if sp5 else _nan()
            f["spd2"] = float(np.mean(sp2)) if sp2 else _nan()
            f["stop5"] = float(np.mean([x < 3 for x in sp5])) if sp5 else _nan()
            if len(v5) > 1:
                xs = np.array([p.lon for p in v5])
                ys = np.array([p.lat for p in v5])
                f["path5"] = float(np.hypot(np.diff(xs) * KX, np.diff(ys) * KY).sum())
            f["dist_tgt"] = math.hypot((last.lon - s.lon[ti]) * KX, (last.lat - s.lat[ti]) * KY)
            nx = int(idx[-1]) + 1 if len(idx) else (tk.next_idx or 0)
            if nx < n:
                f["dist_next"] = math.hypot((last.lon - s.lon[nx]) * KX, (last.lat - s.lat[nx]) * KY)
    f["dwell"] = tk.dwell_s(T)
    f["at_stop"] = float(tk.at_stop())
    return f


SEQ_BINS = 20
SEQ_STEP_S = 30.0
SEQ_CHANNELS = 6


def gps_sequence(tk: VehicleTracker, T: float, ti: int) -> np.ndarray:
    """Последние 10 минут телеметрии до ``T`` в виде ``(20 бинов × 6 каналов)`` для нейросети.

    Каналы: наличие данных, доля валидных точек, средняя скорость/50, доля
    стоянки, пройденный путь/300 м, расстояние до целевой остановки/5 км.
    """
    s = tk.s
    out = np.zeros((SEQ_BINS, SEQ_CHANNELS), dtype=np.float32)
    start = T - SEQ_BINS * SEQ_STEP_S
    pts = [p for p in tk.gps if start < p.t <= T]
    if not pts:
        return out
    prev = None
    for b in range(SEQ_BINS):
        lo, hi = start + b * SEQ_STEP_S, start + (b + 1) * SEQ_STEP_S
        w = [p for p in pts if lo < p.t <= hi]
        if not w:
            continue
        v = [p for p in w if p.valid]
        sp = [p.speed for p in v if math.isfinite(p.speed)]
        out[b, 0] = 1.0
        out[b, 1] = len(v) / len(w)
        out[b, 2] = (np.mean(sp) / 50.0) if sp else 0.0
        out[b, 3] = float(np.mean([x < 3 for x in sp])) if sp else 0.0
        if v:
            path = 0.0
            for p in v:
                if prev is not None:
                    path += math.hypot((p.lon - prev.lon) * KX, (p.lat - prev.lat) * KY)
                prev = p
            out[b, 4] = min(path / 300.0, 5.0)
            last = v[-1]
            out[b, 5] = min(math.hypot((last.lon - s.lon[ti]) * KX, (last.lat - s.lat[ti]) * KY) / 5000.0, 5.0)
    return out
