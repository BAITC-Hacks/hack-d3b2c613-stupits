"""
Оптимизатор: полный перебор всех допустимых сценариев.

Сценарий — это 5 мер из 14 плюс район для каждой районной меры:
C(14, 5) = 2002 набора мер и ≈1.4 млн вариантов вместе с районами.
Чтобы перебор занимал секунды:

  1. Правила, которые не зависят от районов (бюджет, не более 2 мер
     на направление, несовместимость «в любом районе»), проверяются для
     набора мер ДО перебора районов — невалидные наборы отсекаются сразу.
  2. Реализованный эффект каждой меры посчитан заранее (CityData.effect_vectors).
  3. Все варианты районов одного набора (до 5^5 = 3125) считаются одной
     векторной операцией numpy по той же функции score_parts(), что и
     в simulate(); варианты с конфликтом «в одном районе» убираются маской.
  4. Лучшие варианты пересчитываются обычным evaluate(), поэтому числа
     в выдаче оптимизатора в точности совпадают с simulate().

Режим устойчивости (robust=True) ищет план с лучшим Score в ХУДШЕМ случае
среди городских событий: добавка мер считается один раз, а оценивается от
нескольких стартовых точек (без события и после каждого события).
"""

from __future__ import annotations

import math
import numbers
import time
from collections import Counter
from itertools import combinations
from math import prod

import numpy as np

from .data import SCALE_MAX, SCALE_MIN, CityData, get_data
from .events import resolve_events, with_event
from .model import evaluate, score_parts
from .report import event_report, min_district, placement_label, plural, r2, synergy_list
from .robustness import event_outcome, worst_outcome
from .validation import check_decisions


def optimize(top_n: int = 10, constraints: dict | None = None, data: CityData | None = None,
             event_id: str | None = None, robust: bool = False, robust_events: list | None = None) -> dict:
    """Полный перебор: возвращает top_n лучших допустимых наборов по Score.

    constraints (все ключи необязательны):
        {"exclude": ["M3"],                      # эти меры не использовать
         "include": ["M12", {"measure": "M7", "district": "nura"}],
                                                 # эти меры обязательно включить;
                                                 # с районом — район закреплён
         "budget": 80}                           # лимит бюджета
    event_id (необязательно) — искать лучший план в условиях события:
        от ухудшенных стартовых показателей и с изменённым бюджетом.
    robust=True (необязательно) — режим устойчивости: лучший Score в худшем
        случае среди событий robust_events (по умолчанию все). План должен
        проходить при каждом событии, поэтому при секвестре он обязан
        уложиться в урезанный бюджет.

    Трактовки: лимит бюджета может только ужесточить правило ТЗ, но не ослабить
    его (берётся минимум из лимита и бюджета из данных). При равном Score
    выше стоит более дешёвый набор.
    """
    data = data or get_data()
    started = time.perf_counter()
    data, errors = with_event(data, event_id)
    exclude, include, budget, constraint_errors = _parse_constraints(constraints, data)
    errors += constraint_errors
    if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 1:
        errors.append("top_n должен быть целым числом не меньше 1.")
    robust_ids = []
    if not isinstance(robust, bool):
        errors.append(f"robust должен быть true или false, а не {robust!r}.")
    elif robust and event_id is not None:
        errors.append("Режим robust ищет план, устойчивый ко всем событиям сразу, — event_id с ним не задаётся.")
    elif robust:
        robust_ids, robust_errors = resolve_events(data, robust_events)
        errors += robust_errors
    used_constraints = {
        "exclude": [m for m in data.measure_ids if m in exclude],
        "include": [{"measure": m, "district": d} for m, d in include.items()],
        "budget": budget,
    }

    # Стартовые точки: bases[0] — без события (или после события event_id),
    # остальные — после каждого события режима robust.
    shocked = [with_event(data, e)[0] for e in robust_ids]
    bases = [data.base] + [s.base for s in shocked]
    if robust:  # план должен уложиться в бюджет при любом событии (например, при секвестре)
        budget = min([budget] + [s.budget for s in shocked])
    if not errors:
        errors += _include_conflicts(data, include, budget)
    if errors:
        return {
            "results": [],
            "errors": errors,
            "message": "Ограничения заданы неверно — перебор не выполнялся.",
            "constraints": used_constraints,
            "stats": None,
        }

    stats = Counter()
    candidates = []  # (цель, Score, стоимость, набор мер, районные меры набора, районы)
    pool = [m for m in data.measure_ids if m not in exclude]
    n_districts = len(data.district_ids)

    for combo in combinations(pool, data.num_decisions):
        if any(m not in combo for m in include):
            continue
        stats["measure_sets"] += 1
        stats["scenarios_total"] += prod(
            1 if include.get(m) else n_districts for m in combo if not data.is_city(m)
        )
        # Шаг 1: правила, не зависящие от районов, — до перебора районов
        cost = data.cost(combo)
        if cost > budget or not _rules_ok_without_districts(data, combo):
            continue
        stats["measure_sets_passed"] += 1

        # Шаг 3: все варианты районов этого набора — одной операцией numpy
        scores, district_measures, where = _score_all_placements(data, combo, include, bases)
        # Цель поиска: Score или, в режиме robust, Score в худшем из событий
        objective = scores[1:].min(axis=0) if robust else scores[0]
        stats["scenarios_valid"] += len(objective)
        for j in _top_indices(objective, top_n):
            candidates.append((float(objective[j]), float(scores[0, j]), cost, combo, district_measures,
                               tuple(int(x) for x in where[j])))

    # Лучшие сверху; при равной цели — выше Score без событий, затем дешевле (точность 1e-9)
    candidates.sort(key=lambda c: (-round(c[0], 9), -round(c[1], 9), c[2], c[3], c[5]))

    base_score = evaluate(data, []).score
    results, rejected = [], 0
    for _, _, cost, combo, district_measures, where in candidates:
        if len(results) == top_n:
            break
        district_of = {m: data.district_ids[i] for m, i in zip(district_measures, where)}
        placements = [(m, district_of.get(m)) for m in combo]
        decisions = [{"measure": m, "district": d} for m, d in placements]
        ev = evaluate(data, placements)  # тот же расчёт, что и в simulate()
        worst = _worst_case(data, placements, ev, robust_ids) if robust else None
        # Страховка: каждый план перед выдачей проходит тот же validate(), что и ручной ввод,
        # и проверку ограничений. Перебор и так отсекает запрещённое правилами — это защита
        # от ошибки в быстрых проверках: невалидный план не может попасть в выдачу.
        if (check_decisions(decisions, data)[1] or _breaks_constraints(decisions, exclude, include, budget, data)
                or (robust and worst["score"] is None)):
            rejected += 1
            continue
        result = {
            "rank": len(results) + 1,
            "score": r2(ev.score),
            "score_delta": r2(ev.score - base_score),
            "cost": cost,
            "budget_left": data.budget - cost,
            "decisions": decisions,
            "summary": " + ".join(placement_label(data, m, d) for m, d in placements),
            "D_avg": r2(ev.D_avg),
            "min_district": min_district(data, ev),
            "N_crit": ev.n_crit,
            "synergies": [" + ".join(s["pair"]) for s in synergy_list(data, ev)],
        }
        if robust:
            result["worst_case"] = worst
        results.append(result)

    valid_text = _num(stats["scenarios_valid"])
    if not results:
        message = "Допустимых наборов при заданных ограничениях нет."
        if robust:
            message = "Нет планов, которые проходят при всех выбранных событиях (например, укладываются в урезанный бюджет)."
    elif robust:
        shown = len(results)
        message = (
            f"Режим устойчивости: из {valid_text} сценариев, которые проходят при всех {len(robust_ids)} "
            f"событиях, {plural(shown, 'показан', 'показаны', 'показаны')} {shown} "
            f"{plural(shown, 'план', 'плана', 'планов')} с лучшим Score в худшем случае."
        )
    else:
        message = f"Полный перебор: из {valid_text} допустимых сценариев показаны {len(results)} лучших."
        if data.event is not None:
            message = f"Событие «{data.event['name']}». {message}"

    output = {
        "results": results,
        # Непустой список только при внутренней ошибке: план, отсеянный страховкой, не показан
        "errors": [f"Внутренняя проверка: {rejected} найденных планов не прошли validate() "
                   "и не показаны. Сообщите разработчикам движка."] if rejected else [],
        "message": message,
        "constraints": used_constraints,
        "stats": {
            "measure_sets": stats["measure_sets"],                # наборов из 5 мер (с учётом include/exclude)
            "measure_sets_passed": stats["measure_sets_passed"],  # прошли правила, не зависящие от районов
            "scenarios_total": stats["scenarios_total"],          # вариантов «набор × районы» всего
            "scenarios_valid": stats["scenarios_valid"],          # допустимых вариантов — все посчитаны
            "seconds": round(time.perf_counter() - started, 2),
        },
    }
    # Новые ключи появляются только в новых режимах — обычный ответ не меняется
    if data.event is not None:
        output["event"] = {**event_report(data),
                           "baseline_score_without_event": r2(evaluate(data.official, []).score)}
    if robust:
        output["mode"] = "robust"
        output["robust_events"] = robust_ids
        output["robust_budget"] = budget
    return output


def _breaks_constraints(decisions: list, exclude: set, include: dict, budget, data: CityData) -> bool:
    """Нарушает ли готовый план ограничения пользователя (exclude, include, budget)."""
    chosen = {d["measure"]: d["district"] for d in decisions}
    return (
        any(m in chosen for m in exclude)
        or any(m not in chosen or (did is not None and chosen[m] != did) for m, did in include.items())
        or data.cost(chosen) > budget
    )


def _worst_case(data: CityData, placements: list, normal, event_ids: list) -> dict:
    """Худшее событие для найденного плана — тем же кодом, что и robustness()."""
    decisions = [{"measure": m, "district": d} for m, d in placements]
    outcomes = [event_outcome(data, decisions, placements, normal, e) for e in event_ids]
    worst = worst_outcome(outcomes)
    return {key: worst[key] for key in ("event", "name", "score", "drop", "hardest_hit")}


def _rules_ok_without_districts(data: CityData, combo) -> bool:
    """Правила ТЗ, которые проверяются без районов (бюджет проверяется отдельно).

    Это быстрые версии проверок из validation.py; их согласованность
    с validate() проверяют тесты.
    """
    per_direction = Counter(data.measures[m]["direction"] for m in combo)
    if max(per_direction.values()) > data.max_per_direction:
        return False
    return not any(
        inc["scope"] == "any" and all(m in combo for m in inc["pair"])
        for inc in data.incompatibilities
    )


def _score_all_placements(data: CityData, combo, include: dict, bases: list):
    """Score всех допустимых вариантов размещения районных мер набора combo.

    Добавка мер от стартовой точки не зависит, поэтому считается один раз,
    а Score — от каждой стартовой точки из bases (без события и после событий).
    Возвращает (scores, district_measures, where): scores[b][j] — Score j-го
    варианта от точки b; where[j][i] — индекс района i-й районной меры.
    """
    n_d, n_k = data.base.shape
    chosen = set(combo)
    district_measures = [m for m in combo if not data.is_city(m)]

    # Общегородские меры одинаковы во всех вариантах — прибавляем один раз.
    fixed = np.zeros((n_d, n_k))
    for m in combo:
        if data.is_city(m):
            fixed += data.effect_vectors[m]
    # Эффект районных мер; бонус сработавшей синергии «ездит» вместе с районом
    # своей меры (applies_to_district_of), поэтому прибавляем его к её эффекту.
    vec = {m: data.effect_vectors[m].copy() for m in district_measures}
    for syn in data.synergies:
        if set(syn["pair"]) <= chosen:
            target = syn["applies_to_district_of"]
            if target in vec:
                vec[target] += data.vector(syn["bonus"])
            else:  # цель — общегородская мера: бонус во всех районах (как в model.py)
                fixed += data.vector(syn["bonus"])

    # Варианты района для каждой районной меры: все районы или закреплённый в include
    options = [
        [data.district_index[include[m]]] if include.get(m) else list(range(n_d))
        for m in district_measures
    ]

    # Складываем добавки через broadcasting: ось i перебирает район i-й районной меры.
    k = len(district_measures)
    deltas = fixed.reshape((1,) * k + (n_d, n_k))
    for i, m in enumerate(district_measures):
        opts = options[i]
        stack = np.zeros((len(opts), n_d, n_k))
        stack[np.arange(len(opts)), opts, :] = vec[m]  # эффект только в строке выбранного района
        shape = [1] * k + [n_d, n_k]
        shape[i] = len(opts)
        deltas = deltas + stack.reshape(shape)
    deltas = deltas.reshape(-1, n_d, n_k)

    # Какой район у каждой районной меры в каждом варианте (тот же порядок, что у deltas)
    if k:
        grid = np.indices([len(o) for o in options]).reshape(k, -1).T
        where = np.stack([np.asarray(options[i])[grid[:, i]] for i in range(k)], axis=1)
    else:
        where = np.zeros((1, 0), dtype=int)

    # Несовместимости «в одном районе» — маской по вариантам
    ok = np.ones(len(deltas), dtype=bool)
    for inc in data.incompatibilities:
        if inc["scope"] == "same_district" and all(m in district_measures for m in inc["pair"]):
            a, b = (district_measures.index(m) for m in inc["pair"])
            ok &= where[:, a] != where[:, b]

    deltas = deltas[ok]
    scores = np.stack([
        score_parts(np.clip(base + deltas, SCALE_MIN, SCALE_MAX), data)["score"] for base in bases
    ])
    return scores, district_measures, where[ok]


def _top_indices(scores: np.ndarray, n: int):
    """Индексы n лучших значений без полной сортировки."""
    if len(scores) <= n:
        return range(len(scores))
    return np.argpartition(-scores, n - 1)[:n]


def _parse_constraints(constraints, data: CityData):
    """Разбор и проверка ограничений. Возвращает (exclude, include, budget, errors):
    exclude — множество id мер, include — {мера: район или None}.

    Любой неверный ввод — неизвестный ключ, не тот тип, NaN или бесконечность
    в бюджете, противоречивые include — даёт понятную ошибку. Молча ничего
    не игнорируется и не «исправляется».
    """
    errors = []
    exclude, include, budget = set(), {}, data.budget
    if constraints is None:
        return exclude, include, budget, errors
    if not isinstance(constraints, dict):
        return exclude, include, budget, [
            'constraints должен быть словарём: {"exclude": [...], "include": [...], "budget": 80}.'
        ]

    unknown_keys = set(constraints) - {"exclude", "include", "budget"}
    if unknown_keys:
        errors.append(f"Неизвестные ограничения: {', '.join(sorted(map(str, unknown_keys)))}. "
                      "Доступны: exclude, include, budget.")

    # exclude: список id мер
    items, error = _as_list(constraints.get("exclude"), "exclude", 'например ["M3"]')
    errors += error
    for item in items:
        mid = data.find_measure(item) if isinstance(item, str) else None
        if not isinstance(item, str):
            errors.append(f'exclude: элемент {item!r} должен быть строкой с id меры, например "M3".')
        elif mid is None:
            errors.append(f"exclude: неизвестная мера «{item}».")
        else:
            exclude.add(mid)

    # include: id меры или объект {"measure": ..., "district": ...}
    items, error = _as_list(constraints.get("include"), "include",
                            'например ["M12", {"measure": "M7", "district": "nura"}]')
    errors += error
    for item in items:
        mid, did, error = _parse_include_item(item, data)
        if error:
            errors.append(error)
        elif mid in include and None not in (include[mid], did) and include[mid] != did:
            # Одна мера — один район: противоречие не перезаписываем молча
            errors.append(f"include: мера {mid} закреплена в двух районах сразу "
                          f"({data.district_name(include[mid])} и {data.district_name(did)}), "
                          "а каждую меру можно выбрать только один раз.")
        elif did is not None or mid not in include:
            include[mid] = did

    both = [m for m in include if m in exclude]
    if both:
        errors.append(f"Меры {', '.join(both)} одновременно в include и exclude.")
    if len(include) > data.num_decisions:
        errors.append(f"В include {len(include)} мер, а в наборе всего {data.num_decisions} решений.")

    if constraints.get("budget") is not None:
        limit = constraints["budget"]
        # NaN отключил бы сравнение cost > budget, бесконечность — лимит. Оба — ошибка ввода.
        if isinstance(limit, bool) or not isinstance(limit, numbers.Real) or not math.isfinite(limit) or limit < 0:
            errors.append(f"budget должен быть конечным неотрицательным числом, например 80; получено: {limit!r}.")
        else:
            budget = min(limit, data.budget)
    return exclude, include, budget, errors


def _parse_include_item(item, data: CityData):
    """Один элемент include → (мера, район или None, текст ошибки или None)."""
    if isinstance(item, str):
        raw_mid, district = item, None
    elif isinstance(item, dict):
        extra = sorted(map(str, set(item) - {"measure", "district"}))
        if extra:
            return None, None, f"include: неизвестные поля {extra} в {item!r} — допустимы только measure и district."
        raw_mid, district = item.get("measure"), item.get("district")
    else:
        return None, None, (f'include: элемент {item!r} должен быть id меры ("M7") '
                            'или объектом {"measure": "M7", "district": "nura"}.')

    mid = data.find_measure(raw_mid) if isinstance(raw_mid, str) else None
    if mid is None:
        return None, None, f"include: неизвестная мера «{raw_mid}»."
    if district is not None and not isinstance(district, str):
        return None, None, f"include: район у меры {mid} должен быть строкой или null, а не {district!r}."
    if district is None or district.strip() == "":
        return mid, None, None
    if data.is_city(mid):
        return None, None, f"include: мера {mid} общегородская — район для неё не указывается."
    did = data.find_district(district)
    if did is None:
        return None, None, f"include: неизвестный район «{district}» у меры {mid}."
    return mid, did, None


def _include_conflicts(data: CityData, include: dict, budget) -> list:
    """Закреплённые меры сами по себе не должны нарушать правила ТЗ.

    Иначе перебор вернул бы пустой список без объяснения, а так пользователь
    (или ИИ-советник) сразу видит, какое требование невыполнимо.
    """
    errors = []
    cost = data.cost(include)
    if cost > budget:
        errors.append(f"include: закреплённые меры стоят {cost:g} у.е. — больше лимита {budget:g} у.е.")
    per_direction = Counter(data.measures[m]["direction"] for m in include)
    for direction, count in per_direction.items():
        if count > data.max_per_direction:
            errors.append(f"include: {count} меры направления «{data.directions[direction]}», "
                          f"а можно не более {data.max_per_direction}.")
    for inc in data.incompatibilities:
        a, b = inc["pair"]
        if a not in include or b not in include:
            continue
        if inc["scope"] == "any":
            errors.append(f"include: меры {a} и {b} несовместимы. Причина: {inc['reason']}.")
        elif include[a] is not None and include[a] == include[b]:
            errors.append(f"include: меры {a} и {b} нельзя размещать в одном районе "
                          f"({data.district_name(include[a])}). Причина: {inc['reason']}.")
    return errors


def _as_list(value, name: str, example: str) -> tuple[list, list]:
    """None → [], одиночное значение → [значение], список → список; иначе ошибка типа."""
    if value is None:
        return [], []
    if isinstance(value, (str, dict)):
        return [value], []
    if isinstance(value, (list, tuple)):
        return list(value), []
    return [], [f"{name} должен быть списком, {example}; получено: {value!r}."]


def _num(n: int) -> str:
    """1407050 → «1 407 050»."""
    return f"{n:,}".replace(",", " ")
