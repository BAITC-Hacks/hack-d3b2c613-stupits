"""
Общие помощники отчётов движка: округление, самый слабый район, подстановка
чисел в формулу, синергии, критические значения, описание события.

Всё здесь возвращает JSON-сериализуемые значения (float, int, str, list, dict).
"""

from __future__ import annotations

from .data import CityData
from .model import Evaluation


def r2(x) -> float:
    """Округление до 2 знаков для отчёта; «+ 0.0» превращает -0.0 в 0.0."""
    return round(float(x), 2) + 0.0


def plural(n: int, one: str, few: str, many: str) -> str:
    """Согласование с числом: 1 мера, 2 меры, 5 мер."""
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def placement_label(data: CityData, mid: str, did: str | None) -> str:
    """«M7 (Нура)», «M12 (весь город)» — короткая подпись решения."""
    if did is None and not data.is_city(mid):
        return f"{mid} (район не указан)"
    return f"{mid} ({data.district_name(did)})"


def min_district(data: CityData, ev: Evaluation) -> dict:
    """Самый слабый район сценария."""
    did = data.district_ids[ev.min_index]
    return {"id": did, "name": data.districts[did]["name"], "D": r2(ev.min_D)}


def score_breakdown(data: CityData, ev: Evaluation) -> str:
    """Подстановка чисел в формулу Score — чтобы объяснение было проверяемым."""
    return (
        f"{data.w_avg:g} * {ev.D_avg:.2f} + {data.w_min:g} * {ev.min_D:.2f} "
        f"- {data.crit_penalty:g} * {ev.n_crit} = {ev.score:.2f}"
    )


def synergy_list(data: CityData, ev: Evaluation) -> list:
    """Сработавшие синергии: пара мер, бонус и район, где он применён."""
    return [
        {
            "pair": list(s["synergy"]["pair"]),
            "bonus": dict(s["synergy"]["bonus"]),
            "district": s["district"],
            "district_name": data.district_name(s["district"]),
            "note": s["synergy"].get("note", ""),
        }
        for s in ev.synergies
    ]


def critical_list(data: CityData, values) -> list:
    """Все пары (район, показатель) со значением строго ниже порога."""
    return [
        {
            "district": did,
            "district_name": data.districts[did]["name"],
            "indicator": k,
            "indicator_name": data.indicators[k]["name"],
            "value": r2(values[i, j]),
        }
        for i, did in enumerate(data.district_ids)
        for j, k in enumerate(data.indicator_ids)
        if values[i, j] < data.crit_threshold
    ]


def event_report(data: CityData) -> dict | None:
    """Описание события, которое применено к данным (None — события нет)."""
    event = data.event
    if event is None:
        return None
    effects = event.get("effects", {})
    return {
        "id": event["id"],
        "name": event["name"],
        "description": event.get("description", ""),
        "district": event.get("district"),
        "district_name": data.district_name(event.get("district")),
        "effects": dict(effects),
        "effects_text": ", ".join(f"{data.indicators[k]['name']} {v:+g}" for k, v in effects.items())
        or "показатели не меняются",
        "budget_change": event.get("budget_change", 0),
        "budget": data.budget,
        "hint": event.get("hint", ""),
    }
