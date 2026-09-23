#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_real_context.py

Считает по каждому району Астаны реальные объекты из OpenStreetMap:
школы, детсады, поликлиники/больницы, остановки общественного транспорта, парки.

Результат — data/real_context.json:
    {"esil": {"schools": 0, "kindergartens": 0, "clinics": 0, "bus_stops": 0, "parks": 0}, ...}

Дополнительно:
    data/real_context_meta.json      — источник, дата, статус, какие теги считались
    data/real_context_points.geojson — сами объекты точками (для справочного слоя pydeck)

ВАЖНО: это только справочный слой для карты и объяснений.
Он НЕ участвует в официальном расчёте индекса качества жизни.

Требует data/astana_districts.geojson (сначала запустите fetch_districts.py).
Объекты скачиваются ОДНИМ запросом по рамке города и раскладываются по районам
локально (точка в полигоне), поэтому скрипт работает и с границами из OSM,
и со схематичными запасными полигонами.

Данные: © участники OpenStreetMap, лицензия ODbL.
Запуск: python fetch_real_context.py [--timeout 180]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

# Скрипт лежит в корне проекта, поэтому результаты не зависят от текущей
# рабочей директории.
ROOT = Path(__file__).resolve().parent
DISTRICTS_PATH = ROOT / "data" / "astana_districts.geojson"
OUT_PATH = ROOT / "data" / "real_context.json"
META_PATH = ROOT / "data" / "real_context_meta.json"
POINTS_PATH = ROOT / "data" / "real_context_points.geojson"

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
USER_AGENT = "akim-5h-simulator/1.0 (hackathon project, Streamlit + pydeck)"

DISTRICT_IDS = ("esil", "almaty", "saryarka", "baikonur", "nura")
CATEGORIES = ("schools", "kindergartens", "clinics", "bus_stops", "parks")

# Какие теги OSM считаем каждой категорией (записывается в meta для прозрачности).
CATEGORY_TAGS = {
    "schools": "amenity=school",
    "kindergartens": "amenity=kindergarten",
    "clinics": "amenity=clinic|hospital, healthcare=clinic|hospital",
    "bus_stops": "highway=bus_stop, public_transport=platform + bus/trolleybus=yes",
    "parks": "leisure=park",
}

# В OSM один объект часто нанесён дважды: точкой и контуром здания/территории
# (или остановка — точкой и платформой). Объекты одной категории с одинаковым
# названием ближе этого расстояния (в метрах) считаем одним объектом.
DEDUP_RADIUS_M = {
    "schools": 150,
    "kindergartens": 100,
    "clinics": 100,
    "bus_stops": 25,   # остановки на разных сторонах улицы — это разные остановки
    "parks": 150,
}


# ---------------------------------------------------------------------------
# Районы и геометрия
# ---------------------------------------------------------------------------

def point_in_ring(x: float, y: float, ring) -> bool:
    """Ray casting: лежит ли точка (x=lon, y=lat) внутри кольца."""
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y):
            if x < xi + (y - yi) * (xj - xi) / (yj - yi):
                inside = not inside
        j = i
    return inside


def load_districts(path: Path):
    """Читает GeoJSON районов. Возвращает (словарь районов, источник границ)."""
    if not path.exists():
        raise FileNotFoundError(f"нет файла {path.relative_to(ROOT)}")
    with open(path, encoding="utf-8") as f:
        fc = json.load(f)

    districts = {}
    for feat in fc.get("features", []):
        did = (feat.get("properties") or {}).get("id")
        geom = feat.get("geometry") or {}
        if did not in DISTRICT_IDS:
            continue
        if geom.get("type") == "Polygon":
            polygons = [geom["coordinates"]]
        elif geom.get("type") == "MultiPolygon":
            polygons = geom["coordinates"]
        else:
            continue
        xs = [pt[0] for poly in polygons for pt in poly[0]]
        ys = [pt[1] for poly in polygons for pt in poly[0]]
        districts[did] = {"polygons": polygons, "bbox": (min(xs), min(ys), max(xs), max(ys))}

    missing = [d for d in DISTRICT_IDS if d not in districts]
    if missing:
        raise ValueError(f"в {path.name} нет районов: {', '.join(missing)}")
    source = (fc.get("metadata") or {}).get("source", "unknown")
    return districts, source


def find_district(lon: float, lat: float, districts: dict):
    """Возвращает id района, в который попадает точка, или None."""
    for did, d in districts.items():
        x0, y0, x1, y1 = d["bbox"]
        if not (x0 <= lon <= x1 and y0 <= lat <= y1):
            continue  # быстрая отсечка по рамке
        for poly in d["polygons"]:
            if point_in_ring(lon, lat, poly[0]) and not any(
                point_in_ring(lon, lat, hole) for hole in poly[1:]
            ):
                return did
    return None


def query_bbox(districts: dict, margin: float = 0.01):
    """Общая рамка всех районов (юг, запад, север, восток) с небольшим запасом."""
    boxes = [d["bbox"] for d in districts.values()]
    return (
        min(b[1] for b in boxes) - margin, min(b[0] for b in boxes) - margin,
        max(b[3] for b in boxes) + margin, max(b[2] for b in boxes) + margin,
    )


# ---------------------------------------------------------------------------
# Overpass API
# ---------------------------------------------------------------------------

class OverpassError(RuntimeError):
    """Не удалось получить ответ ни от одного сервера Overpass."""


def overpass_query(query: str, timeout: int, deadline: float, rounds: int = 2) -> dict:
    """Запрос к серверам Overpass по очереди, с повторами и общим лимитом времени."""
    last_error = "неизвестная ошибка"
    for attempt in range(1, rounds + 1):
        for url in OVERPASS_URLS:
            remaining = deadline - time.monotonic()
            if remaining <= 5:
                raise OverpassError(f"превышен общий лимит времени; последняя ошибка: {last_error}")
            try:
                print(f"    -> {url}")
                resp = requests.post(
                    url,
                    data={"data": query},
                    headers={"User-Agent": USER_AGENT},
                    timeout=(10, min(timeout + 30, remaining)),
                )
                if resp.status_code in (429, 502, 503, 504):
                    last_error = f"HTTP {resp.status_code} (сервер перегружен или лимит запросов)"
                    print(f"       {last_error}")
                    continue
                resp.raise_for_status()
                data = resp.json()
                remark = data.get("remark", "")
                if remark and "error" in remark.lower() and not data.get("elements"):
                    last_error = f"ошибка Overpass: {remark}"
                    print(f"       {last_error}")
                    continue
                return data
            except requests.exceptions.RequestException as exc:
                last_error = f"сетевая ошибка: {exc.__class__.__name__}: {exc}"
                print(f"       {last_error}")
            except ValueError as exc:
                last_error = f"некорректный ответ (не JSON): {exc}"
                print(f"       {last_error}")
        if attempt < rounds:
            pause = 5 * attempt
            print(f"    все серверы не ответили, повтор через {pause} с...")
            time.sleep(pause)
    raise OverpassError(last_error)


def build_query(bbox, timeout: int) -> str:
    """Один запрос за всеми нужными объектами в рамке города.
    `out center` даёт координаты точек и центры контуров (здания, парки)."""
    s, w, n, e = bbox
    b = f"({s:.5f},{w:.5f},{n:.5f},{e:.5f})"
    return f"""
[out:json][timeout:{timeout}];
(
  nwr["amenity"~"^(school|kindergarten|clinic|hospital)$"]{b};
  nwr["healthcare"~"^(clinic|hospital)$"]{b};
  node["highway"="bus_stop"]{b};
  nwr["public_transport"="platform"]["bus"="yes"]{b};
  nwr["public_transport"="platform"]["trolleybus"="yes"]{b};
  nwr["leisure"="park"]{b};
);
out center;
"""


# ---------------------------------------------------------------------------
# Классификация, дедупликация, подсчёт
# ---------------------------------------------------------------------------

def classify(tags: dict) -> list[str]:
    """К каким нашим категориям относится объект OSM."""
    cats = []
    amenity = tags.get("amenity", "")
    healthcare = tags.get("healthcare", "")
    if amenity == "school":
        cats.append("schools")
    if amenity == "kindergarten":
        cats.append("kindergartens")
    if amenity in ("clinic", "hospital") or healthcare in ("clinic", "hospital"):
        cats.append("clinics")
    if tags.get("highway") == "bus_stop" or (
        tags.get("public_transport") == "platform"
        and (tags.get("bus") == "yes" or tags.get("trolleybus") == "yes")
    ):
        cats.append("bus_stops")
    if tags.get("leisure") == "park":
        cats.append("parks")
    return cats


def element_coords(el: dict):
    """Координаты точки или центра контура; None, если их нет."""
    if "lat" in el and "lon" in el:
        return el["lon"], el["lat"]
    center = el.get("center")
    if center:
        return center["lon"], center["lat"]
    return None


def distance_m(lon1, lat1, lon2, lat2) -> float:
    """Расстояние в метрах (равнопромежуточное приближение — точно на малых дистанциях)."""
    kx = 111_320 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lon2 - lon1) * kx, (lat2 - lat1) * 110_570)


def normalize_name(name: str) -> str:
    return " ".join(name.lower().replace("ё", "е").split())


def collect_points(elements: list) -> tuple[list, int]:
    """Превращает элементы Overpass в точки по категориям и убирает дубли.
    Возвращает (точки, число удалённых дублей)."""
    kept = []
    seen_by_name = {}  # (категория, имя) -> список уже учтённых точек
    duplicates = 0
    for el in elements:
        tags = el.get("tags") or {}
        coords = element_coords(el)
        if coords is None:
            continue
        lon, lat = coords
        name = tags.get("name", "") or tags.get("name:ru", "") or tags.get("name:kk", "")
        for cat in classify(tags):
            key = (cat, normalize_name(name)) if name else None
            if key:
                if any(distance_m(lon, lat, p["lon"], p["lat"]) <= DEDUP_RADIUS_M[cat]
                       for p in seen_by_name.get(key, [])):
                    duplicates += 1
                    continue
            point = {"category": cat, "name": name, "lon": lon, "lat": lat,
                     "osm_type": el.get("type"), "osm_id": el.get("id")}
            kept.append(point)
            if key:
                seen_by_name.setdefault(key, []).append(point)
    return kept, duplicates


def aggregate(points: list, districts: dict):
    """Раскладывает точки по районам и считает количество по категориям."""
    counts = {did: {cat: 0 for cat in CATEGORIES} for did in DISTRICT_IDS}
    outside = {cat: 0 for cat in CATEGORIES}
    for p in points:
        did = find_district(p["lon"], p["lat"], districts)
        p["district"] = did
        if did is None:
            outside[p["category"]] += 1
        else:
            counts[did][p["category"]] += 1
    return counts, outside


# ---------------------------------------------------------------------------
# Запись результатов
# ---------------------------------------------------------------------------

def write_json(path: Path, obj):
    """Атомарная запись JSON (приложение не увидит полузаписанный файл)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def points_to_geojson(points: list) -> dict:
    """Точки объектов для справочного слоя на карте (только те, что внутри районов)."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(p["lon"], 6), round(p["lat"], 6)]},
                "properties": {
                    "category": p["category"], "name": p["name"], "district": p["district"],
                    "osm": f"{p['osm_type']}/{p['osm_id']}",
                },
            }
            for p in points if p.get("district")
        ],
    }


def make_meta(status: str, districts_source: str, **extra) -> dict:
    meta = {
        "status": status,  # "ok" или "unavailable" (тогда нули в real_context.json = «нет данных»)
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "OpenStreetMap через Overpass API",
        "attribution": "© OpenStreetMap contributors (ODbL)",
        "districts_source": districts_source,
        "usage": "Справочный слой. НЕ влияет на официальный расчёт индекса.",
        "category_tags": CATEGORY_TAGS,
        "dedup_radius_m": DEDUP_RADIUS_M,
    }
    if districts_source != "osm":
        meta["warning"] = ("Границы районов схематичные: распределение объектов "
                           "по районам приблизительное.")
    meta.update(extra)
    return meta


def handle_failure(reason: str, districts_source: str) -> int:
    """Сеть/Overpass недоступны: старые данные не трогаем; если их нет — пишем
    заглушку с нулями и статусом unavailable, чтобы приложение не падало."""
    if OUT_PATH.exists():
        print(f"[!] Оставляю предыдущий {OUT_PATH.relative_to(ROOT)} без изменений.")
        return 1
    zeros = {did: {cat: 0 for cat in CATEGORIES} for did in DISTRICT_IDS}
    write_json(OUT_PATH, zeros)
    write_json(META_PATH, make_meta("unavailable", districts_source, error=reason))
    print(f"[!] Записана заглушка с нулями: {OUT_PATH.relative_to(ROOT)}")
    print("    В meta status='unavailable' — не показывайте эти нули как реальные данные.")
    return 1


def print_table(counts: dict, outside: dict):
    header = f"{'район':<10}" + "".join(f"{c:>15}" for c in CATEGORIES)
    print(header)
    print("-" * len(header))
    for did in DISTRICT_IDS:
        print(f"{did:<10}" + "".join(f"{counts[did][c]:>15}" for c in CATEGORIES))
    print(f"{'вне районов':<10}" + "".join(f"{outside[c]:>15}" for c in CATEGORIES))


def parse_args():
    p = argparse.ArgumentParser(description="Реальные объекты инфраструктуры по районам Астаны (OSM)")
    p.add_argument("--timeout", type=int, default=180,
                   help="таймаут запроса к Overpass, с (по умолчанию 180)")
    p.add_argument("--max-time", type=int, default=420,
                   help="общий лимит времени на скачивание, с (по умолчанию 420)")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    # 1. Границы районов
    try:
        districts, districts_source = load_districts(DISTRICTS_PATH)
    except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"[x] Не удалось загрузить границы районов: {exc}")
        print("    Сначала запустите: python fetch_districts.py")
        return 1
    print(f"[*] Границы районов загружены (источник: {districts_source})")
    if districts_source != "osm":
        print("[!] Границы схематичные — распределение объектов по районам будет приблизительным.")

    # 2. Объекты из OSM
    bbox = query_bbox(districts)
    print(f"[*] Запрашиваю объекты в рамке {tuple(round(v, 3) for v in bbox)}...")
    try:
        if requests is None:
            raise OverpassError("не установлена библиотека requests (pip install requests)")
        data = overpass_query(build_query(bbox, args.timeout), args.timeout,
                              time.monotonic() + args.max_time)
    except OverpassError as exc:
        print(f"[!] Не удалось скачать данные: {exc}")
        return handle_failure(str(exc), districts_source)

    elements = data.get("elements", [])
    print(f"[*] Получено объектов OSM: {len(elements)}")

    # 3. Подсчёт
    try:
        points, duplicates = collect_points(elements)
        counts, outside = aggregate(points, districts)
    except Exception as exc:  # неожиданный формат данных — не валим приложение
        print(f"[!] Ошибка при обработке данных: {exc!r}")
        return handle_failure(f"ошибка обработки: {exc!r}", districts_source)

    # 4. Запись
    try:
        write_json(OUT_PATH, counts)
        write_json(META_PATH, make_meta(
            "ok", districts_source,
            osm_elements=len(elements), duplicates_removed=duplicates,
            outside_districts=outside,
        ))
        write_json(POINTS_PATH, points_to_geojson(points))
    except OSError as exc:
        print(f"[x] Не удалось записать результаты: {exc}")
        return 1

    print(f"[*] Убрано дублей (точка + контур одного объекта): {duplicates}\n")
    print_table(counts, outside)
    print(f"\n[ok] Сохранено: {OUT_PATH.relative_to(ROOT)}, {META_PATH.relative_to(ROOT)}, "
          f"{POINTS_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
