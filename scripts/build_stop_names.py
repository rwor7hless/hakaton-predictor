"""Названия для остановок без адреса в расписании: обратное геокодирование OSM (Nominatim).

Результат — ``artifacts/reference/stop_names.json`` (``stop_key -> название``). Запросы
идут не чаще раза в секунду (политика Nominatim), ответы кэшируются.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libs/transit_core"))

from transit_core.schedule import load_schedule  # noqa: E402

OUT = ROOT / "artifacts/reference/stop_names.json"
CACHE = ROOT / "artifacts/cache/nominatim.json"
UA = "transit-delay-dashboard/1.0 (hackathon demo)"


def label(j: dict) -> str | None:
    a = j.get("address", {})
    road = a.get("road") or a.get("pedestrian") or a.get("highway")
    name = j.get("name") or road
    if not name:
        return None
    area = a.get("suburb") or a.get("quarter") or a.get("city_district") or a.get("village") or a.get("town")
    return f"{name}, {area}" if area and area not in name else name


def main() -> None:
    s = load_schedule(str(ROOT / "data/dataset/validate/schedule_plan.csv"))
    todo = s[s["name"] == "Остановка без адреса"].drop_duplicates("stop_key")
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    out = {}
    with httpx.Client(headers={"User-Agent": UA}, timeout=30) as c:
        for r in todo.itertuples():
            if r.stop_key not in cache:
                resp = c.get("https://nominatim.openstreetmap.org/reverse",
                              params={"format": "jsonv2", "lat": r.lat, "lon": r.lon, "zoom": 17,
                                      "accept-language": "ru", "addressdetails": 1})
                cache[r.stop_key] = resp.json() if resp.status_code == 200 else {}
                CACHE.parent.mkdir(parents=True, exist_ok=True)
                CACHE.write_text(json.dumps(cache, ensure_ascii=False))
                time.sleep(1.1)
            name = label(cache[r.stop_key])
            if name:
                out[r.stop_key] = name
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=0))
    print(f"{len(out)} из {len(todo)} остановок получили название → {OUT}")


if __name__ == "__main__":
    main()
