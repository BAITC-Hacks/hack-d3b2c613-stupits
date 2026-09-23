"""Автоматический разбор, чат и открытый журнал вызовов движка."""

from copy import deepcopy
import hashlib
import json
import logging

import streamlit as st

from agent.offline import offline_response
from agent.prompts import AUTO_QUESTION


LOGGER = logging.getLogger(__name__)
TOOL_LABELS = {"baseline": "Исходное состояние", "validate": "Проверка правил",
               "simulate": "Расчёт плана", "optimize": "Поиск лучших планов"}


def _plan_key(result: dict) -> str:
    return hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _ask(question: str, result: dict, *, history=None, checked_plans=None) -> dict:
    try:
        from agent.advisor import ask_advisor

        with st.spinner("Советник думает…"):
            response = ask_advisor(question, deepcopy(result), history=history, checked_plans=checked_plans)
        if not isinstance(response, dict) or not isinstance(response.get("answer"), str):
            raise ValueError("Неверный формат ответа советника")
        return response
    except Exception as exc:
        LOGGER.warning("Не удалось получить ответ советника (%s)", type(exc).__name__)
        return offline_response(result, reason="Ошибка подключения советника. Показан разбор текущего плана.")


def _conversation_context() -> tuple[list[dict], list[dict]]:
    """Последние реплики и ранее проверенные составы остаются в этой сессии."""
    messages = st.session_state.messages[-6:]
    history = [{"role": m["role"], "content": m["content"]} for m in messages]
    # Состав не теряется после нескольких уточнений без новых расчётов:
    # окно текста ограничено, а последние проверенные планы ищем во всей сессии.
    responses = [st.session_state.get("advisor_analysis") or {},
                 *(m.get("response") or {} for m in st.session_state.messages)]
    plans = []
    for response in responses:
        for plan in response.get("checked_plans", []):
            # Повторный расчёт того же набора не вытесняет остальные обсуждавшиеся планы.
            if plan in plans:
                plans.remove(plan)
            plans.append(deepcopy(plan))
    return history, plans[-6:]


def ensure_auto_analysis() -> None:
    """Один раз на результат, а не на каждый rerun/переключение вкладки."""
    simulation = st.session_state.simulation
    if simulation is None:
        return
    raw = simulation["raw"]
    key = _plan_key(raw)
    if st.session_state.get("advisor_plan_key") == key and st.session_state.get("advisor_analysis"):
        return
    response = _ask(AUTO_QUESTION, raw)
    # Если пользователь успел изменить план, объяснение старого результата не сохраняем.
    current = st.session_state.simulation
    if current is not None and _plan_key(current["raw"]) == key:
        st.session_state.advisor_analysis = response
        st.session_state.advisor_plan_key = key


def render_response(response: dict) -> None:
    if response.get("offline"):
        st.info((response.get("notice") or "Советник работает в офлайн-режиме.") + " " +
                (response.get("reason") or "Онлайн-соединение недоступно; использованы данные движка."))
    st.markdown(response["answer"])
    with st.expander("Как советник пришёл к выводу"):
        calls = response.get("tool_calls", [])
        if not calls:
            st.caption("Новых вызовов инструментов не было. Использован готовый результат расчёта плана.")
        for index, call in enumerate(calls, start=1):
            name = call.get("name", "")
            label = TOOL_LABELS.get(name, "Неизвестный инструмент")
            st.markdown(f"**{index}. {label} · `{name}`**")
            st.caption("Выбор ИИ" if call.get("source") == "model" else "Обязательная проверка приложения")
            if call.get("note"):
                st.caption(call["note"])
            st.write("Аргументы:")
            st.json(call.get("arguments", {}), expanded=False)
            if call.get("status") in ("error", "invalid"):
                st.warning(call.get("summary", "Вызов не завершился успешно."))
            else:
                st.write(call.get("summary", ""))
            st.json(call.get("result", {}), expanded=False)


def render_auto_analysis() -> None:
    if st.session_state.simulation is None:
        return
    ensure_auto_analysis()
    if st.session_state.get("advisor_analysis"):
        st.subheader("Автоматический разбор советника")
        render_response(st.session_state.advisor_analysis)


def render_advisor() -> None:
    st.subheader("ИИ-советник акима")
    result = st.session_state.simulation
    st.caption("Спросите о последствиях плана или задайте ограничения для поиска альтернативы.")
    if result is None:
        st.info("Сначала рассчитайте план, чтобы советник мог объяснить его результат.")
    else:
        ensure_auto_analysis()
        if st.button("Обновить разбор", key="refresh_advisor"):
            st.session_state.advisor_plan_key = None
            st.rerun()
        if st.session_state.get("advisor_analysis"):
            with st.chat_message("assistant"):
                render_response(st.session_state.advisor_analysis)
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            if message.get("response"):
                render_response(message["response"])
            else:
                st.write(message["content"])
    question = st.chat_input("Например: найди план без ЛРТ с бюджетом до 80 у.е.",
                             key="advisor_question", disabled=result is None)
    if question:
        history, plans = _conversation_context()
        st.session_state.messages.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.write(question)
        response = _ask(question, result["raw"], history=history, checked_plans=plans)
        st.session_state.messages.append({"role": "assistant", "content": response["answer"], "response": response})
        with st.chat_message("assistant"):
            render_response(response)
