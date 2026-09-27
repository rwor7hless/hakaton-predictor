"""Геометрия маршрутов по дорогам: полный оборот ТС, привязанный к улично-дорожной сети.

Для каждого ТС с расписанием берётся типичный оборот (от отправления с конечной до
следующего отправления с неё же). Реальный GPS-трек этого оборота прореживается и
прокладывается по дорожному графу сервисом OSRM ``route`` с направлением движения в
каждой точке (``bearings``) — так маршрут идёт по нужной полосе без разворотов. Если
трека нет или результат неправдоподобен, маршрут прокладывается через остановки с
направлениями по порядку остановок. Ответы OSRM кэшируются.

Результат — ``artifacts/reference/route_geometry.json``::

    {tr_id: {"path": [[lon, lat], ...], "cum_m": [...], "length_m": L,
             "stop_idx": [индексы остановок оборота в плане], "stop_m": [...],
             "closed": bool, "source": "osrm-gps|osrm-stops|stops"}}

Запуск: ``python scripts/build_routes.py`` (нужен доступ к router.project-osrm.org).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import time
from collections import Counter
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libs/transit_core"))

from transit_core.schedule import build_vehicle_schedules, load_schedule  # noqa: E402
from transit_core.tracker import KX, KY, VehicleTracker, stop_roles  # noqa: E402

D = ROOT / "data/dataset"
OUT = ROOT / "artifacts/reference/route_geometry.json"
CACHE = ROOT / "artifacts/cache/osrm"
OSRM = "https://router.project-osrm.org"
CHUNK = 150


def dist(a, b) -> float:
    return math.hypot((a[0] - b[0]) * KX, (a[1] - b[1]) * KY)


def plen(pts) -> float:
    return float(sum(dist(pts[i - 1], pts[i]) for i in range(1, len(pts))))


def osrm(kind: str, coords: list, params: dict) -> dict:
    q = ";".join(f"{x:.6f},{y:.6f}" for x, y in coords)
    key = hashlib.md5((kind + q + json.dumps(params, sort_keys=True)).encode()).hexdigest()
    f = CACHE / f"{key}.json"
    if f.exists():
        return json.loads(f.read_text())
    for attempt in range(5):
        try:
            r = httpx.get(f"{OSRM}/{kind}/v1/driving/{q}", params=params, timeout=60)
            if r.status_code == 429:
                time.sleep(2 + attempt * 2)
                continue
            j = r.json()
            CACHE.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(j))
            time.sleep(0.4)
            return j
        except httpx.HTTPError:
            time.sleep(2 + attempt * 2)
    return {"code": "Error"}


def thin(pts: np.ndarray, min_m: float = 120.0) -> np.ndarray:
    out = [pts[0]]
    for p in pts[1:]:
        if dist(out[-1], p) >= min_m:
            out.append(p)
    return np.array(out)


def join(parts: list[list]) -> list:
    out: list = []
    for part in parts:
        if not part:
            continue
        if out:
            k = int(np.argmin([dist(out[-1], p) for p in part[: max(1, len(part) // 3)]]))
            part = part[k:]
            if part and dist(out[-1], part[0]) < 1:
                part = part[1:]
        out.extend(part)
    return out


def bearings(pts: np.ndarray) -> list[int]:
    """Направление движения в каждой точке (по соседним точкам), градусы от севера."""
    out = []
    for k in range(len(pts)):
        a, b = pts[max(0, k - 1)], pts[min(len(pts) - 1, k + 1)]
        out.append(int(round(math.degrees(math.atan2((b[0] - a[0]) * KX, (b[1] - a[1]) * KY)) % 360)))
    return out


def route_through(pts: np.ndarray, tol: int, radius: int) -> list:
    """Маршрут по дорогам через точки с заданными направлениями подъезда (без разворотов на встречку).

    Точки, для которых OSRM не находит подходящий сегмент (манёвры на конечной, стоянка вне
    дороги), выбрасываются, и запрос повторяется.
    """
    br = bearings(pts)
    parts = []
    i = 0
    while i < len(pts) - 1:
        chunk, bc = [list(p) for p in pts[i: i + CHUNK]], br[i: i + CHUNK]
        for _ in range(25):
            j = osrm("route", chunk, {"overview": "full", "geometries": "geojson", "continue_straight": "true",
                                      "bearings": ";".join(f"{b},{tol}" for b in bc),
                                      "radiuses": ";".join([str(radius)] * len(chunk))})
            m = re.search(r"coordinate (\d+)", j.get("message", ""))
            if j.get("code") == "NoSegment" and m and len(chunk) > 2:
                k = int(m.group(1))
                del chunk[k], bc[k]
                continue
            break
        if j.get("code") != "Ok":
            return []
        parts.append(j["routes"][0]["geometry"]["coordinates"])
        i += CHUNK - 1
    return join(parts)


def route_legs(stops: list, tol: int = 70, radius: int = 60) -> dict[tuple[int, int], list]:
    """Геометрия каждого перегона между соседними остановками по дорогам (OSRM, с направлениями)."""
    P = np.array(stops)
    br = bearings(P)
    out: dict[tuple[int, int], list] = {}
    i = 0
    while i < len(P) - 1:
        keep = list(range(i, min(len(P), i + CHUNK)))
        for _ in range(40):
            j = osrm("route", [list(P[k]) for k in keep],
                     {"overview": "false", "steps": "true", "geometries": "geojson", "continue_straight": "true",
                      "bearings": ";".join(f"{br[k]},{tol}" for k in keep),
                      "radiuses": ";".join([str(radius)] * len(keep))})
            m = re.search(r"coordinate (\d+)", j.get("message", ""))
            if j.get("code") == "NoSegment" and m and len(keep) > 2:
                del keep[int(m.group(1))]
                continue
            break
        if j.get("code") == "Ok":
            for (a, b), leg in zip(zip(keep, keep[1:]), j["routes"][0]["legs"]):
                pts: list = []
                for st in leg["steps"]:
                    for c in st["geometry"]["coordinates"]:
                        if not pts or c != pts[-1]:
                            pts.append(c)
                out[(a, b)] = pts
        i = keep[-1] if keep[-1] > i else i + 1
    return out


def route_pair(a, b, bearing: bool) -> list:
    """Один перегон по дорогам: с направлением подъезда или без него."""
    params = {"overview": "full", "geometries": "geojson"}
    if bearing:
        br = bearings(np.array([a, b]))
        params.update({"bearings": ";".join(f"{x},70" for x in br), "radiuses": "60;60"})
    j = osrm("route", [list(a), list(b)], params)
    return j["routes"][0]["geometry"]["coordinates"] if j.get("code") == "Ok" else []


def mean_dev(pts: list, poly: list) -> float:
    """Среднее расстояние (м) от точек трека до ломаной."""
    if len(pts) == 0 or len(poly) < 2:
        return math.inf
    P = np.array(poly)
    ax, ay = P[:-1, 0], P[:-1, 1]
    dx, dy = (P[1:, 0] - ax) * KX, (P[1:, 1] - ay) * KY
    L2 = np.maximum(dx * dx + dy * dy, 1e-9)
    out = []
    for x, y in pts:
        px, py = (x - ax) * KX, (y - ay) * KY
        u = np.clip((px * dx + py * dy) / L2, 0, 1)
        out.append(float(np.min(np.hypot(px - u * dx, py - u * dy))))
    return float(np.mean(out))


def clean(path: list) -> list:
    """Убрать мелкие артефакты привязки к дорогам, не трогая ход маршрута.

    Один проход, без повторов: вырезаются узелки (линия возвращается в ту же точку
    через 25–150 м) и одиночные зубцы-развороты с плечами короче 40 м. Длинные
    участки «туда-обратно» по одной улице сохраняются.
    """
    pts = [p for i, p in enumerate(path) if i == 0 or dist(p, path[i - 1]) > 0.5]
    out = []
    i = 0
    while i < len(pts):
        cut = None
        run = 0.0
        for j in range(i + 2, min(len(pts), i + 40)):
            run += dist(pts[j - 1], pts[j])
            if run > 150:
                break
            if dist(pts[i], pts[j]) < 10 and run > 25:
                cut = j
        out.append(pts[i])
        i = cut if cut is not None else i + 1
    res = [out[0]]
    for k in range(1, len(out) - 1):
        a, b, c = res[-1], out[k], out[k + 1]
        v1 = ((b[0] - a[0]) * KX, (b[1] - a[1]) * KY)
        v2 = ((c[0] - b[0]) * KX, (c[1] - b[1]) * KY)
        n1, n2 = math.hypot(*v1), math.hypot(*v2)
        if 0 < n1 < 40 and 0 < n2 < 40 and (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2) < -0.9:
            continue
        res.append(b)
    res.append(out[-1])
    return res


def chaikin(pts: list, n: int = 2) -> list:
    """Сглаживание ломаной (срезание углов Чайкина), концы сохраняются."""
    for _ in range(n):
        if len(pts) < 3:
            return pts
        q = [pts[0]]
        for a, b in zip(pts[:-1], pts[1:]):
            q.append([0.75 * a[0] + 0.25 * b[0], 0.75 * a[1] + 0.25 * b[1]])
            q.append([0.25 * a[0] + 0.75 * b[0], 0.25 * a[1] + 0.75 * b[1]])
        q.append(pts[-1])
        pts = q
    return pts


def measures(path: list, stops: list) -> list[float]:
    """Положение каждой остановки вдоль ломаной, строго по порядку следования.

    Ломаная собрана перегон за перегоном в порядке остановок, поэтому остановка
    ищется только в окне сразу за предыдущей: первая — в начале линии, каждая
    следующая — не дальше разумного пробега от предыдущей.
    """
    P = np.array(path)
    ax, ay = P[:-1, 0], P[:-1, 1]
    dx, dy = (P[1:, 0] - ax) * KX, (P[1:, 1] - ay) * KY
    L = np.hypot(dx, dy)
    L2 = np.maximum(L * L, 1e-9)
    cum = np.r_[0.0, np.cumsum(L)]
    out, prev = [], 0.0
    for k, st in enumerate(stops):
        hi = 1500.0 if k == 0 else prev + max(1500.0, 4 * dist(stops[k - 1], st) + 500)
        lo = 0.0 if k == 0 else prev - 5
        mask = (cum[1:] >= lo) & (cum[:-1] <= hi)
        if not mask.any():
            out.append(round(prev, 1))
            continue
        idx = np.where(mask)[0]
        px, py = (st[0] - ax[idx]) * KX, (st[1] - ay[idx]) * KY
        u = np.clip((px * dx[idx] + py * dy[idx]) / L2[idx], 0, 1)
        d = np.hypot(px - u * dx[idx], py - u * dy[idx])
        j = int(np.argmin(d))
        m = max(prev, float(cum[idx[j]] + u[j] * L[idx[j]]))
        out.append(round(m, 1))
        prev = m
    return out


def pair_tracks(v, tk, gt, gx, gy) -> dict[tuple[str, str], list]:
    """Лучший за день GPS-проход каждого перегона ``(ключ остановки A, ключ B)``."""
    best: dict[tuple[str, str], list] = {}
    n = len(v)
    for i in range(n - 1):
        j = i + 1
        while j < n and v.stop_key[j] == v.stop_key[i]:
            j += 1
        if j >= n or not (np.isfinite(tk.arr[i]) and np.isfinite(tk.arr[j])):
            continue
        if tk.arr_interp[i] or tk.arr_interp[j] or not (0 < tk.arr[j] - tk.arr[i] < 900):
            continue
        m = (gt >= tk.arr[i]) & (gt <= tk.arr[j])
        pts = [[float(x), float(y)] for x, y in zip(gx[m], gy[m])]
        key = (v.stop_key[i], v.stop_key[j])
        if len(pts) > len(best.get(key, [])):
            best[key] = pts
    return best


def project_near(path: list, cum: list, pt, hint: float, win: float = 500.0) -> float:
    """Метраж ближайшей к ``pt`` точки ломаной в окне ``hint ± win``."""
    best, bm = math.inf, hint
    for k in range(1, len(path)):
        if cum[k] < hint - win or cum[k - 1] > hint + win:
            continue
        a, b = path[k - 1], path[k]
        ax, ay = (b[0] - a[0]) * KX, (b[1] - a[1]) * KY
        px, py = (pt[0] - a[0]) * KX, (pt[1] - a[1]) * KY
        L2 = ax * ax + ay * ay
        u = min(1.0, max(0.0, (px * ax + py * ay) / L2)) if L2 > 0 else 0.0
        d = math.hypot(px - u * ax, py - u * ay)
        if d < best:
            best, bm = d, cum[k - 1] + u * math.sqrt(L2)
    return bm


def cycles(v, roles) -> list[tuple[int, int]]:
    starts = [i for i in range(len(v)) if roles[i] == 1]
    out = []
    for a in starts:
        nxt = [b for b in starts if b > a and v.stop_key[b] == v.stop_key[a]]
        if nxt:
            out.append((a, nxt[0]))
    if not out:
        out = [(a, b) for a, b in zip(starts, starts[1:] + [len(v) - 1])]
    return out


def build() -> dict:
    VS = build_vehicle_schedules(load_schedule(str(D / "validate/schedule_plan.csv")))
    t = pd.read_csv(D / "validate/traffic.csv", usecols=["tr_id", "event_time", "location_valid", "lon", "lat",
                                                         "speed", "heading"])
    t["t"] = (pd.to_datetime(t.event_time) - pd.Timestamp("1970-01-01")).dt.total_seconds()
    res = {}
    for tr, v in VS.items():
        roles = stop_roles(v)
        g = t[t.tr_id == tr].sort_values("t")
        tk = VehicleTracker(v)
        for r in zip(g.t, g.lon, g.lat, g.speed, g.heading, g.location_valid):
            tk.update(r[0], r[1], r[2], r[3], r[4], bool(r[5]))
        cyc = cycles(v, roles)
        if not cyc:
            continue
        typical = Counter(b - a for a, b in cyc).most_common(1)[0][0]
        cand = [(a, b) for a, b in cyc if b - a == typical] or cyc
        direct = ~tk.arr_interp & np.isfinite(tk.arr)
        a, b = max(cand, key=lambda c: (direct[c[0]:c[1] + 1].sum(), -c[0]))
        idx = list(range(a, b + 1))
        uniq = []
        for i in idx[:-1]:
            if not uniq or v.stop_key[uniq[-1]] != v.stop_key[i]:
                uniq.append(i)
        stops = [(float(v.lon[i]), float(v.lat[i])) for i in uniq]
        closed = v.stop_key[b] == v.stop_key[a]
        target = stops + ([stops[0]] if closed else [(float(v.lon[b]), float(v.lat[b]))])
        straight = plen(target)
        legs = route_legs(target)
        gps = g[g.location_valid & np.isfinite(g.lon)]
        gt, gx, gy = gps.t.to_numpy(), gps.lon.to_numpy(), gps.lat.to_numpy()
        idx_full = uniq + [b]
        pairs = pair_tracks(v, tk, gt, gx, gy)
        path, n_osrm, n_gps, n_line = [], 0, 0, 0
        approx_pts = []
        for k in range(len(target) - 1):
            sa, sb = target[k], target[k + 1]
            straight_k = dist(sa, sb)
            ia, ib = idx_full[k], idx_full[k + 1]
            gps_pts = pairs.get((v.stop_key[ia], v.stop_key[ib]), [])
            gleg = [list(sa)] + gps_pts + [list(sb)]
            gps_ok = len(gps_pts) >= 2 and plen(gleg) <= 2.2 * straight_k + 200
            cands = []
            if (k, k + 1) in legs:
                cands.append(legs[(k, k + 1)])
            if not cands or gps_ok:
                for bearing in (True, False):
                    c = route_pair(sa, sb, bearing)
                    if c and all(c != x for x in cands):
                        cands.append(c)
            best = None
            if gps_ok:
                gl = plen(gleg)
                scored = [(mean_dev(gps_pts, c), c) for c in cands if plen(c) <= 1.35 * gl + 150]
                scored = [x for x in scored if x[0] <= 35]
                if scored:
                    best = min(scored, key=lambda x: x[0])[1]
            else:
                ok = [c for c in cands if plen(c) <= 2.5 * straight_k + 400]
                if ok:
                    best = min(ok, key=plen)
            approx = False
            if best is not None:
                seg, n_osrm = [list(p) for p in best], n_osrm + 1
            elif gps_ok:
                seg, n_gps = chaikin(gleg, 3), n_gps + 1
            else:
                seg, n_line, approx = [list(sa), list(sb)], n_line + 1, True
            if approx:
                m0 = plen(path) if path else 0.0
                approx_pts.append((seg[0], seg[-1], m0, m0 + plen(seg)))
            path.extend(seg if not path else seg[1:])
        raw_len = plen(path)
        path = clean(path)
        source = f"osrm {n_osrm}/gps {n_gps}/line {n_line}, чистка {raw_len / 1000:.1f}→{plen(path) / 1000:.1f} km"
        path = [[round(x, 6), round(y, 6)] for x, y in path]
        cum = [0.0]
        for k in range(1, len(path)):
            cum.append(round(cum[-1] + dist(path[k - 1], path[k]), 1))
        sm = measures(path, stops)
        approx = []
        for a, b2, h0, h1 in approx_pts:
            ma, mb = project_near(path, cum, a, h0), project_near(path, cum, b2, h1)
            if mb > ma:
                approx.append([round(ma, 1), round(mb, 1)])
        res[str(tr)] = {"path": path, "cum_m": cum, "length_m": cum[-1], "stop_idx": [int(i) for i in uniq],
                        "stop_m": sm, "closed": bool(closed), "source": source, "approx_m": approx}
        print(f"{tr}: {source:10s} stops={len(uniq):3d} path={len(path):4d} pts, {cum[-1] / 1000:.1f} km "
              f"(по остановкам {straight / 1000:.1f} km)")
    return res


def main() -> None:
    res = build()
    OUT.write_text(json.dumps(res, separators=(",", ":")))
    print(f"→ {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
