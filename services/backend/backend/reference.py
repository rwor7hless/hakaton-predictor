"""Справочные данные: расписание (только план), маршруты, соответствие терминалов ТС."""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from transit_core.features import route_key
from transit_core.schedule import VehicleSchedule, build_vehicle_schedules, load_schedule
from transit_core.tracker import stop_roles

from .geometry import RouteGeometry


@dataclass
class RouteInfo:
    """Геометрия и название маршрута одного ТС для карты."""

    tr_id: int
    key: str
    name: str
    loop_idx: list[int]


class Reference:
    """Всё, что backend знает заранее: план, маршруты, unit_id → tr_id, fallback-профиль.

    Args:
        schedule_csv: плановое расписание (``schedule_plan.csv``; факт не используется).
        units_csv: соответствие ``tr_id, unit_id``.
        fallback_json: профиль остановок для деградированного режима без ML.
        route_paths_json: геометрия маршрутов по GPS-трекам (запасной вариант).
        route_geometry_json: геометрия оборотов по дорогам (``scripts/build_routes.py``).
        stop_names_json: названия остановок без адреса (``scripts/build_stop_names.py``).
    """

    def __init__(self, schedule_csv: Path, units_csv: Path, fallback_json: Path, route_paths_json: Path | None = None,
                 route_geometry_json: Path | None = None, stop_names_json: Path | None = None):
        df = load_schedule(str(schedule_csv))
        df["fact"] = np.nan
        if stop_names_json is not None and Path(stop_names_json).exists():
            names = json.loads(Path(stop_names_json).read_text())
            miss = df["name"] == "Остановка без адреса"
            df.loc[miss, "name"] = df.loc[miss, "stop_key"].map(names).fillna("Остановка без адреса")
        self.schedules: dict[int, VehicleSchedule] = build_vehicle_schedules(df)
        self.day_start = float(np.floor(df["plan"].min() / 86400) * 86400)
        units = pd.read_csv(units_csv)
        self.unit_to_tr = {int(u): int(t) for t, u in zip(units["tr_id"], units["unit_id"])}
        self.tr_to_unit = {t: u for u, t in self.unit_to_tr.items()}
        self.routes = {tr: self._route(tr, s) for tr, s in self.schedules.items()}
        fb = json.loads(Path(fallback_json).read_text()) if Path(fallback_json).exists() else {}
        self.fb_k = float(fb.get("k", 0.6))
        self.fb_bias = fb.get("stop_bias", {})
        self.fb_global = float(fb.get("global_bias", 0.0))
        rp = Path(route_paths_json) if route_paths_json else None
        self.paths: dict[int, list] = ({int(k): v for k, v in json.loads(rp.read_text()).items()}
                                       if rp is not None and rp.exists() else {})
        self.geometry: dict[int, RouteGeometry] = {}
        rg = Path(route_geometry_json) if route_geometry_json else None
        if rg is not None and rg.exists():
            for k, g in json.loads(rg.read_text()).items():
                tr = int(k)
                if tr in self.schedules:
                    s = self.schedules[tr]
                    geo = RouteGeometry(g, [s.stop_key[i] for i in g["stop_idx"]])
                    self.geometry[tr] = geo
                    self.routes[tr].loop_idx = geo.stop_idx

    @staticmethod
    def _route(tr: int, s: VehicleSchedule) -> RouteInfo:
        roles = stop_roles(s)
        ends = Counter(s.name[i] for i in range(len(s)) if roles[i] in (1, 2))
        top = [n for n, _ in ends.most_common(2)]
        name = " — ".join(top) if top else f"ТС {tr}"
        trips, cur = [], []
        for i in range(len(s)):
            if roles[i] == 1 and cur:
                trips.append(cur)
                cur = []
            cur.append(i)
        if cur:
            trips.append(cur)
        best = max(trips, key=lambda t: len({s.stop_key[i] for i in t})) if trips else list(range(len(s)))
        loop = []
        seen = set()
        for i in best:
            if s.stop_key[i] not in seen:
                seen.add(s.stop_key[i])
                loop.append(i)
        return RouteInfo(tr, route_key(s), name, loop)

    def project_time(self, t: float) -> float:
        """Привести метку времени к дню расписания (для живого эмулятора с текущим временем)."""
        if abs(t - self.day_start) < 2 * 86400:
            return t
        return self.day_start + (t % 86400)

    def fallback_delay(self, tgt_stop: str, est_now: float | None) -> float:
        """Прогноз без ML: сжатое текущее отклонение + систематическое смещение остановки."""
        base = self.fb_k * est_now if est_now is not None and np.isfinite(est_now) else 0.0
        return float(base + self.fb_bias.get(tgt_stop, self.fb_global))
