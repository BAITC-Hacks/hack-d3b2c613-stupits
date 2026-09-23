"""
Тесты движка «Аким на 5 часов».

Эталонные числа — из ТЗ: раздел 3 (базовый Score 52.56, пример ≈ 56.5)
и раздел 4 (правила, самый дешёвый набор за 61 у.е.).
Запуск из корня проекта: pytest
"""

import json
from itertools import combinations, product
from math import comb

import pytest

import engine
from engine import baseline, optimize, simulate, validate
from engine.model import evaluate
from engine.validation import check_decisions


def dec(measure, district=None):
    """Короткая запись решения."""
    return {"measure": measure, "district": district}


def errors_of(decisions):
    """Список ошибок невалидного набора."""
    result = validate(decisions)
    assert result["valid"] is False
    return result["errors"]


def district_row(result, district_id):
    return next(row for row in result["districts"] if row["id"] == district_id)


# Допустимый набор-заготовка: 85 у.е., направления 2/1/1/1, конфликтов нет.
# Тесты правил меняют в нём одно решение, чтобы получить ровно одно нарушение.
VALID = [dec("M7", "nura"), dec("M8", "nura"), dec("M10", "nura"), dec("M12"), dec("M4", "esil")]


@pytest.fixture(scope="module")
def data():
    return engine.load_data()


@pytest.fixture(scope="module")
def example(data):
    """Пример допустимого набора из ТЗ (reference_checks.example_valid_set)."""
    return data.raw["reference_checks"]["example_valid_set"]["decisions"]


@pytest.fixture(scope="module")
def top10():
    return optimize(top_n=10)


# ---------- Базовый сценарий ----------

def test_baseline_score_matches_tz():
    base = baseline()
    assert round(base["score"], 2) == 52.56
    assert base["N_crit"] == 2
    assert round(base["D_avg"], 2) == 56.86
    assert base["min_district"] == {"id": "nura", "name": "Нура", "D": 49.18}


def test_baseline_critical_values_are_nura_s1_s2():
    crit = {(c["district"], c["indicator"]) for c in baseline()["critical_indicators"]}
    assert crit == {("nura", "S1"), ("nura", "S2")}


def test_baseline_district_scores_match_data_file(data):
    # base_D в city_data.json посчитаны авторами ТЗ — движок должен их воспроизвести
    for row in baseline()["districts"]:
        assert row["D"] == pytest.approx(data.districts[row["id"]]["base_D"], abs=0.005)


# ---------- Пример из ТЗ ----------

def test_example_set_is_valid(example):
    assert validate(example) == {"valid": True, "errors": []}


def test_example_set_cost_and_score(example):
    res = simulate(example)
    assert res["valid"]
    assert res["cost"] == 95
    assert res["budget_left"] == 5
    assert res["score"] == pytest.approx(56.5, abs=0.05)
    assert res["delta"]["score"] == pytest.approx(4.0, abs=0.05)  # «+4.0 к базе» по ТЗ
    assert res["N_crit"] == 0  # оба критических значения Нуры устранены
    assert {(c["district"], c["indicator"]) for c in res["critical_resolved"]} == {("nura", "S1"), ("nura", "S2")}


def test_example_synergy_m10_m12(example):
    res = simulate(example)
    assert [(s["pair"], s["district"]) for s in res["synergies"]] == [(["M10", "M12"], "nura")]
    # B1 в Нуре: 55 + 12 * (8 - 1) / 8 + фиксированный бонус 2 = 67.5
    assert district_row(res, "nura")["indicators"]["B1"]["after"] == 67.5


def test_synergy_needs_both_measures(example):
    # M12 заменяем на M14 — пары M10 + M12 больше нет, бонуса тоже
    decisions = [dec("M14") if x["measure"] == "M12" else x for x in example]
    res = simulate(decisions)
    assert res["valid"] and res["synergies"] == []
    assert district_row(res, "nura")["indicators"]["B1"]["after"] == 65.5


def test_lag_scales_effect(example):
    res = simulate(example)
    nura = district_row(res, "nura")["indicators"]
    assert nura["S1"]["delta"] == 10.0   # M7: 16 * (8 - 3) / 8
    assert nura["S2"]["delta"] == 8.75   # M8: 14 * (8 - 3) / 8
    # M12 общегородская: C2 растёт во всех районах на 5 * (8 - 1) / 8 = 4.375
    assert all(row["indicators"]["C2"]["delta"] == pytest.approx(4.375, abs=0.01) for row in res["districts"])


def test_measure_contributions(example, data):
    res = simulate(example)
    by_id = {m["measure"]: m for m in res["measures"]}
    assert set(by_id) == {"M7", "M8", "M10", "M12", "M5"}

    # Вклад = Score полного набора − Score набора без этой меры
    placements, _ = check_decisions(example, data)
    full = evaluate(data, placements).score
    for i, (mid, _) in enumerate(placements):
        without = evaluate(data, placements[:i] + placements[i + 1:]).score
        assert by_id[mid]["score_contribution"] == pytest.approx(full - without, abs=0.006)
        assert sum(by_id[mid]["contribution_parts"].values()) == pytest.approx(
            by_id[mid]["score_contribution"], abs=0.02
        )
    # M7 и M8 убирают по одному критическому значению в Нуре — +1 балл каждая
    assert by_id["M7"]["contribution_parts"]["via_N_crit"] == 1.0
    assert by_id["M8"]["contribution_parts"]["via_N_crit"] == 1.0


def test_decision_order_does_not_matter(example):
    assert simulate(list(reversed(example)))["score"] == simulate(example)["score"]


def test_changing_decisions_changes_score(example):
    # Критерий ТЗ: изменение набора решений меняет Score
    moved = [dec("M5", "esil") if x["measure"] == "M5" else x for x in example]
    assert simulate(moved)["score"] != simulate(example)["score"]


def test_new_critical_value_is_reported():
    # M11 снижает T1 на 2 * 7/8 = 1.75: в Алматы T1 = 40 → 38.25 — новое критическое значение
    decisions = [dec("M11", "almaty"), dec("M7", "nura"), dec("M8", "nura"), dec("M12"), dec("M4", "esil")]
    res = simulate(decisions)
    assert [(c["district"], c["indicator"]) for c in res["critical_new"]] == [("almaty", "T1")]


# ---------- Самый дешёвый набор ----------

def test_cheapest_set_is_valid_and_costs_61(data):
    measures = data.raw["reference_checks"]["cheapest_valid_set"]["measures"]
    # в ТЗ районы не указаны — расставляем любые допустимые
    districts = {"M9": "nura", "M11": "esil", "M10": "nura", "M4": "saryarka"}
    decisions = [dec(m, districts.get(m)) for m in measures]
    assert validate(decisions)["valid"]
    assert simulate(decisions)["cost"] == 61


def test_no_valid_set_is_cheaper_than_61():
    # С лимитом 60 у.е. допустимых наборов нет, с лимитом 61 — есть
    assert optimize(constraints={"budget": 60})["results"] == []
    res = optimize(constraints={"budget": 61})
    assert res["results"] and all(r["cost"] == 61 for r in res["results"])


# ---------- Правила валидации ----------

def test_valid_template_is_valid():
    assert validate(VALID)["valid"]


def test_rule_budget_exceeded():
    # 30 + 22 + 25 + 24 + 28 = 129 у.е.; остальные правила соблюдены
    decisions = [dec("M3", "nura"), dec("M2"), dec("M5", "saryarka"), dec("M7", "nura"), dec("M13", "almaty")]
    errors = errors_of(decisions)
    assert len(errors) == 1 and "бюджет" in errors[0] and "129" in errors[0]


def test_rule_exactly_five_decisions():
    errors = errors_of(VALID[:4])
    assert len(errors) == 1 and "ровно 5" in errors[0]


def test_rule_no_repeats():
    decisions = VALID[:4] + [dec("M10", "esil")]  # M10 второй раз
    errors = errors_of(decisions)
    assert len(errors) == 1 and "M10" in errors[0] and "только один раз" in errors[0]


def test_rule_max_two_per_direction():
    decisions = VALID[:4] + [dec("M9", "nura")]  # M7, M8, M9 — три меры соцсферы
    errors = errors_of(decisions)
    assert len(errors) == 1 and "Соцсфера" in errors[0] and "не более 2" in errors[0]


def test_rule_m1_m3_forbidden_in_any_districts():
    decisions = [dec("M1", "esil"), dec("M3", "nura"), dec("M10", "nura"), dec("M12"), dec("M4", "esil")]
    errors = errors_of(decisions)
    assert len(errors) == 1 and "M1" in errors[0] and "M3" in errors[0] and "несовместимы" in errors[0]


def test_rule_m4_m7_forbidden_in_same_district():
    decisions = VALID[:4] + [dec("M4", "nura")]  # M7 тоже в Нуре
    errors = errors_of(decisions)
    assert len(errors) == 1 and "M4" in errors[0] and "M7" in errors[0] and "одном районе" in errors[0]
    assert validate(VALID)["valid"]  # в разных районах (Есиль и Нура) — можно


def test_rule_m5_m13_forbidden_in_same_district():
    decisions = [dec("M5", "saryarka"), dec("M13", "saryarka"), dec("M10", "nura"), dec("M9", "nura"), dec("M11", "esil")]
    errors = errors_of(decisions)
    assert len(errors) == 1 and "M5" in errors[0] and "M13" in errors[0] and "одном районе" in errors[0]
    decisions[1] = dec("M13", "almaty")  # в разных районах — можно
    assert validate(decisions)["valid"]


def test_rule_district_measure_requires_district():
    decisions = [dec("M7")] + VALID[1:]
    errors = errors_of(decisions)
    assert len(errors) == 1 and "M7" in errors[0] and "укажите район" in errors[0]


def test_rule_city_measure_without_district():
    decisions = VALID[:3] + [dec("M12", "nura"), VALID[4]]
    errors = errors_of(decisions)
    assert len(errors) == 1 and "M12" in errors[0] and "общегородская" in errors[0]


def test_unknown_measure_and_district():
    assert "M99" in errors_of(VALID[:4] + [dec("M99", "esil")])[0]
    assert "moscow" in errors_of(VALID[:4] + [dec("M4", "moscow")])[0]


def test_all_violations_are_reported_at_once():
    decisions = [dec("M1", "nura"), dec("M2"), dec("M3", "nura"), dec("M3", "nura"), dec("M7", "nura"), dec("M4", "nura")]
    text = " | ".join(errors_of(decisions))
    for fragment in ("ровно 5", "только один раз", "бюджет", "Транспорт", "несовместимы", "одном районе"):
        assert fragment in text


def test_district_can_be_given_by_russian_name():
    decisions = [dec("m7", "Нура")] + VALID[1:]
    assert validate(decisions)["valid"]
    assert simulate(decisions)["score"] == simulate(VALID)["score"]


def test_invalid_set_gets_no_score():
    res = simulate(VALID[:4])
    assert res["valid"] is False and res["score"] is None and res["errors"]


def test_results_are_json_serializable(example):
    small = optimize(top_n=3, constraints={"include": ["M7", "M8", "M10"]})
    for obj in (baseline(), simulate(example), simulate(VALID[:4]), small):
        json.dumps(obj, ensure_ascii=False)


# ---------- Оптимизатор ----------

def test_optimizer_is_full_search(data, top10):
    # Все наборы 5 из 14 и все варианты районов для районных мер
    expected_space = sum(
        len(data.district_ids) ** sum(not data.is_city(m) for m in combo)
        for combo in combinations(data.measure_ids, data.num_decisions)
    )
    assert top10["errors"] == []
    assert top10["stats"]["measure_sets"] == comb(14, 5)
    assert top10["stats"]["scenarios_total"] == expected_space
    assert top10["stats"]["seconds"] < 30


def test_optimizer_results_are_sorted_valid_and_match_simulate(top10):
    results = top10["results"]
    assert len(results) == 10
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)
    for r in results:
        assert validate(r["decisions"])["valid"], r["summary"]
        sim = simulate(r["decisions"])
        assert (sim["score"], sim["cost"], sim["N_crit"]) == (r["score"], r["cost"], r["N_crit"])


def test_optimizer_beats_example(top10, example):
    assert top10["results"][0]["score"] >= simulate(example)["score"]


@pytest.mark.parametrize("fixed", [
    [dec("M7", "nura"), dec("M8", "nura"), dec("M10", "nura")],
    [dec("M5", "saryarka"), dec("M12")],
])
def test_optimizer_matches_naive_search(data, fixed):
    # Независимая проверка векторного перебора «в лоб»: закрепляем часть мер,
    # остальные меры и районы перебираем обычными validate + evaluate.
    fixed_ids = {x["measure"] for x in fixed}
    others = [m for m in data.measure_ids if m not in fixed_ids]
    best, n_valid = float("-inf"), 0
    for rest in combinations(others, data.num_decisions - len(fixed)):
        options = [[None] if data.is_city(m) else data.district_ids for m in rest]
        for districts in product(*options):
            placements, errors = check_decisions(fixed + [dec(m, d) for m, d in zip(rest, districts)], data)
            if not errors:
                n_valid += 1
                best = max(best, evaluate(data, placements).score)

    res = optimize(top_n=1, constraints={"include": fixed})
    assert res["stats"]["scenarios_valid"] == n_valid
    assert res["results"][0]["score"] == round(best, 2)


def test_optimizer_constraints_exclude_include_budget():
    res = optimize(top_n=5, constraints={"exclude": ["M3"], "include": ["M12"], "budget": 80})
    assert res["errors"] == [] and res["results"]
    for r in res["results"]:
        measures = [x["measure"] for x in r["decisions"]]
        assert "M3" not in measures and "M12" in measures and r["cost"] <= 80


def test_optimizer_include_with_fixed_district():
    res = optimize(top_n=5, constraints={"include": [{"measure": "M3", "district": "almaty"}]})
    assert res["results"]
    assert all({"measure": "M3", "district": "almaty"} in r["decisions"] for r in res["results"])


def test_optimizer_rejects_bad_constraints():
    res = optimize(constraints={"exclude": ["M99"]})
    assert res["results"] == [] and "M99" in res["errors"][0]


# ---------- Данные ----------

def test_broken_data_file_is_rejected(tmp_path, data):
    raw = json.loads(json.dumps(data.raw))
    raw["measures"][0]["effects"]["X9"] = 5  # несуществующий показатель
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="X9"):
        engine.load_data(path)
