"""Синхронный советник с OpenAI-совместимым function calling и офлайн-режимом."""

from __future__ import annotations

from copy import deepcopy
import json
import logging
import os
from pathlib import Path
import time

from agent.evidence import EvidenceError, render_grounded_answer
from agent.offline import offline_response
from agent.prompts import AUTO_QUESTION, SYSTEM_PROMPT
from agent.tools import EngineTools, TOOL_SCHEMAS


ROOT = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger(__name__)
MAX_STEPS = 6  # Включая попытки исправить ответ и заключительный запрос без инструментов.
REQUEST_TIMEOUT = 20.0
TOTAL_TIMEOUT = 60.0


def read_settings() -> dict[str, str]:
    """Переменные процесса важнее .env; файл перечитывается без изменения os.environ."""
    from dotenv import dotenv_values

    local = dotenv_values(ROOT / ".env", encoding="utf-8-sig", interpolate=False)
    return {key: (os.environ.get(key, local.get(key)) or "").strip()
            for key in ("OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL")}


def _create_client(settings: dict):
    # Ленивый импорт: отсутствие SDK или ключа не ломает локальный разбор.
    from openai import OpenAI

    options = {"api_key": settings["OPENAI_API_KEY"], "timeout": REQUEST_TIMEOUT, "max_retries": 0,
               "base_url": settings["OPENAI_BASE_URL"] or "https://api.openai.com/v1"}
    return OpenAI(**options)


def _decisions(result: dict) -> list[dict]:
    if isinstance(result.get("decisions"), list):
        return deepcopy(result["decisions"])
    return [{"measure": measure["measure"], "district": measure.get("district")}
            for measure in result.get("measures", []) if isinstance(measure, dict) and "measure" in measure]


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _reject_constant(value):
    raise ValueError("Аргумент должен содержать конечные числа.")


def _run_model(client, settings: dict, question: str, decisions: list, tools: EngineTools) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _json({
            "question": question, "current_decisions": decisions, "catalogue": tools.catalogue(),
            "prepared_evidence": [{"evidence_id": key, "result": result}
                                  for key, result in tools.evidence.items()],
        })},
    ]
    deadline = time.monotonic() + TOTAL_TIMEOUT
    model_used_tool = False
    for step in range(MAX_STEPS):
        remaining_time = deadline - time.monotonic()
        if remaining_time <= 0:
            raise TimeoutError("Истекло время ответа")
        final_step = step == MAX_STEPS - 1 or tools.remaining <= 0
        choice = "none" if final_step else ("auto" if model_used_tool else "required")
        if step == 0 and question == AUTO_QUESTION:
            # Авторазбор обязательно получает проверенную альтернативу для раздела улучшений.
            choice = {"type": "function", "function": {"name": "optimize"}}
        if final_step:
            messages.append({"role": "user", "content":
                             "Заверши ответ по проверенным данным. Новые инструменты недоступны. "
                             "Числа вставляй только ссылками на поля результатов."})
        completion = client.chat.completions.create(
            model=settings["OPENAI_MODEL"], messages=messages, tools=TOOL_SCHEMAS,
            tool_choice=choice, parallel_tool_calls=False,
            timeout=min(REQUEST_TIMEOUT, remaining_time),
        )
        if not completion.choices:
            raise ValueError("API вернул пустой список ответов")
        selection = completion.choices[0]
        if selection.finish_reason in ("length", "content_filter"):
            raise ValueError("API не завершил ответ")
        message = selection.message
        calls = message.tool_calls or []
        if calls:
            if final_step or len(calls) > tools.remaining:
                raise ValueError("API превысил лимит инструментов")
            # Сохраняем ids для стандартного протокола tool_call_id; reasoning не показываем.
            messages.append({"role": "assistant", "content": message.content,
                             "tool_calls": [{"id": call.id, "type": "function", "function": {
                                 "name": call.function.name, "arguments": call.function.arguments}}
                                            for call in calls]})
            for call in calls:
                try:
                    if len(call.function.arguments) > 20_000:
                        raise ValueError("Слишком большой аргумент")
                    arguments = json.loads(call.function.arguments, parse_constant=_reject_constant)
                except (ValueError, TypeError):
                    arguments = None  # Неверную форму toolkit запишет как ошибку, не выполнит.
                response = tools.call(call.function.name, arguments)
                if response["status"] != "error":
                    model_used_tool = True
                messages.append({"role": "tool", "tool_call_id": call.id, "content": _json(response)})
            continue
        try:
            if not model_used_tool:
                raise EvidenceError("Модель не вызвала инструмент")
            return render_grounded_answer(message.content, tools.evidence)
        except EvidenceError:
            # Неверный текст не попадает в UI. На исправление действует тот же лимит шагов.
            messages.append({"role": "assistant", "content": message.content or ""})
            messages.append({"role": "user", "content":
                             "Ответ не прошёл проверку источников. Сначала вызови инструмент, если ещё "
                             "не сделал этого. Убери самостоятельные цифры и числительные; используй "
                             "только ссылки вида {{e3:/score}} на существующие поля результатов."})
    raise EvidenceError("Не получен подтверждённый ответ за отведённые шаги")


def ask_advisor(question: str, simulation_result: dict) -> dict:
    """Вернуть {answer, tool_calls, offline, notice, reason}.

    В онлайне: максимум шесть запросов к модели и шесть вызовов движка суммарно,
    включая обязательные baseline, validate, simulate. Никаких записей в engine.
    Ключи, HTTP-ответы провайдера и внутренние рассуждения в журнал не попадают.
    """
    supplied = simulation_result if isinstance(simulation_result, dict) else {}
    supplied = deepcopy(supplied.get("raw", supplied))
    result, toolkit, client = supplied, None, None
    try:
        toolkit = EngineTools()
        decisions = _decisions(supplied)
        baseline = toolkit.call("baseline", {}, source="preparation")
        checked = toolkit.call("validate", {"decisions": decisions}, source="preparation")
        if checked["status"] == "error":
            return offline_response(result, trace=toolkit.trace, reason="Проверка движком пока недоступна.")
        if not checked["result"].get("valid"):
            return {"answer": "План не прошёл проверку движка: " +
                    " ".join(checked["result"].get("errors", [])), "tool_calls": toolkit.trace,
                    "offline": True, "notice": "Советник работает в офлайн-режиме.",
                    "reason": "Сначала исправьте план."}
        calculated = toolkit.call("simulate", {"decisions": decisions}, source="preparation")
        if calculated["status"] != "ok" or baseline["status"] != "ok":
            return offline_response(result, trace=toolkit.trace, reason="Повторный расчёт пока недоступен.")
        result = calculated["result"]
        settings = read_settings()
        if not settings["OPENAI_API_KEY"] or not settings["OPENAI_MODEL"]:
            return offline_response(result, trace=toolkit.trace,
                                    reason="Не настроены ключ или модель. Показан разбор по данным движка.")
        client = _create_client(settings)
        answer = _run_model(client, settings, str(question or AUTO_QUESTION), decisions, toolkit)
        return {"answer": answer, "tool_calls": toolkit.trace, "offline": False, "notice": "", "reason": ""}
    except Exception as exc:
        # Не логируем текст исключения API: он может содержать настройки подключения.
        LOGGER.warning("Советник перешёл в офлайн-режим (%s)", type(exc).__name__)
        return offline_response(result, trace=toolkit.trace if toolkit else [],
                                reason="Онлайн-ответ недоступен или не прошёл проверку. Показан разбор по данным движка.")
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
