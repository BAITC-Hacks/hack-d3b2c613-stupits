"""
Тесты проверки входов оптимизатора и страховочного validate() для каждого плана.

Любой неверный ввод должен давать понятную ошибку, а не исключение и не
«молча работать»: например, NaN в бюджете раньше отключал проверку бюджета.
Запуск из корня проекта: python -m pytest
"""

import math

import pytest

import engine
from engine import optimize, robustness, validate


def dec(measure, district=None):
    """Короткая запись решения."""
    return {"measure": measure, "district": district}


def errors_for(**kwargs) -> str:
    """Ошибки optimize() одной строкой; при ошибке перебор не выполняется."""
    result = optimize(top_n=1, **kwargs)
    assert result["results"] == [] and result["errors"], result["message"]
    return " | ".join(result["errors"])


# ---------- Бюджет ----------

@pytest.mark.parametrize("budget", [math.nan, math.inf, -math.inf, -5, "80", True, [80]])
def test_bad_budget_is_rejected(budget):
    assert "budget" in errors_for(constraints={"budget": budget})


def test_nan_budget_no_longer_disables_budget_check():
    # Раньше NaN делал сравнение cost > budget ложным, и выдавался план за 112 у.е.
    result = optimize(top_n=5, constraints={"budget": math.nan})
    assert result["results"] == []


# ---------- Типы exclude / include ----------

@pytest.mark.parametrize("constraints, fragment", [
    ({"exclude": 5}, "exclude должен быть списком"),
    ({"exclude": [5]}, "должен быть строкой"),
    ({"exclude": [None]}, "должен быть строкой"),
    ({"include": 7}, "include должен быть списком"),
    ({"include": [3.5]}, "должен быть id меры"),
    ({"include": [{"measure": "M7", "districts": "nura"}]}, "неизвестные поля"),
    ({"include": [{"measure": 7}]}, "неизвестная мера"),
    ({"include": [{"measure": "M7", "district": 3}]}, "должен быть строкой или null"),
    ({"include": [{"measure": "M12", "district": "nura"}]}, "общегородская"),
    ({"include": [{"measure": "M7", "district": "moscow"}]}, "неизвестный район"),
    ({"unknown": 1}, "Неизвестные ограничения"),
])
def test_wrong_types_give_clear_errors(constraints, fragment):
    assert fragment in errors_for(constraints=constraints)


def test_constraints_must_be_a_dict():
    assert "словарём" in errors_for(constraints=["M3"])


# ---------- Противоречивые include ----------

def test_same_measure_in_two_districts_is_an_error():
    text = errors_for(constraints={"include": [dec("M7", "nura"), dec("M7", "esil")]})
    assert "M7" in text and "Нура" in text and "Есиль" in text


def test_repeated_measure_without_conflict_is_fine():
    # «M7» и «M7 в Нуре» не противоречат друг другу: район просто уточняется
    result = optimize(top_n=3, constraints={"include": ["M7", dec("M7", "nura"), dec("M7", "nura")]})
    assert result["errors"] == [] and result["results"]
    assert all(dec("M7", "nura") in r["decisions"] for r in result["results"])


@pytest.mark.parametrize("include, fragment", [
    (["M1", "M3"], "несовместимы"),
    ([dec("M4", "nura"), dec("M7", "nura")], "в одном районе"),
    ([dec("M5", "saryarka"), dec("M13", "saryarka")], "в одном районе"),
    (["M7", "M8", "M9"], "Соцсфера"),
])
def test_include_that_breaks_rules_is_explained(include, fragment):
    assert fragment in errors_for(constraints={"include": include})


def test_include_over_budget_is_explained():
    # M3 (30) + M13 (28) = 58 у.е. при лимите 50
    assert "больше лимита" in errors_for(constraints={"include": ["M3", "M13"], "budget": 50})


def test_include_in_different_districts_is_allowed():
    result = optimize(top_n=1, constraints={"include": [dec("M4", "esil"), dec("M7", "nura")]})
    assert result["errors"] == [] and result["results"]


# ---------- Режим robust и события ----------

@pytest.mark.parametrize("kwargs, fragment", [
    ({"robust": "yes"}, "robust должен быть"),
    ({"robust": 1}, "robust должен быть"),
    ({"robust": True, "robust_events": 5}, "списком id"),
    ({"robust": True, "robust_events": [5]}, "строкой с id"),
    ({"robust": True, "robust_events": ["E99"]}, "Неизвестное событие"),
    ({"top_n": 0}, "top_n"),
    ({"top_n": 2.5}, "top_n"),
])
def test_bad_modes_give_clear_errors(kwargs, fragment):
    result = optimize(**kwargs)
    assert result["results"] == [] and fragment in " ".join(result["errors"])


def test_robustness_rejects_bad_event_list():
    example = engine.get_data().raw["reference_checks"]["example_valid_set"]["decisions"]
    assert "списком id" in robustness(example, events=5)["errors"][0]


# ---------- Каждый план проходит validate() ----------

@pytest.mark.parametrize("kwargs", [
    {},
    {"event_id": "E2"},
    {"event_id": "E6"},
    {"robust": True},
    {"constraints": {"exclude": ["M3"], "include": [dec("M7", "nura")], "budget": 80}},
])
def test_every_returned_plan_passes_validate(kwargs):
    result = optimize(top_n=5, **kwargs)
    assert result["errors"] == [] and result["results"]
    for plan in result["results"]:
        assert validate(plan["decisions"], event_id=kwargs.get("event_id"))["valid"], plan["summary"]


def test_safety_net_blocks_invalid_plans(monkeypatch):
    # Ломаем быструю предпроверку перебора: теперь до подсчёта доходят наборы с M1 + M3,
    # тремя мерами одного направления и т. п. Страховочный validate() не должен их выпустить.
    monkeypatch.setattr("engine.optimizer._rules_ok_without_districts", lambda data, combo: True)
    result = optimize(top_n=5)
    assert result["results"], "валидные планы всё равно должны найтись"
    for plan in result["results"]:
        assert validate(plan["decisions"])["valid"], plan["summary"]
    assert "validate()" in " ".join(result["errors"])  # о сбое сообщено, а не скрыто
