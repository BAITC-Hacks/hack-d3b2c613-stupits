"""Запуск: python -m streamlit run app.py."""

from __future__ import annotations

from copy import deepcopy
import json
import logging

import streamlit as st

from ui.components import district_name, effect_text, number, render_map, render_profile, render_results
from ui.advisor_panel import render_advisor, render_auto_analysis
from ui.engine_adapter import AdapterError, EngineAdapter, EngineUnavailable, ROOT


LOGGER = logging.getLogger(__name__)


def initialize_state(adapter: EngineAdapter) -> None:
    """Все пользовательские данные изолированы в сессии Streamlit."""
    defaults = {"decisions": [], "simulation": None, "best_plans": [],
                "messages": [], "plan_errors": [], "notice": None,
                "selected_district": next(iter(adapter.districts)),
                "advisor_analysis": None, "advisor_plan_key": None, "simulation_key": None}
    for key, value in defaults.items():
        st.session_state.setdefault(key, deepcopy(value))
    if st.session_state.get("source_fingerprint") != adapter.fingerprint:
        # Сохраняем выбор пользователя, но удаляем результаты старой версии модели.
        for key in ("simulation", "best_plans", "messages", "plan_errors", "advisor_analysis", "advisor_plan_key", "simulation_key"):
            st.session_state[key] = deepcopy(defaults[key])
        st.session_state.source_fingerprint = adapter.fingerprint
    st.session_state.district_ids = list(adapter.districts)
    if st.session_state.selected_district not in adapter.districts:
        st.session_state.selected_district = next(iter(adapter.districts))


def simulation_key(adapter: EngineAdapter, decisions: list[dict]) -> tuple:
    """Порядок решений не меняет план; версия движка и данных меняет расчёт."""
    canonical = sorted(json.dumps(item, sort_keys=True, ensure_ascii=False) for item in decisions)
    return adapter.fingerprint, tuple(canonical)


def calculate(adapter: EngineAdapter) -> None:
    key = simulation_key(adapter, st.session_state.decisions)
    if st.session_state.simulation is not None and st.session_state.get("simulation_key") == key:
        # Повторная кнопка сохраняет расчёт, историю и уже полученный разбор без нового API.
        return
    st.session_state.simulation = None
    st.session_state.simulation_key = None
    st.session_state.advisor_analysis = None
    st.session_state.advisor_plan_key = None
    st.session_state.messages = []
    st.session_state.plan_errors = []
    st.session_state.notice = None
    try:
        validation = adapter.validate(st.session_state.decisions)
        if not validation["valid"]:
            st.session_state.plan_errors = validation["errors"]
            return
        st.session_state.simulation = adapter.simulate(st.session_state.decisions)
        st.session_state.simulation_key = key
    except EngineUnavailable as exc:
        st.session_state.notice = str(exc)
    except AdapterError as exc:
        st.session_state.plan_errors = [str(exc)]
    except Exception:
        LOGGER.exception("Ошибка расчёта плана")
        st.session_state.plan_errors = ["Не удалось рассчитать план. Попробуйте ещё раз после подключения движка."]


def replace_plan(adapter: EngineAdapter, decisions: list[dict]) -> None:
    """Карта и чат не должны показывать результат ранее выбранного плана."""
    unchanged = (st.session_state.simulation is not None and
                 st.session_state.get("simulation_key") == simulation_key(adapter, decisions))
    st.session_state.decisions = deepcopy(decisions)
    if unchanged:
        return
    st.session_state.simulation = None
    st.session_state.simulation_key = None
    st.session_state.messages = []
    st.session_state.advisor_analysis = None
    st.session_state.advisor_plan_key = None
    st.session_state.plan_errors = []
    st.session_state.notice = None
    if len(decisions) == adapter.required:
        calculate(adapter)


def render_header(adapter: EngineAdapter, baseline: dict) -> None:
    st.markdown('<div class="eyebrow">ГОРОДСКАЯ ЛАБОРАТОРИЯ · АСТАНА</div>', unsafe_allow_html=True)
    st.title("Аким на 5 часов")
    st.write(f"Вы — аким Астаны. Выберите {adapter.required} проектов из {len(adapter.measures)}, "
             f"уложитесь в {adapter.budget:g} у.е. и улучшите жизнь города.")
    result = st.session_state.simulation
    score = result["score"] if result else baseline["score"]
    spent = adapter.cost(st.session_state.decisions)
    remaining = adapter.budget - spent
    columns = st.columns(4)
    columns[0].metric("Оценка качества жизни", number(score),
                       number(result["score_delta"], True) if result and result["score_delta"] is not None else None)
    columns[1].metric("Базовая оценка", number(baseline["score"]))
    columns[2].metric("Остаток бюджета", f"{remaining:g} у.е.")
    columns[3].metric("Выбрано проектов", f"{len(st.session_state.decisions)} из {adapter.required}")
    percent = max(0, min(100, round(remaining / adapter.budget * 100)))
    st.progress(percent, text=f"Осталось {remaining:g} из {adapter.budget:g} у.е. · {percent}% бюджета")
    if result is None:
        st.caption("Показаны исходные оценки города. Новый результат появится после расчёта полного плана.")
    else:
        st.caption(f"Показан результат выбранного плана на горизонте {adapter.data['horizon_quarters']} кварталов.")
    if not adapter.ready:
        st.info(adapter.status)
    if st.session_state.notice:
        st.info(st.session_state.notice)
    for error in st.session_state.plan_errors:
        st.error(error)


def render_plan(adapter: EngineAdapter) -> None:
    st.subheader("Ваш план")
    decisions = st.session_state.decisions
    st.write(f"**Выбрано {len(decisions)} из {adapter.required}**")
    if not decisions:
        st.caption("Добавляйте проекты из каталога. Для районных проектов выберите место реализации.")
    for index, decision in enumerate(decisions):
        measure = adapter.measures.get(decision.get("measure"))
        if measure is None:
            st.warning("Выбранный проект больше не доступен в данных города.")
        else:
            st.markdown(f"**{index + 1}. {measure['name']}**")
            st.caption(f"{district_name(adapter, decision.get('district'))} · {measure['cost']:g} у.е.")
        if st.button("Убрать из плана", key=f"remove_{index}", width="stretch"):
            replace_plan(adapter, decisions[:index] + decisions[index + 1:])
            st.rerun()
    st.divider()
    st.write(f"**Стоимость: {adapter.cost(decisions):g} из {adapter.budget:g} у.е.**")
    complete = len(decisions) == adapter.required
    if st.button("Рассчитать результат", key="calculate", type="primary", disabled=not complete,
                 help=None if complete else f"Сначала выберите ровно {adapter.required} проектов.",
                 width="stretch"):
        with st.spinner("Рассчитываем изменения в городе…"):
            calculate(adapter)
        st.rerun()
    if not complete:
        st.caption(f"До расчёта осталось выбрать: {max(0, adapter.required - len(decisions))}.")
    if len(decisions) == adapter.required - 1:
        try:
            if adapter.has_final_project(decisions) is False:
                st.warning("Этот план нельзя завершить: ни один оставшийся проект не укладывается "
                           "в бюджет и правила совместимости. Уберите или замените один из выбранных проектов.")
        except (EngineUnavailable, AdapterError):
            st.caption("Проверка возможности завершить план пока недоступна.")
        except Exception:
            LOGGER.exception("Не удалось проверить оставшиеся проекты")
            st.caption("Проверка возможности завершить план пока недоступна.")
    if st.button("Очистить план", key="clear_plan", disabled=not decisions, width="stretch"):
        replace_plan(adapter, [])
        st.rerun()


def render_catalogue(adapter: EngineAdapter) -> None:
    st.subheader("Городские проекты")
    st.caption("Эффекты указаны в пунктах показателей до учёта лага. Лаг — время до запуска проекта в кварталах.")
    st.caption("Район реализации выбирается отдельно в каждой карточке. Район на карте и в профиле "
               "служит только для просмотра и не меняет размещение проектов.")
    with st.expander("Правила и совместимость проектов"):
        for rule in adapter.data.get("rules", []):
            st.write(f"• {rule}")
        for rule in adapter.data.get("incompatibilities", []):
            names = " + ".join(adapter.measures[mid]["name"] for mid in rule["pair"])
            st.caption(f"{names}: {rule['reason']}")
    directions = [None, *adapter.data["directions"]]
    direction = st.selectbox("Направление проектов", directions, key="direction_filter",
                             format_func=lambda value: adapter.data["directions"].get(value, "Все направления"))
    measures = [m for m in adapter.measures.values() if direction is None or m["direction"] == direction]
    ids = list(adapter.districts)
    for start in range(0, len(measures), 2):
        columns = st.columns(2)
        for column, measure in zip(columns, measures[start:start + 2]):
            with column, st.container(border=True):
                mid = measure["id"]
                st.markdown(f"**{mid} · {measure['name']}**")
                st.caption(adapter.data["directions"][measure["direction"]])
                st.write(f"**{measure['cost']:g} у.е.** · Лаг: {measure['lag']} кв.")
                st.write(effect_text(adapter, measure["effects"]))
                selected = next((d for d in st.session_state.decisions if d["measure"] == mid), None)
                already_selected = selected is not None
                if measure["scope"] == "district":
                    target_key = f"target_{mid}"
                    # Выбранный проект всегда показывает сохранённый район, в том числе после оптимизации.
                    if already_selected and selected.get("district") in ids:
                        st.session_state[target_key] = selected["district"]
                    elif st.session_state.get(target_key) not in ids:
                        st.session_state[target_key] = ids[0]
                    district = st.selectbox("Район реализации проекта", ids, key=target_key,
                                             disabled=already_selected,
                                             format_func=lambda value: district_name(adapter, value))
                    if already_selected:
                        st.caption("Чтобы изменить район, уберите проект из плана и добавьте заново.")
                else:
                    district = None
                    st.caption("Действует во всех районах города")
                decision = {"measure": mid, "district": district}
                reasons = adapter.can_add(st.session_state.decisions, decision)
                if already_selected:
                    reasons = ["Проект уже в вашем плане."]
                if st.button("В плане" if already_selected else "Добавить проект", key=f"add_{mid}",
                             disabled=bool(reasons), help=" ".join(reasons) or "Добавить проект в план",
                             width="stretch"):
                    replace_plan(adapter, [*st.session_state.decisions, decision])
                    st.rerun()
                if reasons:
                    st.caption(reasons[0])


def render_optimizer(adapter: EngineAdapter, baseline: dict) -> None:
    st.subheader("Лучшие планы для города")
    st.write(f"Движок сравнит допустимые планы из {adapter.required} проектов и вернёт пять лучших по оценке качества жизни.")
    st.caption("Поиск выполняется среди всех проектов. Нажатие «Применить» заменит текущий план.")
    if st.button("Найти лучший план", key="optimize", type="primary"):
        st.session_state.best_plans = []
        try:
            with st.spinner("Ищем лучшие планы. Это может занять некоторое время…"):
                st.session_state.best_plans = adapter.optimize(top_n=5)
            if not st.session_state.best_plans:
                st.info("Движок не нашёл допустимых планов.")
        except (EngineUnavailable, AdapterError) as exc:
            st.info(str(exc))
        except Exception:
            LOGGER.exception("Ошибка оптимизации")
            st.error("Не удалось найти лучшие планы. Попробуйте повторить поиск позже.")
    for index, plan in enumerate(st.session_state.best_plans):
        with st.container(border=True):
            st.markdown(f"#### План № {index + 1}")
            left, right = st.columns(2)
            left.metric("Оценка города", number(plan["score"]),
                        number(plan["score_delta"], True) if plan["score_delta"] is not None else None)
            right.metric("Стоимость", f"{plan['cost']:g} у.е.")
            for decision in plan["decisions"]:
                st.write(f"• {adapter.measures[decision['measure']]['name']} — {district_name(adapter, decision.get('district'))}")
            if st.button("Применить", key=f"apply_plan_{index}", type="primary"):
                try:
                    validation = adapter.validate(plan["decisions"])
                    if validation["valid"]:
                        replace_plan(adapter, plan["decisions"])
                        st.rerun()
                    else:
                        for error in validation["errors"]:
                            st.error(error)
                except Exception:
                    LOGGER.exception("Ошибка применения найденного плана")
                    st.error("Не удалось применить план. Повторите поиск и попробуйте ещё раз.")


def main() -> None:
    st.set_page_config(page_title="Аким на 5 часов · Астана", page_icon="🏙️", layout="wide")
    # Дизайн заменяется одним файлом без правок кода компонентов.
    css = ROOT / "ui" / "style.css"
    if css.is_file():
        st.markdown(f"<style>{css.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)
    try:
        adapter = EngineAdapter()
        initialize_state(adapter)
        baseline = adapter.baseline()
    except Exception:
        LOGGER.exception("Не удалось загрузить исходное состояние")
        st.error("Не удалось загрузить данные города. Проверьте city_data.json и подключение движка.")
        st.stop()
    render_header(adapter, baseline)
    city_tab, results_tab, optimizer_tab, advisor_tab = st.tabs(
        ["Город и проекты", "Результаты", "Лучшие планы", "ИИ-советник"])
    with city_tab:
        map_column, profile_column = st.columns([1.2, 1], gap="large")
        result = st.session_state.simulation or baseline
        with map_column:
            render_map(adapter, result)
        with profile_column:
            render_profile(adapter, result)
        st.divider()
        catalogue_column, plan_column = st.columns([2, 1], gap="large")
        with catalogue_column:
            render_catalogue(adapter)
        with plan_column:
            render_plan(adapter)
    with results_tab:
        render_results(adapter, baseline, st.session_state.simulation)
        render_auto_analysis()
    with optimizer_tab:
        render_optimizer(adapter, baseline)
    with advisor_tab:
        render_advisor()
    st.caption("Учебный симулятор городских решений · Астана")


if __name__ == "__main__":
    main()
