"""Разрешённые инструменты. Правила и все расчёты остаются в engine/."""

from __future__ import annotations

from copy import deepcopy
import importlib
import logging

from agent.evidence import format_value


LOGGER = logging.getLogger(__name__)
MAX_TOOL_CALLS = 6

DECISION = {
    "type": "object",
    "properties": {"measure": {"type": "string"}, "district": {"type": ["string", "null"]}},
    "required": ["measure", "district"], "additionalProperties": False,
}
DECISIONS = {"type": "object", "properties": {
    "decisions": {"type": "array", "items": DECISION, "maxItems": 30}},
    "required": ["decisions"], "additionalProperties": False}
CONSTRAINTS = {
    "type": "object",
    "properties": {
        "exclude": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
        "include": {"type": "array", "items": {"anyOf": [{"type": "string"}, DECISION]}, "maxItems": 30},
        "budget": {"type": ["number", "null"]},
    },
    "additionalProperties": False,
}
SCHEMAS = {
    "baseline": ("Исходные показатели районов и базовая оценка города.",
                 {"type": "object", "properties": {}, "additionalProperties": False}),
    "validate": ("Проверить решения по всем правилам движка; вернуть причины нарушений.", DECISIONS),
    "simulate": ("Рассчитать допустимый план: Score, изменения, риски, синергии и вклад мер.", DECISIONS),
    "optimize": ("Найти лучшие допустимые планы: exclude исключает меры; include закрепляет меры "
                 "и при необходимости районы; budget ограничивает бюджет. Передай {} без ограничений.",
                 {"type": "object", "properties": {"constraints": CONSTRAINTS},
                  "required": ["constraints"], "additionalProperties": False}),
    "list_events": ("Каталог отдельных городских событий от обычного базиса до событий; не меняет выбранное событие.",
                    {"type": "object", "properties": {}, "additionalProperties": False}),
    "robustness": ("Проверить план до событий: движок прогоняет его по отдельным событиям от обычного базиса. "
                   "Показывает худший случай и бюджетные риски, не накладывает события друг на друга.",
                   {"type": "object", "properties": {
                       "decisions": DECISIONS["properties"]["decisions"],
                       "events": {"type": ["array", "null"], "items": {"type": "string"}, "maxItems": 30}},
                    "required": ["decisions"], "additionalProperties": False}),
    "compare": ("Сравнить названные планы в выбранных условиях: рейтинг, оценки и отличия составов.",
                {"type": "object", "properties": {"plans": {
                    "type": "object", "minProperties": 1, "maxProperties": 6,
                    "additionalProperties": DECISIONS["properties"]["decisions"]}},
                 "required": ["plans"], "additionalProperties": False}),
}
TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": name, "description": description, "parameters": schema}}
    for name, (description, schema) in SCHEMAS.items()
]


class ToolLimitError(RuntimeError):
    """Лимит включает подготовку контекста и вызовы по выбору модели."""


def summarize(name: str, result: dict) -> tuple[str, dict]:
    """Краткий журнал: только поля фактически возвращённого ответа движка."""
    if result.get("errors"):
        return "Проверка не пройдена: " + " ".join(map(str, result["errors"])), {
            "valid": result.get("valid", False), "errors": result["errors"]}
    if name == "validate":
        return ("План допустим." if result.get("valid") else "План не прошёл проверку."), deepcopy(result)
    if name == "optimize":
        compact = {"message": result.get("message", ""), "constraints": result.get("constraints", {}),
                   "plans": [{key: plan[key] for key in ("score", "cost", "decisions") if key in plan}
                             for plan in result.get("results", [])]}
        return result.get("message") or "Поиск завершён.", compact
    if name in {"robustness", "compare", "list_events"}:
        compact = {key: deepcopy(result[key]) for key in (
            "score", "cost", "worst_score", "worst_case", "fails_under", "summary", "count", "baseline_score",
            "leader", "ranking", "events") if key in result}
        summary = result.get("summary")
        if isinstance(summary, list):
            summary = " ".join(map(str, summary))
        return summary or ("Каталог событий получен." if name == "list_events" else "Сравнение выполнено."), compact
    compact = {key: deepcopy(result[key]) for key in
               ("score", "delta", "cost", "budget_left", "N_crit", "min_district") if key in result}
    parts = []
    for key, label in (("score", "Оценка"), ("cost", "Стоимость"),
                       ("budget_left", "Остаток бюджета"), ("N_crit", "Критических показателей")):
        if result.get(key) is not None:
            parts.append(f"{label}: {format_value(result[key])}")
    return "; ".join(parts) or "Инструмент вернул результат.", compact


class EngineTools:
    def __init__(self, event_id: str | None = None) -> None:
        self.engine = importlib.import_module("engine")
        self.trace: list[dict] = []
        self.event_id = str(event_id).strip().upper() if event_id else None
        self.data = data = self.engine.get_data()
        # Контекст закрепляется приложением. Ни schema, ни arguments модели не
        # позволяют снять событие и вернуть бюджет обычного города.
        self.context = self.engine.baseline(data=data, event_id=self.event_id) if self.event_id else None
        # Правила — тоже факты движка, а не числа из памяти LLM.
        self.rules = {"budget": (self.context or {}).get("budget", data.budget), "num_decisions": data.num_decisions,
                      "max_per_direction": data.max_per_direction, "crit_threshold": data.crit_threshold,
                      "score_formula": deepcopy(data.raw["score_formula"])}
        if self.context and self.context.get("event"):
            self.rules["event"] = deepcopy(self.context["event"])
        self.evidence: dict[str, dict] = {"e0": deepcopy(self.rules)}

    @property
    def remaining(self) -> int:
        return MAX_TOOL_CALLS - len(self.trace)

    def catalogue(self) -> list[dict]:
        # Только справочник названий/id для составления аргументов, без чисел модели.
        data = self.data
        return [{"measure": mid, "name": measure["name"], "scope": measure["scope"]}
                for mid, measure in data.measures.items()]

    def model_payload(self, evidence_id: str, *, brief: bool = False) -> dict:
        """Короткий контекст с готовыми ссылками; полные ответы храним для проверки.

        Убираем большие таблицы и повторяющиеся описания, но не считаем новых чисел.
        Ссылки всегда ведут в bare result: /results/0/score, без лишнего /result.
        """
        raw = self.evidence[evidence_id]
        if evidence_id == "e0" or raw.get("errors"):
            selected = deepcopy(raw)
        elif "ranking" in raw:
            selected = {key: deepcopy(raw[key]) for key in ("event", "baseline_score", "leader", "summary", "errors") if key in raw}
            selected["ranking"] = [{key: deepcopy(row[key]) for key in (
                "name", "rank", "valid", "errors", "score", "delta_vs_baseline", "cost", "budget_left",
                "N_crit", "min_district", "decisions", "gap_to_leader", "differs_from_leader") if key in row}
                for row in raw["ranking"][:3]]
        elif "events" in raw:
            selected = {key: deepcopy(raw[key]) for key in (
                "valid", "errors", "count", "score", "baseline_score", "budget", "cost", "worst_case",
                "worst_score", "fails_under", "average_drop", "summary") if key in raw}
            selected["events"] = [{key: deepcopy(row[key]) for key in (
                "id", "event", "name", "district_name", "effects", "budget", "budget_change", "valid",
                "errors", "score", "baseline_score", "baseline_delta", "drop", "plan_gain", "hardest_hit") if key in row}
                for row in raw["events"]]
        elif "results" in raw:
            selected = {key: deepcopy(raw[key]) for key in ("event", "constraints", "message", "errors") if key in raw}
            selected["results"] = [{key: deepcopy(plan[key]) for key in
                ("score", "score_delta", "cost", "budget_left", "decisions", "min_district", "N_crit")
                if key in plan} for plan in raw["results"][:2]]
        else:
            selected = {key: deepcopy(raw[key]) for key in
                ("event", "valid", "errors", "score", "cost", "budget", "budget_left", "N_crit", "min_district", "delta")
                if key in raw}
            if "baseline" in raw:
                selected["baseline"] = {key: raw["baseline"][key] for key in ("score", "N_crit")
                                        if key in raw["baseline"]}
            # Авторазбору нужны реальные сильные стороны и побочные эффекты даже
            # без повторного simulate; это компактнее полной таблицы показателей.
            for key in ("critical_resolved", "critical_new", "synergies"):
                if key in raw:
                    selected[key] = deepcopy(raw[key])
            if brief and any(any(value < 0 for value in measure.get("effects_realized", {}).values())
                             for measure in raw.get("measures", [])):
                # Пустые записи сохраняют реальные индексы JSON Pointer. Даже
                # короткий контекст обязан показывать побочные эффекты текущего плана.
                selected["measures"] = [
                    {key: deepcopy(m[key]) for key in ("measure", "name", "district_name", "effects_realized")
                     if key in m} if any(v < 0 for v in m.get("effects_realized", {}).values()) else {}
                    for m in raw["measures"]]
            if not brief:
                for key in ("critical_resolved", "critical_indicators", "critical_new", "synergies"):
                    if key in raw:
                        selected[key] = deepcopy(raw[key])
                selected["districts"] = [{key: deepcopy(d[key]) for key in
                    ("id", "name", "D", "D_before", "D_after", "D_delta") if key in d}
                    for d in raw.get("districts", [])]
                if "measures" in raw:
                    selected["measures"] = [{key: deepcopy(m[key]) for key in
                        ("measure", "name", "district_name", "score_contribution", "effects_realized") if key in m}
                        for m in raw["measures"]]

        def referenced(value, path=""):
            if isinstance(value, dict):
                return {key: referenced(item, path + "/" + str(key).replace("~", "~0").replace("/", "~1"))
                        for key, item in value.items()}
            if isinstance(value, list):
                return [referenced(item, path + f"/{index}") for index, item in enumerate(value)]
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return {"value": value, "ref": "{{" + evidence_id + ":" + path + "}}"}
            return value

        call = next((row for row in self.trace if row["evidence_id"] == evidence_id), {})
        return {"evidence_id": evidence_id, "facts": referenced(selected),
                "context": {"event_id": self.event_id, "scope": call.get("scope", "selected_conditions")}}

    def checked_plans(self) -> list[dict]:
        """Для следующей реплики сохраняем состав и ограничения, не верим старым Score."""
        plans = []
        for call in self.trace:
            if call["status"] != "ok" or call["source"] == "preparation":
                continue
            raw = self.evidence.get(call["evidence_id"], {})
            if call["name"] == "optimize" and raw.get("results"):
                index = call.get("recommended_index", 0)
                plans.append({"decisions": deepcopy(raw["results"][index]["decisions"]),
                              "constraints": deepcopy(call["arguments"].get("constraints", {}))})
            elif call["name"] == "simulate" and raw.get("valid"):
                plans.append({"decisions": deepcopy(call["arguments"]["decisions"]), "constraints": {}})
            elif call["name"] == "compare":
                leader = next((row for row in raw.get("ranking", []) if row.get("valid")), None)
                if leader:
                    plans.append({"decisions": deepcopy(leader["decisions"]), "constraints": {}})
        if self.event_id:
            for plan in plans:
                plan["event_id"] = self.event_id
        return plans[-6:]

    def call(self, name: str, arguments, *, source: str = "model") -> dict:
        if self.remaining <= 0:
            raise ToolLimitError("Лимит обращений к движку исчерпан.")
        evidence_id = f"e{len(self.trace) + 1}"
        status = "ok"
        actual_arguments = deepcopy(arguments)
        scope = {"robustness": "before_events", "list_events": "event_catalogue"}.get(name, "selected_conditions")
        try:
            from jsonschema import Draft7Validator

            if name not in SCHEMAS:
                raise ValueError("Неизвестное имя инструмента")
            # Проверяем форму аргументов, а правила игры проверяет только движок.
            if not Draft7Validator(SCHEMAS[name][1]).is_valid(arguments):
                raise ValueError("Аргументы не соответствуют схеме")
            context = {"event_id": self.event_id} if self.event_id else {}
            if name in {"baseline", "validate", "simulate", "optimize", "compare"}:
                actual_arguments.update(context)
            functions = {
                "baseline": lambda **kw: self.engine.baseline(data=self.data, **kw),
                "validate": lambda **kw: self.engine.validate(data=self.data, **kw),
                "simulate": lambda **kw: self.engine.simulate(data=self.data, **kw),
                "optimize": lambda **kw: self.engine.optimize(top_n=5, data=self.data, **kw),
                "compare": lambda **kw: self.engine.compare(data=self.data, **kw),
                # Эти две операции по смыслу движка всегда относятся к городу ДО
                # событий. Выбранное событие сюда не накладываем и не меняем.
                "list_events": lambda: self.engine.list_events(data=self.data),
                "robustness": lambda **kw: self.engine.robustness(data=self.data, **kw),
            }
            result = functions[name](**actual_arguments)
            if not isinstance(result, dict):
                raise TypeError("Ожидался словарь результата")
            self.evidence[evidence_id] = deepcopy(result)
            if result.get("valid") is False or result.get("errors"):
                status = "invalid"
            summary, compact = summarize(name, result)
        except Exception as exc:
            # Не раскрываем произвольные исключения, пути или настройки API.
            LOGGER.warning("Ошибка инструмента %s (%s)", name, type(exc).__name__)
            status = "error"
            self.evidence.pop(evidence_id, None)
            result = {"errors": ["Не удалось выполнить инструмент. Проверьте его имя и формат аргументов."]}
            summary, compact = result["errors"][0], result
        self.trace.append({"name": name, "arguments": actual_arguments, "result": compact,
                           "summary": summary, "status": status, "source": source,
                           "evidence_id": evidence_id, "scope": scope, "context_event_id": self.event_id})
        return {"evidence_id": evidence_id, "result": result, "status": status}
