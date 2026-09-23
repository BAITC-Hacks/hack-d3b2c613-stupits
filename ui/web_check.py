"""Проверки настоящего HTTP API: python -B -m ui.web_check.

Сервер слушает свободный локальный порт. Движок работает без подмены;
только граница советника заменяется mock, чтобы не использовать ключ или сеть.
Тесты не создают файлы и не меняют данные проекта.
"""

from __future__ import annotations

from http.client import HTTPConnection
from html.parser import HTMLParser
import json
import threading
import unittest
from unittest.mock import patch

import engine
from ui.web_server import Handler, MAX_BODY, ROOT, create_server


EXAMPLE = [
    {"measure": "M7", "district": "nura"},
    {"measure": "M8", "district": "nura"},
    {"measure": "M10", "district": "nura"},
    {"measure": "M12", "district": None},
    {"measure": "M5", "district": "saryarka"},
]


class AssetReferences(HTMLParser):
    """Включённый HTML должен получать все локальные скрипты и стили без 404."""
    def __init__(self):
        super().__init__()
        self.paths = set()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        path = values.get("src") if tag == "script" else values.get("href") if tag == "link" else None
        if path and path.startswith("/") and not path.startswith("//"):
            self.paths.add(path)


class WebChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = create_server(port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.quiet = patch.object(Handler, "log_message", return_value=None)
        cls.quiet.start()
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.quiet.stop()

    def request(self, method, path, payload=None, *, raw=None, headers=None):
        body = raw
        request_headers = {"Origin": f"http://127.0.0.1:{self.port}"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        connection = HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            wire = response.read()
            response_headers = dict(response.getheaders())
            value = json.loads(wire.decode("utf-8")) if wire and "application/json" in response_headers.get(
                "Content-Type", "") else wire
            return response.status, response_headers, value
        finally:
            connection.close()

    def post(self, path, payload):
        status, _, value = self.request("POST", path, payload)
        self.assertEqual(status, 200, value)
        return value

    def test_health_and_utf8_bootstrap(self):
        status, headers, value = self.request("GET", "/api/health")
        self.assertEqual((status, value), (200, {"status": "ok"}))
        self.assertIn("charset=utf-8", headers["Content-Type"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        status, _, value = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(value["baseline"]["score"], 52.56)
        self.assertEqual(value["baseline"], engine.baseline())
        self.assertEqual(len(value["catalog"]["measures"]), 14)
        self.assertEqual(len(value["catalog"]["districts"]), 5)
        self.assertIn("Нура", [d["name"] for d in value["catalog"]["districts"]])
        self.assertEqual(value["events"], engine.list_events()["events"])

    def test_simulate_matches_actual_engine(self):
        result = self.post("/api/simulate", {"decisions": EXAMPLE, "score": 999999})
        self.assertEqual(result, engine.simulate(EXAMPLE))
        self.assertTrue(result["valid"])
        self.assertEqual((result["score"], result["delta"]["score"], result["cost"]), (56.54, 3.99, 95))
        self.assertEqual(self.post("/api/validate", {"decisions": EXAMPLE}), engine.validate(EXAMPLE))

    def test_optimize_top_five_and_budget_constraint(self):
        result = self.post("/api/optimize", {"top_n": 5})
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["results"]), 5)
        self.assertEqual(result["results"][0]["score"], 57.24)
        for plan in result["results"]:
            calculated = engine.simulate(plan["decisions"])
            self.assertTrue(calculated["valid"])
            self.assertEqual(calculated["score"], plan["score"])
        limited = self.post("/api/optimize", {"constraints": {"exclude": ["M3"], "budget": 80}})
        for plan in limited["results"]:
            self.assertLessEqual(plan["cost"], 80)
            self.assertNotIn("M3", [d["measure"] for d in plan["decisions"]])

    def test_partial_plan_reports_money_and_completability(self):
        partial = self.post("/api/plan-status", {"decisions": EXAMPLE[:4]})
        self.assertEqual((partial["count"], partial["cost"], partial["budget_left"]), (4, 70, 30))
        self.assertFalse(partial["validation"]["valid"])
        self.assertTrue(partial["can_complete"])
        expensive_four = [
            {"measure": "M3", "district": "nura"}, {"measure": "M13", "district": "almaty"},
            {"measure": "M5", "district": "saryarka"}, {"measure": "M14", "district": None},
        ]
        blocked = self.post("/api/plan-status", {"decisions": expensive_four})
        self.assertEqual((blocked["count"], blocked["cost"], blocked["budget_left"]), (4, 99, 1))
        self.assertFalse(blocked["can_complete"])
        self.assertEqual(blocked["validation"], engine.validate(expensive_four))

    def test_invalid_plans_preserve_engine_errors(self):
        plans = [[], EXAMPLE[:4], EXAMPLE + [EXAMPLE[0]], [EXAMPLE[0]] * 5,
                 [{"measure": "M404", "district": "nura"}, *EXAMPLE[1:]],
                 [{"measure": "M7", "district": None}, *EXAMPLE[1:]],
                 [{"measure": "M7", "district": "missing"}, *EXAMPLE[1:]],
                 [*EXAMPLE[:3], {"measure": "M12", "district": "nura"}, EXAMPLE[4]],
                 [{"measure": [], "district": {}}, *EXAMPLE[1:]],
                 [None, *EXAMPLE[1:]]]
        for plan in plans:
            with self.subTest(plan=plan):
                validation = self.post("/api/validate", {"decisions": plan})
                self.assertEqual(validation, engine.validate(plan))
                self.assertFalse(validation["valid"])
                self.assertTrue(validation["errors"])
                simulation = self.post("/api/simulate", {"decisions": plan})
                self.assertFalse(simulation["valid"])
                self.assertIsNone(simulation.get("score"))

    def test_transport_rejects_malformed_json_and_shapes(self):
        for raw in (b"{", b"[]", b'{"decisions": NaN}', b'\xff'):
            with self.subTest(raw=raw):
                status, _, result = self.request("POST", "/api/simulate", raw=raw)
                self.assertEqual(status, 400)
                self.assertFalse(result["valid"])
                self.assertTrue(result["errors"])
        for path, payload in (
            ("/api/simulate", {"decisions": "M7"}),
            ("/api/simulate", {"decisions": [None] * 31}),
            ("/api/simulate", {"decisions": EXAMPLE, "event_id": []}),
            ("/api/optimize", {"top_n": True}), ("/api/optimize", {"top_n": 6}),
            ("/api/optimize", {"robust": "yes"}), ("/api/compare", {"plans": []}),
            ("/api/advisor", {"decisions": EXAMPLE, "history": {}}),
        ):
            with self.subTest(path=path, payload=payload):
                status, _, result = self.request("POST", path, payload)
                self.assertEqual(status, 400, result)
                self.assertTrue(result["errors"])
        self.assertEqual(self.request("POST", "/api/simulate", raw=b"{}",
                                      headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/simulate", raw=b" " * (MAX_BODY + 1))[0], 413)

    def test_event_baseline_and_simulation_use_same_event(self):
        status, _, bootstrap = self.request("GET", "/api/bootstrap?event_id=E1")
        self.assertEqual(status, 200)
        self.assertEqual(bootstrap["baseline"], engine.baseline(event_id="E1"))
        self.assertNotEqual(bootstrap["baseline"]["score"], 52.56)
        result = self.post("/api/simulate", {"decisions": EXAMPLE, "event_id": " e1 "})
        self.assertEqual(result, engine.simulate(EXAMPLE, event_id="E1"))
        self.assertEqual(result["baseline"]["score"], bootstrap["baseline"]["score"])
        self.assertEqual(result["event"]["id"], "E1")
        unknown = self.post("/api/simulate", {"decisions": EXAMPLE, "event_id": "E404"})
        self.assertFalse(unknown["valid"])
        self.assertTrue(unknown["errors"])

    def test_robustness_and_compare_are_real_engine_results(self):
        robust = self.post("/api/robustness", {"decisions": EXAMPLE})
        self.assertEqual(robust, engine.robustness(EXAMPLE))
        self.assertIn("E6", robust["fails_under"])
        plans = {"Пример ТЗ": EXAMPLE, "Неполный план": EXAMPLE[:4]}
        compared = self.post("/api/compare", {"plans": plans, "event_id": "E1"})
        self.assertEqual(compared, engine.compare(plans, event_id="E1"))
        self.assertTrue(compared["ranking"][0]["valid"])
        self.assertFalse(compared["ranking"][-1]["valid"])

    def test_advisor_distrusts_browser_score_and_preserves_context(self):
        history = [{"role": "user", "content": f"Вопрос {i}"} for i in range(8)]
        checked = [{"decisions": EXAMPLE, "constraints": {}} for _ in range(8)]
        expected = {"answer": "Проверено", "tool_calls": [], "offline": True, "reason": "Тест без API"}
        with patch("agent.advisor.ask_advisor", return_value=expected) as advisor:
            reply = self.post("/api/advisor", {"question": "Почему Нура?", "decisions": EXAMPLE,
                "score": 999999, "simulation_result": {"score": 999999}, "event_id": "e1",
                "history": history, "checked_plans": checked})
            self.assertEqual(reply, expected)
            advisor.assert_called_once_with("Почему Нура?", {"decisions": EXAMPLE},
                                            history=history[-6:], checked_plans=checked[-6:], event_id="E1")

    def test_other_origin_cannot_call_advisor(self):
        with patch("agent.advisor.ask_advisor") as advisor:
            for headers in ({"Origin": "https://other.invalid"}, {"Origin": "http://["},
                            {"Origin": "http://other.invalid", "Host": "other.invalid"}):
                with self.subTest(headers=headers):
                    status, _, result = self.request("POST", "/api/advisor", {"decisions": EXAMPLE},
                                                    headers=headers)
                    self.assertEqual(status, 403)
                    self.assertTrue(result["errors"])
            advisor.assert_not_called()

    def test_static_whitelist_never_serves_project_secrets(self):
        for path in ("/.env", "/.git/config", "/agent/advisor.py", "/README.md",
                     "/../.env", "/%2e%2e/.env", "/web/../.env", "/unknown.js"):
            with self.subTest(path=path):
                status, headers, result = self.request("GET", path)
                self.assertEqual(status, 404)
                self.assertIn("application/json", headers["Content-Type"])
                self.assertTrue(result["errors"])

    def test_index_asset_when_frontend_is_present(self):
        asset = ROOT / "web" / "index.html"
        if not asset.is_file():
            self.skipTest("HTML-интерфейс ещё не создан; HTTP API проверяется независимо.")
        status, headers, result = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html; charset=utf-8", headers["Content-Type"])
        self.assertEqual(result, asset.read_bytes())
        result.decode("utf-8-sig")

    def test_index_local_dependencies_are_served(self):
        asset = ROOT / "web" / "index.html"
        if not asset.is_file():
            self.skipTest("HTML-интерфейс ещё не создан.")
        references = AssetReferences()
        references.feed(asset.read_text(encoding="utf-8-sig"))
        self.assertTrue(references.paths)
        for path in sorted(references.paths):
            with self.subTest(path=path):
                status, _, result = self.request("GET", path)
                self.assertEqual(status, 200, f"HTML ссылается на недоступный ресурс {path}")
                self.assertTrue(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
