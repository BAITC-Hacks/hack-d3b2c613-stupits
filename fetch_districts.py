#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_districts.py

Скачивает реальные OSM-границы шести районов Астаны, включая Сарайшык,
из OpenStreetMap через Overpass API и сохраняет data/astana_districts.geojson.

У каждого района в результате есть свойства:
    id    — esil, almaty, saryarka, baikonur, nura, saraishyk
    name  — название по-русски
и служебные: name_full, source, osm_id, osm_names, area_km2, center_lon, center_lat
(center_* удобно использовать для подписей районов в pydeck TextLayer).

При ошибке скачивания существующий GeoJSON сохраняется. Схематичные границы
не создаются. OSM — общественная карта, а не юридически заверенные границы.
Сарайшык присутствует на карте, но не входит в расчётную модель из ТЗ.

Данные OSM: © участники OpenStreetMap, лицензия ODbL — укажите это на карте.

Запуск:
    python -B fetch_districts.py             # обновить из OSM
    python -B fetch_districts.py --offline   # собрать из сохранённого ответа OSM
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import unicodedata
from itertools import combinations
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:  # для --offline сеть и requests не нужны
    requests = None


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

# Скрипт лежит в корне проекта, поэтому результаты не зависят от текущей
# рабочей директории.
ROOT = Path(__file__).resolve().parent
OUT_PATH = ROOT / "data" / "astana_districts.geojson"
CACHE_PATH = ROOT / "data" / "geo_sources" / "astana_districts_overpass.json"

# Публичные серверы Overpass API. Пробуем по очереди: основной часто перегружен.
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
USER_AGENT = "akim-5h-simulator/1.0 (public district boundary downloader)"

# Рамка вокруг Астаны: (юг, запад, север, восток). Нужна для запроса по рамке
# и для проверки, что найденная граница действительно в Астане.
ASTANA_BBOX = (50.90, 71.0, 51.5, 72.0)

# Наши районы. keywords — корни названий, по которым ищем совпадения в тегах OSM.
# В OSM районы могут называться по-казахски («Есіл ауданы», «Байқоңыр ауданы»),
# по-русски («район Есиль», «Есильский район», «район Байконур»),
# по-английски («Yesil District», «Baikonur District») или казахской латиницей
# («Saryarqa audany», «Baiqoñyr audany»). Перед сравнением всё нормализуется:
# казахские буквы -> русские (і->и, қ->к, ң->н, ұ->у ...), убираются диакритики.
DISTRICTS = {
    "esil": {
        "name": "Есиль",
        "name_full": "район Есиль",
        "keywords": ["есил", "esil", "yesil", "yessil", "jesil"],
    },
    "almaty": {
        "name": "Алматы",
        "name_full": "район Алматы",
        "keywords": ["алмат", "almat"],
    },
    "saryarka": {
        "name": "Сарыарка",
        "name_full": "район Сарыарка",
        "keywords": ["сарыарк", "saryark", "saryarq"],
    },
    "baikonur": {
        "name": "Байконур",
        "name_full": "район Байконур",
        "keywords": ["байкон", "baikon", "baykon", "baiqon", "bayqon"],
    },
    "nura": {
        "name": "Нура",
        "name_full": "район Нура",
        "keywords": ["нура", "nura"],
    },
    "saraishyk": {
        "name": "Сарайшык",
        "name_full": "район Сарайшык",
        "keywords": ["сарайшык", "saraishyk", "saray", "saraishy", "saraish", "saraisy"],
    },
}
DISTRICT_ORDER = list(DISTRICTS.keys())
# Явные идентификаторы исключают одноимённые районы области и старые дубли.
# У Сарайшыка в OSM admin_level=8, у остальных =6; это теги источника.
RELATION_IDS = {
    "esil": 3479876, "almaty": 3482819, "saryarka": 3486954,
    "baikonur": 8593081, "nura": 20593940, "saraishyk": 19733918,
}

# Теги с названиями, которые проверяем (old_name сознательно не берём:
# старые названия могут указывать на соседний район).
NAME_TAGS = [
    "name", "name:ru", "name:kk", "name:en", "name:kk-Latn",
    "official_name", "official_name:ru", "official_name:kk",
    "alt_name", "int_name",
]


# ---------------------------------------------------------------------------
# Нормализация и сопоставление названий
# ---------------------------------------------------------------------------

# Казахские буквы, у которых нет разложения в Unicode, переводим в русские вручную.
_KAZAKH_TO_RUSSIAN = str.maketrans({
    "і": "и", "қ": "к", "ң": "н", "ғ": "г", "ү": "у", "ұ": "у",
    "ө": "о", "ә": "а", "һ": "х", "ı": "i",
})


def normalize(text: str) -> str:
    """Нижний регистр, казахские буквы -> русские, без диакритик (ñ->n, ё->е, й->и)."""
    text = text.lower().translate(_KAZAKH_TO_RUSSIAN)
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


# Ключевые слова нормализуем тем же способом, что и названия из OSM.
_KEYWORDS_NORM = {
    did: [normalize(k) for k in info["keywords"]] for did, info in DISTRICTS.items()
}


def match_district_ids(tags: dict) -> set[str]:
    """Возвращает множество наших id, которым соответствуют названия в тегах."""
    matched = set()
    for key in NAME_TAGS:
        value = tags.get(key)
        if not value:
            continue
        norm = normalize(value)
        tokens = re.findall(r"\w+", norm)
        compact = "".join(tokens)  # «Сары-Арка» -> «сарыарка»
        for did, keywords in _KEYWORDS_NORM.items():
            for kw in keywords:
                # Слово начинается с корня («есильский» -> «есил»),
                # либо длинный корень встречается в склеенной строке.
                if any(t.startswith(kw) for t in tokens) or (len(kw) >= 6 and kw in compact):
                    matched.add(did)
    return matched


# ---------------------------------------------------------------------------
# Геометрия (чистый Python, без shapely)
# ---------------------------------------------------------------------------

def ring_signed_area(ring) -> float:
    """Площадь кольца в «квадратных градусах» со знаком (>0 — против часовой)."""
    s = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        s += x1 * y2 - x2 * y1
    return s / 2.0


def orient(ring, ccw: bool):
    """Внешние кольца — против часовой стрелки, дыры — по часовой (RFC 7946)."""
    is_ccw = ring_signed_area(ring) > 0
    return ring if is_ccw == ccw else list(reversed(ring))


def point_in_ring(pt, ring) -> bool:
    """Классический ray casting: лежит ли точка внутри кольца."""
    x, y = pt
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y):
            x_cross = xi + (y - yi) * (xj - xi) / (yj - yi)
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def point_in_geometry(pt, geometry: dict) -> bool:
    """Точка внутри Polygon/MultiPolygon с учётом дыр."""
    polys = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    for poly in polys:
        if point_in_ring(pt, poly[0]) and not any(point_in_ring(pt, h) for h in poly[1:]):
            return True
    return False


def ring_centroid(ring):
    """Центр тяжести кольца; для вырожденных колец — среднее по вершинам."""
    a = ring_signed_area(ring)
    if abs(a) < 1e-12:
        xs, ys = zip(*ring)
        return sum(xs) / len(xs), sum(ys) / len(ys)
    cx = cy = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        f = x1 * y2 - x2 * y1
        cx += (x1 + x2) * f
        cy += (y1 + y2) * f
    return cx / (6 * a), cy / (6 * a)


def geometry_stats(geometry: dict):
    """Приблизительная площадь в км² и центр самого большого полигона."""
    polys = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    biggest = max(polys, key=lambda p: abs(ring_signed_area(p[0])))
    center = ring_centroid(biggest[0])
    # Перевод «квадратных градусов» в км² на широте центра.
    kx = 111.32 * math.cos(math.radians(center[1]))
    ky = 110.57
    area_deg2 = sum(
        abs(ring_signed_area(p[0])) - sum(abs(ring_signed_area(h)) for h in p[1:])
        for p in polys
    )
    return area_deg2 * kx * ky, center


def assemble_rings(ways):
    """
    Склеивает линии (way) границы в замкнутые кольца.
    В OSM граница района — это отношение из многих кусков-way, которые
    идут в произвольном порядке и направлении; их надо сшить по общим точкам.
    Возвращает (список колец, число кусков, которые не удалось замкнуть).
    """
    pool = [list(w) for w in ways if len(w) >= 2]
    rings, broken = [], 0
    while pool:
        current = pool.pop(0)
        while current[0] != current[-1]:
            attached = False
            for i, w in enumerate(pool):
                if w[0] == current[-1]:
                    current = current + w[1:]
                elif w[-1] == current[-1]:
                    current = current + w[-2::-1]
                elif w[-1] == current[0]:
                    current = w[:-1] + current
                elif w[0] == current[0]:
                    current = w[:0:-1] + current
                else:
                    continue
                pool.pop(i)
                attached = True
                break
            if not attached:
                break
        # Не дорисовываем даже небольшие разрывы: геометрия должна быть из OSM.
        if current[0] == current[-1] and len(current) >= 4:
            rings.append(current)
        else:
            broken += 1
    return rings, broken


def relation_to_geometry(element: dict):
    """Собирает GeoJSON-геометрию из отношения Overpass (вывод `out geom`)."""
    outer_ways, inner_ways = [], []
    for m in element.get("members", []):
        if m.get("type") != "way":
            continue
        if m.get("role") not in ("outer", "inner", ""):
            continue
        if not m.get("geometry"):
            return None, 1
        if any(p is None for p in m["geometry"]):
            return None, 1
        coords = [(p["lon"], p["lat"]) for p in m["geometry"]]
        (inner_ways if m.get("role") == "inner" else outer_ways).append(coords)

    outers, broken_outer = assemble_rings(outer_ways)
    inners, broken_inner = assemble_rings(inner_ways)
    if not outers or broken_outer or broken_inner:
        return None, broken_outer + broken_inner

    # Самые большие внешние кольца — первыми; каждой дыре находим её внешнее кольцо.
    outers.sort(key=lambda r: abs(ring_signed_area(r)), reverse=True)
    polygons = [[orient(r, ccw=True)] for r in outers]
    for hole in inners:
        for poly in polygons:
            if point_in_ring(hole[0], poly[0]):
                poly.append(orient(hole, ccw=False))
                break
        else:
            return None, 1

    # Сохраняем исходную точность OSM; не упрощаем и не сдвигаем вершины.
    coordinates = [[[[x, y] for x, y in ring] for ring in poly] for poly in polygons]
    if len(coordinates) == 1:
        return {"type": "Polygon", "coordinates": coordinates[0]}, broken_outer
    return {"type": "MultiPolygon", "coordinates": coordinates}, broken_outer


# ---------------------------------------------------------------------------
# Overpass API
# ---------------------------------------------------------------------------

class OverpassError(RuntimeError):
    """Не удалось получить ответ ни от одного сервера Overpass."""


def overpass_query(query: str, timeout: int, deadline: float, rounds: int = 2) -> dict:
    """Отправляет запрос на серверы Overpass по очереди, с повторами и общим лимитом времени."""
    last_error = "неизвестная ошибка"
    for attempt in range(1, rounds + 1):
        for url in OVERPASS_URLS:
            remaining = deadline - time.monotonic()
            if remaining <= 5:
                raise OverpassError(f"превышен общий лимит времени; последняя ошибка: {last_error}")
            try:
                print(f"    -> {url}")
                resp = requests.get(
                    url,
                    params={"data": query},
                    headers={"User-Agent": USER_AGENT},
                    timeout=(10, min(timeout, remaining)),
                )
                if resp.status_code in (429, 502, 503, 504):
                    last_error = f"HTTP {resp.status_code} (сервер перегружен или лимит запросов)"
                    print(f"       {last_error}")
                    continue
                resp.raise_for_status()
                data = resp.json()
                # Overpass иногда отвечает 200, но с ошибкой выполнения в поле remark.
                remark = data.get("remark", "")
                if remark and "error" in remark.lower() and not data.get("elements"):
                    last_error = f"ошибка Overpass: {remark}"
                    print(f"       {last_error}")
                    continue
                if remark:
                    raise ValueError("Overpass вернул неполный ответ: " + remark)
                data["_provenance"] = {
                    "source_url": resp.url,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "relation_ids": list(RELATION_IDS.values()),
                }
                return data
            except requests.exceptions.RequestException as exc:
                last_error = f"сетевая ошибка: {exc.__class__.__name__}: {exc}"
                print(f"       {last_error}")
            except ValueError as exc:  # ответ не JSON
                last_error = f"некорректный ответ (не JSON): {exc}"
                print(f"       {last_error}")
        if attempt < rounds:
            pause = 5 * attempt
            print(f"    все серверы не ответили, повтор через {pause} с...")
            time.sleep(pause)
    raise OverpassError(last_error)


def in_astana_bbox(lon: float, lat: float) -> bool:
    s, w, n, e = ASTANA_BBOX
    return s <= lat <= n and w <= lon <= e


def describe_tags(tags: dict) -> str:
    """Короткая строка с названиями для лога."""
    parts = [f"{k}='{tags[k]}'" for k in ("name", "name:ru", "name:kk", "name:en") if tags.get(k)]
    return ", ".join(parts) or "(без названия)"


def fetch_from_osm(timeout: int, max_time: int):
    """Получает именно шесть отношений; кэш обновляется только после проверок."""
    deadline = time.monotonic() + max_time
    ids = ",".join(str(value) for value in RELATION_IDS.values())
    query = f"[out:json][timeout:{min(timeout, 90)}];rel(id:{ids});out meta geom;"
    data = overpass_query(query, timeout, deadline)
    collection = collection_from_osm(data)
    write_geojson(CACHE_PATH, data)
    return collection


def collection_from_osm(data):
    """Повторяемая сборка из сохранённого сырого ответа Overpass."""
    provenance = data.get("_provenance", {})
    if not provenance.get("retrieved_at") or not provenance.get("source_url"):
        raise ValueError("У ответа OSM нет метаданных источника и времени получения")
    relations = {e["id"]: e for e in data.get("elements", []) if e.get("type") == "relation"}
    features = []
    for did, relation_id in RELATION_IDS.items():
        element = relations.get(relation_id)
        if not element:
            raise ValueError(f"В ответе OSM отсутствует {did}: relation {relation_id}")
        tags = element.get("tags", {})
        if tags.get("boundary") != "administrative" or did not in match_district_ids(tags):
            raise ValueError(f"Неожиданные теги района {did}: relation {relation_id}")
        geometry, broken = relation_to_geometry(element)
        if geometry is None or broken:
            raise ValueError(f"Незамкнутая или неполная граница {did}; файл не обновлён")
        features.append(make_feature(
            did, geometry, source="osm", osm_id=relation_id,
            osm_names={k: tags[k] for k in NAME_TAGS if tags.get(k)},
            retrieved_at=provenance["retrieved_at"],
            osm_timestamp=element.get("timestamp"), osm_version=element.get("version"),
            osm_admin_level=tags.get("admin_level"),
        ))
    validation = check_overlaps(features)
    collection = make_collection(features, source="osm")
    collection["metadata"].update({
        "source_url": provenance["source_url"],
        "retrieved_at": provenance["retrieved_at"],
        "osm_base_timestamp": data.get("osm3s", {}).get("timestamp_osm_base"),
        "validation": validation,
    })
    return collection


def check_overlaps(features):
    """Базовые проверки всегда; полная топология и пересечения — с Shapely."""
    for feature in features:
        geometry = feature["geometry"]
        polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
        for polygon in polygons:
            for ring in polygon:
                if len(ring) < 4 or ring[0] != ring[-1] or abs(ring_signed_area(ring)) < 1e-12:
                    raise ValueError("Вырожденное или открытое кольцо " + feature["id"])
                if not all(math.isfinite(x) and math.isfinite(y) and in_astana_bbox(x, y) for x, y in ring):
                    raise ValueError("Координаты вне Астаны: " + feature["id"])
    try:
        from shapely.geometry import shape
        from shapely.validation import explain_validity
    except ImportError:
        print("[!] Shapely не установлен: проверены кольца и координаты, но не полная топология.")
        return {"rings_closed": True, "coordinates_checked": True, "topology_checked": False}
    geometries = {f["id"]: shape(f["geometry"]) for f in features}
    for did, geometry in geometries.items():
        if not geometry.is_valid:
            raise ValueError(f"Невалидная геометрия {did}: {explain_validity(geometry)}")
    for (a, geom_a), (b, geom_b) in combinations(geometries.items(), 2):
        # Общая линия допустима, положительная площадь пересечения — ошибка.
        if geom_a.intersection(geom_b).area > 1e-12:
            raise ValueError(f"Районы {a} и {b} перекрываются; файл не обновлён")
    return {"rings_closed": True, "coordinates_checked": True, "topology_checked": True,
            "all_geometries_valid": True, "overlapping_pairs": 0, "checked_pairs": 15}


# ---------------------------------------------------------------------------
# Сборка и запись GeoJSON
# ---------------------------------------------------------------------------

def make_feature(did, geometry, source, osm_id=None, osm_names=None, **provenance):
    info = DISTRICTS[did]
    area_km2, (clon, clat) = geometry_stats(geometry)
    return {
        "type": "Feature",
        "id": did,
        "properties": {
            "id": did,
            "name": info["name"],
            "name_full": info["name_full"],
            "source": source,
            "osm_id": osm_id,
            "source_url": f"https://www.openstreetmap.org/relation/{osm_id}",
            "scoring_enabled": did != "saraishyk",
            "geometry_status": "osm_community_boundary",
            "osm_names": osm_names or {},
            "area_km2": round(area_km2, 1),
            "area_method": "approximate_planar_area_at_centroid_latitude",
            "center_lon": round(clon, 5),
            "center_lat": round(clat, 5),
            **provenance,
        },
        "geometry": geometry,
    }


def make_collection(features, source):
    if source != "osm":
        raise ValueError("Допускаются только реальные границы OSM")
    attribution = "© OpenStreetMap contributors (ODbL)"
    note = ("Реальные полигоны административных отношений OpenStreetMap. "
            "Общественный источник, не официальное юридическое описание границ. "
            "Сарайшык показан географически, но отсутствует в расчётном наборе ТЗ.")
    return {
        "type": "FeatureCollection",
        "metadata": {
            "source": source,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "attribution": attribution,
            "license_url": "https://www.openstreetmap.org/copyright",
            "note": note,
        },
        "features": features,
    }


def write_geojson(path: Path, collection: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(collection, f, ensure_ascii=False)
    tmp.replace(path)  # атомарная замена: карта не увидит полузаписанный файл


def parse_args():
    p = argparse.ArgumentParser(description="Границы районов Астаны из OpenStreetMap")
    p.add_argument("--offline", action="store_true",
                   help="не ходить в сеть; использовать сохранённый сырой ответ OSM")
    p.add_argument("--source-json", type=Path,
                   help="собрать GeoJSON из указанного ответа Overpass с _provenance")
    p.add_argument("--timeout", type=int, default=120,
                   help="таймаут одного запроса к Overpass, с (по умолчанию 120)")
    p.add_argument("--max-time", type=int, default=300,
                   help="общий лимит времени на скачивание, с (по умолчанию 300)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    collection = None

    try:
        if args.offline or args.source_json:
            source_path = args.source_json or CACHE_PATH
            collection = collection_from_osm(json.loads(source_path.read_text(encoding="utf-8")))
        elif requests is None:
            raise RuntimeError("Для скачивания установите requests либо используйте --offline")
        else:
            collection = fetch_from_osm(args.timeout, args.max_time)
    except Exception as exc:
        print(f"[x] Обновление отменено: {exc}. Существующий GeoJSON не изменён.")
        return 1

    try:
        write_geojson(OUT_PATH, collection)
    except OSError as exc:
        print(f"[x] Не удалось записать {OUT_PATH}: {exc}")
        return 1

    src = collection["metadata"]["source"]
    print(f"\n[ok] Сохранено: {OUT_PATH.relative_to(ROOT)}  (источник: {src})")
    for feat in collection["features"]:
        p = feat["properties"]
        print(f"     {p['id']:<9} {p['name']:<9} ~{p['area_km2']:>6.1f} км²  "
              f"центр {p['center_lat']:.4f}, {p['center_lon']:.4f}")
    return 0


if __name__ == "__main__":
    # Русский журнал и знак км² должны работать и при перенаправлении в Windows.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    sys.exit(main())
