#!/usr/bin/env python3
"""Собирает mock/routes.json для демо-режима дашборда из validate/schedule_plan.csv.

Для каждого ТС берётся один полный круг по плану (от первой остановки после --from-time
до возврата к ней), траектория — из traffic.csv того же периода, если файл доступен.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
DEFAULT_DATASET = HERE.parents[2] / "data" / "dataset" / "validate"


def haversine(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def parse_point(geom: str):
    m = re.match(r"POINT\s*\(\s*([-\d.]+)\s+([-\d.]+)\s*\)", str(geom))
    if not m:
        return None, None
    return float(m.group(2)), float(m.group(1))


def street(name: str) -> str:
    return re.split(r",\s*(?:д\.|вл\.|стр\.|к\.)", name)[0].strip()


def extract_loop(g: pd.DataFrame, from_time: str):
    g = g.sort_values(["t", "tt_action_item_id"]).reset_index(drop=True)
    day = g["t"].dt.normalize().iloc[0]
    start_at = day + pd.Timedelta(from_time + ":00")
    cand = g[g["t"] >= start_at]
    if len(cand) < 10:
        cand = g
    rows = []
    for r in cand.itertuples(index=False):
        if rows and haversine(rows[-1].lat, rows[-1].lon, r.lat, r.lon) < 30:
            continue
        rows.append(r)
    if len(rows) < 4:
        return None
    s0 = rows[0]
    far = 0.0
    end = None
    for j in range(1, min(len(rows), 160)):
        d = haversine(s0.lat, s0.lon, rows[j].lat, rows[j].lon)
        far = max(far, d)
        if (rows[j].t - s0.t).total_seconds() > 4 * 3600:
            break
        if j >= 6 and far > 1200 and d < 300:
            end = j
            break
    if end is None:
        limit = s0.t + pd.Timedelta(minutes=90)
        end = max(k for k, r in enumerate(rows) if r.t <= limit)
    return rows[: end + 1]


def build_path(traffic: pd.DataFrame | None, tr_id: int, loop):
    fallback = [[round(r.lon, 6), round(r.lat, 6)] for r in loop]
    if traffic is None:
        return fallback, "stops"
    t0 = loop[0].t - pd.Timedelta(minutes=6)
    t1 = loop[-1].t + pd.Timedelta(minutes=12)
    tr = traffic[(traffic.tr_id == tr_id) & (traffic.event_time >= t0) & (traffic.event_time <= t1)]
    tr = tr.sort_values("event_time").reset_index(drop=True)
    med_lat = tr["lat"].rolling(7, center=True, min_periods=1).median()
    med_lon = tr["lon"].rolling(7, center=True, min_periods=1).median()
    pts = []
    for r, mlat, mlon in zip(tr.itertuples(index=False), med_lat, med_lon):
        if haversine(r.lat, r.lon, mlat, mlon) > 400:
            continue
        if pts and haversine(pts[-1][1], pts[-1][0], r.lat, r.lon) < 35:
            continue
        pts.append([r.lon, r.lat])
    if len(pts) < 12:
        return fallback, "stops"

    n = len(pts)
    dist = [[haversine(r.lat, r.lon, p[1], p[0]) for p in pts] for r in loop]
    cost = [dist[0][:]]
    back = [[-1] * n]
    for si in range(1, len(loop)):
        prev = cost[-1]
        best, bi = float("inf"), 0
        row, brow = [], []
        for pi in range(n):
            if prev[pi] < best:
                best, bi = prev[pi], pi
            row.append(best + dist[si][pi])
            brow.append(bi)
        cost.append(row)
        back.append(brow)
    pi = min(range(n), key=lambda k: cost[-1][k])
    idx = [pi]
    for si in range(len(loop) - 1, 0, -1):
        pi = back[si][pi]
        idx.append(pi)
    idx.reverse()
    miss = sorted(dist[si][pi] for si, pi in enumerate(idx))
    if miss[int(len(miss) * 0.9)] > 250 or idx[-1] - idx[0] < 5:
        return fallback, "stops"
    pts = pts[idx[0] : idx[-1] + 1]
    path = [[round(loop[0].lon, 6), round(loop[0].lat, 6)]]
    path += [[round(p[0], 6), round(p[1], 6)] for p in pts]
    path.append([round(loop[-1].lon, 6), round(loop[-1].lat, 6)])
    return path, "gps"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--schedule", type=Path, default=DEFAULT_DATASET / "schedule_plan.csv")
    ap.add_argument("--traffic", type=Path, default=DEFAULT_DATASET / "traffic.csv")
    ap.add_argument("--out", type=Path, default=HERE.parent / "mock" / "routes.json")
    ap.add_argument("--from-time", default="07:00", help="искать круг начиная с этого времени (ЧЧ:ММ)")
    args = ap.parse_args()

    sch = pd.read_csv(args.schedule)
    sch["t"] = pd.to_datetime(sch["time_begin"])
    sch[["lat", "lon"]] = sch["geom"].apply(lambda g: pd.Series(parse_point(g)))
    sch = sch.dropna(subset=["lat", "lon"])
    named = sch.dropna(subset=["building_address"])[["lat", "lon", "building_address"]].drop_duplicates()

    def stop_name(addr, lat, lon):
        if isinstance(addr, str) and addr.strip():
            return addr.strip()
        best, bn = 1e18, None
        for r in named.itertuples(index=False):
            d = haversine(lat, lon, r.lat, r.lon)
            if d < best:
                best, bn = d, r.building_address
        return f"Остановка у {bn}" if bn and best < 400 else "Остановка без адреса"

    traffic = None
    unit_map: dict[int, int] = {}
    if args.traffic.exists():
        traffic = pd.read_csv(
            args.traffic,
            usecols=["tr_id", "unit_id", "event_time", "location_valid", "lon", "lat"],
        )
        traffic["event_time"] = pd.to_datetime(traffic["event_time"], format="mixed")
        unit_map = traffic.groupby("tr_id")["unit_id"].first().astype(int).to_dict()
        traffic = traffic[traffic["location_valid"] == True].dropna(subset=["lat", "lon"])  # noqa: E712

    routes = []
    for idx, (tr_id, g) in enumerate(sorted(sch.groupby("tr_id"), key=lambda x: x[0])):
        loop = extract_loop(g, args.from_time)
        if not loop:
            continue
        t0 = loop[0].t
        stops = [
            {
                "stop_id": str(r.tt_action_item_id),
                "name": stop_name(r.building_address, r.lat, r.lon),
                "lat": round(r.lat, 6),
                "lon": round(r.lon, 6),
                "t": int((r.t - t0).total_seconds()),
            }
            for r in loop
        ]
        real = [x for x in stops if not x["name"].startswith("Остановка ")]
        if real:
            first = real[0]
            far = max(real, key=lambda x: haversine(first["lat"], first["lon"], x["lat"], x["lon"]))
            a, b = street(first["name"]), street(far["name"])
            if a == b:
                a, b = first["name"], far["name"]
            route_name = f"{a} — {b}"
        else:
            for k, x in enumerate(stops, 1):
                x["name"] = f"Остановка {k} (без адреса)"
            route_name = "Круговой, адреса остановок не заданы"
        path, path_src = build_path(traffic, int(tr_id), loop)
        routes.append(
            {
                "tr_id": int(tr_id),
                "unit_id": int(unit_map.get(int(tr_id), 660000 + idx)),
                "route_name": route_name,
                "loop_start": t0.strftime("%H:%M:%S"),
                "loop_s": stops[-1]["t"],
                "path_source": path_src,
                "stops": stops,
                "path": path,
            }
        )
        print(f"{tr_id}: {len(stops)} остановок, круг {stops[-1]['t'] // 60} мин, путь {path_src} ({len(path)} т.)")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(routes, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"записано {len(routes)} маршрутов в {args.out}")


if __name__ == "__main__":
    main()
