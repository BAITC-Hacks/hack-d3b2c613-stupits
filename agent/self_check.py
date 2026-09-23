"""Проверки советника без внешнего API: python -B -m agent.self_check.

HTTP заменяется локальным транспортом; расчёты выполняет настоящий движок.
Файлы engine/, tests/ и данные не меняются. Реальные ключи не используются.
"""

from copy import deepcopy
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI

import engine
from agent import advisor
from agent.evidence import EvidenceError, render_grounded_answer
from agent.prompts import AUTO_QUESTION
from agent.tools import EngineTools
from ui.engine_adapter import EngineAdapter, EngineUnavailable


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

    def run_api(self, responder, question="Объясни мой план"):
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
            response = advisor.ask_advisor(question, self.result)
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
        self.assertEqual([c["name"] for c in response["tool_calls"]], ["baseline", "validate", "simulate"])
        self.assertIn(str(self.result["delta"]["score"]).replace(".", ","), response["answer"])

    def test_constrained_search_then_validate_and_simulate(self):
        constraints = {"exclude": ["M3"], "include": [{"measure": "M7", "district": "nura"}], "budget": 80}
        chosen = []

        def responder(payload, turn):
            if turn == 1:
                self.assertEqual(payload["tool_choice"], "required")
                return tool_message("optimize", {"constraints": constraints})
            if turn == 2:
                last = json.loads(payload["messages"][-1]["content"])
                chosen.extend(last["result"]["results"][0]["decisions"])
                return tool_message("validate", {"decisions": chosen})
            if turn == 3:
                return tool_message("simulate", {"decisions": chosen})
            self.assertEqual(payload["tool_choice"], "none")
            return text_message("Проверенный вариант: оценка {{e6:/score}}, стоимость {{e6:/cost}} у.е.")

        response, requests = self.run_api(responder, "Без ЛРТ, школа в Нуре, бюджет до 80")
        self.assertFalse(response["offline"])
        self.assertEqual(len(requests), 4)
        self.assertEqual(len(response["tool_calls"]), 6)
        self.assertEqual(response["tool_calls"][3]["arguments"]["constraints"], constraints)
        self.assertNotIn("M3", [d["measure"] for d in chosen])
        self.assertIn({"measure": "M7", "district": "nura"}, chosen)
        self.assertLessEqual(engine.simulate(chosen)["cost"], 80)

    def test_auto_analysis_forces_checked_alternative(self):
        def responder(payload, turn):
            if turn == 1:
                self.assertEqual(payload["tool_choice"]["function"]["name"], "optimize")
                return tool_message("optimize", {"constraints": {}})
            return text_message("Оценка текущего плана {{e3:/score}}; найденный план {{e4:/results/0/score}}.")
        response, _ = self.run_api(responder, AUTO_QUESTION)
        self.assertFalse(response["offline"])

    def test_hallucinated_numbers_are_repaired_before_display(self):
        def responder(payload, turn):
            if turn == 1:
                return tool_message("baseline", {})
            if turn == 2:
                return text_message("Оценка будет 999999.")
            return text_message("Оценка по движку — {{e3:/score}}; база — {{e1:/score}}.")
        response, requests = self.run_api(responder)
        self.assertFalse(response["offline"])
        self.assertEqual(len(requests), 3)
        self.assertNotIn("999999", response["answer"])

    def test_model_must_actually_call_a_tool(self):
        response, requests = self.run_api(lambda *_: text_message("Оценка {{e3:/score}}."))
        self.assertTrue(response["offline"])
        self.assertEqual(len(requests), advisor.MAX_STEPS)
        self.assertFalse(any(call["source"] == "model" for call in response["tool_calls"]))

    def test_loop_and_tool_execution_limits(self):
        response, requests = self.run_api(lambda *_: tool_message("baseline", {}))
        self.assertTrue(response["offline"])
        self.assertLessEqual(len(requests), 6)
        self.assertLessEqual(len(response["tool_calls"]), 6)

    def test_malformed_arguments_and_unknown_tool_are_not_executed(self):
        def responder(payload, turn):
            if turn == 1:
                return tool_message("load_data", {"path": "do-not-open"})
            if turn == 2:
                return tool_message("simulate", "{malformed")
            if turn == 3:
                return tool_message("baseline", {})
            return text_message("Оценка {{e3:/score}}.")
        response, _ = self.run_api(responder)
        self.assertFalse(response["offline"])
        self.assertEqual([c["status"] for c in response["tool_calls"]][3:], ["error", "error", "ok"])

    def test_api_errors_and_timeout_fall_back_without_secrets(self):
        for mode in ("unauthorized", "timeout"):
            def responder(*_):
                if mode == "timeout":
                    raise httpx.ReadTimeout("secret-do-not-print")
                return httpx.Response(401, json={"error": {"message": "secret-do-not-print"}})
            response, _ = self.run_api(responder)
            self.assertTrue(response["offline"])
            self.assertNotIn("secret-do-not-print", json.dumps(response))

    def test_evidence_guard(self):
        evidence = {"e1": {"score": 52.56, "districts": [{"name": "Нура"}]}}
        self.assertEqual(render_grounded_answer("База {{e1:/score}}.", evidence), "База 52,56.")
        for draft in ("База 52.56", "Рост вдвое", "{{e2:/score}}", "{{e1:/missing}}", "{{e1:/districts}}",
                      "Оценка -{{e1:/score}}", "{{e1:/score}}{{e1:/score}}", "{{e1:/score}}%",
                      "{{e1:/score}}e{{e1:/score}}", "{{e1:/score}},{{e1:/score}}"):
            with self.assertRaises(EvidenceError, msg=draft):
                render_grounded_answer(draft, evidence)

    def test_environment_precedence_and_optional_base_url(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / ".env").write_text("OPENAI_API_KEY=file-key\nOPENAI_MODEL=file-model\nOPENAI_BASE_URL=\n", encoding="utf-8")
            with patch.object(advisor, "ROOT", Path(folder)), patch.dict(os.environ, {"OPENAI_API_KEY": "env-key"}, clear=True):
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
        def fake_advisor(question, result):
            asked.append(question)
            return offline_response(result, trace=[{"name": "simulate", "arguments": {"decisions": self.plan},
                "result": {"score": result["score"]}, "summary": "Результат проверен.", "source": "model", "status": "ok"}])
        app = AppTest.from_file(str(advisor.ROOT / "app.py"), default_timeout=30)
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
            app.button(key="refresh_advisor").click().run()
            self.assertEqual(len(asked), 3)
            app.button(key="remove_0").click().run()
            self.assertIsNone(app.session_state["advisor_analysis"])
            self.assertEqual(app.session_state["messages"], [])
            self.assertTrue(app.chat_input[0].disabled)
            self.assertFalse(app.exception)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main(verbosity=2)
