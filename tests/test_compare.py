"""
Тесты сравнения планов (опциональная фича ТЗ «сравнение результатов нескольких команд»).
Запуск из корня проекта: python -m pytest
"""

import json

import pytest

import engine
from engine import compare, optimize, simulate


def dec(measure, district=None):
    """Короткая запись решения."""
    return {"measure": measure, "district": district}


# Самый дешёвый набор из ТЗ (61 у.е.)
CHEAP = [dec("M9", "nura"), dec("M11", "esil"), dec("M10", "nura"), dec("M12"), dec("M4", "saryarka")]


@pytest.fixture(scope="module")
def example():
    """Пример допустимого набора из ТЗ: 95 у.е., Score 56.54."""
    return engine.load_data().raw["reference_checks"]["example_valid_set"]["decisions"]


@pytest.fixture(scope="module")
def best():
    """Лучший план полного перебора: Score 57.24."""
    return optimize(top_n=1)["results"][0]


def test_compare_ranks_by_score(example, best):
    res = compare({"Пример ТЗ": example, "Оптимум": best["decisions"], "Дешёвый": CHEAP})
    assert res["errors"] == [] and res["leader"] == "Оптимум"
    assert [(r["name"], r["rank"]) for r in res["ranking"]] == [("Оптимум", 1), ("Пример ТЗ", 2), ("Дешёвый", 3)]
    for row in res["ranking"]:
        sim = simulate(row["decisions"])  # числа рейтинга — те же, что у simulate()
        assert (row["score"], row["cost"], row["delta_vs_baseline"]) == (sim["score"], sim["cost"], sim["delta"]["score"])
        assert row["gap_to_leader"] == pytest.approx(best["score"] - row["score"], abs=0.011)


def test_invalid_plan_gets_no_rank(example):
    res = compare({"С ошибкой": example[:4], "Пример": example})
    assert [r["name"] for r in res["ranking"]] == ["Пример", "С ошибкой"]
    bad = res["ranking"][1]
    assert bad["rank"] is None and bad["valid"] is False and bad["score"] is None and bad["errors"]


def test_equal_plans_share_rank(example):
    res = compare({"А": example, "Б": list(reversed(example))})
    assert [r["rank"] for r in res["ranking"]] == [1, 1]


def test_compare_shows_differences(example, best):
    res = compare({"Пример": example, "Оптимум": best["decisions"]})
    row = next(r for r in res["ranking"] if r["name"] == "Пример")
    assert "M3 (Нура)" in row["differs_from_leader"]["leader_has"]
    assert "M5 (Сарыарка)" in row["differs_from_leader"]["plan_has"]
    assert "M5 (Сарыарка)" in row["unique_measures"]
    assert res["common_measures"] == ["M8 (Нура)"]
    assert len(res["summary"]) >= 2


def test_compare_accepts_optimizer_results():
    top = optimize(top_n=2)["results"]
    res = compare({"№1": top[0], "№2": top[1]})
    assert [(r["name"], r["rank"]) for r in res["ranking"]] == [("№1", 1), ("№2", 2)]


def test_compare_under_event(example):
    # При секвестре (85 у.е.) пример за 95 у.е. не проходит — выигрывает дешёвый план
    res = compare({"Пример": example, "Дешёвый": CHEAP}, event_id="E6")
    assert res["event"]["id"] == "E6" and res["leader"] == "Дешёвый"
    assert next(r for r in res["ranking"] if r["name"] == "Пример")["valid"] is False


def test_compare_rejects_bad_input(example):
    assert compare({})["errors"]
    assert compare([example])["errors"]
    assert compare({"А": example}, event_id="E99")["errors"]


def test_compare_is_json_serializable(example, best):
    json.dumps(compare({"Пример": example, "Оптимум": best, "Ошибка": example[:2]}), ensure_ascii=False)
