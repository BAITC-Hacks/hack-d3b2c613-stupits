"""
Городские события — опциональная фича ТЗ («моделирование неожиданных
городских событий, требующих перераспределения бюджета»).

Событие меняет стартовые условия ДО применения мер:
  * ухудшает исходные показатели — в одном районе (district) или во всём
    городе (district = null). Эффект события лагом не масштабируется:
    событие уже случилось;
  * и/или меняет бюджет (budget_change, например секвестр −15 у.е.).

Дальше расчёт идёт по обычным формулам ТЗ, только от новой точки старта:
    I_event = clip(I + эффект события, 0, 100)
    I'      = clip(I_event + Σ эффект меры × (8 − L)/8 + синергии, 0, 100)

Без event_id движок работает ровно как раньше: официальный Score
считается без событий. События читаются из data/events.json.
"""

from __future__ import annotations

import copy

import numpy as np

from .data import SCALE_MAX, SCALE_MIN, CityData, get_data
from .model import evaluate
from .report import critical_list, event_report, r2


def with_event(data: CityData, event_id: str | None) -> tuple[CityData, list]:
    """Данные «после события» и список ошибок.

    Без event_id возвращает те же данные. Неизвестное событие — ошибка
    в списке, а не исключение: её можно показать пользователю или ИИ-советнику.
    """
    if event_id is None:
        return data, []
    event = data.events.get(str(event_id).strip().upper())
    if event is None:
        known = ", ".join(data.events) or "нет (не найден файл data/events.json)"
        return data, [f"Неизвестное событие «{event_id}». Доступные события: {known}."]
    return apply_event(data, event), []


def apply_event(data: CityData, event: dict) -> CityData:
    """Копия данных, в которой событие уже изменило исходные показатели и бюджет.

    Копия поверхностная: справочники и эффекты мер общие, заменяются только
    стартовая матрица base и бюджет. Исходные данные не меняются.
    """
    shocked = copy.copy(data)
    shock = np.outer(data.district_mask(event.get("district")), data.vector(event.get("effects", {})))
    shocked.base = np.clip(data.base + shock, SCALE_MIN, SCALE_MAX)
    shocked.budget = max(0, data.budget + event.get("budget_change", 0))
    shocked.event = event
    shocked.official = data.official
    return shocked


def resolve_events(data: CityData, event_ids=None) -> tuple[list, list]:
    """Какие события анализировать: по умолчанию все. Возвращает (id событий, ошибки)."""
    if event_ids is None:
        ids = list(data.events)
        return ids, ([] if ids else ["Событий нет: не найден файл data/events.json."])
    if isinstance(event_ids, str):
        event_ids = [event_ids]
    ids, errors = [], []
    for raw in event_ids:
        event_id = str(raw).strip().upper()
        if event_id not in data.events:
            errors.append(f"Неизвестное событие «{raw}». Доступные события: {', '.join(data.events)}.")
        elif event_id not in ids:
            ids.append(event_id)
    if not ids and not errors:
        errors.append("Не выбрано ни одного события.")
    return ids, errors


def list_events(data: CityData | None = None) -> dict:
    """Каталог городских событий и то, как каждое бьёт по городу без мер."""
    data = data or get_data()
    official = evaluate(data, [])
    official_critical = {(c["district"], c["indicator"]) for c in critical_list(data, official.values)}
    events = []
    for event_id in data.events:
        shocked, _ = with_event(data, event_id)
        after = evaluate(shocked, [])
        events.append({
            **event_report(shocked),
            "baseline_score": r2(after.score),
            "baseline_delta": r2(after.score - official.score),
            "new_critical": [
                c for c in critical_list(shocked, after.values)
                if (c["district"], c["indicator"]) not in official_critical
            ],
        })
    return {
        "events": events,
        "count": len(events),
        "baseline_score": r2(official.score),
        "budget": data.budget,
        "notes": [
            "Событие меняет стартовые условия до применения мер: ухудшает исходные показатели "
            "(в одном районе или во всём городе) и/или меняет бюджет. Эффект события лагом не масштабируется.",
            "baseline_score — Score города без мер сразу после события, baseline_delta — изменение "
            "относительно официальной базы, new_critical — показатели, которые из-за события упали ниже порога.",
            "Официальный Score считается без событий; события — сценарии «что если» для проверки устойчивости плана.",
        ],
    }
