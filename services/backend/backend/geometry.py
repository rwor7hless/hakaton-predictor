"""Геометрия маршрута по дорогам и положение ТС вдоль неё (метры от начала оборота).

Положение считается проекцией GPS-точки на участок ломаной между последней пройденной
и следующей остановкой (по данным трекера), поэтому ТС не «перескакивает» на
встречное направление, идущее по той же улице.
"""
from __future__ import annotations

import math

import numpy as np

from transit_core.tracker import KX, KY

MAX_OFF_ROUTE_M = 90.0


class RouteGeometry:
    """Ломаная оборота маршрута с накопленной длиной и положениями остановок.

    Args:
        g: запись из ``route_geometry.json`` (path, cum_m, stop_idx, stop_m, closed).
        stop_keys: ключи остановок оборота (в порядке ``stop_idx``).
    """

    def __init__(self, g: dict, stop_keys: list[str]):
        p = np.asarray(g["path"], dtype=float)
        self.lon, self.lat = p[:, 0], p[:, 1]
        self.cum = np.asarray(g["cum_m"], dtype=float)
        self.length = float(g["length_m"])
        self.stop_idx = list(g["stop_idx"])
        self.stop_m = list(g["stop_m"])
        self.closed = bool(g["closed"])
        self.keys = stop_keys
        self.path = g["path"]
        self.approx = [list(r) for r in g.get("approx_m", [])]
        dx, dy = np.diff(self.lon) * KX, np.diff(self.lat) * KY
        self.seg_bearing = (np.degrees(np.arctan2(dx, dy)) + 360.0) % 360.0

    def span(self, key_from: str, key_to: str) -> tuple[int, int] | None:
        """Позиции остановок оборота ``(a, b)``: ``a`` с ключом ``key_from``, ближайшая за ней ``b`` с ``key_to``."""
        n = len(self.keys)
        best = None
        for a in (i for i, k in enumerate(self.keys) if k == key_from):
            for step in range(1, min(n, 12)):
                b = a + step
                if b >= n:
                    if not self.closed:
                        break
                    b -= n
                if self.keys[b] == key_to:
                    if best is None or step < best[2]:
                        best = (a, b, step)
                    break
        return (best[0], best[1]) if best else None

    def _project(self, lon: float, lat: float, lo_m: float, hi_m: float,
                 heading: float | None = None) -> tuple[float, float]:
        i0 = max(0, int(np.searchsorted(self.cum, lo_m)) - 1)
        i1 = min(len(self.cum) - 1, int(np.searchsorted(self.cum, hi_m)) + 1)
        if i1 <= i0:
            return math.nan, math.inf
        ax, ay = self.lon[i0:i1], self.lat[i0:i1]
        bx, by = self.lon[i0 + 1:i1 + 1], self.lat[i0 + 1:i1 + 1]
        dx, dy = (bx - ax) * KX, (by - ay) * KY
        px, py = (lon - ax) * KX, (lat - ay) * KY
        L2 = dx * dx + dy * dy
        u = np.clip(np.where(L2 > 0, (px * dx + py * dy) / np.where(L2 > 0, L2, 1), 0), 0, 1)
        d = np.hypot(px - u * dx, py - u * dy)
        if heading is not None:
            diff = np.abs((self.seg_bearing[i0:i1] - heading + 180.0) % 360.0 - 180.0)
            d = np.where(diff > 100.0, d + 1000.0, d)
        k = int(np.argmin(d))
        return float(self.cum[i0 + k] + u[k] * math.sqrt(L2[k])), float(d[k])

    def _window(self, lon, lat, lo, hi, heading):
        """Проекция в окне ``[lo, hi]`` с переходом через конец кольца для замкнутого оборота."""
        if not self.closed or (lo >= 0 and hi <= self.length):
            return [self._project(lon, lat, max(lo, 0), min(hi, self.length), heading)]
        out = [self._project(lon, lat, max(lo, 0), min(hi, self.length), heading)]
        if lo < 0:
            out.append(self._project(lon, lat, self.length + lo, self.length, heading))
        if hi > self.length:
            out.append(self._project(lon, lat, 0, hi - self.length, heading))
        return out

    def locate(self, lon: float, lat: float, span: tuple[int, int] | None, prev_m: float | None,
               heading: float | None = None) -> float | None:
        """Положение ТС вдоль оборота, м; ``None``, если ТС вне маршрута.

        Args:
            span: позиции последней пройденной и следующей остановок в обороте.
            prev_m: предыдущее положение (для непрерывности).
            heading: курс движения, град (участки встречного направления отбрасываются).
        """
        cands = []
        if span is not None:
            a, b = span
            ma, mb = self.stop_m[a], self.stop_m[b]
            if mb < ma:
                mb += self.length
            cands += self._window(lon, lat, ma - 60, mb + 60, heading)
        if not cands or min(c[1] for c in cands) > MAX_OFF_ROUTE_M:
            if prev_m is not None:
                cands += self._window(lon, lat, prev_m - 200, prev_m + 2500, heading)
            if not cands or min(c[1] for c in cands) > MAX_OFF_ROUTE_M:
                cands.append(self._project(lon, lat, 0, self.length, heading))
        m, d = min(cands, key=lambda c: c[1])
        return round(m, 1) if d <= MAX_OFF_ROUTE_M else None
