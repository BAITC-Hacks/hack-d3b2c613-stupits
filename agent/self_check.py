"""Проверки советника без внешнего API: python -B -m agent.self_check.

HTTP заменяется локальным транспортом; расчёты выполняет настоящий движок.
Файлы engine/, tests/ и данные не меняются. Реальные ключи не используются.
"""

from copy import deepcopy
from types import SimpleNamespace
import json
import logging
import os
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI

import engine
from agent import advisor
from agent.evidence import EvidenceError, render_grounded_answer
from agent.prompts import AUTO_QUESTION
from agent.tools import EngineTools
from legacy.ui.engine_adapter import EngineAdapter, EngineUnavailable


SETTINGS = {"OPENAI_API_KEY": "test-only", "OPENAI_MODEL": "test-model",
            "OPENAI_BASE_URL": "https://compatible.invalid/v1"}
EMPTY_SETTINGS = {key: "" for key in SETTINGS}


def tool_message(name, arguments, call_id="call_test"):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {
            "name": name, "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments)}}]}


def text_message(text):
    return {"role": "assistant", "content": text}


class AdvisorChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = engine.get_data().raw["reference_checks"]["example_valid_set"]["decisions"]
        cls.result = engine.simulate(cls.plan)

    def run_api(self, responder, question="Объясни мой план", *, history=None, checked_plans=None, event_id=None):
        requests = []

        def handler(request):
            payload = json.loads(request.content)
            requests.append(payload)
            message = responder(payload, len(requests))
            if isinstance(message, httpx.Response):
                return message
            return httpx.Response(200, json={
                "id": "local-check", "object": "chat.completion", "created": 0, "model": "test-model",
                "choices": [{"index": 0, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                             "message": message}],
            })

        # Настоящий SDK, но HTTP физически не выходит из процесса.
        client = OpenAI(api_key="test-only", base_url=SETTINGS["OPENAI_BASE_URL"], max_retries=0,
                        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        with patch.object(advisor, "read_settings", return_value=SETTINGS), \
             patch.object(advisor, "_create_client", return_value=client):
            response = advisor.ask_advisor(question, self.result, history=history, checked_plans=checked_plans,
                                           event_id=event_id)
        self.assertTrue(client.is_closed())
        return response, requests

    def test_offline_without_key(self):
        supplied = deepcopy(self.result)
        with patch.object(advisor, "read_settings", return_value=EMPTY_SETTINGS), \
             patch.object(advisor, "_create_client") as factory:
            response = advisor.ask_advisor(AUTO_QUESTION, supplied)
        self.assertTrue(response["offline"])
        factory.assert_not_called()
        self.assertEqual(supplied, self.result)
        self.assertEqual([c["name"] for c in response["tool_calls"]], ["simulate"])
        self.assertEqual(response["tool_calls"][0]["source"], "preparation")
        self.assertEqual(response["reason_code"], "missing_key")
        self.assertIn(str(self.result["delta"]["score"]).replace(".", ","), response["answer"])

    def test_offline_without_model(self):
        with patch.object(advisor, "read_settings", return_value={**SETTINGS, "OPENAI_MODEL": ""}), \
             patch.object(advisor, "_create_client") as factory:
            response = advisor.ask_advisor(AUTO_QUESTION, self.result)
        self.assertTrue(response["offline"])
        self.assertEqual(response["reason_code"], "missing_model")
        factory.assert_not_called()

    def test_all_current_scenario_tools_keep_the_selected_event(self):
        tools = EngineTools(event_id="E2")
        self.assertEqual(tools.rules["budget"], engine.baseline(event_id="E2")["budget"])
        requests = [("baseline", {}), ("validate", {"decisions": self.plan}),
                    ("simulate", {"decisions": self.plan}), ("optimize", {"constraints": {"budget": 80}}),
                    ("compare", {"plans": {"Текущий": self.plan}})]
        for name, arguments in requests:
            actual = tools.call(name, arguments)
            self.assertEqual(actual["status"], "ok")
            self.assertEqual(tools.trace[-1]["arguments"]["event_id"], "E2")
            self.assertNotIn("event_id", arguments)  # Вход модели не мутируется.
            if name == "simulate":
                self.assertEqual(actual["result"], engine.simulate(self.plan, event_id="E2"))
            if name == "compare":
                self.assertEqual(actual["result"]["ranking"][0]["score"],
                                 engine.simulate(self.plan, event_id="E2")["score"])
            if name == "optimize":
                for plan in actual["result"]["results"]:
                    self.assertEqual(plan["score"], engine.simulate(plan["decisions"], event_id="E2")["score"])
        self.assertTrue(all(plan["event_id"] == "E2" for plan in tools.checked_plans()))

    def test_advisor_infers_event_and_ignores_stale_supplied_score(self):
        event_result = engine.simulate(self.plan, event_id="E2")
        event_result["score"] = 999999  # Ответ обязан заново прийти из движка.

        def responder(payload, turn):
            if turn == 1:
                context = json.loads(payload["messages"][-1]["content"])
                self.assertEqual(context["selected_event_id"], "E2")
                return tool_message("baseline", {})
            return text_message("Score {{e1:/score}}; база {{e2:/score}}.")

        with patch.object(self, "result", event_result):
            response, requests = self.run_api(responder)
        self.assertFalse(response["offline"])
        self.assertEqual(response["event_id"], "E2")
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(call["arguments"]["event_id"] == "E2" for call in response["tool_calls"]))
        self.assertNotIn("999999", response["answer"])
        self.assertIn("Сильный буран", response["answer"])

    def test_remaining_event_risks_use_current_plan_even_if_model_requests_baseline(self):
        plan = engine.optimize(top_n=1)["results"][0]["decisions"]
        current = engine.simulate(plan, event_id="E1")
        self.assertEqual(current["N_crit"], 1)
        self.assertEqual(current["baseline"]["N_crit"], 3)

        def responder(payload, turn):
            if turn == 1:
                self.assertEqual(payload["tool_choice"]["function"]["name"], "simulate")
                # Провайдер игнорирует forced choice: не должны принять базу
                # за оставшиеся после проектов критические показатели.
                return tool_message("baseline", {})
            facts = json.loads(next(m["content"] for m in reversed(payload["messages"]) if m["role"] == "tool"))
            self.assertEqual(facts["executed_tool"], "simulate")
            self.assertEqual(facts["analysis_scope"], "current_plan_after_measures")
            self.assertEqual(facts["facts"]["N_crit"]["value"], 1)
            critical = facts["facts"]["critical_indicators"]
            self.assertEqual(len(critical), 1)
            self.assertEqual(critical[0]["district"], "almaty")
            self.assertEqual(critical[0]["value"]["value"], 39.38)
            self.assertIn("ПОСЛЕ", facts["instruction"])
            return text_message("Осталось критических показателей: {{e2:/N_crit}}. "
                                "ЖКХ Алматы — {{e2:/critical_indicators/0/value}}. "
                                "Слабейший район — Нура, D {{e2:/min_district/D}}.")

        with patch.object(self, "result", current):
            response, requests = self.run_api(responder, "Какие риски остались после события?")
        self.assertFalse(response["offline"])
        self.assertEqual(len(requests), 2)
        self.assertEqual(response["tool_calls"][-1]["name"], "simulate")
        self.assertEqual(response["tool_calls"][-1]["requested_name"], "baseline")
        self.assertEqual(response["tool_calls"][-1]["arguments"], {"decisions": plan, "event_id": "E1"})
        self.assertIn("39,38", response["answer"])
        self.assertIn("54,09", response["answer"])
        self.assertNotIn("49,18", response["answer"])

    def test_nura_baseline_question_is_not_rerouted_to_current_risks(self):
        def responder(payload, turn):
            if turn == 1:
                self.assertEqual(payload["tool_choice"], "required")
                return tool_message("baseline", {})
            return text_message("До проектов оценка Нуры — {{e2:/min_district/D}}.")

        response, _ = self.run_api(responder, "Почему Нура так важна?")
        self.assertFalse(response["offline"])
        self.assertEqual(response["tool_calls"][-1]["name"], "baseline")
        self.assertFalse(advisor._current_plan_risks("Какие критические показатели были до проектов?"))

    def test_explicit_event_overrides_inferred_context_and_offline_keeps_it(self):
        supplied = engine.simulate(self.plan, event_id="E2")
        with patch.object(advisor, "read_settings", return_value=EMPTY_SETTINGS), \
             patch.object(advisor, "_create_client") as factory:
            response = advisor.ask_advisor(AUTO_QUESTION, supplied, event_id="E5")
        factory.assert_not_called()
        expected = engine.simulate(self.plan, event_id="E5")
        self.assertEqual(response["event_id"], "E5")
        self.assertEqual(response["tool_calls"][0]["result"]["score"], expected["score"])
        self.assertIn("Вспышка смога", response["answer"])
        self.assertIn(str(expected["score"]).replace(".", ","), response["answer"])
        self.assertEqual(supplied["event"]["id"], "E2")

    def test_budget_cut_limits_rules_and_fallback_search_to_event_budget(self):
        plan = engine.optimize(top_n=1, constraints={"budget": 80})["results"][0]["decisions"]
        supplied = engine.simulate(plan, event_id="E6")
        with patch.object(advisor, "read_settings", return_value=EMPTY_SETTINGS):
            response = advisor.ask_advisor("Предложи бюджет 100", supplied)
        self.assertEqual(response["event_id"], "E6")
        self.assertIn("не выше 85", response["answer"])
        searched = next(call for call in response["tool_calls"] if call["name"] == "optimize")
        self.assertEqual(searched["arguments"]["event_id"], "E6")
        self.assertEqual(searched["result"]["constraints"]["budget"], 85)
        self.assertTrue(all(plan["cost"] <= 85 for plan in searched["result"]["plans"]))
        self.assertTrue(all(plan["event_id"] == "E6" for plan in response["checked_plans"]))

    def test_budget_cut_invalidates_expensive_plan_without_api(self):
        with patch.object(advisor, "_create_client") as factory:
            response = advisor.ask_advisor(AUTO_QUESTION, self.result, event_id="E6")
        factory.assert_not_called()
        self.assertEqual(response["reason_code"], "invalid_plan")
        self.assertIn("85", response["answer"])
        self.assertIn("95", response["answer"])
        self.assertEqual(response["tool_calls"][0]["status"], "invalid")
        self.assertEqual(response["tool_calls"][0]["arguments"]["event_id"], "E6")

    def test_model_cannot_override_the_event(self):
        tools = EngineTools(event_id="E6")
        with patch.object(tools.engine, "simulate") as simulate:
            response = tools.call("simulate", {"decisions": self.plan, "event_id": None})
        simulate.assert_not_called()
        self.assertEqual(response["status"], "error")
        self.assertEqual(tools.event_id, "E6")

    def test_event_catalogue_and_robustness_are_explicitly_before_events(self):
        from agent.evidence import REFERENCE, resolve_pointer
        from agent.offline import offline_response

        tools = EngineTools(event_id="E2")
        for name, arguments, expected, scope in (
            ("list_events", {}, engine.list_events(), "event_catalogue"),
            ("robustness", {"decisions": self.plan}, engine.robustness(self.plan), "before_events"),
        ):
            response = tools.call(name, arguments)
            self.assertEqual(response["result"], expected)  # E2 не наложено повторно.
            self.assertNotIn("event_id", tools.trace[-1]["arguments"])
            payload = tools.model_payload(response["evidence_id"])
            self.assertEqual(payload["context"], {"event_id": "E2", "scope": scope})
            for match in REFERENCE.finditer(json.dumps(payload)):
                self.assertIsNotNone(resolve_pointer(tools.evidence[match[1]], match[2]))
            offline = offline_response(engine.simulate(self.plan, event_id="E2"),
                                       trace=tools.trace, evidence=tools.evidence)
            self.assertIn("обычного базиса", offline["answer"])
        self.assertIn("Устойчивость плана до событий", offline["answer"])

    def test_checked_plans_do_not_leak_between_event_contexts(self):
        plans = [{"decisions": self.plan}, {"decisions": self.plan, "event_id": "E2"},
                 {"decisions": self.plan, "event_id": "E6"}]
        self.assertEqual([p.get("event_id") for p in advisor._checked(plans, "E2")], ["E2"])
        self.assertEqual([p.get("event_id") for p in advisor._checked(plans)], [None])

    def test_constrained_search_answers_after_first_successful_tool(self):
        constraints = {"exclude": ["M3"], "include": [{"measure": "M7", "district": "nura"}], "budget": 80}
        chosen = []

        def responder(payload, turn):
            if turn == 1:
                self.assertEqual(payload["tool_choice"], "required")
                return tool_message("optimize", {"constraints": constraints})
            if turn == 2:
                last = json.loads(next(m["content"] for m in reversed(payload["messages"]) if m["role"] == "tool"))
                chosen.extend(last["facts"]["results"][0]["decisions"])
                self.assertEqual(last["facts"]["results"][0]["score"]["ref"], "{{e2:/results/0/score}}")
            self.assertEqual(payload["tool_choice"], "none")
            return text_message("Проверенный вариант: оценка {{e2:/results/0/score}}, стоимость {{e2:/results/0/cost}} у.е.")

        response, requests = self.run_api(responder, "Без ЛРТ, школа в Нуре, бюджет до 80")
        self.assertFalse(response["offline"])
        self.assertEqual(len(requests), 2)
        self.assertEqual([call["name"] for call in response["tool_calls"]], ["simulate", "optimize"])
        self.assertEqual(response["tool_calls"][1]["arguments"]["constraints"], constraints)
        self.assertNotIn("M3", [d["measure"] for d in chosen])
        self.assertIn({"measure": "M7", "district": "nura"}, chosen)
        self.assertLessEqual(engine.simulate(chosen)["cost"], 80)

    def test_auto_analysis_forces_checked_alternative(self):
        def responder(payload, turn):
            if turn == 1:
                self.assertEqual(payload["tool_choice"]["function"]["name"], "optimize")
                # Модель не должна превращать поиск альтернативы в фиксацию исходного плана.
                return tool_message("optimize", {"constraints": {"include": self.plan, "budget": 95}})
            return text_message("Оценка текущего плана {{e1:/score}}; найденный план {{e2:/results/0/score}}.")
        response, requests = self.run_api(responder, AUTO_QUESTION)
        self.assertFalse(response["offline"])
        self.assertEqual(len(requests), 2)
        search_call = next(call for call in response["tool_calls"] if call["name"] == "optimize")
        self.assertEqual(search_call["arguments"]["constraints"], {})
        self.assertTrue(response["checked_plans"])
        current = sorted((item["measure"], item["district"] or "") for item in self.plan)
        self.assertTrue(any(sorted((item["measure"], item.get("district") or "") for item in plan["decisions"]) != current
                            for plan in response["checked_plans"]))

    def test_auto_current_optimum_keeps_the_distinct_alternative_for_followup(self):
        plans = engine.optimize(top_n=2)["results"]
        current = plans[0]["decisions"]

        def responder(payload, turn):
            if turn == 1:
                return tool_message("optimize", {"constraints": {}})
            facts = json.loads(next(m["content"] for m in reversed(payload["messages"]) if m["role"] == "tool"))
            self.assertEqual(facts["alternative_reference"], "{{e2:/results/1/score}}")
            self.assertNotIn("results", facts["facts"])
            self.assertEqual(facts["facts"]["recommended_plan"]["decisions"], plans[1]["decisions"])
            return text_message("Оценка другого состава {{e2:/results/1/score}}.")

        with patch.object(self, "result", engine.simulate(current)):
            response, _ = self.run_api(responder, AUTO_QUESTION)
        self.assertFalse(response["offline"])
        self.assertIn(plans[1]["decisions"], [plan["decisions"] for plan in response["checked_plans"]])

    def test_hallucinated_numbers_are_repaired_before_display(self):
        def responder(payload, turn):
            if turn == 1:
                return tool_message("baseline", {})
            if turn == 2:
                return text_message("Оценка будет 999999.")
            return text_message("Оценка по движку — {{e1:/score}}; база — {{e2:/score}}.")
        response, requests = self.run_api(responder)
        self.assertFalse(response["offline"])
        self.assertEqual(len(requests), 3)
        self.assertNotIn("999999", response["answer"])

    def test_second_rejected_answer_falls_back_to_latest_search(self):
        constraints = {"exclude": ["M3"], "budget": 72}

        def responder(payload, turn):
            if turn == 1:
                return tool_message("optimize", {"constraints": constraints})
            return text_message("Предлагаю Score 999999 без расчётов.")

        response, requests = self.run_api(responder, "Найди план без ЛРТ с бюджетом до 72")
        self.assertTrue(response["offline"])
        self.assertEqual(response["reason_code"], "answer_validation")
        self.assertEqual(len(requests), 3)  # Инструмент, ответ и ровно одна попытка исправления.
        self.assertIn("стоимость — 72 у.е.", response["answer"])
        self.assertNotIn("Стоимость — 95", response["answer"])
        self.assertNotIn("999999", response["answer"])
        self.assertTrue(response["checked_plans"])

    def test_model_must_actually_call_a_tool(self):
        response, requests = self.run_api(lambda *_: text_message("Оценка {{e1:/score}}."))
        self.assertTrue(response["offline"])
        self.assertLessEqual(len(requests), 2)
        self.assertEqual(response["reason_code"], "answer_validation")
        self.assertFalse(any(call["source"] == "model" for call in response["tool_calls"]))

    def test_loop_and_tool_execution_limits(self):
        response, requests = self.run_api(lambda *_: tool_message("baseline", {}))
        self.assertTrue(response["offline"])
        self.assertLessEqual(len(requests), 4)
        self.assertLessEqual(len([call for call in response["tool_calls"] if call["source"] == "model"]), 2)

    def test_malformed_arguments_and_unknown_tool_are_not_executed(self):
        def responder(payload, turn):
            if turn == 1:
                return tool_message("load_data", {"path": "do-not-open"})
            if turn == 2:
                return tool_message("simulate", "{malformed")
            return text_message("Оценка {{e1:/score}}.")
        response, requests = self.run_api(responder)
        self.assertTrue(response["offline"])
        self.assertLessEqual(len(requests), 4)
        self.assertEqual([c["status"] for c in response["tool_calls"]][1:], ["error", "error"])

    def test_api_errors_and_timeout_fall_back_without_secrets(self):
        for mode in ("unauthorized", "timeout"):
            def responder(*_):
                if mode == "timeout":
                    raise httpx.ReadTimeout("secret-do-not-print")
                return httpx.Response(401, json={"error": {"message": "secret-do-not-print"}})
            response, _ = self.run_api(responder)
            self.assertTrue(response["offline"])
            self.assertEqual(response["reason_code"], "timeout" if mode == "timeout" else "api_error")
            self.assertNotIn("secret-do-not-print", json.dumps(response))

    def test_history_keeps_last_six_messages_and_checked_plans(self):
        history = [{"role": "user" if index % 2 == 0 else "assistant", "content": f"history_marker_{index}"}
                   for index in range(8)]
        constraints = {"exclude": ["M3"], "budget": 72}
        chosen = engine.optimize(top_n=1, constraints=constraints)["results"][0]["decisions"]
        plans = [{"decisions": chosen, "constraints": constraints}]

        def responder(payload, turn):
            if turn == 1:
                context = json.dumps(payload["messages"], ensure_ascii=False)
                for index in range(2):
                    self.assertNotIn(f"history_marker_{index}", context)
                for index in range(2, 8):
                    self.assertIn(f"history_marker_{index}", context)
                contexts = []
                for message in payload["messages"]:
                    try:
                        contexts.append(json.loads(message.get("content") or ""))
                    except (ValueError, TypeError):
                        pass

                def contains_checked_plan(value):
                    if isinstance(value, dict):
                        return value.get("decisions") == chosen or any(contains_checked_plan(v) for v in value.values())
                    return isinstance(value, list) and any(contains_checked_plan(v) for v in value)

                self.assertTrue(contains_checked_plan(contexts))
                return tool_message("baseline", {})
            return text_message("Исходная оценка {{e2:/score}}.")

        response, requests = self.run_api(responder, "А этот вариант дешевле?", history=history, checked_plans=plans)
        self.assertFalse(response["offline"])
        self.assertEqual(len(requests), 2)

    def test_impossible_six_projects_and_larger_budget_keep_rule_notice(self):
        def responder(payload, turn):
            if turn == 1:
                self.assertEqual(payload["tool_choice"]["function"]["name"], "optimize")
                context = json.loads(payload["messages"][-1]["content"])
                self.assertNotIn("150", context["question"])
                return tool_message("optimize", {"constraints": {
                    "include": [*self.plan, {"measure": "M11", "district": "nura"}], "budget": 150}})
            return text_message("Запрос нарушает правила симулятора.")

        response, requests = self.run_api(responder, "Добавь шестую меру, бюджет 150")
        self.assertFalse(response["offline"])
        self.assertEqual(len(requests), 2)
        self.assertIn("5", response["answer"])
        self.assertIn("100", response["answer"])
        self.assertIn("бюджет", response["answer"].lower())
        optimized = next(call for call in response["tool_calls"] if call["name"] == "optimize")
        self.assertEqual(optimized["arguments"], {"constraints": {}})
        self.assertEqual(optimized["requested_arguments"]["constraints"]["budget"], 150)
        self.assertTrue(response["checked_plans"])

    def test_same_plan_cheaper_uses_latest_checked_plan_even_with_wrong_model_arguments(self):
        constraints = {"exclude": ["M3"], "budget": 72}
        chosen = engine.optimize(top_n=1, constraints=constraints)["results"][0]["decisions"]

        def responder(payload, turn):
            if turn == 1:
                self.assertEqual(payload["tool_choice"]["function"]["name"], "simulate")
                # Модель ошибочно взяла исходный план: приложение должно восстановить обсуждаемый.
                return tool_message("simulate", {"decisions": self.plan})
            self.assertEqual(payload["tool_choice"], "none")
            return text_message("Стоимость этого состава {{e2:/cost}} у.е. Для экономии нужна замена мер.")

        checked = [{"decisions": self.plan, "constraints": {}}, {"decisions": chosen, "constraints": constraints}]
        response, requests = self.run_api(responder, "А если оставить тот же набор, но дешевле?", checked_plans=checked)
        self.assertFalse(response["offline"])
        self.assertEqual(len(requests), 2)
        self.assertEqual(response["tool_calls"][-1]["arguments"]["decisions"], chosen)
        self.assertEqual(response["tool_calls"][-1]["result"]["cost"], 72)
        self.assertIn("72", response["answer"])

    def test_evidence_guard(self):
        evidence = {"e1": {"score": 52.56, "districts": [{"name": "Нура"}]}}
        self.assertEqual(render_grounded_answer("База {{e1:/score}}.", evidence), "База 52,56.")
        self.assertEqual(render_grounded_answer("База 52.56.", evidence), "База 52.56.")
        for draft in ("База 999999", "Рост вдвое", "{{e2:/score}}", "{{e1:/missing}}", "{{e1:/districts}}",
                      "Оценка -{{e1:/score}}", "{{e1:/score}}{{e1:/score}}", "{{e1:/score}}%",
                      "{{e1:/score}}e{{e1:/score}}", "{{e1:/score}},{{e1:/score}}"):
            with self.assertRaises(EvidenceError, msg=draft):
                render_grounded_answer(draft, evidence)

    def test_literal_number_roles_and_small_numerals_match_engine_fields(self):
        evidence = {"e0": {"budget": 100, "num_decisions": 5},
                    "e1": {"score": 52.56, "N_crit": 2, "remaining_critical": 0},
                    "e2": {"constraints": {"budget": 80}, "results": [{"score": 56.87, "cost": 72}]}}
        for draft in ("Score 52,56.", "Бюджет до 80.", "Общий бюджет до 100.",
                      "Пять проектов, два показателя; план с нулём критических значений."):
            self.assertEqual(render_grounded_answer(draft, evidence), draft)
        for draft in ("Score 5.", "Score пять.", "Бюджет до 100.", "Бюджет до двух.",
                      "Нужно семь проектов.", "два пять", "сто пять", "два-пять", "Улучшение вдвое.",
                      "Score 999999.", "Качество 80%."):
            with self.assertRaises(EvidenceError, msg=draft):
                render_grounded_answer(draft, evidence)

    def test_brief_tool_payload_keeps_negative_effect_and_original_json_index(self):
        # M11 стоит не в начале списка: фильтрация не должна сдвинуть JSON Pointer.
        plan = [{"measure": "M9", "district": "nura"}, {"measure": "M10", "district": "nura"},
                {"measure": "M12", "district": None}, {"measure": "M11", "district": "nura"},
                {"measure": "M4", "district": "nura"}]
        tools = EngineTools()
        actual = tools.call("simulate", {"decisions": plan}, source="preparation")
        self.assertEqual(actual["status"], "ok")
        index = next(i for i, measure in enumerate(actual["result"]["measures"]) if measure["measure"] == "M11")
        self.assertGreater(index, 0)
        brief = tools.model_payload(actual["evidence_id"], brief=True)
        negative = brief["facts"]["measures"][index]["effects_realized"]["T1"]
        self.assertEqual(negative["value"], -1.75)
        self.assertEqual(negative["ref"], "{{" + actual["evidence_id"] + f":/measures/{index}/effects_realized/T1" + "}}")
        self.assertEqual(render_grounded_answer(negative["ref"], tools.evidence), "-1,75")

    def test_conversation_keeps_checked_plan_after_more_than_six_plain_messages(self):
        from legacy.ui import advisor_panel

        constraints = {"exclude": ["M3"], "budget": 72}
        chosen = engine.optimize(top_n=1, constraints=constraints)["results"][0]["decisions"]
        checked = {"decisions": chosen, "constraints": constraints}
        old = {"decisions": self.plan, "constraints": {}}
        messages = [{"role": "assistant", "content": "Проверенный план за 72.",
                     "response": {"checked_plans": [checked]}}]
        messages += [{"role": "user" if i % 2 == 0 else "assistant", "content": f"plain_message_{i}"}
                     for i in range(8)]

        class Session(dict):
            __getattr__ = dict.__getitem__

        session = Session(messages=messages, advisor_analysis={"checked_plans": [old]})
        with patch.object(advisor_panel, "st", SimpleNamespace(session_state=session)):
            history, plans = advisor_panel._conversation_context()
        self.assertEqual(len(history), 6)
        self.assertEqual(plans[-1], checked)
        self.assertEqual(engine.simulate(plans[-1]["decisions"])["cost"], 72)
        self.assertEqual(history[0]["content"], "plain_message_2")

    def test_evidence_guard_accepts_words_and_known_identifier_codes(self):
        evidence = {"e1": {"score": 52.56, "measure": "M7", "indicators": {"S1": 38}}}
        for draft in ("Сотрудники поликлиники", "Сотрудничество с жителями", "Пятна загрязнения",
                      "Мера M7 влияет на S1."):
            self.assertEqual(render_grounded_answer(draft, evidence), draft)
        for draft in ("Нужно пять проектов", "Улучшение вдвое", "Мера M999", "Показатель S999"):
            with self.assertRaises(EvidenceError, msg=draft):
                render_grounded_answer(draft, evidence)

    def test_evidence_accepts_extra_result_prefix_and_explains_wrong_field(self):
        evidence = {"e1": {"score": 52.56}}
        self.assertEqual(render_grounded_answer("База {{e1:/result/score}}.", evidence), "База 52,56.")
        # Настоящее поле result не следует путать с лишней обёрткой транспорта.
        nested = {"e1": {"result": {"score": 12}}}
        self.assertEqual(render_grounded_answer("База {{e1:/result/score}}.", nested), "База 12.")
        with self.assertRaises(EvidenceError) as raised:
            render_grounded_answer("База {{e1:/score_missing}}.", evidence)
        self.assertIn("score_missing", str(raised.exception))
        self.assertIn("score", str(raised.exception))

    def test_environment_precedence_and_optional_base_url(self):
        with patch("dotenv.dotenv_values", return_value={"OPENAI_API_KEY": "file-key", "OPENAI_MODEL": "file-model",
                                                        "OPENAI_BASE_URL": ""}), \
             patch.dict(os.environ, {"OPENAI_API_KEY": "env-key"}, clear=True):
            settings = advisor.read_settings()
        self.assertEqual(settings, {"OPENAI_API_KEY": "env-key", "OPENAI_MODEL": "file-model", "OPENAI_BASE_URL": ""})
        with patch.dict(os.environ, {"OPENAI_BASE_URL": ""}):
            client = advisor._create_client({**SETTINGS, "OPENAI_BASE_URL": ""})
            self.assertEqual(str(client.base_url), "https://api.openai.com/v1/")
            client.close()

    def test_final_validation_is_only_the_engine(self):
        adapter = EngineAdapter()
        with patch.object(adapter, "_local_errors", side_effect=AssertionError("Не вызывать локальную проверку")), \
             patch.object(adapter.engine, "validate", return_value={"valid": False, "errors": ["Ответ движка"]}) as validate:
            self.assertEqual(adapter.validate(self.plan), {"valid": False, "errors": ["Ответ движка"]})
            validate.assert_called_once_with(self.plan)
        adapter.engine = None
        with self.assertRaises(EngineUnavailable):
            adapter.validate(self.plan)

    def test_streamlit_auto_analysis_is_cached_and_invalidated(self):
        from streamlit.testing.v1 import AppTest
        from agent.offline import offline_response

        asked = []
        contexts = []
        constraints = {"exclude": ["M3"], "budget": 72}
        checked = [{"decisions": engine.optimize(top_n=1, constraints=constraints)["results"][0]["decisions"],
                    "constraints": constraints}]
        def fake_advisor(question, result, **kwargs):
            asked.append(question)
            contexts.append(deepcopy(kwargs))
            response = offline_response(result, trace=[{"name": "simulate", "arguments": {"decisions": self.plan},
                "result": {"score": result["score"]}, "summary": "Результат проверен.", "source": "model", "status": "ok"}])
            response["checked_plans"] = deepcopy(checked)
            return response
        app = AppTest.from_file(str(advisor.ROOT / "legacy" / "app.py"), default_timeout=30)
        app.session_state["decisions"] = deepcopy(self.plan)
        with patch.object(advisor, "ask_advisor", side_effect=fake_advisor):
            app.run()
            app.button(key="calculate").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(asked, [AUTO_QUESTION])
            self.assertTrue(any(e.label == "Как советник пришёл к выводу" for e in app.expander))
            app.selectbox(key="selected_district").set_value("nura").run()
            self.assertEqual(len(asked), 1)
            app.chat_input[0].set_value("Как улучшить?").run()
            self.assertEqual(len(asked), 2)
            self.assertIn("history", contexts[-1])
            self.assertTrue(contexts[-1].get("checked_plans"))
            app.chat_input[0].set_value("А дешевле?").run()
            self.assertEqual(len(asked), 3)
            self.assertIn("Как улучшить?", json.dumps(contexts[-1]["history"], ensure_ascii=False))
            self.assertLessEqual(len(contexts[-1]["history"]), 6)
            # Неизменившийся план не сбрасывает переписку и не вызывает советника повторно.
            messages = deepcopy(app.session_state["messages"])
            app.button(key="calculate").click().run()
            self.assertEqual(len(asked), 3)
            self.assertEqual(app.session_state["messages"], messages)
            app.button(key="refresh_advisor").click().run()
            self.assertEqual(len(asked), 4)
            app.button(key="remove_0").click().run()
            self.assertIsNone(app.session_state["advisor_analysis"])
            self.assertEqual(app.session_state["messages"], [])
            self.assertTrue(app.chat_input[0].disabled)
            self.assertFalse(app.exception)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main(verbosity=2)
