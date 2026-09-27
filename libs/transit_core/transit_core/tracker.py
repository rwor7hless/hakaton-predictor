"""Онлайн-сопоставление телеметрии ТС с ниткой графика (map matching по остановкам).

Трекер получает GPS-точки одного ТС в порядке времени и восстанавливает
фактические моменты прохождения остановок расписания. Используется одинаково
в backend (поток NDTP) и при обучении модели (проигрывание CSV), что
гарантирует совпадение признаков офлайн и онлайн.

Алгоритм:
  * кандидаты — ближайшие по порядку ещё не пройденные остановки, чьё плановое
    время совместимо с текущим (от −20 до +25 минут);
  * расстояние считается от остановки до отрезка между двумя соседними
    GPS-точками, момент прохождения интерполируется вдоль отрезка — это
    устраняет пропуски при редкой (8–15 c) телеметрии;
  * остановки встречного направления отсекаются по курсу движения, а
    физически невозможные скачки задержки — по сравнению с последней
    пройденной остановкой;
  * промежуточная остановка засчитывается в момент максимального сближения,
    конечная — в момент въезда в зону (прибытие), начальная остановка рейса —
    в момент выезда из зоны (отправление);
  * пропущенные остановки между двумя засчитанными интерполируются по плану.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np

from .schedule import VehicleSchedule

KX = math.cos(math.radians(55.75)) * 111320.0
KY = 111320.0

R_IN = 45.0
R_EXIT = 90.0
R_EXIT_TERMINAL = 350.0
WINDOW = 8
EARLY_S = 20 * 60
LATE_S = 25 * 60
MAX_SEG_GAP_S = 90.0
MAX_INTERP_S = 40 * 60
TERMINAL_PAIR_S = 45 * 60
TRIP_GAP_S = 10 * 60
STOP_SPEED_KMH = 3.0
MAX_HEADING_DIFF = 100.0
REANCHOR_AFTER = 3
LOCATE_RADIUS_M = 500.0
MIN_MOVE_M = 8.0
OFF_ROUTE_M = 5000.0       # дальше от всех остановок маршрута — сбой GPS, точка к плану не привязывается
RELOCATE_GAP_S = 300.0     # сбой GPS дольше 5 мин: после него ТС ищется заново, как при холодном старте


@dataclass
class GpsPoint:
    """Одна валидная или невалидная точка телеметрии."""

    t: float
    lon: float
    lat: float
    speed: float
    course: float
    valid: bool


@dataclass
class Candidate:
    """Остановка, в зоне которой ТС находится прямо сейчас."""

    idx: int
    best_d: float
    best_t: float
    first_t: float
    last_t: float


def _seg_closest(x0, y0, t0, x1, y1, t1, sx, sy):
    """Расстояние (м) от точек остановок до отрезка и интерполированное время сближения."""
    ax, ay = (x1 - x0) * KX, (y1 - y0) * KY
    px, py = (sx - x0) * KX, (sy - y0) * KY
    L2 = ax * ax + ay * ay
    if L2 < 1e-6:
        u = np.zeros_like(px)
    else:
        u = np.clip((px * ax + py * ay) / L2, 0.0, 1.0)
    dx, dy = px - u * ax, py - u * ay
    return np.hypot(dx, dy), t0 + u * (t1 - t0)


def _bearing(dx_m, dy_m):
    return (np.degrees(np.arctan2(dx_m, dy_m)) + 360.0) % 360.0


def _angle_diff(a, b):
    return np.abs((a - b + 180.0) % 360.0 - 180.0)


def stop_bearings(s: VehicleSchedule) -> np.ndarray:
    """Направление движения через каждую остановку (по соседним в плане), NaN если не определено."""
    n = len(s)
    out = np.full(n, np.nan)
    for i in range(n):
        a = i - 1
        while a >= 0 and s.stop_key[a] == s.stop_key[i]:
            a -= 1
        b = i + 1
        while b < n and s.stop_key[b] == s.stop_key[i]:
            b += 1
        if a >= 0 and b < n and s.plan[i] - s.plan[a] < TRIP_GAP_S and s.plan[b] - s.plan[i] < TRIP_GAP_S:
            dx, dy = (s.lon[b] - s.lon[a]) * KX, (s.lat[b] - s.lat[a]) * KY
            if math.hypot(dx, dy) > 30:
                out[i] = _bearing(dx, dy)
    return out


def stop_roles(s: VehicleSchedule) -> np.ndarray:
    """Роль остановки в рейсе: 0 — промежуточная, 1 — начало рейса (отправление), 2 — конец рейса (прибытие)."""
    n = len(s)
    role = np.zeros(n, dtype=np.int8)
    for i in range(n):
        start = i == 0 or s.stop_key[i - 1] == s.stop_key[i] or s.plan[i] - s.plan[i - 1] > TRIP_GAP_S
        end = i == n - 1 or s.stop_key[i + 1] == s.stop_key[i] or s.plan[i + 1] - s.plan[i] > TRIP_GAP_S
        role[i] = 1 if start and not end else 2 if end and not start else 0
    return role


class VehicleTracker:
    """Состояние одного ТС: история GPS, пройденные остановки, простой.

    Args:
        sched: расписание ТС на сутки.
        history_s: сколько секунд GPS-истории держать в памяти.
        relocate: отбрасывать точки дальше ``OFF_ROUTE_M`` от маршрута (сбой GPS) и после сбоя
            дольше ``RELOCATE_GAP_S`` искать ТС заново. Включается в онлайн-сервисе; офлайн-конвейер
            (обучение, сабмит) работает без этого, как при обучении текущих моделей.
    """

    def __init__(self, sched: VehicleSchedule, history_s: float = 1800.0, relocate: bool = False):
        self.s = sched
        self.relocate = relocate
        n = len(sched)
        self.arr = np.full(n, np.nan)
        self.arr_interp = np.zeros(n, dtype=bool)
        self.next_idx: int | None = None
        self.cand: Candidate | None = None
        self.gps: deque[GpsPoint] = deque()
        self.history_s = history_s
        self.last_t = -math.inf
        self.last_valid: GpsPoint | None = None
        self.stopped_since: float | None = None
        self.events: list[tuple[int, float, bool]] = []
        self._rejected: set[int] = set()
        self._first: GpsPoint | None = None
        self._lost_since: float | None = None
        xy = np.unique(np.column_stack([sched.lon, sched.lat]), axis=0)
        self._stop_lon, self._stop_lat = xy[:, 0], xy[:, 1]
        self.bearing = stop_bearings(sched)
        self.role = stop_roles(sched)
        self._by_key: dict[str, np.ndarray] = {}
        keys = sched.stop_key
        for k in set(keys):
            self._by_key[k] = np.where(keys == k)[0]

    def reset(self) -> None:
        """Сбросить состояние (например, при перемотке потока назад)."""
        self.__init__(self.s, self.history_s, self.relocate)

    def update(self, t: float, lon: float | None, lat: float | None, speed: float | None,
               course: float | None = None, valid: bool = True) -> None:
        """Принять очередную точку телеметрии (время — unix-секунды потока)."""
        if t < self.last_t - 1.0:
            return
        ok = bool(valid) and lon is not None and lat is not None and np.isfinite(lon) and np.isfinite(lat) \
            and lon != 0.0 and lat != 0.0
        # Сбой GPS: точка в километрах от маршрута (бывает — блок шлёт координаты за 18 км на
        # 100 км/ч). Она не участвует ни в привязке, ни в признаках, как невалидная.
        off = ok and self.relocate and bool(
            np.min(np.hypot((self._stop_lon - lon) * KX, (self._stop_lat - lat) * KY)) > OFF_ROUTE_M)
        ok = ok and not off
        spd = float(speed) if speed is not None and np.isfinite(speed) and 0 <= speed < 150 else np.nan
        p = GpsPoint(t, float(lon) if ok else np.nan, float(lat) if ok else np.nan, spd,
                     float(course) if course is not None and np.isfinite(course) else np.nan, ok)
        self.last_t = t
        self.gps.append(p)
        while self.gps and self.gps[0].t < t - self.history_s:
            self.gps.popleft()
        if not ok:
            if off and self._lost_since is None:
                self._lost_since = t
            return
        # После долгого сбоя ищем ТС заново: иначе трекер ждёт давно пройденные остановки
        # и теряет факты на полчаса и больше.
        if self._lost_since is not None:
            if t - self._lost_since >= RELOCATE_GAP_S and self.next_idx is not None:
                self._relocate()
            self._lost_since = None
        if np.isfinite(spd):
            if spd < STOP_SPEED_KMH:
                self.stopped_since = self.stopped_since if self.stopped_since is not None else t
            else:
                self.stopped_since = None
        prev = self.last_valid
        self.last_valid = p
        self._match(prev, p)

    @property
    def off_route(self) -> bool:
        """Идёт сбой GPS: последние точки далеко от маршрута (только при ``relocate``)."""
        return self._lost_since is not None

    def _relocate(self) -> None:
        """Забыть текущую привязку: следующая точка найдёт ТС в плане через ``_localize``."""
        self.next_idx, self.cand, self._first, self.last_valid = None, None, None, None
        self._rejected.clear()

    def _localize(self, prev: GpsPoint | None, p: GpsPoint) -> int | None:
        """Найти место ТС в плане при холодном старте: ближайшая остановка с совместимым временем и направлением.

        Ждём движения (чтобы знать курс) не дольше 3 минут; если ТС стоит — локализуемся без курса.
        """
        s = self.s
        if self._first is None:
            self._first = p
        heading = None
        if prev is not None:
            dx, dy = (p.lon - prev.lon) * KX, (p.lat - prev.lat) * KY
            if math.hypot(dx, dy) >= 15:
                heading = _bearing(dx, dy)
        if heading is None and p.t - self._first.t < 180:
            return None
        lo = int(np.searchsorted(s.plan, p.t - 30 * 60))
        hi = int(np.searchsorted(s.plan, p.t + 20 * 60))
        if hi <= lo:
            return int(np.searchsorted(s.plan, p.t - LATE_S + 600))
        js = np.arange(lo, hi)
        d = np.hypot((p.lon - s.lon[js]) * KX, (p.lat - s.lat[js]) * KY)
        if heading is not None:
            sb = self.bearing[js]
            d = np.where(np.isfinite(sb) & (_angle_diff(sb, heading) > MAX_HEADING_DIFF), np.inf, d)
        score = d + 4.0 * np.abs(p.t - s.plan[js]) / 60.0
        k = int(np.argmin(score))
        if d[k] <= LOCATE_RADIUS_M:
            return int(js[k])
        return int(np.searchsorted(s.plan, p.t - LATE_S + 600))

    def _match(self, prev: GpsPoint | None, p: GpsPoint) -> None:
        s, n = self.s, len(self.s)
        if self.next_idx is None:
            self.next_idx = self._localize(prev, p)
            if self.next_idx is None:
                return
        while (self.next_idx < n and p.t > s.plan[self.next_idx] + LATE_S
               and (self.cand is None or self.cand.idx != self.next_idx)):
            self.next_idx += 1
        if self.next_idx >= n:
            return
        lo = self.next_idx
        hi = min(n, lo + WINDOW)
        js = np.arange(lo, hi)
        ok = (p.t >= s.plan[js] - EARLY_S) & (p.t <= s.plan[js] + LATE_S)
        js = js[ok]
        if prev is not None and p.t - prev.t <= MAX_SEG_GAP_S:
            d, tt = _seg_closest(prev.lon, prev.lat, prev.t, p.lon, p.lat, p.t, s.lon[js], s.lat[js])
        else:
            d = np.hypot((p.lon - s.lon[js]) * KX, (p.lat - s.lat[js]) * KY)
            tt = np.full(len(js), p.t)
        if prev is not None:
            dx, dy = (p.lon - prev.lon) * KX, (p.lat - prev.lat) * KY
            if math.hypot(dx, dy) >= MIN_MOVE_M:
                hd = _bearing(dx, dy)
                sb = self.bearing[js]
                wrong = np.isfinite(sb) & (_angle_diff(sb, hd) > MAX_HEADING_DIFF)
                d = np.where(wrong, np.inf, d)
        last = self._last_arrival()
        if last is not None:
            li, lt = last
            dev_last = lt - s.plan[li]
            dev_j = tt - s.plan[js]
            slack = np.maximum(0.0, s.plan[js] - s.plan[li])
            lo_dev = dev_last - 150.0 - 0.6 * slack
            hi_dev = dev_last + 150.0 + np.maximum(0.0, p.t - lt)
            implausible = (dev_j < lo_dev) | (dev_j > hi_dev)
            if p.t - lt < 30 * 60:
                blocked = implausible & (d <= R_IN)
                if blocked.any():
                    self._rejected.update(int(j) for j in js[blocked])
                if len(self._rejected) < REANCHOR_AFTER:
                    d = np.where(implausible, np.inf, d)
                else:
                    self._rejected.clear()
        c = self.cand
        if c is not None:
            dc_now = math.hypot((p.lon - s.lon[c.idx]) * KX, (p.lat - s.lat[c.idx]) * KY)
            m = js == c.idx
            if m.any() and d[m][0] < c.best_d:
                c.best_d, c.best_t = float(d[m][0]), float(tt[m][0])
            r_exit = R_EXIT_TERMINAL if self._is_terminal(c.idx) else R_EXIT
            if dc_now <= r_exit:
                c.last_t = p.t
            later = (js > c.idx) & (d <= R_IN) & (s.stop_key[js] != s.stop_key[c.idx])
            if self._is_terminal(c.idx):
                later &= s.plan[js] - s.plan[c.idx] < 3 * 60 + max(0.0, p.t - s.plan[c.idx])
            if dc_now > r_exit or later.any():
                self._finalize()
                c = None
        if self.cand is None:
            inside = np.where((d <= R_IN) & (js >= self.next_idx))[0]
            if len(inside):
                k = inside[0]
                self.cand = Candidate(int(js[k]), float(d[k]), float(tt[k]), float(tt[k]), p.t)

    def _finalize(self) -> None:
        c = self.cand
        self.cand = None
        s, n = self.s, len(self.s)
        i = c.idx
        nxt = i + 1
        if (nxt < n and s.stop_key[nxt] == s.stop_key[i] and s.plan[nxt] - s.plan[i] <= TERMINAL_PAIR_S
                and c.last_t - c.first_t > 60):
            self._set_arrival(i, c.first_t)
            self._set_arrival(nxt, max(c.last_t, c.first_t))
            self.next_idx = nxt + 1
        else:
            role = self.role[i]
            t = c.last_t if role == 1 else c.first_t if role == 2 else c.best_t
            self._set_arrival(i, t)
            self.next_idx = i + 1

    def _is_terminal(self, i: int) -> bool:
        n = len(self.s)
        return bool(self.role[i] == 1 or (i + 1 < n and self.s.stop_key[i + 1] == self.s.stop_key[i]))

    def _last_arrival(self) -> tuple[int, float] | None:
        k = self.next_idx - 1 if self.next_idx is not None else -1
        while k >= 0 and not np.isfinite(self.arr[k]):
            k -= 1
        if k < 0 or self.arr_interp[k]:
            return None if k < 0 else (k, float(self.arr[k]))
        return k, float(self.arr[k])

    def _set_arrival(self, i: int, t: float) -> None:
        prev = np.where(np.isfinite(self.arr[:i]))[0]
        if len(prev):
            pa = prev[-1]
            gap = t - self.arr[pa]
            if 0 < gap <= MAX_INTERP_S and i - pa > 1:
                span = self.s.plan[i] - self.s.plan[pa]
                for k in range(pa + 1, i):
                    frac = (self.s.plan[k] - self.s.plan[pa]) / span if span > 0 else (k - pa) / (i - pa)
                    self.arr[k] = self.arr[pa] + frac * gap
                    self.arr_interp[k] = True
                    self.events.append((k, float(self.arr[k]), True))
        self.arr[i] = t
        self.arr_interp[i] = False
        self._rejected.clear()
        self.events.append((i, float(t), False))

    def pop_events(self) -> list[tuple[int, float, bool]]:
        """Забрать новые зафиксированные прохождения остановок ``(idx, t, interpolated)``."""
        ev, self.events = self.events, []
        return ev

    def arrivals_upto(self, T: float) -> tuple[np.ndarray, np.ndarray]:
        """Индексы и времена прохождения остановок, известные к моменту ``T``.

        Текущая «открытая» остановка (ТС в зоне) учитывается как пройденная.
        """
        arr = self.arr.copy()
        c = self.cand
        if c is not None and c.first_t <= T and not np.isfinite(arr[c.idx]) and self.role[c.idx] != 1:
            arr[c.idx] = c.best_t if c.best_t <= T else c.first_t
        idx = np.where(np.isfinite(arr) & (arr <= T))[0]
        return idx, arr

    def dwell_s(self, T: float) -> float:
        """Длительность текущей стоянки (скорость < 3 км/ч), с."""
        return T - self.stopped_since if self.stopped_since is not None else 0.0

    def at_stop(self) -> bool:
        """Находится ли ТС сейчас в зоне остановки расписания."""
        return self.cand is not None
