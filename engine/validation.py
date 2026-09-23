"""
Проверка набора решений по правилам ТЗ (раздел 4 «Правила»).

Невалидный набор не получает Score. Валидатор возвращает ВСЕ нарушения
сразу и по-русски, чтобы пользователь или ИИ-советник мог исправить
набор за одну итерацию.

Трактовки:
  * Правила проверяются по решениям «как переданы»: если мера повторена,
    её стоимость и направление учитываются столько раз, сколько она выбрана.
  * Район можно указать по id («nura») или по названию («Нура»),
    регистр не важен; id меры тоже без учёта регистра («m7» = «M7»).
"""

from __future__ import annotations

from collections import Counter

from .data import CityData, get_data
from .events import with_event
from .report import plural as _plural


def validate(decisions, data: CityData | None = None, event_id: str | None = None) -> dict:
    """Проверяет набор решений. Возвращает {"valid": bool, "errors": [причины]}.

    event_id (необязательно) — проверить в условиях городского события,
    например при урезанном бюджете. Без него — официальные правила ТЗ.
    """
    data = data or get_data()
    data, errors = with_event(data, event_id)
    _, rule_errors = check_decisions(decisions, data)
    errors = errors + rule_errors
    return {"valid": not errors, "errors": errors}


def check_decisions(decisions, data: CityData) -> tuple[list, list]:
    """Разбирает и проверяет решения.

    Возвращает (placements, errors): placements — пары (мера, район)
    с каноническими id; у общегородских мер район None.
    """
    if not isinstance(decisions, (list, tuple)):
        return [], ['Решения нужно передать списком записей вида {"measure": "M7", "district": "nura"}.']

    placements, errors = _parse(decisions, data)

    # 1. Ровно N решений (не «до N»)
    n, need = len(decisions), data.num_decisions
    if n != need:
        errors.append(f"Нужно ровно {need} {_plural(need, 'решение', 'решения', 'решений')}, а выбрано {n}.")

    # 2. Каждое мероприятие — максимум один раз
    for mid, count in Counter(mid for mid, _ in placements).items():
        if count > 1:
            errors.append(
                f"Мера {_label(data, mid)} выбрана {count} {_plural(count, 'раз', 'раза', 'раз')}, "
                "а каждое мероприятие можно выбрать только один раз."
            )

    # 3. Бюджет
    cost = data.cost(mid for mid, _ in placements)
    if cost > data.budget:
        message = (
            f"Превышен бюджет: набор стоит {cost:g} у.е. при лимите {data.budget:g} у.е. "
            f"(перерасход {cost - data.budget:g} у.е.)."
        )
        if data.event is not None and data.event.get("budget_change"):
            message += f" Лимит изменён событием «{data.event['name']}» ({data.event['budget_change']:+g} у.е.)."
        errors.append(message)

    # 4. Не более max_per_direction мер из одного направления
    by_direction = {}
    for mid, _ in placements:
        by_direction.setdefault(data.measures[mid]["direction"], []).append(mid)
    for direction, mids in by_direction.items():
        if len(mids) > data.max_per_direction:
            errors.append(
                f"Направление «{data.directions[direction]}»: выбрано {len(mids)} "
                f"{_plural(len(mids), 'мера', 'меры', 'мер')} ({', '.join(mids)}), "
                f"а можно не более {data.max_per_direction}."
            )

    # 5. Несовместимости: «any» — нельзя вместе вообще, «same_district» — нельзя в одном районе
    districts_of = {}
    for mid, did in placements:
        districts_of.setdefault(mid, []).append(did)
    for inc in data.incompatibilities:
        a, b = inc["pair"]
        if a not in districts_of or b not in districts_of:
            continue
        if inc["scope"] == "any":
            errors.append(
                f"Меры {_label(data, a)} и {_label(data, b)} несовместимы — их нельзя выбирать вместе. "
                f"Причина: {inc['reason']}."
            )
        else:
            # Районы, где стоят обе меры (район None — не указан или неизвестен — уже учтён в ошибках выше)
            common = [did for did in districts_of[a] if did is not None and did in districts_of[b]]
            for did in dict.fromkeys(common):
                errors.append(
                    f"Меры {_label(data, a)} и {_label(data, b)} нельзя размещать в одном районе, "
                    f"а обе стоят в районе {data.district_name(did)}. Причина: {inc['reason']}."
                )

    return placements, errors


def _parse(decisions, data: CityData) -> tuple[list, list]:
    """Приводит каждое решение к паре (мера, район) и проверяет его поля.

    Мера с ошибкой в районе всё равно попадает в placements (с районом None),
    чтобы учесть её в проверках бюджета, повторов и направлений.
    """
    placements, errors = [], []
    district_options = ", ".join(f"{did} ({d['name']})" for did, d in data.districts.items())
    for i, dec in enumerate(decisions, start=1):
        if not isinstance(dec, dict) or not dec.get("measure"):
            errors.append(f'Решение №{i}: ожидается запись вида {{"measure": "M7", "district": "nura"}}.')
            continue
        mid = data.find_measure(dec["measure"])
        if mid is None:
            errors.append(
                f"Решение №{i}: неизвестная мера «{dec['measure']}». "
                f"Доступные меры: {', '.join(data.measure_ids)}."
            )
            continue

        given = dec.get("district")
        if given is not None and str(given).strip() == "":
            given = None

        if data.is_city(mid):
            if given is not None:
                errors.append(
                    f"Мера {_label(data, mid)} общегородская — район для неё не указывается "
                    f"(district: null), а указан «{given}»."
                )
            placements.append((mid, None))
        elif given is None:
            errors.append(f"Мера {_label(data, mid)} действует на один район — укажите район ({district_options}).")
            placements.append((mid, None))
        else:
            did = data.find_district(given)
            if did is None:
                errors.append(
                    f"Мера {_label(data, mid)}: неизвестный район «{given}». Допустимые районы: {district_options}."
                )
            placements.append((mid, did))
    return placements, errors


def _label(data: CityData, mid: str) -> str:
    """«M7 «Школа + детсад (модульное строительство)»» — id и название меры для сообщений."""
    return f"{mid} «{data.measures[mid]['name']}»"
