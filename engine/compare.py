"""
Сравнение планов — опциональная фича ТЗ «сравнение результатов нескольких команд».

compare() принимает словарь «название → набор решений» (подойдёт и элемент
выдачи optimize() с полем decisions), считает каждый план тем же simulate()
и возвращает рейтинг: валиден ли план, Score, стоимость, разница с базой,
место и то, чем планы отличаются друг от друга.

Трактовки:
  * места — только у валидных планов, по Score (как в отчёте, до 2 знаков);
    при равном Score выше более дешёвый план, при равных Score и стоимости
    место общее (1, 2, 2, 4);
  * отличия считаются по парам «мера + район»: M8 в Нуре и M8 в Есиле — разные решения.
"""

from __future__ import annotations

from collections import Counter

from .data import CityData, get_data
from .events import with_event
from .model import evaluate
from .report import event_report, placement_label, r2
from .simulation import simulate
from .validation import check_decisions


def compare(plans: dict, event_id: str | None = None, data: CityData | None = None) -> dict:
    """Рейтинг планов: место, Score, стоимость, разница с базой и отличия планов.

    event_id (необязательно) — сравнить все планы в условиях одного события.
    """
    data = data or get_data()
    if not isinstance(plans, dict) or not plans:
        return {"errors": ['Передайте словарь «название → набор решений», например '
                           '{"Команда 1": [...], "Команда 2": [...]}.'], "ranking": []}
    conditions, errors = with_event(data, event_id)
    if errors:
        return {"errors": errors, "ranking": []}

    rows, labels = [], {}
    for name, plan in plans.items():
        decisions = plan.get("decisions") if isinstance(plan, dict) else plan
        result = simulate(decisions, data=data, event_id=event_id)
        placements, _ = check_decisions(decisions, conditions)
        labels[str(name)] = [placement_label(conditions, mid, did) for mid, did in placements]
        rows.append(_row(str(name), decisions, result, labels[str(name)]))

    ranked = _rank([row for row in rows if row["valid"]])
    leader = ranked[0] if ranked else None
    for row in rows:
        others = set().union(*(set(labels[r["name"]]) for r in rows if r is not row))
        row["unique_measures"] = [label for label in row["measures"] if label not in others]
        if leader is not None and row["valid"]:
            row["gap_to_leader"] = r2(leader["score"] - row["score"])
            if row is not leader:
                row["differs_from_leader"] = {
                    "leader_has": [x for x in leader["measures"] if x not in row["measures"]],
                    "plan_has": [x for x in row["measures"] if x not in leader["measures"]],
                }
    common = [x for x in rows[0]["measures"] if all(x in row["measures"] for row in rows)]
    ranking = ranked + [row for row in rows if not row["valid"]]

    output = {"errors": []}
    if conditions.event is not None:
        output["event"] = event_report(conditions)
    output.update({
        "baseline_score": r2(evaluate(conditions, []).score),
        "leader": leader["name"] if leader else None,
        "ranking": ranking,
        "common_measures": common,
        "summary": _summary(ranking, common),
    })
    return output


def _row(name: str, decisions, result: dict, labels: list) -> dict:
    """Строка рейтинга для одного плана (поля, которые зависят от других планов, — позже)."""
    row = {
        "name": name,
        "rank": None,
        "valid": result["valid"],
        "errors": result["errors"],
        "score": result["score"],
        "delta_vs_baseline": None,
        "gap_to_leader": None,
        "cost": result["cost"],
        "budget_left": result["budget_left"],
        "N_crit": None,
        "min_district": None,
        "measures": labels,
        "decisions": decisions,
        "unique_measures": [],
        "differs_from_leader": None,
        "top_measure": None,
        "focus_districts": {},
        "synergies": [],
    }
    if result["valid"]:
        best = max(result["measures"], key=lambda m: m["score_contribution"])
        focus = Counter(m["district_name"] for m in result["measures"] if m["district"] is not None)
        row.update({
            "delta_vs_baseline": result["delta"]["score"],
            "N_crit": result["N_crit"],
            "min_district": result["min_district"],
            "decisions": [{"measure": m["measure"], "district": m["district"]} for m in result["measures"]],
            "top_measure": {"measure": best["measure"], "district_name": best["district_name"],
                            "score_contribution": best["score_contribution"]},
            "focus_districts": dict(focus.most_common()),
            "synergies": [" + ".join(s["pair"]) for s in result["synergies"]],
        })
    return row


def _rank(rows: list) -> list:
    """Места по Score (выше — лучше), при равенстве — по стоимости; равные делят место."""
    rows = sorted(rows, key=lambda row: (-row["score"], row["cost"]))
    for i, row in enumerate(rows):
        same_as_previous = i > 0 and (row["score"], row["cost"]) == (rows[i - 1]["score"], rows[i - 1]["cost"])
        row["rank"] = rows[i - 1]["rank"] if same_as_previous else i + 1
    return rows


def _summary(ranking: list, common: list) -> list:
    """Короткие выводы по рейтингу — готовый текст для интерфейса и советника."""
    lines = []
    for row in ranking:
        if not row["valid"]:
            lines.append(f"«{row['name']}» — план не допущен: {row['errors'][0]}")
        elif row["gap_to_leader"] == 0 and row["differs_from_leader"] is None:
            lines.append(f"{row['rank']} место — «{row['name']}»: Score {row['score']:.2f} "
                         f"({row['delta_vs_baseline']:+.2f} к базе), стоимость {row['cost']:g} у.е.")
        else:
            diff = row["differs_from_leader"] or {"leader_has": [], "plan_has": []}
            details = []
            if diff["leader_has"]:
                details.append("нет " + ", ".join(diff["leader_has"]))
            if diff["plan_has"]:
                details.append("вместо этого " + ", ".join(diff["plan_has"]))
            lines.append(f"{row['rank']} место — «{row['name']}»: Score {row['score']:.2f}, отставание "
                         f"{row['gap_to_leader']:.2f}, стоимость {row['cost']:g} у.е."
                         + (f" По сравнению с лидером: {'; '.join(details)}." if details else ""))
    if common:
        lines.append("Есть во всех планах: " + ", ".join(common) + ".")
    return lines
