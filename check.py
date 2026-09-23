"""
Проверка движка одной командой:  python check.py  [путь к city_data.json]

Печатает базовый Score, примеры из ТЗ, топ-5 наборов полного перебора,
устойчивость лучшего плана и примера из ТЗ к городским событиям и самые
устойчивые планы. Сверяет всё с эталонами из reference_checks и завершается
с кодом 1, если хоть что-то не совпало (удобно для CI), иначе — с кодом 0.
"""

import sys
from math import comb

import engine
from engine.report import plural


class Checks:
    """Сверки с эталоном: возвращает вердикт для печати и копит расхождения."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = []

    def __call__(self, label: str, ok: bool, good: str = "совпадает", bad: str = "НЕ СОВПАДАЕТ") -> str:
        if ok:
            self.passed += 1
            return good
        self.failed.append(label)
        return bad


def num(n: int) -> str:
    """1407050 -> «1 407 050»."""
    return f"{n:,}".replace(",", " ")


def print_robustness(title: str, decisions: list) -> None:
    """Таблица «событие → Score, падение, кто пострадал сильнее» и худший случай."""
    res = engine.robustness(decisions)
    print(f"   {title}: {res['plan']}")
    print(f"   Без событий: Score {res['score']:.2f}, стоимость {res['cost']} у.е.")
    for row in res["events"]:
        label = f"{row['event']} {row['name']}"
        if row["valid"]:
            print(f"     {label:<38} Score {row['score']:6.2f}  падение {row['drop']:5.2f}  "
                  f"сильнее всего: {row['hardest_hit']['name']}")
        else:
            print(f"     {label:<38} не проходит: бюджет урезан до {row['budget']:g} у.е.")
    worst = res["worst_case"]
    worst_text = f"{res['worst_score']:.2f}" if res["worst_score"] is not None else "план не проходит"
    print(f"   Худший случай: «{worst['name']}» -> {worst_text}")
    print(f"   {res['summary']}")


def main(argv: list | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # При выводе в файл или пайп Windows может выбрать кодировку cp1251 —
    # не падаем на символах, которых в ней нет.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    data = engine.load_data(argv[0] if argv else None)
    ref = data.raw["reference_checks"]
    check = Checks()
    print(f"Данные: {data.source}\n")

    # 1. Базовый сценарий
    base = engine.baseline()
    weakest = base["min_district"]
    crit = ", ".join(f"{c['district_name']} {c['indicator']} = {c['value']:g}" for c in base["critical_indicators"])
    districts_ok = all(abs(row["D"] - data.districts[row["id"]]["base_D"]) < 0.005 for row in base["districts"])
    print("1) Базовый сценарий (без мер)")
    print(f"   Score = {base['score']:.2f}   (по ТЗ {ref['baseline_score']}) -> "
          f"{check('базовый Score', round(base['score'], 2) == ref['baseline_score'])}")
    print(f"   {base['score_breakdown']}")
    print(f"   D_avg = {base['D_avg']:.2f} (по ТЗ {ref['baseline_D_avg']}) -> "
          f"{check('D_avg базы', round(base['D_avg'], 2) == ref['baseline_D_avg'])}; "
          f"N_crit = {base['N_crit']} (по ТЗ {ref['baseline_N_crit']}) -> "
          f"{check('N_crit базы', base['N_crit'] == ref['baseline_N_crit'])}")
    print(f"   Критические показатели: {crit}; самый слабый район: {weakest['name']} (D = {weakest['D']:.2f})")
    print("   Оценки районов D: " + ", ".join(f"{row['name']} {row['D']:.2f}" for row in base["districts"])
          + f" -> {check('оценки районов из таблицы ТЗ', districts_ok)} с таблицей ТЗ")

    # 2. Примеры из ТЗ
    example = ref["example_valid_set"]
    res = engine.simulate(example["decisions"])
    print("\n2) Пример из ТЗ")
    print(f"   Набор валиден -> {check('пример ТЗ валиден', res['valid'], 'да', 'НЕТ')}")
    if res["valid"]:
        print("   " + " + ".join(f"{m['measure']} ({m['district_name']})" for m in res["measures"]))
        print(f"   Стоимость {res['cost']} из {res['budget']} (остаток {res['budget_left']}) -> "
              f"{check('стоимость примера ТЗ', res['cost'] == example['cost'])} с ТЗ")
        print(f"   Score = {res['score']:.2f} ({res['delta']['score']:+.2f} к базе), по ТЗ ~{example['expected_score_approx']} -> "
              f"{check('Score примера ТЗ', abs(res['score'] - example['expected_score_approx']) < 0.05)}")
        print(f"   {res['score_breakdown']}")
        synergies = ", ".join(f"{' + '.join(s['pair'])} (в районе {s['district_name']})" for s in res["synergies"])
        print(f"   Синергии: {synergies or 'нет'}")
        print("   Вклад мер в Score: " + ", ".join(f"{m['measure']} {m['score_contribution']:+.2f}" for m in res["measures"]))
    else:
        print("   Причины: " + "; ".join(res["errors"]))

    # Самый дешёвый набор ТЗ задан без районов — ставим районные меры в самый слабый район
    cheapest = ref["cheapest_valid_set"]
    cheap = engine.simulate([{"measure": m, "district": None if data.is_city(m) else weakest["id"]}
                             for m in cheapest["measures"]])
    print(f"   Самый дешёвый набор ТЗ {' + '.join(cheapest['measures'])}: валиден — {'да' if cheap['valid'] else 'НЕТ'}, "
          f"стоимость {cheap['cost']} (по ТЗ {cheapest['cost']}) -> "
          f"{check('самый дешёвый набор ТЗ', cheap['valid'] and cheap['cost'] == cheapest['cost'])}")

    # 3. Оптимизатор
    opt = engine.optimize(top_n=5)
    s = opt["stats"] or {}
    full = comb(len(data.measure_ids), data.num_decisions)
    plans_valid = bool(opt["results"]) and not opt["errors"] and all(
        engine.validate(r["decisions"])["valid"] for r in opt["results"])
    print("\n3) Оптимизатор: полный перебор")
    print(f"   Наборов мер: {num(s.get('measure_sets', 0))} из C({len(data.measure_ids)}, {data.num_decisions}) = {num(full)} -> "
          f"{check('перебраны все наборы мер', s.get('measure_sets') == full, 'все', 'НЕ ВСЕ')}; "
          f"прошли правила до перебора районов: {num(s.get('measure_sets_passed', 0))}")
    print(f"   Сценариев с районами: {num(s.get('scenarios_total', 0))}, допустимых: {num(s.get('scenarios_valid', 0))}; "
          f"время {s.get('seconds', 0):.2f} с")
    print(f"   Все найденные планы проходят validate() -> {check('планы оптимизатора валидны', plans_valid, 'да', 'НЕТ')}")
    if opt["results"] and res["valid"]:
        best = opt["results"][0]["score"]
        print(f"   Лучший план не хуже примера ТЗ: {best:.2f} >= {res['score']:.2f} -> "
              f"{check('оптимум не хуже примера ТЗ', best >= res['score'], 'да', 'НЕТ')}")
    print("   Топ-5:")
    for r in opt["results"]:
        print(f"   {r['rank']}. Score {r['score']:.2f} ({r['score_delta']:+.2f}) | стоимость {r['cost']:>3} | {r['summary']}")

    # 4–5. Городские события: устойчивость планов и режим устойчивости оптимизатора
    events_count = engine.list_events()["count"]
    if events_count:
        print(f"\n4) Устойчивость к городским событиям ({events_count} событий из events.json)")
        if opt["results"]:
            print_robustness("Лучший план", opt["results"][0]["decisions"])
            print()
        print_robustness("Пример из ТЗ", example["decisions"])

        robust = engine.optimize(top_n=3, robust=True)
        print("\n5) Самые устойчивые планы: лучший Score в худшем случае среди событий")
        print(f"   {robust['message']} Время {(robust['stats'] or {}).get('seconds', 0):.2f} с")
        for r in robust["results"]:
            worst = r["worst_case"]
            print(f"   {r['rank']}. в худшем случае {worst['score']:.2f} («{worst['name']}») | Score {r['score']:.2f} "
                  f"| стоимость {r['cost']:>3} | {r['summary']}")
        print(f"   Устойчивый план найден -> "
              f"{check('режим устойчивости нашёл план', bool(robust['results']) and not robust['errors'], 'да', 'НЕТ')}")
    else:
        print("\n4) Файл событий не найден — разделы про устойчивость пропущены.")

    total = check.passed + len(check.failed)
    print()
    if check.failed:
        print(f"ИТОГ: НЕ СОВПАЛО {len(check.failed)} из {total}: " + "; ".join(check.failed))
        return 1
    print(f"ИТОГ: все {total} {plural(total, 'проверка пройдена', 'проверки пройдены', 'проверок пройдены')}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
