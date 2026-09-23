"""Обновить примеры для дизайнера: python -B docs/for_designer/generate_samples.py.

Записывает только sample_*.json рядом с этим скриптом. Все результаты возвращают
настоящие engine и ask_advisor; конфигурация советника принудительно офлайн.
Файл .env не читается, внешние API не вызываются, байткод не создаётся.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from unittest.mock import patch

# Работает и без -B: импорт проекта не должен создавать файлы в чужих папках.
sys.dont_write_bytecode = True
OUTPUT = Path(__file__).resolve().parent
ROOT = OUTPUT.parents[1]
sys.path.insert(0, str(ROOT))

import engine  # noqa: E402
from agent import advisor  # noqa: E402
from agent.prompts import AUTO_QUESTION  # noqa: E402


EXAMPLE_DECISIONS = [
    {"measure": "M7", "district": "nura"},
    {"measure": "M8", "district": "nura"},
    {"measure": "M10", "district": "nura"},
    {"measure": "M12", "district": None},
    {"measure": "M5", "district": "saryarka"},
]

# Ровно пять мер: превышение бюджета и конфликт парка со школой в Нуре.
INVALID_DECISIONS = [
    {"measure": "M4", "district": "nura"},
    {"measure": "M7", "district": "nura"},
    {"measure": "M3", "district": "esil"},
    {"measure": "M13", "district": "almaty"},
    {"measure": "M12", "district": None},
]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> None:
    data = engine.load_data()  # data/city_data.json либо прежнее имя папки «дата».
    baseline = engine.baseline()
    require(engine.validate(EXAMPLE_DECISIONS)["valid"], "Пример из ТЗ больше не проходит проверку.")
    simulation = engine.simulate(EXAMPLE_DECISIONS)
    invalid = engine.validate(INVALID_DECISIONS)
    optimized = engine.optimize(top_n=5)

    # Меняем только настройки в памяти, не функции расчёта и не текст ответа.
    # Даже при наличии настоящего ключа в .env этот запуск не обращается к API.
    empty_settings = {key: "" for key in ("OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL")}
    with patch.object(advisor, "read_settings", return_value=empty_settings), patch.object(
        advisor, "_create_client", side_effect=AssertionError("Онлайн-вызов запрещён при генерации примеров.")
    ) as client_factory:
        advisor_reply = advisor.ask_advisor(AUTO_QUESTION, simulation)
        client_factory.assert_not_called()

    # Проверяем смысл примеров до записи, чтобы не выдавать повреждённые ответы.
    require(simulation["valid"] and simulation["score"] is not None, "Симуляция не рассчитана.")
    require(not invalid["valid"], "Невалидный пример неожиданно принят движком.")
    require(any("бюджет" in error.lower() for error in invalid["errors"]), "Нет ошибки превышения бюджета.")
    require(any("M4" in error and "M7" in error for error in invalid["errors"]), "Нет ошибки конфликта M4 + M7.")
    require(not optimized.get("errors") and len(optimized["results"]) == 5, "Движок не вернул топ-5.")
    for plan in optimized["results"]:
        require(engine.validate(plan["decisions"])["valid"], "Оптимизатор вернул недопустимый план.")
        require(engine.simulate(plan["decisions"])["score"] == plan["score"], "Оценка оптимального плана не совпала с simulate.")
    calls = advisor_reply["tool_calls"]
    require(advisor_reply["offline"] is True and bool(advisor_reply["answer"]), "Нет офлайн-разбора.")
    require([call["name"] for call in calls] == ["baseline", "validate", "simulate"], "Неожиданный журнал советника.")
    require(all(call["status"] == "ok" and call["source"] == "preparation" for call in calls),
            "Офлайн-советник не смог выполнить реальные проверки движка.")

    samples = {"sample_baseline.json": baseline, "sample_simulation.json": simulation,
               "sample_invalid.json": invalid, "sample_optimize.json": optimized,
               "sample_advisor.json": advisor_reply}
    # Сериализация сохраняет ответы без оболочки, округления и ручных правок.
    payloads = {name: json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
                for name, value in samples.items()}
    for name, payload in payloads.items():
        (OUTPUT / name).write_text(payload, encoding="utf-8")
        require(json.loads((OUTPUT / name).read_text(encoding="utf-8")) == samples[name],
                f"Ошибка записи {name}.")

    print(json.dumps({"files": list(samples), "data_source": str(Path(data.source).relative_to(ROOT)),
                      "baseline_score": baseline["score"], "simulation_score": simulation["score"],
                      "score_delta": simulation["delta"]["score"], "cost": simulation["cost"],
                      "budget_left": simulation["budget_left"], "invalid_errors": invalid["errors"],
                      "best_score": optimized["results"][0]["score"], "advisor_offline": advisor_reply["offline"]},
                     ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
