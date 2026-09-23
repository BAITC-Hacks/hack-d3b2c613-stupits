"""
Устойчивость плана к городским событиям — оригинальная фича проекта.

Хороший аким выбирает не просто план с лучшим Score, а план, который не
развалится при кризисе. robustness() прогоняет один и тот же план через
каждое событие из data/events.json и показывает, сколько Score он теряет,
какой район страдает сильнее и какой сценарий для него худший.

Трактовка: план фиксирован и выбран до события. Если событие урезает бюджет
и план в него не укладывается, план «не проходит» (valid = false, score = None).
Это считается худшим исходом: такой план при кризисе пришлось бы перекраивать.
"""

from __future__ import annotations

from .data import CityData, get_data
from .events import resolve_events, with_event
from .model import Evaluation, evaluate
from .report import critical_list, min_district, placement_label, r2
from .validation import check_decisions


def robustness(decisions, events: list | None = None, data: CityData | None = None) -> dict:
    """Score плана при каждом событии, падение, сильнее всего пострадавший район и худший случай.

    events (необязательно) — список id событий; по умолчанию все события.
    """
    data = data or get_data()
    placements, errors = check_decisions(decisions, data)
    event_ids, event_errors = resolve_events(data, events)
    errors = errors + event_errors
    cost = data.cost(mid for mid, _ in placements)
    if errors:
        return {"valid": False, "errors": errors, "score": None, "cost": cost,
                "events": [], "worst_case": None, "worst_score": None}

    normal = evaluate(data, placements)
    outcomes = [event_outcome(data, decisions, placements, normal, event_id) for event_id in event_ids]
    rows = [row for row, _ in outcomes]
    failed = [row for row, score in outcomes if score is None]
    passed = [(row, score) for row, score in outcomes if score is not None]
    worst = worst_outcome(outcomes)
    average_drop = sum(normal.score - score for _, score in passed) / len(passed) if passed else None

    return {
        "valid": True,
        "errors": [],
        "plan": " + ".join(placement_label(data, mid, did) for mid, did in placements),
        "decisions": [{"measure": mid, "district": did} for mid, did in placements],
        "score": r2(normal.score),
        "cost": cost,
        "events": rows,
        "worst_case": worst,
        "worst_score": None if failed else worst["score"],
        "fails_under": [row["event"] for row in failed],
        "average_drop": r2(average_drop) if average_drop is not None else None,
        "summary": _summary(cost, failed, passed, average_drop),
        "notes": [
            "score — Score плана при событии; drop — насколько он ниже Score того же плана "
            "без события; hardest_hit — район, чья оценка D упала сильнее всего.",
            "baseline_score — Score города без мер при этом событии; plan_gain — сколько план даёт поверх него.",
            "worst_score — Score в худшем случае среди событий (None, если при каком-то событии план "
            "не проходит, например не укладывается в урезанный бюджет).",
        ],
    }


def event_outcome(data: CityData, decisions, placements: list, normal: Evaluation,
                  event_id: str) -> tuple[dict, float | None]:
    """Итог плана при одном событии: строка таблицы и точный Score (None — план не проходит)."""
    shocked, _ = with_event(data, event_id)
    event = shocked.event
    city_after_event = evaluate(shocked, [])
    row = {
        "event": event["id"],
        "name": event["name"],
        "district": event.get("district"),
        "district_name": shocked.district_name(event.get("district")),
        "budget": shocked.budget,
        "baseline_score": r2(city_after_event.score),
    }
    _, errors = check_decisions(decisions, shocked)
    if errors:  # при событии меняется только бюджет правил — значит, план в него не влез
        row.update(valid=False, errors=errors, score=None, drop=None, plan_gain=None, N_crit=None,
                   min_district=None, hardest_hit=None, new_critical=[])
        return row, None

    after = evaluate(shocked, placements)
    critical_before = {(c["district"], c["indicator"]) for c in critical_list(data, normal.values)}
    row.update(
        valid=True,
        errors=[],
        score=r2(after.score),
        drop=r2(normal.score - after.score),
        plan_gain=r2(after.score - city_after_event.score),
        N_crit=after.n_crit,
        min_district=min_district(shocked, after),
        hardest_hit=_hardest_hit(data, normal, after),
        new_critical=[
            c for c in critical_list(shocked, after.values)
            if (c["district"], c["indicator"]) not in critical_before
        ],
    )
    return row, after.score


def worst_outcome(outcomes: list) -> dict:
    """Худший случай: событие, при котором план не проходит, иначе — с минимальным Score."""
    for row, score in outcomes:
        if score is None:
            return row
    return min(outcomes, key=lambda outcome: outcome[1])[0]


def _hardest_hit(data: CityData, normal: Evaluation, after: Evaluation) -> dict:
    """Район, чья оценка D из-за события упала сильнее всего."""
    drops = normal.D - after.D
    i = int(drops.argmax())
    if drops[i] < 0.005:
        return {"id": None, "name": "никто: показатели не изменились", "D_drop": 0.0}
    if drops.max() - drops.min() < 0.005:  # общегородское событие бьёт по всем одинаково
        return {"id": None, "name": "все районы одинаково", "D_drop": r2(drops[i])}
    did = data.district_ids[i]
    return {"id": did, "name": data.districts[did]["name"], "D_drop": r2(drops[i])}


def _summary(cost, failed: list, passed: list, average_drop: float | None) -> str:
    """Вывод в одну-две фразы для интерфейса и советника."""
    parts = []
    for row in failed:
        parts.append(
            f"При событии «{row['name']}» план не проходит: он стоит {cost:g} у.е., "
            f"а бюджет урезан до {row['budget']:g} у.е."
        )
    if passed:
        row = min(passed, key=lambda outcome: outcome[1])[0]
        hit = row["hardest_hit"]
        if hit["id"] is not None:
            hit_text = f"сильнее всего страдает район {hit['name']} (оценка D падает на {hit['D_drop']:.2f})"
        elif hit["D_drop"] > 0:
            hit_text = f"удар одинаковый для всех районов (оценка D падает на {hit['D_drop']:.2f})"
        else:
            hit_text = "показатели районов не меняются"
        worst_label = "Худший из выдерживаемых сценариев" if failed else "Худший сценарий"
        parts.append(
            f"{worst_label} — «{row['name']}»: Score {row['score']:.2f} (падение {row['drop']:.2f}), {hit_text}."
        )
        parts.append(f"В среднем по событиям план теряет {average_drop:.2f} балла.")
    return " ".join(parts)
