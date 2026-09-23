"""
Расчёт сценария и подробный отчёт о нём.

simulate() и baseline() возвращают обычные словари, которые сразу можно
передать в json.dumps(): их показывает интерфейс и получает ИИ-советник.
Все числа посчитаны здесь — LLM их только объясняет и не пересчитывает.
Сам расчёт идёт без округлений; в отчёте числа округлены до 2 знаков.

Необязательный event_id считает тот же сценарий после городского события
(см. events.py): в ответе появляется блок "event", остальной формат прежний.
Без event_id результат в точности такой же, как без поддержки событий.
"""

from __future__ import annotations

from .data import CityData, get_data
from .events import with_event
from .model import Evaluation, evaluate
from .report import critical_list, event_report, min_district, r2, score_breakdown, synergy_list
from .validation import check_decisions


def baseline(data: CityData | None = None, event_id: str | None = None) -> dict:
    """Состояние города и Score без мер — точка отсчёта для любых сравнений.

    event_id (необязательно) — состояние города сразу после события, до мер.
    """
    data = data or get_data()
    data, errors = with_event(data, event_id)
    if errors:
        return {"valid": False, "errors": errors, "score": None}
    ev = evaluate(data, [])
    return {
        **_event_block(data),  # только при событии
        "score": r2(ev.score),
        "score_breakdown": score_breakdown(data, ev),
        "D_avg": r2(ev.D_avg),
        "min_district": min_district(data, ev),
        "N_crit": ev.n_crit,
        "critical_indicators": critical_list(data, ev.values),
        "budget": data.budget,
        "districts": [
            {
                "id": did,
                "name": d["name"],
                "population_share": d["population_share"],
                "profile": d.get("profile", ""),
                "D": r2(ev.D[i]),
                "indicators": {
                    k: {"name": data.indicators[k]["name"], "value": r2(ev.values[i, j])}
                    for j, k in enumerate(data.indicator_ids)
                },
            }
            for i, (did, d) in enumerate(data.districts.items())
        ],
        "notes": explanation_notes(data, with_measures=False),
    }


def simulate(decisions, data: CityData | None = None, event_id: str | None = None) -> dict:
    """Полный расчёт набора решений: Score, изменения по районам и показателям,
    критические значения, синергии и вклад каждой меры.

    Невалидный набор Score не получает: valid=False, score=None, все причины — в errors.
    event_id (необязательно) — тот же расчёт после городского события: «было»
    в отчёте — это город после события, до мер.
    """
    data = data or get_data()
    data, errors = with_event(data, event_id)
    placements, rule_errors = check_decisions(decisions, data)
    errors = errors + rule_errors
    cost = data.cost(mid for mid, _ in placements)
    if errors:
        return {
            "valid": False,
            "errors": errors,
            **_event_block(data),  # только при событии
            "score": None,
            "cost": cost,
            "budget": data.budget,
            "budget_left": data.budget - cost,
        }

    before = evaluate(data, [])
    after = evaluate(data, placements)
    contributions = [_contribution(data, placements, i, after) for i in range(len(placements))]
    resolved, new = _critical_changes(data, before.values, after.values)

    return {
        "valid": True,
        "errors": [],
        **_event_block(data, placements),  # только при событии
        "score": r2(after.score),
        "score_breakdown": score_breakdown(data, after),
        "cost": cost,
        "budget": data.budget,
        "budget_left": data.budget - cost,
        "D_avg": r2(after.D_avg),
        "min_district": min_district(data, after),
        "N_crit": after.n_crit,
        "baseline": {
            "score": r2(before.score),
            "D_avg": r2(before.D_avg),
            "min_district": min_district(data, before),
            "N_crit": before.n_crit,
        },
        "delta": {
            "score": r2(after.score - before.score),
            "D_avg": r2(after.D_avg - before.D_avg),
            "min_D": r2(after.min_D - before.min_D),
            "N_crit": after.n_crit - before.n_crit,
        },
        "measures": [_measure_report(data, mid, did, c) for (mid, did), c in zip(placements, contributions)],
        "measure_contributions_sum": r2(sum(c["score"] for c in contributions)),
        "synergies": synergy_list(data, after),
        "directions_coverage": _directions_coverage(data, placements),
        "critical_indicators": critical_list(data, after.values),
        "critical_resolved": resolved,
        "critical_new": new,
        "districts": _district_changes(data, before, after),
        "notes": explanation_notes(data),
    }


def _event_block(data: CityData, placements: list | None = None) -> dict:
    """{"event": описание события + Score без события} или {}, если события нет."""
    if data.event is None:
        return {}
    block = event_report(data)
    block["baseline_score_without_event"] = r2(evaluate(data.official, []).score)
    if placements is not None:
        # Тот же план в обычных условиях — чтобы было видно, сколько отняло событие
        block["plan_score_without_event"] = r2(evaluate(data.official, placements).score)
    return {"event": block}


# --- Вклад меры ----------------------------------------------------------------

def _contribution(data: CityData, placements: list, i: int, full: Evaluation) -> dict:
    """Вклад i-й меры = Score полного набора − Score набора без неё.

    Трактовка: набор без меры содержит 4 решения и формально невалиден,
    поэтому для оценки вклада он считается без проверки правил
    («что было бы без этой меры»). Вместе с мерой пропадают и синергии,
    в которых она участвовала. Вклад раскладывается на три слагаемых формулы.
    """
    without = evaluate(data, placements[:i] + placements[i + 1:])
    return {
        "score": full.score - without.score,
        "without": without.score,
        "via_D_avg": data.w_avg * (full.D_avg - without.D_avg),
        "via_min_D": data.w_min * (full.min_D - without.min_D),
        "via_N_crit": -data.crit_penalty * (full.n_crit - without.n_crit),
    }


def _measure_report(data: CityData, mid: str, did: str | None, contrib: dict) -> dict:
    m = data.measures[mid]
    share = data.realized_share(mid)
    return {
        "measure": mid,
        "name": m["name"],
        "direction": m["direction"],
        "direction_name": data.directions[m["direction"]],
        "scope": m["scope"],
        "district": did,
        "district_name": data.district_name(did),
        "cost": m["cost"],
        "lag_quarters": m["lag"],
        "realized_share": round(share, 3),
        "effects_full": dict(m["effects"]),
        "effects_realized": {k: r2(v * share) for k, v in m["effects"].items()},
        "score_contribution": r2(contrib["score"]),
        "contribution_parts": {
            "via_D_avg": r2(contrib["via_D_avg"]),
            "via_min_D": r2(contrib["via_min_D"]),
            "via_N_crit": r2(contrib["via_N_crit"]),
        },
        "score_without_it": r2(contrib["without"]),
        "efficiency_per_10": r2(contrib["score"] / m["cost"] * 10) if m["cost"] else None,
    }


def explanation_notes(data: CityData, with_measures: bool = True) -> list:
    """Пояснения к полям отчёта для LLM и для жюри (коэффициенты — из данных)."""
    H = data.horizon
    notes = [
        f"Score = {data.w_avg:g} * D_avg + {data.w_min:g} * min(D_d) - {data.crit_penalty:g} * N_crit. "
        "D_d — оценка района (сумма показателей с весами), D_avg — средняя оценка по городу "
        "с весами долей населения, min(D_d) — оценка самого слабого района, "
        f"N_crit — число показателей строго ниже {data.crit_threshold:g} (штраф за каждый).",
    ]
    if with_measures:
        notes += [
            f"Мера срабатывает с задержкой (лаг): за горизонт {H} кварталов реализуется доля "
            f"({H} - лаг) / {H} полного эффекта — это realized_share, итог — effects_realized.",
            "score_contribution — вклад меры: Score всего набора минус Score этого же набора без неё "
            "(вместе с мерой пропадают и её синергии). Разложение: via_D_avg + via_min_D + via_N_crit.",
            "Вклады мер не обязаны складываться в общий прирост Score: мешают min() по районам, "
            "штраф за критические значения и синергии, которые входят во вклад обеих мер пары.",
            "efficiency_per_10 — вклад меры в Score на каждые 10 у.е. её стоимости.",
        ]
    if data.event is not None:
        notes.append(
            f"Расчёт в условиях события «{data.event['name']}»: «было» (baseline) — город сразу после "
            "события, до мер. Официальный Score считается без событий."
        )
    return notes


# --- Районы, показатели, критические значения ---------------------------------------

def _district_changes(data: CityData, before: Evaluation, after: Evaluation) -> list:
    """Было/стало по каждому району и каждому показателю."""
    rows = []
    for i, did in enumerate(data.district_ids):
        d = data.districts[did]
        rows.append({
            "id": did,
            "name": d["name"],
            "population_share": d["population_share"],
            "profile": d.get("profile", ""),
            "D_before": r2(before.D[i]),
            "D_after": r2(after.D[i]),
            "D_delta": r2(after.D[i] - before.D[i]),
            "indicators": {
                k: {
                    "name": data.indicators[k]["name"],
                    "before": r2(before.values[i, j]),
                    "after": r2(after.values[i, j]),
                    "delta": r2(after.values[i, j] - before.values[i, j]),
                }
                for j, k in enumerate(data.indicator_ids)
            },
        })
    return rows


def _critical_changes(data: CityData, before, after) -> tuple[list, list]:
    """Какие критические значения меры устранили, а какие — создали."""
    resolved, new = [], []
    t = data.crit_threshold
    for i, did in enumerate(data.district_ids):
        for j, k in enumerate(data.indicator_ids):
            was, now = before[i, j] < t, after[i, j] < t
            if was != now:
                (resolved if was else new).append({
                    "district": did,
                    "district_name": data.districts[did]["name"],
                    "indicator": k,
                    "indicator_name": data.indicators[k]["name"],
                    "before": r2(before[i, j]),
                    "after": r2(after[i, j]),
                })
    return resolved, new


def _directions_coverage(data: CityData, placements: list) -> dict:
    """Какие меры выбраны по каждому направлению (пустой список — направление не затронуто)."""
    coverage = {name: [] for name in data.directions.values()}
    for mid, _ in placements:
        coverage[data.directions[data.measures[mid]["direction"]]].append(mid)
    return coverage
