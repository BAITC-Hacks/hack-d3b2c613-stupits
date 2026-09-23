"""
Движок симулятора «Аким на 5 часов» — чистая логика расчёта, без UI и LLM.

Публичные функции (их же вызывает ИИ-советник как инструменты):
    load_data(path=None)                  загрузить city_data.json (и events.json рядом)
    validate(decisions)                   проверить набор по правилам ТЗ
    simulate(decisions)                   Score и полный разбор набора
    baseline()                            состояние города и Score без мер
    optimize(top_n=10, constraints=None)  полный перебор, лучшие наборы

Городские события и сравнение (подробно — в engine/README.md):
    list_events()                         каталог событий и их влияние на город
    robustness(decisions)                 устойчивость плана ко всем событиям
    compare(plans)                        рейтинг нескольких планов (команд)
    validate / simulate / baseline / optimize принимают необязательный event_id,
    optimize — ещё режим robust=True (лучший Score в худшем случае среди событий).

Решение: {"measure": "M7", "district": "nura"} или {"measure": "M12", "district": None}.
Все функции, кроме load_data, возвращают JSON-сериализуемые словари.
"""

from .compare import compare
from .data import CityData, get_data, load_data
from .events import list_events
from .optimizer import optimize
from .robustness import robustness
from .simulation import baseline, simulate
from .validation import validate

__all__ = [
    "CityData", "load_data", "get_data", "validate", "simulate", "baseline", "optimize",
    "list_events", "robustness", "compare",
]
