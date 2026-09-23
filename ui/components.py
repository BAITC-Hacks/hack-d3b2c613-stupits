"""Компоненты отображения: карта, профиль района и результаты."""

from __future__ import annotations

from copy import deepcopy
from html import escape
import json
import logging
from typing import Any

import pandas as pd
import pydeck as pdk
import streamlit as st

from ui.engine_adapter import ROOT, EngineAdapter


LOGGER = logging.getLogger(__name__)
MAP_LAYER = "astana-districts"


def number(value: float, signed: bool = False) -> str:
    return (f"{value:+.2f}" if signed else f"{value:.2f}").replace(".", ",")


def district_name(adapter: EngineAdapter, district: str | None) -> str:
    return adapter.districts.get(district, {}).get("name", "Весь город")


def effect_text(adapter: EngineAdapter, effects: dict) -> str:
    return "; ".join(
        f"{adapter.data['indicators'].get(key, {}).get('name', key)}: {value:+g}"
        for key, value in effects.items()
    )


def problems(adapter: EngineAdapter, values: dict) -> str:
    weakest = sorted(values.items(), key=lambda pair: pair[1])[:3]
    return "; ".join(
        f"{adapter.data['indicators'][key]['name']} — {number(value)}"
        for key, value in weakest
    )


def score_color(value: float) -> list[int]:
    """Шкала 0–50–100: красный, жёлтый, зелёный."""
    stops = ([214, 71, 73], [232, 178, 61], [28, 155, 114])
    position = max(0.0, min(100.0, value)) / 50
    start = min(int(position), 1)
    fraction = position - start
    return [round(stops[start][i] * (1 - fraction) + stops[start + 1][i] * fraction)
            for i in range(3)] + [205]


def load_geojson(adapter: EngineAdapter, result: dict) -> tuple[dict | None, str]:
    """Свойства добавляем в копию в памяти; файл карты никогда не меняем."""
    path = ROOT / "data" / "astana_districts.geojson"
    if not path.is_file():
        path = adapter.path.parent / "astana_districts.geojson"
    if not path.is_file():
        return None, "Карта районов пока недоступна. Выберите район на карточке ниже."
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if data.get("type") != "FeatureCollection":
            raise ValueError("Ожидается коллекция географических объектов")
        features = []
        for original in data["features"]:
            did = original.get("properties", {}).get("id")
            if did not in adapter.districts:
                continue
            geometry = original.get("geometry") or {}
            if geometry.get("type") not in ("Polygon", "MultiPolygon") or not geometry.get("coordinates"):
                raise ValueError("У района отсутствует полигон")
            feature = deepcopy(original)
            row = result["districts"][did]
            feature["properties"].update({
                "name": escape(adapter.districts[did]["name"]),
                "rating": number(row["D_after"]),
                "problems": escape(problems(adapter, row["after"])),
                "fill_color": score_color(row["D_after"]),
                "elevation": max(0, row["D_after"]) * 35,
            })
            features.append(feature)
        if {f["properties"]["id"] for f in features} != set(adapter.districts):
            raise ValueError("В карте отсутствуют районы города")
        return {"type": "FeatureCollection", "features": features}, ""
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        LOGGER.exception("Не удалось прочитать карту районов")
        return None, "Не удалось прочитать карту. Районы доступны на карточках ниже."


def _select_on_map() -> None:
    # Callback выполняется до отрисовки selectbox, поэтому его состояние можно менять.
    event = st.session_state.get("district_map", {})
    objects = event.get("selection", {}).get("objects", {}).get(MAP_LAYER, [])
    if objects:
        did = objects[0].get("properties", {}).get("id")
        if did in st.session_state.get("district_ids", []):
            st.session_state["selected_district"] = did


def render_map(adapter: EngineAdapter, result: dict) -> None:
    st.subheader("Город по районам")
    geojson, notice = load_geojson(adapter, result)
    if geojson is None:
        st.info(notice)
        ids = list(adapter.districts)
        for start in range(0, len(ids), 2):
            columns = st.columns(2)
            for column, did in zip(columns, ids[start:start + 2]):
                with column, st.container(border=True):
                    row = result["districts"][did]
                    st.metric(adapter.districts[did]["name"], number(row["D_after"]),
                               delta=number(row["D_delta"], True) if row.get("D_delta") is not None else None)
                    st.button("Открыть район", key=f"district_card_{did}",
                              on_click=lambda value=did: st.session_state.update(selected_district=value),
                              width="stretch")
        return
    extruded = st.toggle("Объёмная карта", value=True, key="extruded_map")
    layer = pdk.Layer(
        "GeoJsonLayer", data=geojson, id=MAP_LAYER, pickable=True,
        auto_highlight=True, filled=True, stroked=True, extruded=extruded,
        get_fill_color="properties.fill_color", get_elevation="properties.elevation",
        get_line_color=[255, 255, 255], line_width_min_pixels=1,
    )
    deck = pdk.Deck(
        layers=[layer], map_style="light", map_provider="carto",
        initial_view_state=pdk.ViewState(latitude=51.16, longitude=71.44, zoom=9.5,
                                         pitch=40 if extruded else 0, bearing=-10),
        tooltip={"html": "<b>{name}</b><br/>Оценка района D: {rating}<br/>Слабые показатели:<br/>{problems}",
                 "style": {"maxWidth": "350px", "whiteSpace": "normal"}},
    )
    st.pydeck_chart(deck, key="district_map", height=460, on_select=_select_on_map,
                    selection_mode="single-object", width="stretch")
    st.caption("Оценка D: красный — 0 · жёлтый — 50 · зелёный — 100. Нажмите на район, чтобы открыть профиль.")
    if any(feature["properties"].get("source") == "fallback_approx" for feature in geojson["features"]):
        st.caption("Границы районов на этой карте схематичны.")


def render_profile(adapter: EngineAdapter, result: dict) -> None:
    st.subheader("Профиль района")
    did = st.selectbox("Район для просмотра", list(adapter.districts), key="selected_district",
                       format_func=lambda value: district_name(adapter, value))
    st.caption("Этот выбор меняет только профиль. Район реализации указывается в карточке каждого проекта.")
    source, row = adapter.districts[did], result["districts"][did]
    st.write(source["profile"])
    st.caption(f"Доля населения города: {source['population_share']:.0%} · Оценка D: {number(row['D_after'])}")
    for indicator, definition in adapter.data["indicators"].items():
        value = row["after"][indicator]
        severity, label = ("critical", "Критично") if value < adapter.data["crit_threshold"] else (
            ("warning", "Требует внимания") if value < 50 else ("healthy", "В норме"))
        delta = row.get("indicator_deltas", {}).get(indicator)
        change = f" · {number(delta, True)}" if delta else ""
        st.markdown(
            f'<div class="indicator-row {severity}"><div><span class="indicator-name">'
            f'{escape(definition["name"])}</span><small>{label}</small></div>'
            f'<strong>{number(value)}<small>{change}</small></strong></div>', unsafe_allow_html=True,
        )
    threshold = adapter.data["crit_threshold"]
    st.caption(f"Шкала 0–100: чем выше, тем лучше. Ниже {threshold:g} — критично; от {threshold:g} до 50 — требует внимания.")


def _critical_table(adapter: EngineAdapter, entries: list[dict], result: dict) -> pd.DataFrame:
    return pd.DataFrame([
        {"Район": district_name(adapter, entry["district"]),
         "Показатель": adapter.data["indicators"][entry["indicator"]]["name"],
         "Было": result["districts"][entry["district"]]["before"][entry["indicator"]],
         "Стало": result["districts"][entry["district"]]["after"][entry["indicator"]]}
        for entry in entries
    ])


def _render_synergies(adapter: EngineAdapter, synergies: Any) -> None:
    if not synergies:
        st.caption("Синергии не сработали.")
        return
    items = list(synergies.values()) if isinstance(synergies, dict) else synergies
    for item in items:
        if isinstance(item, str):
            st.write(item)
            continue
        pair = item.get("pair", item.get("measures", []))
        title = " + ".join(adapter.measures.get(mid, {}).get("name", str(mid)) for mid in pair)
        st.write(title or item.get("name", "Совместный эффект проектов"))
        effects = item.get("bonus", item.get("effects", {}))
        if effects:
            st.caption(effect_text(adapter, effects))
        district = item.get("district")
        if district:
            st.caption(district_name(adapter, district))


def _render_contributions(adapter: EngineAdapter, contributions: Any) -> None:
    if not contributions:
        st.caption("Движок пока не передал вклад отдельных проектов.")
        return
    # Поддерживаем список записей и словарь вида {M7: {...}}.
    if isinstance(contributions, dict):
        contributions = [dict(value, measure=key) if isinstance(value, dict)
                         else {"measure": key, "score_delta": value}
                         for key, value in contributions.items()]
    rows = []
    for contribution in contributions:
        mid = contribution.get("measure", contribution.get("id", ""))
        row = {"Проект": adapter.measures.get(mid, {}).get("name", mid),
               "Район": district_name(adapter, contribution.get("district"))}
        for key in ("score_delta", "delta_score", "contribution", "delta"):
            if isinstance(contribution.get(key), (int, float)):
                row["Вклад в оценку"] = float(contribution[key])
                break
        effects = contribution.get("effects", contribution.get("effective_effects", {}))
        if effects and all(isinstance(value, (int, float)) for value in effects.values()):
            row["Изменение показателей"] = effect_text(adapter, effects)
        if contribution.get("description"):
            row["Пояснение"] = contribution["description"]
        rows.append(row)
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption("Вклад — разница между оценкой полного плана и оценкой без этой меры, включая её синергии. "
               "Сумма вкладов может отличаться от общего прироста оценки.")


def render_results(adapter: EngineAdapter, baseline: dict, result: dict | None) -> None:
    st.subheader("Результат вашего плана")
    if result is None:
        st.info(f"Выберите {adapter.required} проектов и рассчитайте результат. Здесь появится сравнение с исходным состоянием города.")
        return
    metrics = st.columns(3)
    metrics[0].metric("Итоговая оценка города", number(result["score"]),
                      number(result["score_delta"], True) if result["score_delta"] is not None else None)
    metrics[1].metric("Стоимость плана", f"{result['cost']:g} у.е.")
    metrics[2].metric("Критических показателей", len(result["critical_after"]),
                      len(result["critical_after"]) - len(result["critical_before"]), delta_color="inverse")
    st.caption("Изменение оценки передано движком и рассчитано до округления итоговых значений.")
    frame = pd.DataFrame([
        {"Район": district_name(adapter, did), "Было": row["D_before"],
          "Стало": row["D_after"], "Изменение": row.get("D_delta")}
        for did, row in result["districts"].items()
    ])
    st.dataframe(frame.style.format({"Было": "{:.2f}", "Стало": "{:.2f}", "Изменение": "{:+.2f}"}, na_rep="—"),
                 hide_index=True, width="stretch")
    st.bar_chart(frame, x="Район", y=["Было", "Стало"], stack=False,
                 color=["#9caec2", "#168b78"], y_label="Оценка района D")
    st.markdown("#### Вышли из критической зоны")
    if result["resolved_critical"]:
        st.dataframe(_critical_table(adapter, result["resolved_critical"], result),
                     hide_index=True, width="stretch")
    else:
        st.caption("Ни один показатель не пересёк границу критической зоны вверх.")
    if result["critical_after"]:
        with st.expander("Какие показатели остаются критическими"):
            st.dataframe(_critical_table(adapter, result["critical_after"], result),
                         hide_index=True, width="stretch")
    st.markdown("#### Сработавшие синергии")
    _render_synergies(adapter, result["synergies"])
    st.markdown("#### Вклад каждого проекта")
    _render_contributions(adapter, result["contributions"])
