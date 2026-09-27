"""Плановое расписание (нитка графика) и справочник остановок.

Расписание приходит построчно: одна строка — одно плановое прибытие ТС на
остановку (``tt_action_item_id``). Внутри модуля всё приводится к
unix-секундам «времени потока» (наивное время CSV трактуется как UTC).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

EPOCH = pd.Timestamp("1970-01-01")
HORIZON_MIN_S = 600
HORIZON_MAX_S = 900


def to_unix(ts) -> float:
    """Перевести наивную метку времени (строка/datetime/Timestamp) в unix-секунды."""
    return (pd.Timestamp(ts) - EPOCH).total_seconds()


def from_unix(t: float | None) -> str | None:
    """Unix-секунды → ISO-строка без таймзоны (время потока)."""
    if t is None or not np.isfinite(t):
        return None
    return datetime.utcfromtimestamp(float(t)).replace(microsecond=0).isoformat()


def load_schedule(path: str) -> pd.DataFrame:
    """Загрузить ``schedule.csv`` / ``schedule_plan.csv`` в нормализованный вид.

    Returns:
        DataFrame с колонками ``item_id, tr_id, plan, fact, manual_fill, lon, lat,
        name, stop_key``; ``plan``/``fact`` — unix-секунды (``fact`` = NaN, если
        факта в файле нет). Отсортирован по ``tr_id, plan, item_id``.
    """
    s = pd.read_csv(path)
    xy = s["geom"].str.extract(r"POINT \(([-\d.]+) ([-\d.]+)\)").astype(float)
    out = pd.DataFrame({
        "item_id": s["tt_action_item_id"].astype("int64"),
        "tr_id": s["tr_id"].astype("int64"),
        "plan": (pd.to_datetime(s["time_begin"]) - EPOCH).dt.total_seconds(),
        "fact": (pd.to_datetime(s["time_fact_begin"]) - EPOCH).dt.total_seconds()
        if "time_fact_begin" in s else np.nan,
        "manual_fill": s.get("manual_fill", False).astype(bool),
        "lon": xy[0],
        "lat": xy[1],
        "name": s["building_address"].fillna("").astype(str),
    })
    out["stop_key"] = out["lon"].round(6).astype(str) + "," + out["lat"].round(6).astype(str)
    named = out[out["name"].str.strip() != ""].groupby("stop_key")["name"].first()
    empty = out["name"].str.strip() == ""
    out.loc[empty, "name"] = out.loc[empty, "stop_key"].map(named)
    still = out["name"].isna() | (out["name"].astype(str).str.strip() == "")
    if still.any():
        named_pts = out.loc[~still].drop_duplicates("stop_key")
        nx, ny, nn = named_pts["lon"].to_numpy(), named_pts["lat"].to_numpy(), named_pts["name"].to_numpy()
        kx = np.cos(np.radians(55.75)) * 111320.0
        names = {}
        for key, x, y in out.loc[still, ["stop_key", "lon", "lat"]].drop_duplicates("stop_key").itertuples(index=False):
            if len(nx):
                d = np.hypot((nx - x) * kx, (ny - y) * 111320.0)
                k = int(np.argmin(d))
                names[key] = f"около {nn[k]}" if d[k] <= 600 else "Остановка без адреса"
            else:
                names[key] = "Остановка без адреса"
        out.loc[still, "name"] = out.loc[still, "stop_key"].map(names)
    return out.sort_values(["tr_id", "plan", "item_id"]).reset_index(drop=True)


@dataclass
class VehicleSchedule:
    """Упорядоченная по плану последовательность остановок одного ТС за сутки."""

    tr_id: int
    item_id: np.ndarray
    plan: np.ndarray
    lon: np.ndarray
    lat: np.ndarray
    name: np.ndarray
    stop_key: np.ndarray
    manual_fill: np.ndarray
    fact: np.ndarray
    index_of: dict[int, int] = field(default_factory=dict)

    @classmethod
    def from_frame(cls, df: pd.DataFrame) -> "VehicleSchedule":
        """Собрать из строк одного ``tr_id`` нормализованного расписания."""
        df = df.sort_values(["plan", "item_id"])
        vs = cls(
            tr_id=int(df["tr_id"].iloc[0]),
            item_id=df["item_id"].to_numpy(),
            plan=df["plan"].to_numpy(float),
            lon=df["lon"].to_numpy(float),
            lat=df["lat"].to_numpy(float),
            name=df["name"].to_numpy(object),
            stop_key=df["stop_key"].to_numpy(object),
            manual_fill=df["manual_fill"].to_numpy(bool),
            fact=df["fact"].to_numpy(float),
        )
        vs.index_of = {int(i): k for k, i in enumerate(vs.item_id)}
        return vs

    def __len__(self) -> int:
        return len(self.plan)

    def target_index(self, T: float) -> int | None:
        """Первая по плану остановка с плановым временем в окне ``(T+10 мин, T+15 мин]``."""
        k = int(np.searchsorted(self.plan, T + HORIZON_MIN_S, side="right"))
        if k < len(self.plan) and self.plan[k] <= T + HORIZON_MAX_S:
            return k
        return None

    def last_planned_index(self, T: float) -> int | None:
        """Последняя остановка с плановым временем ``<= T``."""
        k = int(np.searchsorted(self.plan, T, side="right")) - 1
        return k if k >= 0 else None

    def loop_stops(self) -> list[int]:
        """Индексы остановок одного «типового» круга: уникальные точки в порядке первого появления."""
        seen: set[str] = set()
        out = []
        for k, key in enumerate(self.stop_key):
            if key not in seen:
                seen.add(key)
                out.append(k)
        return out


def build_vehicle_schedules(df: pd.DataFrame) -> dict[int, VehicleSchedule]:
    """Разбить нормализованное расписание по ТС."""
    return {int(tr): VehicleSchedule.from_frame(g) for tr, g in df.groupby("tr_id")}
