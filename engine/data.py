"""
Загрузка данных симулятора и подготовка их к расчёту.

Все параметры модели — районы, показатели и их веса, меры, синергии,
несовместимости, бюджет и коэффициенты формулы Score — берутся из
city_data.json; в коде нет «зашитых» чисел из ТЗ. Городские события
(опциональная фича) — из events.json в той же папке.

При загрузке файл проверяется на целостность, а данные один раз
переводятся в матрицы numpy «районы × показатели». По этим матрицам
считают и simulate(), и полный перебор в optimize().
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

# Корень проекта — папка, в которой лежит пакет engine/.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Где искать данные, если путь не передан: data/city_data.json из ТЗ,
# запасной вариант — старое русское имя папки «дата/».
DEFAULT_DATA_PATHS = (
    PROJECT_ROOT / "data" / "city_data.json",
    PROJECT_ROOT / "дата" / "city_data.json",
)

# Городские события лежат в той же папке, что и city_data.json.
EVENTS_FILENAME = "events.json"

# Шкала всех показателей 0–100 задана в ТЗ; отдельного числового поля
# для неё в JSON нет, поэтому границы clip() объявлены здесь.
SCALE_MIN = 0.0
SCALE_MAX = 100.0


class CityData:
    """Данные города в удобном для расчёта виде.

    Строки всех матриц идут в порядке районов из JSON,
    столбцы — в порядке показателей из JSON.
    """

    def __init__(self, raw: dict, source: str | None = None, events: list | None = None):
        self.raw = raw
        self.source = source

        # Правила игры и коэффициенты формулы Score
        self.budget = raw["budget"]
        self.horizon = raw["horizon_quarters"]
        self.num_decisions = raw["num_decisions"]
        self.max_per_direction = raw["max_per_direction"]
        self.crit_threshold = raw["crit_threshold"]
        formula = raw["score_formula"]
        self.w_avg = formula["w_avg"]
        self.w_min = formula["w_min"]
        self.crit_penalty = formula["crit_penalty"]

        # Справочники: id → запись
        self.directions = raw["directions"]
        self.indicators = raw["indicators"]
        self.districts = {d["id"]: d for d in raw["districts"]}
        self.measures = {m["id"]: m for m in raw["measures"]}
        self.synergies = raw["synergies"]
        self.incompatibilities = raw["incompatibilities"]

        self.indicator_ids = list(self.indicators)
        self.district_ids = list(self.districts)
        self.measure_ids = list(self.measures)
        self.indicator_index = {k: j for j, k in enumerate(self.indicator_ids)}
        self.district_index = {d: i for i, d in enumerate(self.district_ids)}

        _check_integrity(self)

        # Городские события (id → событие). event — событие, которое уже применено
        # к этим данным (см. events.with_event); у официальных данных его нет.
        # official — данные без события (для официальных данных — они сами).
        _check_events(self, events or [])
        self.events = {e["id"]: e for e in (events or [])}
        self.event = None
        self.official = self

        # Матрицы для расчёта
        self.weights = np.array([self.indicators[k]["weight"] for k in self.indicator_ids], dtype=float)
        self.population = np.array(
            [self.districts[d]["population_share"] for d in self.district_ids], dtype=float
        )
        self.base = np.array(
            [[self.districts[d]["indicators"][k] for k in self.indicator_ids] for d in self.district_ids],
            dtype=float,
        )
        # Реализованный за горизонт эффект каждой меры (уже с учётом лага) —
        # считается один раз при загрузке, дальше только складывается.
        self.effect_vectors = {
            mid: self.vector(m["effects"]) * self.realized_share(mid) for mid, m in self.measures.items()
        }

        # Район можно указать по id («nura») или по названию («Нура»), регистр не важен.
        self._district_lookup = {}
        for did, d in self.districts.items():
            self._district_lookup[did.lower()] = did
            self._district_lookup[d["name"].lower()] = did

    def realized_share(self, measure_id: str) -> float:
        """Доля эффекта, успевающая проявиться за горизонт: (H − L) / H.
        Если лаг не меньше горизонта, эффекта нет (доля 0, а не отрицательная)."""
        lag = self.measures[measure_id]["lag"]
        return max(0.0, (self.horizon - lag) / self.horizon)

    def vector(self, values: dict) -> np.ndarray:
        """{"S1": 16, ...} → вектор по всем показателям (нули там, где эффекта нет)."""
        vec = np.zeros(len(self.indicator_ids))
        for k, v in values.items():
            vec[self.indicator_index[k]] = v
        return vec

    def is_city(self, measure_id: str) -> bool:
        """True — общегородская мера: действует на все районы, район не указывается."""
        return self.measures[measure_id]["scope"] == "city"

    def coverage(self, measure_id: str, district_id: str | None) -> np.ndarray:
        """Маска районов, на которые действует мера: 1 — действует, 0 — нет."""
        if self.is_city(measure_id):
            return np.ones(len(self.district_ids))
        mask = np.zeros(len(self.district_ids))
        mask[self.district_index[district_id]] = 1.0
        return mask

    def district_mask(self, district_id: str | None) -> np.ndarray:
        """Маска районов для события: None — весь город, иначе только указанный район."""
        if district_id is None:
            return np.ones(len(self.district_ids))
        mask = np.zeros(len(self.district_ids))
        mask[self.district_index[district_id]] = 1.0
        return mask

    def cost(self, measure_ids) -> float:
        """Суммарная стоимость мер."""
        return sum(self.measures[m]["cost"] for m in measure_ids)

    def find_measure(self, value) -> str | None:
        """Id меры в каноническом виде («m7» → «M7») или None, если такой меры нет."""
        mid = str(value).strip().upper()
        return mid if mid in self.measures else None

    def find_district(self, value) -> str | None:
        """Id района по id или русскому названию; None, если район не найден."""
        return self._district_lookup.get(str(value).strip().lower())

    def district_name(self, district_id: str | None) -> str:
        """Название района; для общегородской меры (район None) — «весь город»."""
        return self.districts[district_id]["name"] if district_id else "весь город"


_active: CityData | None = None  # данные, с которыми работают функции движка


def load_data(path: str | Path | None = None, events_path: str | Path | None = None) -> CityData:
    """Загружает city_data.json, проверяет его и делает активным набором данных.

    Без аргумента файл ищется по путям из DEFAULT_DATA_PATHS. События берутся
    из events_path или из events.json рядом с данными; если файла нет,
    событий просто нет — основной расчёт от них не зависит.
    """
    global _active
    file = Path(path) if path is not None else _default_path()
    raw = _read_json(file)
    events_file = Path(events_path) if events_path is not None else file.parent / EVENTS_FILENAME
    events = _read_json(events_file) if events_file.exists() else []
    _active = CityData(raw, source=str(file), events=events)
    return _active


def _read_json(path: Path):
    with open(path, encoding="utf-8") as f:  # явно utf-8: в Windows по умолчанию cp1251
        return json.load(f)


def get_data() -> CityData:
    """Активные данные; при первом обращении загружается файл по умолчанию."""
    return _active if _active is not None else load_data()


def _default_path() -> Path:
    for path in DEFAULT_DATA_PATHS:
        if path.exists():
            return path
    tried = ", ".join(str(p) for p in DEFAULT_DATA_PATHS)
    raise FileNotFoundError(f"Не найден файл данных city_data.json. Искали: {tried}")


def _check_integrity(d: CityData) -> None:
    """Проверяет ссылочную целостность файла данных.

    Ошибка в данных должна останавливать загрузку с понятным сообщением,
    а не давать тихо неверный Score.
    """
    problems = []
    for did, district in d.districts.items():
        missing = [k for k in d.indicator_ids if k not in district["indicators"]]
        if missing:
            problems.append(f"у района {did} нет значений показателей {missing}")
    for mid, m in d.measures.items():
        if m["direction"] not in d.directions:
            problems.append(f"у меры {mid} неизвестное направление {m['direction']!r}")
        if m["scope"] not in ("district", "city"):
            problems.append(f"у меры {mid} scope должен быть district или city, а не {m['scope']!r}")
        unknown = [k for k in m["effects"] if k not in d.indicators]
        if unknown:
            problems.append(f"мера {mid} влияет на неизвестные показатели {unknown}")
    for s in d.synergies:
        unknown = [x for x in s["pair"] if x not in d.measures]
        if unknown:
            problems.append(f"синергия {s['pair']} ссылается на неизвестные меры {unknown}")
        if s["applies_to_district_of"] not in s["pair"]:
            problems.append(f"синергия {s['pair']}: applies_to_district_of должен быть одной из мер пары")
        unknown = [k for k in s["bonus"] if k not in d.indicators]
        if unknown:
            problems.append(f"синергия {s['pair']} даёт бонус неизвестным показателям {unknown}")
    for inc in d.incompatibilities:
        if len(inc["pair"]) != 2 or any(x not in d.measures for x in inc["pair"]):
            problems.append(f"несовместимость {inc['pair']}: нужна пара существующих мер")
        if inc["scope"] not in ("any", "same_district"):
            problems.append(f"несовместимость {inc['pair']}: scope должен быть any или same_district")
    total_weight = sum(ind["weight"] for ind in d.indicators.values())
    if abs(total_weight - 1) > 1e-6:
        problems.append(f"сумма весов показателей = {total_weight:g}, а должна быть 1")
    total_share = sum(district["population_share"] for district in d.districts.values())
    if abs(total_share - 1) > 1e-6:
        problems.append(f"сумма долей населения = {total_share:g}, а должна быть 1")
    if problems:
        raise ValueError("Ошибки в файле данных:\n  - " + "\n  - ".join(problems))


def _check_events(d: CityData, events: list) -> None:
    """Проверка events.json: уникальные id, существующие районы и показатели."""
    if not isinstance(events, list) or not all(isinstance(e, dict) for e in events):
        raise ValueError("Ошибки в файле событий: ожидается список объектов-событий")
    problems = []
    ids = [e.get("id") for e in events]
    for event_id in {i for i in ids if ids.count(i) > 1}:
        problems.append(f"id события {event_id!r} повторяется")
    for e in events:
        label = e.get("id") or e.get("name") or "?"
        if not e.get("id") or not e.get("name"):
            problems.append(f"у события {label} нет id или name")
        if e.get("district") is not None and e["district"] not in d.districts:
            problems.append(f"событие {label}: неизвестный район {e['district']!r}")
        unknown = [k for k in e.get("effects", {}) if k not in d.indicators]
        if unknown:
            problems.append(f"событие {label} влияет на неизвестные показатели {unknown}")
        if not isinstance(e.get("budget_change", 0), (int, float)):
            problems.append(f"событие {label}: budget_change должен быть числом")
    if problems:
        raise ValueError("Ошибки в файле событий:\n  - " + "\n  - ".join(problems))
