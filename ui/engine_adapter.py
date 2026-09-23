"""Граница между Streamlit и расчётным движком.

Только этот модуль знает контракт engine. Пока engine отсутствует, доступен
исходный снимок из JSON; результаты симуляции и оптимизации не выдумываются.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import importlib
import json
import logging
from pathlib import Path
import re
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger(__name__)


class EngineUnavailable(RuntimeError):
    """Движок ещё не подключён или его интерфейс не готов."""


class AdapterError(RuntimeError):
    """Ошибка контракта, которую можно безопасно показать пользователю."""


def data_path() -> Path:
    """Предпочитаем согласованный путь; поддерживаем исходное имя папки."""
    for directory in ("data", "дата"):
        candidate = ROOT / directory / "city_data.json"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Не найден файл данных города: data/city_data.json.")


def humanize_error(error: Any) -> str:
    """Русские сообщения движка сохраняем; известные коды переводим."""
    if isinstance(error, dict):
        message = str(error.get("message") or error.get("reason") or error.get("code", ""))
    else:
        message = str(error)
    if re.search("[А-Яа-яЁё]", message):
        return message
    value = message.lower()
    translations = (
        (("budget", "cost"), "Стоимость проектов превышает бюджет города."),
        (("duplicate", "repeat", "unique"), "Каждый проект можно выбрать только один раз."),
        (("incompat", "conflict"), "В плане есть несовместимые проекты."),
        (("direction", "category"), "Превышен лимит проектов одного направления."),
        (("district", "scope"), "Проверьте район проекта: городские меры не привязаны к району."),
        (("count", "exactly", "five", "number", "length"), "Выберите ровно столько проектов, сколько требуется правилами."),
        (("measure", "unknown"), "В плане указан неизвестный проект."),
    )
    for keywords, translation in translations:
        if any(word in value for word in keywords):
            return translation
    LOGGER.warning("Сообщение движка: %s", message)
    return "Движок отклонил план. Проверьте количество проектов, бюджет и совместимость мер."


def _first(mapping: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


class EngineAdapter:
    """Публичные методы: baseline, validate, simulate, optimize, can_add.

    Нормализованный результат содержит score, districts (before/after,
    D_before/D_after), cost, remaining_budget, critical_before/after,
    resolved_critical, synergies, contributions и raw для ИИ-советника.
    """

    def __init__(self) -> None:
        self.path = data_path()
        self.data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        self.districts = {item["id"]: item for item in self.data["districts"]}
        self.measures = {item["id"]: item for item in self.data["measures"]}
        self.budget = float(self.data["budget"])
        self.required = int(self.data["num_decisions"])
        self.engine = None
        self.status = "Движок ещё не подключён. Доступны исходные данные и сборка плана."
        self._baseline = None
        self._connect()

    @property
    def ready(self) -> bool:
        return self.engine is not None

    @property
    def fingerprint(self) -> tuple:
        """Изменение данных или движка делает старый расчёт неактуальным."""
        files = [self.path, *sorted((ROOT / "engine").glob("*.py"))]
        return (self.ready, tuple((str(p), p.stat().st_mtime_ns) for p in files))

    def _connect(self) -> None:
        importlib.invalidate_caches()
        try:
            module = importlib.import_module("engine")
            if not all(callable(getattr(module, name, None)) for name in
                       ("load_data", "baseline", "validate", "simulate", "optimize")):
                self.status = "Движок найден, но его интерфейс ещё не готов. Доступны исходные данные."
                return
            module.load_data(str(self.path))
            self.engine = module
            self.status = "Расчётный движок подключён."
        except ModuleNotFoundError as exc:
            if exc.name != "engine":
                LOGGER.exception("Не удалось загрузить зависимость движка")
                self.status = "Движок пока не запускается. Доступны исходные данные и сборка плана."
        except Exception:
            LOGGER.exception("Не удалось подключить движок")
            self.status = "Движок пока не запускается. Доступны исходные данные и сборка плана."

    def cost(self, decisions: list[dict]) -> float:
        return sum(float(self.measures[d["measure"]]["cost"]) for d in decisions
                   if d.get("measure") in self.measures)

    def _local_errors(self, decisions: list[dict]) -> list[str]:
        """Только быстрые подсказки конструктора, не окончательная валидация."""
        errors = []
        if len(decisions) > self.required:
            errors.append(f"Нужно выбрать ровно {self.required} проектов. Сейчас выбрано: {len(decisions)}.")
        if self.cost(decisions) > self.budget:
            errors.append(f"Не хватает бюджета: стоимость {self.cost(decisions):g} у.е., бюджет {self.budget:g} у.е.")
        counts: Counter = Counter()
        seen = set()
        for decision in decisions:
            mid, district = decision.get("measure"), decision.get("district")
            if mid not in self.measures:
                errors.append("В плане есть неизвестный проект.")
                continue
            measure = self.measures[mid]
            if mid in seen:
                errors.append(f"Проект «{measure['name']}» уже выбран.")
            seen.add(mid)
            counts[measure["direction"]] += 1
            if measure["scope"] == "district" and district not in self.districts:
                errors.append(f"Выберите район для проекта «{measure['name']}».")
            if measure["scope"] == "city" and district is not None:
                errors.append(f"Проект «{measure['name']}» действует на весь город: район не нужен.")
        limit = self.data["max_per_direction"]
        for direction, count in counts.items():
            if count > limit:
                errors.append(f"Направление «{self.data['directions'][direction]}»: допускается не более {limit} проектов.")
        selected = {d["measure"]: d.get("district") for d in decisions if d.get("measure") in self.measures}
        for rule in self.data.get("incompatibilities", []):
            first, second = rule["pair"]
            if first in selected and second in selected:
                if rule["scope"] == "any" or selected[first] == selected[second]:
                    errors.append(f"{first} + {second}: {rule['reason']}.")
        return list(dict.fromkeys(errors))

    def can_add(self, decisions: list[dict], decision: dict) -> list[str]:
        if len(decisions) >= self.required:
            return [f"Уже выбрано {self.required} проектов. Удалите один, чтобы добавить другой."]
        return self._local_errors([*decisions, decision])

    def has_final_project(self, decisions: list[dict]) -> bool | None:
        """Есть ли допустимый последний проект; полные варианты проверяет движок."""
        if len(decisions) != self.required - 1 or not self.ready:
            return None
        for measure in self.measures.values():
            targets = self.districts if measure["scope"] == "district" else (None,)
            for district in targets:
                candidate = {"measure": measure["id"], "district": district}
                if self.can_add(decisions, candidate):
                    continue
                # Подсказки отсекают очевидное; окончательное решение остаётся у validate.
                if self.validate([*decisions, candidate])["valid"]:
                    return True
        return False

    def validate(self, decisions: list[dict]) -> dict:
        # Без движка нельзя объявлять план окончательно допустимым.
        if not self.ready:
            raise EngineUnavailable("Проверка и расчёт станут доступны после подключения движка. Ваш план сохранён.")
        result = self.engine.validate(deepcopy(decisions))
        if not isinstance(result, dict) or not isinstance(result.get("valid"), bool):
            raise AdapterError("Движок вернул непонятный ответ проверки плана.")
        native_errors = result.get("errors", [])
        if isinstance(native_errors, (str, dict)):
            native_errors = [native_errors]
        errors = [humanize_error(error) for error in native_errors]
        if not result["valid"] and not errors:
            errors.append("Движок отклонил план. Проверьте правила выбора проектов.")
        return {"valid": result["valid"], "errors": list(dict.fromkeys(errors))}

    def baseline(self) -> dict:
        if self._baseline is None:
            if self.ready:
                self._baseline = self._normalize(self.engine.baseline(), [], baseline=True)
            else:
                # Заглушка показывает только опубликованные исходные оценки.
                self._baseline = self._normalize({
                    "score": self.data.get("reference_checks", {}).get("baseline_score"),
                    "districts": {
                        did: {"before": deepcopy(district["indicators"]), "after": deepcopy(district["indicators"]),
                              "D_before": district["base_D"], "D_after": district["base_D"]}
                        for did, district in self.districts.items()
                    },
                }, [], baseline=True)
        return deepcopy(self._baseline)

    def simulate(self, decisions: list[dict]) -> dict:
        validation = self.validate(decisions)
        if not validation["valid"]:
            raise AdapterError("\n".join(validation["errors"]))
        if not self.ready:
            raise EngineUnavailable("Расчёт станет доступен после подключения движка. Ваш план сохранён.")
        return self._normalize(self.engine.simulate(deepcopy(decisions)), decisions)

    def optimize(self, top_n: int = 5, constraints: dict | None = None) -> list[dict]:
        if not self.ready:
            raise EngineUnavailable("Поиск лучшего плана станет доступен после подключения движка.")
        result = self.engine.optimize(top_n=top_n, constraints=constraints or {})
        if isinstance(result, dict) and result.get("errors"):
            raise AdapterError(" ".join(humanize_error(error) for error in result["errors"]))
        plans = _first(result, "plans", "results", "top_plans", default=[]) if isinstance(result, dict) else result
        if not isinstance(plans, (list, tuple)):
            raise AdapterError("Движок вернул непонятный список лучших планов.")
        normalized = []
        for plan in plans[:top_n]:
            decisions = plan.get("decisions") if isinstance(plan, dict) else None
            if not isinstance(decisions, list):
                raise AdapterError("В найденном плане отсутствует список проектов.")
            validation = self.validate(decisions)
            if not validation["valid"]:
                raise AdapterError("Найденный план не прошёл проверку: " + " ".join(validation["errors"]))
            score = _first(plan, "score", "Score")
            if score is None:
                score = self.simulate(decisions)["score"]
            normalized.append({"decisions": deepcopy(decisions), "score": float(score),
                               "score_delta": plan.get("score_delta"),
                               "cost": self.cost(decisions)})
        return sorted(normalized, key=lambda plan: plan["score"], reverse=True)

    def _normalize(self, raw: dict, decisions: list[dict], *, baseline: bool = False) -> dict:
        """Приводим ответ движка к единственному контракту для компонентов UI."""
        if not isinstance(raw, dict):
            raise AdapterError("Движок вернул непонятный результат расчёта.")
        if raw.get("valid") is False:
            raise AdapterError(" ".join(humanize_error(error) for error in raw.get("errors", []))
                               or "Движок отклонил план.")
        score = _first(raw, "score", "Score", "baseline_score")
        if score is None:
            raise AdapterError("В результате движка отсутствует оценка города.")
        rows = raw.get("districts", {})
        if isinstance(rows, list):
            rows = {item["id"]: item for item in rows}
        districts = {}
        for did, source in self.districts.items():
            row = rows.get(did, {})
            before = _first(row, "before", "indicators_before", default=source["indicators"])
            after = _first(row, "after", "indicators_after", "indicators")
            # Реальный engine.simulation возвращает indicators[k] =
            # {value: ...} для базы и {before: ..., after: ...} для симуляции.
            indicators = row.get("indicators", {})
            if indicators and all(isinstance(value, dict) for value in indicators.values()):
                before = {key: _first(value, "before", "value") for key, value in indicators.items()}
                after = {key: _first(value, "after", "value") for key, value in indicators.items()}
            if after is None:
                after = _first(raw, "indicators_after", "after", "indicators", default={}).get(did)
            d_after = _first(row, "D_after", "D", "d_after")
            if d_after is None:
                d_after = _first(raw, "D_after", "D", "district_scores", default={}).get(did)
            if baseline:
                after = after if after is not None else before
                d_after = d_after if d_after is not None else source["base_D"]
            if (not isinstance(after, dict) or d_after is None or
                    any(not isinstance(after.get(i), (int, float)) for i in self.data["indicators"])):
                raise AdapterError(f"В результате движка не хватает показателей района «{source['name']}».")
            districts[did] = {
                "before": deepcopy(before), "after": deepcopy(after),
                "D_before": float(_first(row, "D_before", "d_before",
                                         default=d_after if baseline else source["base_D"])),
                "D_after": float(d_after),
                # Дельту нельзя получать вычитанием уже округлённых D_before/D_after.
                "D_delta": _first(row, "D_delta", "d_delta"),
                "indicator_deltas": {key: value.get("delta") for key, value in indicators.items()
                                     if isinstance(value, dict)},
            }
        # Это сравнение готовых показателей, а не повторный расчёт Score.
        threshold = self.data["crit_threshold"]
        critical = lambda stage: [
            {"district": did, "indicator": indicator, "value": float(value)}
            for did, row in districts.items() for indicator, value in row[stage].items()
            if float(value) < threshold
        ]
        before_critical = critical("before")
        # Критичность из engine точнее проверки округлённых значений отчёта.
        after_critical = raw.get("critical_indicators", critical("after"))
        after_keys = {(entry["district"], entry["indicator"]) for entry in after_critical}
        resolved = raw.get("critical_resolved", [entry for entry in before_critical
                           if (entry["district"], entry["indicator"]) not in after_keys])
        cost = float(_first(raw, "cost", "total_cost", default=self.cost(decisions)))
        contributions = _first(raw, "contributions", "measure_contributions", default=[])
        if "measures" in raw:
            contributions = [
                {"measure": measure["measure"], "district": measure.get("district"),
                 "score_delta": measure["score_contribution"],
                 "effects": measure["effects_realized"]}
                for measure in raw["measures"]
            ]
        return {
            "score": float(score), "score_delta": raw.get("delta", {}).get("score"),
            "districts": districts, "cost": cost,
            "remaining_budget": float(_first(raw, "remaining_budget", "budget_remaining", "budget_left",
                                              default=self.budget - cost)),
            "critical_before": before_critical, "critical_after": after_critical,
            "resolved_critical": resolved,
            "synergies": _first(raw, "synergies", "triggered_synergies", "active_synergies", default=[]),
            "contributions": contributions,
            "decisions": deepcopy(decisions), "raw": deepcopy(raw), "is_stub": not self.ready,
        }
