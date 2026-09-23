"""Локальный HTTP-интерфейс: только движок считает, браузер отображает ответы.

Статические файлы доступны по белому списку. Ключи и исходники проекта
никогда не раздаются браузеру. Состояние каждого пользователя живёт в его вкладке.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit
import webbrowser

import engine


ROOT = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger(__name__)
MAX_BODY = 128 * 1024
CLIENT_DISCONNECTED = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/interface.js": ("interface.js", "text/javascript; charset=utf-8"),
    "/map.js": ("map.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/vendor/maplibre-gl.js": ("vendor/maplibre-gl.js", "text/javascript; charset=utf-8"),
    "/vendor/maplibre-gl.css": ("vendor/maplibre-gl.css", "text/css; charset=utf-8"),
}
POST_ROUTES = {
    "/api/validate", "/api/simulate", "/api/plan-status", "/api/optimize",
    "/api/robustness", "/api/compare", "/api/advisor",
}


def event_id(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Код события должен быть короткой строкой или null.")
    return value.strip().upper() or None


def decisions(body):
    """Проверяем размер транспорта; содержимое и правила проверяет validate()."""
    result = body.get("decisions")
    if not isinstance(result, list) or len(result) > 30:
        raise ValueError("Передайте decisions — список решений, не более 30 записей.")
    return deepcopy(result)


class Backend:
    def __init__(self, project: Path = ROOT):
        self.project = project.resolve()
        self.data = engine.load_data(self.project / "data" / "city_data.json")
        self.catalog = deepcopy(self.data.raw)
        self.events = engine.list_events(data=self.data)["events"]
        self.optimize_lock = threading.Lock()
        # Геометрия необязательна: районы и расчёты доступны без карты.
        try:
            geometry = json.loads((self.project / "data" / "astana_districts.geojson").read_text(encoding="utf-8-sig"))
            self.geojson = geometry if (isinstance(geometry, dict)
                and geometry.get("type") == "FeatureCollection"
                and isinstance(geometry.get("features"), list)) else None
        except (OSError, ValueError):
            self.geojson = None

    def baseline(self, selected_event=None):
        return engine.baseline(data=self.data, event_id=event_id(selected_event))

    def bootstrap(self, selected_event=None):
        baseline = self.baseline(selected_event)
        if baseline.get("valid") is False:
            raise ValueError(" ".join(baseline["errors"]))
        # Административная карта и учебный датасет имеют разный охват.
        # Наличие полигона не означает наличие показателей для расчёта Score.
        modeled = [row["id"] for row in baseline["districts"]]
        features = (self.geojson or {}).get("features", [])
        geographic = [f.get("properties", {}).get("id") for f in features]
        geographic = list(dict.fromkeys(item for item in geographic if item))
        unmodeled = [item for item in geographic if item not in modeled]
        sources = list(dict.fromkeys(f.get("properties", {}).get("source_url", "")
                                    for f in features))
        return {"baseline": baseline, "catalog": self.catalog, "geojson": self.geojson,
                "events": self.events, "mode": "live",
                "model_scope": {"district_ids": modeled,
                    "geographic_district_ids": geographic,
                    "unmodeled_district_ids": unmodeled,
                    "notice": "Расчёт использует районы и показатели датасета задания. "
                              "Районы без исходных показателей показаны на карте без оценки."},
                "geography": {"sources": [url for url in sources if url],
                    "has_approximate": any(f.get("properties", {}).get("source") == "fallback_approx"
                                           for f in features),
                    "feature_count": len(features), "modeled_count": len(modeled)}}

    def plan_status(self, plan, selected_event):
        # simulate возвращает стоимость и бюджет даже у неполного плана.
        # Поэтому JS не содержит вторую реализацию расчёта денег.
        report = engine.simulate(plan, data=self.data, event_id=selected_event)
        validation = engine.validate(plan, data=self.data, event_id=selected_event)
        can_complete = None
        if len(plan) == self.data.num_decisions - 1:
            can_complete = False
            for mid, measure in self.data.measures.items():
                targets = self.data.districts if measure["scope"] == "district" else (None,)
                for target in targets:
                    candidate = [*plan, {"measure": mid, "district": target}]
                    if engine.validate(candidate, data=self.data, event_id=selected_event)["valid"]:
                        can_complete = True
                        break
                if can_complete:
                    break
        return {key: report.get(key) for key in ("cost", "budget", "budget_left")} | {
            "count": len(plan), "can_complete": can_complete,
            "validation": validation, "errors": validation["errors"],
        }

    def post(self, path, body):
        selected_event = event_id(body.get("event_id"))
        if path in ("/api/validate", "/api/simulate"):
            function = engine.validate if path.endswith("validate") else engine.simulate
            return function(decisions(body), data=self.data, event_id=selected_event)
        if path == "/api/plan-status":
            return self.plan_status(decisions(body), selected_event)
        if path == "/api/optimize":
            top_n = body.get("top_n", 5)
            if isinstance(top_n, bool) or not isinstance(top_n, int) or not 1 <= top_n <= 5:
                raise ValueError("Число лучших планов должно быть целым от 1 до 5.")
            robust = body.get("robust", False)
            if not isinstance(robust, bool):
                raise ValueError("Режим устойчивого поиска должен быть true или false.")
            # Не запускаем параллельно тяжёлые переборы после повторных кликов.
            with self.optimize_lock:
                return engine.optimize(top_n=top_n, constraints=body.get("constraints"),
                                       robust=robust, data=self.data, event_id=selected_event)
        if path == "/api/robustness":
            # Официальный контракт: план выбран ДО события; события не складываются.
            return engine.robustness(decisions(body), data=self.data)
        if path == "/api/compare":
            plans = body.get("plans")
            if not isinstance(plans, dict) or not 1 <= len(plans) <= 10:
                raise ValueError("Для сравнения передайте от одного до десяти именованных планов.")
            for name, plan in plans.items():
                if len(name) > 120:
                    raise ValueError("Название плана слишком длинное.")
                decisions(plan if isinstance(plan, dict) else {"decisions": plan})
            return engine.compare(plans, data=self.data, event_id=selected_event)
        if path == "/api/advisor":
            # Браузер присылает только решения: его Score и тексты не являются фактами.
            from agent.advisor import ask_advisor

            plan = decisions(body)
            question = body.get("question", "")
            if not isinstance(question, str) or len(question) > 4000:
                raise ValueError("Вопрос должен быть строкой не длиннее 4000 символов.")
            history = body.get("history", [])
            checked = body.get("checked_plans", [])
            if not isinstance(history, list) or not isinstance(checked, list):
                raise ValueError("История и проверенные планы должны быть списками.")
            # ask_advisor сам пересчитает этот состав перед любым ответом LLM.
            return ask_advisor(question, {"decisions": plan}, history=history[-6:],
                               checked_plans=checked[-6:], event_id=selected_event)
        raise KeyError(path)


class Handler(BaseHTTPRequestHandler):
    server_version = "Akim/2.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def handle(self):
        try:
            super().handle()
        except CLIENT_DISCONNECTED:
            # Обновление страницы может оборвать чтение запроса или flush ответа.
            self.close_connection = True

    def send_bytes(self, status, data, content_type):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except CLIENT_DISCONNECTED:
            # Заголовки тоже пишут в сокет. В закрытое соединение не отправляем 500.
            self.close_connection = True

    def json_reply(self, status, value):
        self.send_bytes(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                        "application/json; charset=utf-8")

    def error_reply(self, status, message):
        self.json_reply(status, {"valid": False, "errors": [message], "score": None})

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/api/health":
            self.json_reply(200, {"status": "ok"})
            return
        try:
            selected_event = parse_qs(parsed.query).get("event_id", [None])[0]
            if path == "/api/bootstrap":
                self.json_reply(200, self.server.backend.bootstrap(selected_event))
            elif path == "/api/baseline":
                self.json_reply(200, self.server.backend.baseline(selected_event))
            elif path == "/api/events":
                self.json_reply(200, {"events": self.server.backend.events})
            elif path in ASSETS:
                filename, content_type = ASSETS[path]
                asset = self.server.backend.project / "web" / filename
                if not asset.is_file():
                    self.error_reply(404, "Файл интерфейса не найден.")
                else:
                    self.send_bytes(200, asset.read_bytes(), content_type)
            else:
                self.error_reply(404, "Страница не найдена.")
        except ValueError as exc:
            self.error_reply(400, str(exc))
        except Exception:
            LOGGER.exception("Ошибка загрузки данных интерфейса")
            self.error_reply(500, "Не удалось загрузить данные движка.")

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in POST_ROUTES:
            self.error_reply(404, "Метод не найден.")
            return
        # HTML и API на одном origin; сторонние страницы не должны расходовать ключ.
        origin = self.headers.get("Origin")
        try:
            host = urlsplit("http://" + self.headers.get("Host", "")).hostname
            allowed_host = host in {"localhost", "127.0.0.1", "::1", self.server.server_address[0]}
            if not allowed_host and self.server.server_address[0] == "0.0.0.0":
                # При явном сетевом запуске допускаем обращение по адресу машины.
                allowed_host = ipaddress.ip_address(host).is_private
            if not allowed_host:
                raise ValueError("Неизвестный адрес сервера")
            parsed = urlsplit(origin) if origin else None
            if parsed and (parsed.scheme not in ("http", "https") or parsed.netloc != self.headers.get("Host")):
                raise ValueError("Сторонний источник запроса")
        except ValueError:
            self.error_reply(403, "Откройте интерфейс через адрес этого сервера.")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                # Небольшое превышение дочитываем перед закрытием соединения:
                # иначе Windows может послать TCP reset раньше ответа 413.
                if MAX_BODY < length <= MAX_BODY * 2:
                    self.rfile.read(length)
                self.error_reply(413, "Пустой или слишком большой запрос.")
                return
            if self.headers.get_content_type() != "application/json":
                self.error_reply(415, "Ожидается Content-Type: application/json.")
                return
            body = json.loads(self.rfile.read(length).decode("utf-8-sig"), parse_constant=_invalid_number)
            if not isinstance(body, dict):
                raise ValueError("Тело запроса должно быть JSON-объектом.")
            self.json_reply(200, self.server.backend.post(path, body))
        except CLIENT_DISCONNECTED:
            # Клиент мог уйти со страницы до окончания чтения тела запроса.
            self.close_connection = True
        except (ValueError, UnicodeDecodeError) as exc:
            self.error_reply(400, str(exc))
        except Exception:
            # Не выводим body, настройки провайдера или содержимое .env.
            LOGGER.exception("Ошибка обработчика %s", path)
            self.error_reply(500, "Не удалось выполнить запрос. Попробуйте ещё раз.")


def _invalid_number(value):
    raise ValueError("JSON должен содержать только конечные числа.")


def create_server(project: Path = ROOT, port: int = 8501, host: str = "127.0.0.1"):
    backend = Backend(project)
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.backend = backend
    return server


def main():
    parser = argparse.ArgumentParser(description="Аким на 5 часов — городской симулятор")
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--open", action="store_true", help="Открыть браузер после запуска")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Порт должен быть от 1 до 65535.")
    try:
        server = create_server(port=args.port, host=args.host)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 10048 or getattr(exc, "errno", None) in (48, 98, 10048):
            parser.exit(1, "Порт занят. Закройте прежнее приложение или задайте другой PORT.\n")
        raise
    address = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    url = f"http://{address}:{args.port}"
    print(f"Аким на 5 часов: {url}\nОстановить — Ctrl+C", flush=True)
    if args.open:
        opener = threading.Timer(0.3, webbrowser.open, args=(url,))
        opener.daemon = True
        opener.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
