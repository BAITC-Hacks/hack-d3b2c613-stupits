"""Короткий цикл function calling с одной коррекцией и полезным резервным ответом."""

from __future__ import annotations

from copy import deepcopy
import json
import logging
import os
from pathlib import Path
import re
import time

from agent.evidence import EvidenceError, format_value, render_grounded_answer
from agent.offline import offline_response
from agent.prompts import AUTO_QUESTION, SYSTEM_PROMPT
from agent.tools import EngineTools, TOOL_SCHEMAS


ROOT = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger(__name__)
MAX_STEPS = 4  # До двух инструментальных ходов, ответ и одна коррекция.
REQUEST_TIMEOUT = 8.0
TOTAL_TIMEOUT = 9.0  # Общий предел ожидания API, а не на каждый шаг.


def read_settings() -> dict[str, str]:
    """Переменные процесса важнее .env; ключи не выводятся в логи."""
    from dotenv import dotenv_values

    local = dotenv_values(ROOT / ".env", encoding="utf-8-sig", interpolate=False)
    return {key: (os.environ.get(key, local.get(key)) or "").strip()
            for key in ("OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL")}


def _create_client(settings: dict):
    from openai import OpenAI

    return OpenAI(api_key=settings["OPENAI_API_KEY"], timeout=REQUEST_TIMEOUT, max_retries=0,
                  base_url=settings["OPENAI_BASE_URL"] or "https://api.openai.com/v1")


def _decisions(result: dict) -> list[dict]:
    if isinstance(result.get("decisions"), list):
        return deepcopy(result["decisions"])
    return [{"measure": m["measure"], "district": m.get("district")}
            for m in result.get("measures", []) if isinstance(m, dict) and "measure" in m]


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _reject_constant(value):
    raise ValueError("Аргумент должен содержать конечные числа.")


def _plan_key(decisions) -> tuple:
    return tuple(sorted((str(d.get("measure")), str(d.get("district"))) for d in decisions))


def _history(history) -> list[dict]:
    # Только роли диалога: историю нельзя превратить в новую системную инструкцию.
    return [{"role": m["role"], "content": str(m.get("content", ""))[:5000]}
            for m in (history or []) if isinstance(m, dict) and m.get("role") in ("user", "assistant")][-6:]


def _checked(plans, event_id=None) -> list[dict]:
    """Храним составы, не доверяем числовым результатам старого диалога."""
    result = []
    for plan in (plans or [])[-6:]:
        if isinstance(plan, dict) and isinstance(plan.get("decisions"), list):
            # Состав из другого сценария нельзя выдавать за уже проверенный в
            # текущих условиях (например, до и после секвестра бюджета).
            if plan.get("event_id") != event_id:
                continue
            result.append({"decisions": deepcopy(plan["decisions"]),
                           "constraints": deepcopy(plan.get("constraints", {})),
                           **({"event_id": event_id} if event_id else {})})
    return result


def _rule_notice(question: str, rules: dict) -> str:
    """Явные невозможные просьбы получают понятный отказ даже при сбое сети."""
    text = question.lower().replace("ё", "е")
    counts = [int(m) for m in re.findall(r"\b(\d+)\s*(?:мер\w*|проект\w*|решени\w*)", text)]
    extra = bool(re.search(r"\b(?:шестую|шестой|шесть|седьмую|седьмой)\b.*(?:мер|проект|решени)", text))
    money = re.search(r"бюджет\w*\s*(?:(?:до|в|на|равен)\s*)?(\d+(?:[.,]\d+)?)", text)
    oversized = money is not None and float(money[1].replace(",", ".")) > rules["budget"]
    if extra or any(n != rules["num_decisions"] for n in counts) or oversized:
        return (f"По правилам нужно выбрать ровно {format_value(rules['num_decisions'])} разных проектов "
                f"с бюджетом не выше {format_value(rules['budget'])} у.е. Добавить лишнюю меру или "
                "увеличить бюджет нельзя. Ниже — вариант в рамках правил.")
    return ""


def _run_model(client, settings: dict, question: str, decisions: list, tools: EngineTools,
               *, history=None, checked_plans=None, rule_notice="") -> str:
    previous = _checked(checked_plans, tools.event_id)
    same_cheaper = (previous and re.search(r"(?:этот|тот|такой)\s+же\s+(?:набор|план|состав)", question.lower())
                    and "дешев" in question.lower())
    discussed = previous[-1]["decisions"] if same_cheaper else decisions
    # Недопустимые числа из просьбы не становятся фактами движка. Отказ уже
    # формирует приложение; модели остаётся объяснить найденный допустимый план.
    model_question = ("Просьба нарушает ограничения игры. Отказ уже показан приложением. "
                      "Найди и кратко объясни лучший допустимый план; не повторяй числа "
                      "из отклонённой просьбы." if rule_notice else question)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *_history(history),
                {"role": "user", "content": _json({
                    "question": model_question, "current_decisions": discussed,
                    "catalogue": tools.catalogue(), "checked_plans": previous,
                    "selected_event_id": tools.event_id,
                    "rule_notice": rule_notice,
                    "prepared_facts": [tools.model_payload(eid, brief=True) for eid in tools.evidence],
                })}]
    deadline = time.monotonic() + TOTAL_TIMEOUT
    model_used_tool = False
    successful_tool = False
    tool_rounds = 0
    repair_used = False
    for step in range(MAX_STEPS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Время ожидания API истекло")
        final = repair_used or successful_tool or tool_rounds >= 2 or step >= MAX_STEPS - 2 or tools.remaining <= 0
        choice = "none" if final else ("auto" if model_used_tool else "required")
        if step == 0 and (question == AUTO_QUESTION or rule_notice):
            choice = {"type": "function", "function": {"name": "optimize"}}
        elif step == 0 and same_cheaper:
            choice = {"type": "function", "function": {"name": "simulate"}}
        completion = client.chat.completions.create(
            model=settings["OPENAI_MODEL"], messages=messages, tools=TOOL_SCHEMAS,
            tool_choice=choice, parallel_tool_calls=False, max_tokens=550, temperature=0.2,
            timeout=min(REQUEST_TIMEOUT, remaining),
        )
        if not completion.choices:
            raise ValueError("Пустой ответ API")
        selected = completion.choices[0]
        if selected.finish_reason in ("length", "content_filter"):
            raise EvidenceError("Ответ API не завершён; готовый результат инструмента сохранён.")
        message = selected.message
        calls = message.tool_calls or []
        if calls:
            if final or len(calls) > tools.remaining:
                raise EvidenceError("Модель не завершила ответ после доступных инструментов.")
            messages.append({"role": "assistant", "content": message.content,
                "tool_calls": [{"id": c.id, "type": "function", "function": {
                    "name": c.function.name, "arguments": c.function.arguments}} for c in calls]})
            for call in calls:
                try:
                    if len(call.function.arguments) > 20000:
                        raise ValueError("Большой аргумент")
                    arguments = json.loads(call.function.arguments, parse_constant=_reject_constant)
                except (ValueError, TypeError):
                    arguments = None
                requested = deepcopy(arguments)
                # Свободный поиск гарантирован кодом, а не только пожеланием в промпте.
                if (question == AUTO_QUESTION or rule_notice) and call.function.name == "optimize":
                    arguments = {"constraints": {}}
                if same_cheaper and call.function.name == "simulate":
                    arguments = {"decisions": discussed}
                response = tools.call(call.function.name, arguments)
                if requested != arguments:
                    tools.trace[-1]["requested_arguments"] = requested
                    tools.trace[-1]["note"] = (
                        "Уточнение относится к последнему обсуждавшемуся набору."
                        if same_cheaper else "Ищем свободную допустимую альтернативу без закрепления текущих мер.")
                model_used_tool |= response["status"] != "error"
                successful_tool |= response["status"] == "ok"
                if response["evidence_id"] in tools.evidence:
                    payload = tools.model_payload(response["evidence_id"])
                    if same_cheaper:
                        payload["instruction"] = (
                            "Это именно ранее обсуждавшийся набор. Ответь кратко: его стоимость cost фиксирована, "
                            "смена района её не снижает; для экономии нужна замена мер. Не повторяй старый "
                            "лимит поиска из истории: сейчас объясняем цену этого состава.")
                    if question == AUTO_QUESTION and call.function.name == "optimize":
                        alternatives = response["result"].get("results", [])
                        distinct = [i for i, p in enumerate(alternatives)
                                    if _plan_key(p["decisions"]) != _plan_key(decisions)]
                        if distinct:
                            tools.trace[-1]["recommended_index"] = distinct[0]
                            shown = payload["facts"].pop("results", [])
                            # Показываем модели именно другой набор; готовые ref
                            # сохраняют исходные индексы полного ответа движка.
                            if distinct[0] < len(shown):
                                payload["facts"]["recommended_plan"] = shown[distinct[0]]
                            old = {d["measure"] for d in decisions}
                            new = {d["measure"] for d in alternatives[distinct[0]]["decisions"]}
                            catalogue = tools.engine.get_data().measures
                            payload["comparison"] = {
                                label: [{"measure": mid, "name": catalogue[mid]["name"]} for mid in sorted(ids)]
                                for label, ids in (("kept", old & new), ("removed", old - new), ("added", new - old))}
                        payload["alternative_reference"] = (
                            "{{" + response["evidence_id"] + f":/results/{distinct[0]}/score" + "}}"
                            if distinct else None)
                        payload["instruction"] = "Сравни именно другой состав. Не обещай рост, если его оценка ниже."
                else:
                    payload = response
                messages.append({"role": "tool", "tool_call_id": call.id, "content": _json(payload)})
            tool_rounds += 1
            continue
        try:
            if not model_used_tool:
                raise EvidenceError("Сначала вызови инструмент движка.")
            return render_grounded_answer(message.content, tools.evidence)
        except EvidenceError as exc:
            if repair_used:
                raise
            repair_used = True
            messages.append({"role": "assistant", "content": message.content or ""})
            messages.append({"role": "user", "content":
                f"Исправь ответ один раз. Конкретная ошибка: {exc} "
                "Копируй готовые ref из facts дословно. Не добавляй /result или /facts. "
                "Убери самостоятельные числа; коды известных мер допустимы. Ответь кратко."})
    raise EvidenceError("Модель не смогла завершить проверенный ответ.")


def _failure_reason(exc: Exception) -> tuple[str, str]:
    # Никогда не отдаём пользователю HTTP-body/str(exc): там могут быть настройки.
    name = type(exc).__name__
    if isinstance(exc, EvidenceError):
        return "answer_validation", "Ответ модели не прошёл проверку фактов. Показан последний результат движка."
    if isinstance(exc, TimeoutError) or "Timeout" in name:
        return "timeout", "API не ответил вовремя. Показан результат уже выполненных расчётов."
    if name == "AuthenticationError":
        return "api_error", "API отклонил ключ. Проверьте ключ и адрес провайдера."
    if name == "RateLimitError":
        return "api_error", "API сообщил о лимите запросов или квоте. Показан результат движка."
    return "api_error", "Ошибка API или подключения к провайдеру. Показан результат движка."


def ask_advisor(question: str, simulation_result: dict, history=None, checked_plans=None,
                event_id: str | None = None) -> dict:
    """Совместимый API; история, составы и выбранное событие необязательны.

    event_id фиксирует условия всех расчётов. Если он не передан, используем
    simulation_result.event.id. Непустой явный id имеет приоритет; пустая строка
    явно выбирает обычные условия. Модель не может подменить выбранное событие.

    simulate сам вызывает validate: три одинаковые подготовительные проверки
    заменены одной. В обычном случае нужны два запроса LLM, коррекция — одна.
    """
    started = time.monotonic()
    question = str(question or AUTO_QUESTION)
    supplied = simulation_result if isinstance(simulation_result, dict) else {}
    result = deepcopy(supplied.get("raw", supplied))
    if event_id is None:
        event = result.get("event")
        event_id = event.get("id") if isinstance(event, dict) else None
    event_id = str(event_id).strip().upper() if event_id else None
    decisions = _decisions(result)
    toolkit, client = None, None
    notice = ""

    def finish(response):
        response["checked_plans"] = toolkit.checked_plans() if toolkit else []
        response["elapsed_seconds"] = round(time.monotonic() - started, 2)
        response["event_id"] = event_id
        return response

    def fallback(reason, code):
        # Даже без сети невозможная просьба получает допустимую альтернативу.
        if notice and toolkit and toolkit.remaining > 0 and not any(
                call["name"] == "optimize" and call["status"] == "ok" for call in toolkit.trace):
            toolkit.call("optimize", {"constraints": {}}, source="fallback")
        response = offline_response(result, trace=toolkit.trace if toolkit else [], reason=reason,
            evidence=toolkit.evidence if toolkit else {}, question=question, rule_notice=notice,
            auto=question == AUTO_QUESTION, current_decisions=decisions)
        response["reason_code"] = code
        return finish(response)

    try:
        toolkit = EngineTools(event_id=event_id)
        notice = _rule_notice(question, toolkit.rules)
        calculated = toolkit.call("simulate", {"decisions": decisions}, source="preparation")
        if calculated["status"] != "ok":
            result = calculated["result"]
            code = "invalid_event" if (toolkit.context or {}).get("errors") else "invalid_plan"
            return fallback("Текущий план не прошёл проверку движка: " + " ".join(result.get("errors", [])), code)
        result = calculated["result"]
        settings = read_settings()
        if not settings["OPENAI_API_KEY"]:
            return fallback("Не указан OPENAI_API_KEY. Добавьте ключ в .env для онлайн-ответов.", "missing_key")
        if not settings["OPENAI_MODEL"]:
            return fallback("Не указана OPENAI_MODEL. Укажите модель в .env для онлайн-ответов.", "missing_model")
        client = _create_client(settings)
        answer = _run_model(client, settings, question, decisions, toolkit,
                            history=history, checked_plans=checked_plans, rule_notice=notice)
        if question == AUTO_QUESTION:
            answer = (f"**Итог.** Score — {format_value(result['score'])}; "
                      f"база — {format_value(result['baseline']['score'])}; "
                      f"изменение — {format_value(result['delta']['score'])}.\n\n" + answer)
        if notice:
            answer = notice + "\n\n" + answer
        if result.get("event"):
            answer = f"Условия сценария: «{result['event']['name']}».\n\n" + answer
        return finish({"answer": answer, "tool_calls": toolkit.trace, "offline": False,
                       "notice": "", "reason": "", "reason_code": ""})
    except Exception as exc:
        LOGGER.warning("Советник использует результат движка (%s)", type(exc).__name__)
        code, reason = _failure_reason(exc)
        return fallback(reason, code)
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
