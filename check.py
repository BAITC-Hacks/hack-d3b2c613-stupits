"""
Проверка движка одной командой:  python check.py

Печатает базовый Score, Score примера из ТЗ, топ-5 наборов полного перебора,
устойчивость лучшего плана и примера из ТЗ к городским событиям
и самые устойчивые планы (лучший Score в худшем случае).
"""

import sys

import engine


def num(n: int) -> str:
    """1407050 -> «1 407 050»."""
    return f"{n:,}".replace(",", " ")


def verdict(ok: bool) -> str:
    return "совпадает" if ok else "НЕ СОВПАДАЕТ"


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


def main() -> None:
    # При выводе в файл или пайп Windows может выбрать кодировку cp1251 —
    # не падаем на символах, которых в ней нет.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    data = engine.load_data()
    ref = data.raw["reference_checks"]
    print(f"Данные: {data.source}\n")

    # 1. Базовый сценарий
    base = engine.baseline()
    weakest = base["min_district"]
    crit = ", ".join(f"{c['district_name']} {c['indicator']} = {c['value']:g}" for c in base["critical_indicators"])
    print("1) Базовый сценарий (без мер)")
    print(f"   Score = {base['score']:.2f}   (по ТЗ {ref['baseline_score']}) -> "
          f"{verdict(round(base['score'], 2) == ref['baseline_score'])}")
    print(f"   {base['score_breakdown']}")
    print(f"   D_avg = {base['D_avg']:.2f}, самый слабый район: {weakest['name']} (D = {weakest['D']:.2f}), "
          f"N_crit = {base['N_crit']} ({crit})")

    # 2. Пример допустимого набора из ТЗ
    example = ref["example_valid_set"]
    res = engine.simulate(example["decisions"])
    print("\n2) Пример из ТЗ")
    if not res["valid"]:
        print("   Набор невалиден: " + "; ".join(res["errors"]))
    else:
        print("   " + " + ".join(f"{m['measure']} ({m['district_name']})" for m in res["measures"]))
        print(f"   Стоимость {res['cost']} из {res['budget']} (остаток {res['budget_left']}) -> "
              f"{verdict(res['cost'] == example['cost'])} с ТЗ")
        print(f"   Score = {res['score']:.2f} ({res['delta']['score']:+.2f} к базе), по ТЗ ~{example['expected_score_approx']} -> "
              f"{verdict(abs(res['score'] - example['expected_score_approx']) < 0.05)}")
        print(f"   {res['score_breakdown']}")
        synergies = ", ".join(f"{' + '.join(s['pair'])} (в районе {s['district_name']})" for s in res["synergies"])
        print(f"   Синергии: {synergies or 'нет'}")
        print("   Вклад мер в Score: " + ", ".join(f"{m['measure']} {m['score_contribution']:+.2f}" for m in res["measures"]))

    # 3. Оптимизатор
    opt = engine.optimize(top_n=5)
    s = opt["stats"]
    print("\n3) Оптимизатор: полный перебор")
    print(f"   Наборов мер: {num(s['measure_sets'])}, прошли правила до перебора районов: {num(s['measure_sets_passed'])}")
    print(f"   Сценариев с районами: {num(s['scenarios_total'])}, допустимых: {num(s['scenarios_valid'])}; "
          f"время {s['seconds']:.2f} с")
    print("   Топ-5:")
    for r in opt["results"]:
        print(f"   {r['rank']}. Score {r['score']:.2f} ({r['score_delta']:+.2f}) | стоимость {r['cost']:>3} | {r['summary']}")

    # 4. Устойчивость к городским событиям
    print(f"\n4) Устойчивость к городским событиям ({engine.list_events()['count']} событий из data/events.json)")
    if opt["results"]:
        print_robustness("Лучший план", opt["results"][0]["decisions"])
        print()
    print_robustness("Пример из ТЗ", example["decisions"])

    # 5. Режим устойчивости оптимизатора
    robust = engine.optimize(top_n=3, robust=True)
    print("\n5) Самые устойчивые планы: лучший Score в худшем случае среди событий")
    print(f"   {robust['message']} Время {robust['stats']['seconds']:.2f} с")
    for r in robust["results"]:
        worst = r["worst_case"]
        print(f"   {r['rank']}. в худшем случае {worst['score']:.2f} («{worst['name']}») | Score {r['score']:.2f} "
              f"| стоимость {r['cost']:>3} | {r['summary']}")


if __name__ == "__main__":
    main()
