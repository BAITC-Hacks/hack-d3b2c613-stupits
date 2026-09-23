"""
Математическая модель симулятора — формулы из раздела 3 ТЗ.

  1. I'_dk = clip(I_dk + Σ_m эффект_mk × (H − L_m)/H + синергии, 0, 100)
  2. D_d   = Σ_k w_k × I'_dk
  3. D_avg = Σ_d pop_d × D_d
  4. Score = w_avg × D_avg + w_min × min_d(D_d) − penalty × N_crit,
     где N_crit — число пар (район, показатель) со значением строго ниже порога.

Коэффициенты (H = 8, 0.7, 0.3, 1.0, порог 40) берутся из city_data.json.
Шаги 2–4 записаны один раз в score_parts() и работают как для одного
сценария, так и для пачки из тысяч сценариев сразу: simulate() и optimize()
считают Score одним и тем же кодом.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .data import SCALE_MAX, SCALE_MIN, CityData


def apply_measures(data: CityData, placements) -> tuple[np.ndarray, list]:
    """Шаг 1 без clip: суммарная добавка к показателям от мер и синергий.

    placements — список пар (мера, район); у общегородской меры район None.
    Возвращает матрицу добавок «районы × показатели» и список сработавших синергий.
    """
    delta = np.zeros_like(data.base)
    district_of = {}
    for mid, did in placements:
        # Эффект с учётом лага: только в своём районе (district) или во всех (city).
        delta += np.outer(data.coverage(mid, did), data.effect_vectors[mid])
        district_of[mid] = did

    fired = []
    for syn in data.synergies:
        if all(m in district_of for m in syn["pair"]):
            # Бонус фиксированный (лагом не масштабируется) и достаётся району
            # меры applies_to_district_of. Трактовка: если эта мера общегородская,
            # бонус получают все районы (в текущих данных такого случая нет).
            target = syn["applies_to_district_of"]
            delta += np.outer(data.coverage(target, district_of[target]), data.vector(syn["bonus"]))
            fired.append({"synergy": syn, "district": district_of[target]})
    return delta, fired


def score_parts(values: np.ndarray, data: CityData) -> dict:
    """Шаги 2–4. values — показатели после clip: матрица «районы × показатели»
    или пачка сценариев «N × районы × показатели»."""
    D = values @ data.weights                                   # оценка каждого района
    D_avg = D @ data.population                                 # средняя по городу, веса — доли населения
    min_D = D.min(axis=-1)                                      # самый слабый район
    n_crit = (values < data.crit_threshold).sum(axis=(-2, -1))  # строго меньше порога
    score = data.w_avg * D_avg + data.w_min * min_D - data.crit_penalty * n_crit
    return {"D": D, "D_avg": D_avg, "min_D": min_D, "N_crit": n_crit, "score": score}


@dataclass
class Evaluation:
    """Результат расчёта одного сценария — точные числа без округления."""

    values: np.ndarray  # показатели после мер: районы × показатели
    D: np.ndarray       # оценка каждого района
    D_avg: float
    min_D: float
    min_index: int      # индекс самого слабого района
    n_crit: int
    score: float
    synergies: list     # сработавшие синергии


def evaluate(data: CityData, placements) -> Evaluation:
    """Считает один сценарий. Правила не проверяет — это задача validation."""
    delta, fired = apply_measures(data, placements)
    values = np.clip(data.base + delta, SCALE_MIN, SCALE_MAX)
    p = score_parts(values, data)
    return Evaluation(
        values=values,
        D=p["D"],
        D_avg=float(p["D_avg"]),
        min_D=float(p["min_D"]),
        min_index=int(np.argmin(p["D"])),
        n_crit=int(p["N_crit"]),
        score=float(p["score"]),
        synergies=fired,
    )
