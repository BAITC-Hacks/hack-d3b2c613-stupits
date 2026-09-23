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
    compact = {key: deepcopy(result[key]) for key in
               ("score", "delta", "cost", "budget_left", "N_crit", "min_district") if key in result}
    parts = []
    for key, label in (("score", "Оценка"), ("cost", "Стоимость"),
                       ("budget_left", "Остаток бюджета"), ("N_crit", "Критических показателей")):
        if result.get(key) is not None:
            parts.append(f"{label}: {format_value(result[key])}")
    return "; ".join(parts) or "Инструмент вернул результат.", compact


class EngineTools:
    def __init__(self) -> None:
        self.engine = importlib.import_module("engine")
        self.trace: list[dict] = []
        data = self.engine.get_data()
        # Правила — тоже факты движка, а не числа из памяти LLM.
        self.rules = {"budget": data.budget, "num_decisions": data.num_decisions,
                      "max_per_direction": data.max_per_direction, "crit_threshold": data.crit_threshold,
                      "score_formula": deepcopy(data.raw["score_formula"])}
        self.evidence: dict[str, dict] = {"e0": deepcopy(self.rules)}

    @property
    def remaining(self) -> int:
        return MAX_TOOL_CALLS - len(self.trace)

    def catalogue(self) -> list[dict]:
        # Только справочник названий/id для составления аргументов, без чисел модели.
        data = self.engine.get_data()
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
        elif "results" in raw:
            selected = {key: deepcopy(raw[key]) for key in ("constraints", "message", "errors") if key in raw}
            selected["results"] = [{key: deepcopy(plan[key]) for key in
                ("score", "score_delta", "cost", "budget_left", "decisions", "min_district", "N_crit")
                if key in plan} for plan in raw["results"][:2]]
        else:
            selected = {key: deepcopy(raw[key]) for key in
                ("valid", "errors", "score", "cost", "budget", "budget_left", "N_crit", "min_district", "delta")
                if key in raw}
            if "baseline" in raw:
                selected["baseline"] = {key: raw["baseline"][key] for key in ("score", "N_crit")
                                        if key in raw["baseline"]}
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

        return {"evidence_id": evidence_id, "facts": referenced(selected)}

    def checked_plans(self) -> list[dict]:
        """Для следующей реплики сохраняем состав и ограничения, не верим старым Score."""
        plans = []
        for call in self.trace:
            if call["status"] != "ok" or call["source"] == "preparation":
                continue
            raw = self.evidence.get(call["evidence_id"], {})
            if call["name"] == "optimize" and raw.get("results"):
                plans.append({"decisions": deepcopy(raw["results"][0]["decisions"]),
                              "constraints": deepcopy(call["arguments"].get("constraints", {}))})
            elif call["name"] == "simulate" and raw.get("valid"):
                plans.append({"decisions": deepcopy(call["arguments"]["decisions"]), "constraints": {}})
        return plans[-6:]

    def call(self, name: str, arguments, *, source: str = "model") -> dict:
        if self.remaining <= 0:
            raise ToolLimitError("Лимит обращений к движку исчерпан.")
        evidence_id = f"e{len(self.trace) + 1}"
        status = "ok"
        try:
            from jsonschema import Draft7Validator

            if name not in SCHEMAS:
                raise ValueError("Неизвестное имя инструмента")
            # Проверяем форму аргументов, а правила игры проверяет только движок.
            if not Draft7Validator(SCHEMAS[name][1]).is_valid(arguments):
                raise ValueError("Аргументы не соответствуют схеме")
            functions = {
                "baseline": self.engine.baseline,
                "validate": self.engine.validate,
                "simulate": self.engine.simulate,
                "optimize": lambda constraints: self.engine.optimize(top_n=5, constraints=constraints),
            }
            result = functions[name](**deepcopy(arguments))
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
        self.trace.append({"name": name, "arguments": deepcopy(arguments), "result": compact,
                           "summary": summary, "status": status, "source": source,
                           "evidence_id": evidence_id})
        return {"evidence_id": evidence_id, "result": result, "status": status}
