"""Детерминированный разбор: исключительно готовые поля engine.simulate."""

from __future__ import annotations

from agent.evidence import format_value


def offline_analysis(result: dict) -> str:
    if not isinstance(result, dict) or result.get("valid") is False or result.get("score") is None:
        if isinstance(result, dict) and result.get("errors"):
            return "**План не прошёл проверку.** " + " ".join(map(str, result["errors"]))
        return "Сначала рассчитайте допустимый план. Без результата движка советник не может назвать оценку."

    def value(item):
        return "нет данных" if item is None else format_value(item)

    # Ни Score, ни прирост, ни остаток не вычисляем повторно.
    baseline = result.get("baseline", {}).get("score")
    delta = result.get("delta", {}).get("score")
    parts = [f"**Итог.** Оценка города — {value(result['score'])}; "
             f"база — {value(baseline)}; изменение — {value(delta)}. "
             f"Стоимость — {value(result.get('cost'))} у.е."]

    resolved = result.get("critical_resolved", [])
    positives = []
    for item in resolved[:3]:
        positives.append(f"{item['district_name']}: показатель «{item['indicator_name']}» "
                         f"вышел из критической зоны ({value(item['before'])} → {value(item['after'])})")
    if not positives:
        gains = sorted((d for d in result.get("districts", []) if d.get("D_delta", 0) > 0),
                       key=lambda d: d["D_delta"], reverse=True)
        positives = [f"В районе {district['name']} оценка выросла на {value(district['D_delta'])}"
                     for district in gains[:2]]
    parts.append("**Сильные стороны.** " + ("; ".join(positives) + "." if positives else
                                           "Движок не показывает улучшения оценок районов."))

    risks = []
    weakest = result.get("min_district", {})
    if weakest:
        risks.append(f"Самая низкая оценка у района {weakest['name']} — {value(weakest['D'])}")
    for district in result.get("districts", []):
        for indicator in district.get("indicators", {}).values():
            if indicator.get("delta", 0) < 0:
                risks.append(f"{district['name']}: «{indicator['name']}» ухудшился "
                             f"({value(indicator['before'])} → {value(indicator['after'])})")
    remaining = result.get("budget_left")
    if remaining is not None:
        risks.append(f"Остаток бюджета — {value(remaining)} у.е.; он не добавляет баллов")
    if result.get("N_crit") is not None:
        risks.append(f"Критических показателей осталось: {value(result['N_crit'])}")
    parts.append("**Риски и компромиссы.** " + "; ".join(risks) + ".")

    improved = {key for district in result.get("districts", [])
                for key, indicator in district.get("indicators", {}).items() if indicator.get("delta", 0) > 0}
    meanings = {"S1": "может стать доступнее обучение в школах и детсадах",
                "S2": "может стать доступнее первичная медицинская помощь",
                "T1": "может уменьшиться нагрузка на дороги",
                "T2": "общественный транспорт может стать доступнее",
                "E1": "может стать больше зелёных пространств для отдыха",
                "E2": "может стать чище воздух",
                "B1": "улицы могут стать безопаснее",
                "B2": "может повыситься безопасность дорожного движения",
                "C1": "коммунальные сети могут работать надёжнее",
                "C2": "городские службы могут быстрее реагировать на обращения"}
    effects = [text for indicator, text in meanings.items() if indicator in improved]
    parts.append("**Для жителей.** По изменениям показателей " + (
        "; ".join(effects) if effects else "заметный эффект пока не подтверждён") +
        ". Это последствия в модели, а не гарантия реальных изменений.")
    parts.append("**Как улучшить.** Проверьте альтернативы кнопкой «Найти лучший план» и сравните "
                 "слабейший район, критические показатели и побочные эффекты. "
                 "Без нового расчёта обещать улучшение нельзя.")
    return "\n\n".join(parts)


def _decisions(result: dict) -> list[dict]:
    """В simulate решения записаны в measures, в optimize — в decisions."""
    if isinstance(result.get("decisions"), list):
        return result["decisions"]
    return [{"measure": item["measure"], "district": item.get("district")}
            for item in result.get("measures", []) if isinstance(item, dict) and "measure" in item]


def _plan_key(decisions: list) -> tuple:
    # Порядок проектов не делает план новой альтернативой.
    return tuple(sorted((str(item.get("measure", "")).upper(), str(item.get("district") or "").lower())
                        for item in decisions if isinstance(item, dict)))


def _decision_labels(decisions: list) -> list[str]:
    """Названия берём из справочника движка; новых расчётов здесь нет."""
    try:
        from engine import get_data
        data = get_data()
        measures, districts = data.measures, data.districts
    except Exception:
        measures, districts = {}, {}
    labels = []
    for decision in decisions:
        mid, did = decision.get("measure"), decision.get("district")
        name = measures.get(mid, {}).get("name", mid or "Неизвестная мера")
        place = districts.get(did, {}).get("name", did) if did else "весь город"
        labels.append(f"{mid} — {name} ({place})")
    return labels


def _plan_summary(plan: dict) -> str:
    """Выводим готовый прирост от базы, а не разность округлённых Score."""
    facts = []
    for key, label, suffix in (("score", "Score", ""), ("score_delta", "изменение от базы", ""),
                               ("cost", "стоимость", " у.е."),
                               ("budget_left", "остаток общего бюджета", " у.е.")):
        if plan.get(key) is not None:
            facts.append(f"{label} — {format_value(plan[key])}{suffix}")
    labels = _decision_labels(_decisions(plan))
    return "; ".join(facts).rstrip(".") + ".\n\n" + "\n".join(f"- {label}" for label in labels)


def _search_analysis(search: dict, current: dict, current_decisions: list, *, auto: bool) -> str:
    errors = search.get("errors") or []
    plans = [plan for plan in search.get("results", [])
             if isinstance(plan, dict) and plan.get("score") is not None]
    if errors or not plans:
        details = " ".join(str(error) for error in errors) or search.get("message")
        return "**Результат поиска.** Подходящий план не найден." + (f" {details}" if details else "")

    current_key = _plan_key(current_decisions)
    if auto:
        plan = next((item for item in plans if _plan_key(_decisions(item)) != current_key), None)
        if plan is None:
            return "**Как улучшить.** Среди полученных результатов нет плана, отличающегося от текущего."
    else:
        plan = plans[0]

    title = "Проверенная альтернатива" if auto else "План по вашему запросу"
    parts = [f"**{title}.** " + _plan_summary(plan)]
    score, current_score = plan.get("score"), current.get("score")
    if isinstance(score, (int, float)) and isinstance(current_score, (int, float)):
        if score > current_score:
            parts.append("Его Score выше текущего по расчёту движка.")
        elif score == current_score:
            parts.append("Его Score равен текущему; прирост относительно вашего плана не подтверждён.")
        else:
            parts.append("Его Score ниже текущего. Этот вариант не улучшает оценку вашего плана.")
    constraints = search.get("constraints") or {}
    limits = []
    if constraints.get("budget") is not None:
        limits.append(f"лимит бюджета — {format_value(constraints['budget'])} у.е.")
    if constraints.get("exclude"):
        limits.append("исключены меры " + ", ".join(map(str, constraints["exclude"])))
    if limits:
        parts.append("Условия поиска: " + "; ".join(limits) + ".")
    return "\n\n".join(parts)


def _baseline_analysis(baseline: dict) -> str:
    """Объясняем слабый район по уже полученному baseline, без новых чисел."""
    parts = []
    weakest = baseline.get("min_district") or {}
    if weakest:
        parts.append(f"Самый слабый район в исходном состоянии — {weakest['name']}: "
                     f"D = {format_value(weakest['D'])}.")
    critical = baseline.get("critical_indicators") or []
    if critical:
        parts.append("Критические показатели: " + "; ".join(
            f"{item['district_name']} — {item['indicator_name']}: {format_value(item['value'])}"
            for item in critical) + ".")
    if baseline.get("score") is not None:
        parts.append(f"Исходный Score города — {format_value(baseline['score'])}.")
    parts.append("Формула учитывает самый слабый район и штрафует за критические показатели. "
                 "Поэтому улучшение только благополучного района оставляет эти ограничения. "
                 "Эффект конкретных проектов нужно проверять отдельным расчётом.")
    return "\n\n".join(parts)


def _extra_analysis(name: str, result: dict) -> str:
    """Готовые сравнения и риски сохраняются даже при недоступном API."""
    if result.get("errors"):
        return "**Проверка не выполнена.** " + " ".join(map(str, result["errors"]))
    if name == "robustness":
        parts = ["**Устойчивость плана до событий.** Каждый сценарий проверен от обычного базиса; "
                 "события не накладываются друг на друга."]
        if result.get("score") is not None:
            parts.append("Score до событий — " + format_value(result["score"]) + ".")
        if result.get("summary"):
            parts.append(str(result["summary"]))
        return "\n\n".join(parts)
    if name == "compare":
        return "**Сравнение планов в выбранных условиях.**\n\n" + "\n".join(
            "- " + str(item) for item in result.get("summary", []))
    rows = []
    for event in result.get("events", []):
        row = f"- {event['id']} — {event['name']}"
        if event.get("baseline_score") is not None:
            row += f"; Score города без проектов — {format_value(event['baseline_score'])}"
        if event.get("budget") is not None:
            row += f"; бюджет — {format_value(event['budget'])} у.е."
        rows.append(row + ".")
    return "**Каталог отдельных событий от обычного базиса.**\n\n" + "\n".join(rows)


def _contextual_analysis(result: dict, trace: list, evidence: dict, *, auto: bool,
                         current_decisions: list) -> str:
    # В журнале UI результат сокращён. Предпочитаем полный ответ движка по evidence_id.
    calls = []
    for call in trace:
        if not isinstance(call, dict) or call.get("source") == "preparation":
            continue
        raw = evidence.get(call.get("evidence_id"))
        if not isinstance(raw, dict):
            raw = call.get("result")
        if isinstance(raw, dict):
            # Старые UI-вызовы передавали только компактный журнал без evidence.
            # Сокращённый simulate/optimize не заменяет полный текущий результат.
            if call.get("status") == "ok" and (
                (call.get("name") == "simulate" and "districts" not in raw)
                or (call.get("name") == "optimize" and "results" not in raw)
            ):
                continue
            calls.append((call, raw))

    if auto:
        for call, raw in reversed(calls):
            if call.get("name") == "optimize" and call.get("status") != "error":
                current = offline_analysis(result).split("\n\n**Как улучшить.**", 1)[0]
                return current + "\n\n" + _search_analysis(raw, result, current_decisions, auto=True)

    # Последний относящийся к вопросу результат важнее общего шаблона текущего плана.
    for call, raw in reversed(calls):
        name = call.get("name")
        if name not in {"optimize", "simulate", "validate", "robustness", "compare", "list_events"}:
            continue
        if call.get("status") == "error":
            continue
        if name in {"robustness", "compare", "list_events"}:
            return _extra_analysis(name, raw)
        if name == "optimize":
            return _search_analysis(raw, result, current_decisions, auto=False)
        if raw.get("valid") is False or raw.get("errors"):
            return "**Проверка предложенного набора.** " + " ".join(map(str, raw.get("errors") or [
                "Набор не прошёл правила движка. Его Score не рассчитывается."]))
        if name == "simulate" and raw.get("score") is not None:
            labels = _decision_labels(_decisions(raw) or call.get("arguments", {}).get("decisions", []))
            description = "**Рассчитанный набор.** " + "; ".join(labels) + ".\n\n" if labels else ""
            return description + offline_analysis(raw)
        # Успешная validate не стирает предшествующий расчёт/результат optimize.

    if not auto:
        for call, raw in reversed(calls):
            if call.get("name") == "baseline" and call.get("status") == "ok":
                return _baseline_analysis(raw)
    return offline_analysis(result)


def offline_response(result: dict, *, trace: list | None = None, reason: str = "",
                     evidence: dict[str, dict] | None = None, question: str = "",
                     rule_notice: str = "", auto: bool = False,
                     current_decisions: list | None = None) -> dict:
    """Запасной ответ сохраняет результат последнего запроса, даже если LLM подвела.

    Старые вызовы с result/trace/reason поддерживаются. Причину отказа готовит
    advisor: здесь не включаем исключения API, настройки или исходный вопрос.
    """
    try:
        answer = _contextual_analysis(result, trace or [], evidence or {}, auto=auto,
                                      current_decisions=current_decisions or _decisions(result))
    except (KeyError, TypeError, ValueError, AttributeError):
        # Повреждённый дополнительный результат не должен скрывать основной расчёт.
        try:
            answer = offline_analysis(result)
        except (KeyError, TypeError, ValueError, AttributeError):
            answer = "В результате движка недостаточно данных для разбора. Повторите расчёт плана."
    if rule_notice:
        answer = rule_notice + "\n\n" + answer
    event = result.get("event") if isinstance(result, dict) else None
    if isinstance(event, dict) and event.get("name"):
        answer = f"Условия сценария: «{event['name']}».\n\n" + answer
    return {"answer": answer, "tool_calls": trace or [], "offline": True,
            "notice": "Советник работает в офлайн-режиме.", "reason": reason}
