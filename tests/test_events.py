"""
Тесты городских событий, устойчивости плана (robustness) и режима robust оптимизатора.

Главное требование: без event_id движок работает ровно как раньше
(базовый Score 52.56, пример из ТЗ 56.54).
Запуск из корня проекта: python -m pytest
"""

import json
from itertools import combinations, product

import pytest

import engine
from engine import baseline, list_events, optimize, robustness, simulate, validate
from engine.validation import check_decisions


def dec(measure, district=None):
    """Короткая запись решения."""
    return {"measure": measure, "district": district}


# Самый дешёвый набор из ТЗ (61 у.е.) — проходит при любом событии, даже при секвестре
CHEAP = [dec("M9", "nura"), dec("M11", "esil"), dec("M10", "nura"), dec("M12"), dec("M4", "saryarka")]


@pytest.fixture(scope="module")
def data():
    return engine.load_data()


@pytest.fixture(scope="module")
def example(data):
    """Пример допустимого набора из ТЗ: 95 у.е., Score 56.54."""
    return data.raw["reference_checks"]["example_valid_set"]["decisions"]


@pytest.fixture(scope="module")
def robust_top():
    return optimize(top_n=5, robust=True)


# ---------- Каталог событий ----------

def test_events_catalogue(data):
    catalogue = list_events()
    assert 6 <= catalogue["count"] == len(catalogue["events"]) <= 8
    for event in catalogue["events"]:
        for key in ("id", "name", "description", "district", "effects", "budget_change", "hint"):
            assert key in event
        assert event["district"] is None or event["district"] in data.districts
    assert catalogue["baseline_score"] == 52.56


# ---------- Без события всё как раньше ----------

def test_without_event_results_are_unchanged(example):
    assert baseline()["score"] == 52.56
    assert simulate(example)["score"] == 56.54
    assert baseline(event_id=None) == baseline()
    assert simulate(example, event_id=None) == simulate(example)
    assert validate(example, event_id=None) == validate(example)
    assert "event" not in baseline() and "event" not in simulate(example)


def test_optimizer_without_event_is_unchanged():
    res = optimize(top_n=1)
    assert res["results"][0]["score"] == 57.24
    assert "event" not in res and "mode" not in res and "worst_case" not in res["results"][0]


# ---------- Событие ухудшает стартовые показатели ----------

def test_every_event_changes_start_before_measures(data):
    official = baseline()
    for event_id, event in data.events.items():
        shocked = baseline(event_id=event_id)
        for before, after in zip(official["districts"], shocked["districts"]):
            hit = event["district"] in (None, before["id"])  # null — весь город
            for k, info in before["indicators"].items():
                change = event["effects"].get(k, 0) if hit else 0
                assert after["indicators"][k]["value"] == pytest.approx(info["value"] + change), (event_id, k)
        if event["effects"]:
            assert shocked["score"] < official["score"], event_id
        assert shocked["budget"] == official["budget"] + event["budget_change"]


def test_district_event_touches_only_its_district(example):
    normal, shocked = simulate(example), simulate(example, event_id="E1")  # теплосеть в Алматы
    for row, row0 in zip(shocked["districts"], normal["districts"]):
        if row["id"] == "almaty":
            assert row["indicators"]["C1"]["after"] == row0["indicators"]["C1"]["after"] - 15
        else:
            assert row["indicators"] == row0["indicators"]


def test_simulate_reports_event(example):
    res = simulate(example, event_id="E1")
    assert res["valid"] and res["event"]["id"] == "E1"
    assert res["event"]["baseline_score_without_event"] == 52.56
    assert res["event"]["plan_score_without_event"] == 56.54
    assert res["baseline"]["score"] < 52.56  # старт после аварии хуже
    assert res["score"] < 56.54


def test_budget_cut_event(example):
    # «Сокращение бюджета» (−15 у.е.): пример за 95 у.е. больше не проходит
    result = validate(example, event_id="E6")
    assert not result["valid"]
    assert "85" in result["errors"][0] and "Сокращение бюджета" in result["errors"][0]
    assert simulate(example, event_id="E6")["score"] is None
    # план за 61 у.е. проходит, показатели при секвестре не меняются
    cheap = simulate(CHEAP, event_id="E6")
    assert cheap["score"] == simulate(CHEAP)["score"] and cheap["budget_left"] == 85 - 61


def test_unknown_event_is_reported(example):
    assert validate(example, event_id="E99")["valid"] is False
    assert "E99" in simulate(example, event_id="E99")["errors"][0]
    assert baseline(event_id="E99")["errors"]
    assert optimize(event_id="E99")["errors"]
    assert robustness(example, events=["E99"])["errors"]


@pytest.mark.parametrize("event_id", ["E2", "E6"])
def test_optimize_under_event(event_id):
    res = optimize(top_n=3, event_id=event_id)
    assert res["event"]["id"] == event_id and res["results"]
    for r in res["results"]:
        assert r["cost"] <= res["event"]["budget"]
        assert validate(r["decisions"], event_id=event_id)["valid"]
        assert simulate(r["decisions"], event_id=event_id)["score"] == r["score"]


# ---------- Устойчивость плана ----------

def test_robustness_covers_all_events(example, data):
    res = robustness(example)
    assert [row["event"] for row in res["events"]] == list(data.events)


def test_robustness_rows_match_simulate(example):
    res = robustness(example)
    for row in res["events"]:
        sim = simulate(example, event_id=row["event"])
        assert row["valid"] == sim["valid"]
        if row["valid"]:
            assert row["score"] == sim["score"]
            assert row["drop"] == pytest.approx(res["score"] - row["score"], abs=0.011)


def test_expensive_plan_fails_budget_cut(example):
    res = robustness(example)  # 95 у.е. больше урезанных 85 у.е.
    assert res["fails_under"] == ["E6"]
    assert res["worst_case"]["event"] == "E6" and res["worst_case"]["valid"] is False
    assert res["worst_score"] is None


def test_worst_case_is_minimum_score():
    res = robustness(CHEAP)
    scores = [row["score"] for row in res["events"]]
    assert res["fails_under"] == [] and None not in scores
    assert res["worst_score"] == res["worst_case"]["score"] == min(scores)


def test_hardest_hit_district():
    rows = {row["event"]: row for row in robustness(CHEAP)["events"]}
    assert rows["E1"]["hardest_hit"]["id"] == "almaty"    # авария на теплосети в Алматы
    assert rows["E5"]["hardest_hit"]["id"] == "saryarka"  # смог в Сарыарке
    assert rows["E2"]["hardest_hit"]["id"] is None        # буран бьёт по всем районам одинаково
    assert rows["E6"]["drop"] == 0.0                      # секвестр не меняет показатели


# ---------- Режим robust оптимизатора ----------

def test_robust_mode_results(robust_top, data):
    assert robust_top["errors"] == [] and robust_top["mode"] == "robust"
    assert robust_top["robust_events"] == list(data.events)
    worst = [r["worst_case"]["score"] for r in robust_top["results"]]
    assert worst == sorted(worst, reverse=True)
    for r in robust_top["results"]:
        assert robustness(r["decisions"])["worst_score"] == r["worst_case"]["score"]  # тот же расчёт
        assert r["cost"] <= robust_top["robust_budget"] == 85  # проходит и при секвестре


def test_robust_plan_survives_where_best_plan_fails(robust_top, data):
    best = optimize(top_n=1)["results"][0]
    assert robustness(best["decisions"])["worst_score"] is None  # 98 у.е. — секвестр не переживает
    assert robust_top["results"][0]["worst_case"]["score"] is not None
    # И без секвестра устойчивый оптимум в худшем случае не хуже лучшего по Score плана
    shocks = [e for e in data.events if data.events[e]["effects"]]
    robust = optimize(top_n=1, robust=True, robust_events=shocks)["results"][0]
    assert robust["worst_case"]["score"] >= robustness(best["decisions"], events=shocks)["worst_score"]


def test_robust_mode_matches_naive_search(data):
    # Независимая проверка «в лоб»: закрепляем 3 меры, остальные перебираем через robustness()
    fixed = [dec("M8", "nura"), dec("M9", "nura"), dec("M12")]
    fixed_ids = {x["measure"] for x in fixed}
    best = float("-inf")
    for rest in combinations([m for m in data.measure_ids if m not in fixed_ids], 2):
        options = [[None] if data.is_city(m) else data.district_ids for m in rest]
        for districts in product(*options):
            decisions = fixed + [dec(m, d) for m, d in zip(rest, districts)]
            if check_decisions(decisions, data)[1]:
                continue
            worst = robustness(decisions)["worst_score"]
            if worst is not None:
                best = max(best, worst)
    res = optimize(top_n=1, robust=True, constraints={"include": fixed})
    assert res["results"][0]["worst_case"]["score"] == best


def test_robust_and_event_id_are_exclusive():
    assert optimize(robust=True, event_id="E2")["errors"]


# ---------- Файл событий ----------

def test_results_are_json_serializable(example, robust_top):
    for obj in (list_events(), simulate(example, event_id="E2"), baseline(event_id="E5"),
                robustness(example), robust_top, optimize(top_n=2, event_id="E6")):
        json.dumps(obj, ensure_ascii=False)


def test_broken_events_file_is_rejected(tmp_path, data):
    events = [dict(e) for e in data.events.values()]
    events[0]["district"] = "moscow"
    city = tmp_path / "city_data.json"
    city.write_text(json.dumps(data.raw, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "events.json").write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="moscow"):
        engine.load_data(city)


def test_missing_events_file_is_not_an_error(tmp_path, data, example):
    city = tmp_path / "city_data.json"
    city.write_text(json.dumps(data.raw, ensure_ascii=False), encoding="utf-8")
    try:
        loaded = engine.load_data(city)  # рядом нет events.json
        assert loaded.events == {} and list_events()["count"] == 0
        assert simulate(example)["score"] == 56.54  # основной расчёт от событий не зависит
        assert robustness(example)["errors"]
    finally:
        engine.load_data()  # вернуть данные по умолчанию для остальных тестов
