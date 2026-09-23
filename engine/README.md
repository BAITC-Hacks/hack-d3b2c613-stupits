# engine — API для интерфейса и советника

Движок — чистая логика расчёта, без UI и LLM. Все функции (кроме `load_data`) возвращают
JSON-сериализуемые словари. Ошибки ввода не бросают исключений: они приходят списком
русских сообщений в поле `errors`. Если всё хорошо, `errors == []`.

```python
import engine
engine.load_data()                       # data/city_data.json + data/events.json
engine.simulate(decisions, event_id="E2")
```

Решение: `{"measure": "M7", "district": "nura"}`. У общегородской меры `"district": null`.

## Что уже было (формат не изменился)

| Функция | Что делает |
|---|---|
| `load_data(path=None, events_path=None)` | загружает данные и события; `events_path` — новый необязательный параметр |
| `validate(decisions, data=None, event_id=None)` | `{"valid", "errors"}` |
| `simulate(decisions, data=None, event_id=None)` | полный разбор плана (Score, районы, вклад мер) |
| `baseline(data=None, event_id=None)` | город без мер |
| `optimize(top_n=10, constraints=None, data=None, event_id=None, robust=False, robust_events=None)` | полный перебор |

**Без `event_id` и `robust` ответы старых функций не изменились ни на байт.** Это проверено
сравнением со снимком ответов до изменений и тестами. Официальный Score (база 52.56, пример
из ТЗ 56.54) всегда считается без событий. Новые ключи появляются только при новых параметрах.

Новые параметры передавайте по имени (`event_id=...`), потому что `data` стоит раньше них.

### Проверка входов `optimize`

Неверный ввод даёт понятную ошибку в `errors` (перебор не выполняется, `results: []`),
а не исключение и не «тихую» работу:
- `budget` — NaN, ±∞, отрицательное число, строка, `true`;
- `exclude` / `include` — число вместо списка, элемент не того типа, неизвестные поля
  (`{"measure": "M7", "districts": "nura"}`), неизвестная мера или район;
- противоречивые `include`: одна мера в двух районах, несовместимые меры (M1+M3,
  M4+M7 или M5+M13 в одном районе), больше 2 мер одного направления, закреплённые меры
  дороже бюджета;
- `robust` не `true`/`false`, `robust_events` не список id, `top_n` не целое ≥ 1.

Каждый план перед выдачей проходит `validate()` и проверку ограничений. Если бы из-за ошибки
в быстрых проверках перебора туда попал невалидный план, он не был бы показан, а в `errors`
появилось бы сообщение «Внутренняя проверка: …». В нормальной работе `errors == []`.

## Новые функции

| Функция | Для чего |
|---|---|
| `list_events(data=None)` | каталог городских событий и как каждое бьёт по городу |
| `robustness(decisions, events=None, data=None)` | устойчивость плана ко всем событиям |
| `compare(plans, event_id=None, data=None)` | рейтинг нескольких планов (команд) |
| `optimize(..., robust=True)` | план с лучшим Score в худшем случае среди событий |
| `event_id` в `validate`/`simulate`/`baseline`/`optimize` | тот же расчёт после события |

### Как устроено событие

Событие (`data/events.json`, 8 штук, id `E1`–`E8`) меняет стартовые условия до мер:
ухудшает исходные показатели в одном районе (`district`) или во всём городе (`district: null`)
и/или меняет бюджет (`budget_change`). Эффект события лагом не масштабируется.
Дальше считаются обычные формулы ТЗ от новой точки старта.

| id | Событие | Где | Эффект | Score города без мер |
|---|---|---|---|---|
| E1 | Авария на магистральной теплосети | Алматы | C1 −15, C2 −5 | 51.22 |
| E2 | Сильный буран | весь город | T1 −6, T2 −5, B2 −4 | 48.10 |
| E3 | Рост ДТП в межсезонье | весь город | B2 −8, T1 −2 | 50.64 |
| E4 | Прорыв водопровода | Байконур | C1 −18, C2 −4, T1 −3 | 51.33 |
| E5 | Вспышка смога | Сарыарка | E2 −12, S2 −3 | 51.33 |
| E6 | Сокращение бюджета | весь город | бюджет −15 (100 → 85) | 52.56 |
| E7 | Закрытие моста на капремонт | Есиль | T1 −10, T2 −4, B2 −2 | 51.26 |
| E8 | Наплыв новосёлов | Нура | S1 −6, S2 −5, T2 −4 | 50.89 |

Неизвестное событие не бросает исключение, а возвращается ошибкой:
`{"valid": false, "errors": ["Неизвестное событие «E99». Доступные события: E1, …"]}`.

---

### `list_events(data=None) -> dict`

```json
{
  "events": [
    {"id": "E1", "name": "Авария на магистральной теплосети", "description": "…",
     "district": "almaty", "district_name": "Алматы",
     "effects": {"C1": -15, "C2": -5},
     "effects_text": "Надёжность ЖКХ -15, Скорость решения обращений жителей -5",
     "budget_change": 0, "budget": 100, "hint": "M13 … в Алматы, M14 …, M12 …",
     "baseline_score": 51.22, "baseline_delta": -1.34,
     "new_critical": [{"district": "almaty", "district_name": "Алматы", "indicator": "C1",
                       "indicator_name": "Надёжность ЖКХ", "value": 35.0}]}
  ],
  "count": 8, "baseline_score": 52.56, "budget": 100, "notes": ["…"]
}
```

`baseline_score` — Score города без мер после события. `new_critical` — что из-за события
упало ниже 40. `hint` — какие меры помогут (текст для карточки события).

### `event_id` в существующих функциях

- **`simulate(decisions, event_id="E1")`**: прежний формат плюс блок `event` сразу после
  `errors`. В этом режиме `baseline` в ответе — город после события, до мер, и `delta` считается
  от него.
  ```json
  "event": {"id": "E1", "name": "…", "description": "…", "district": "almaty", "district_name": "Алматы",
            "effects": {…}, "effects_text": "…", "budget_change": 0, "budget": 100, "hint": "…",
            "baseline_score_without_event": 52.56, "plan_score_without_event": 56.54}
  ```
- **`validate(decisions, event_id="E6")`**: проверка при изменённом бюджете. Пример из ТЗ
  (95 у.е.) при `E6` получает ошибку «…при лимите 85 у.е. … Лимит изменён событием
  «Сокращение бюджета» (-15 у.е.)».
- **`baseline(event_id="E2")`**: прежний формат плюс блок `event` первым ключом. Значения
  показателей — после события.
- **`optimize(event_id="E2")`**: лучший план для города после события. К ответу добавляется
  блок `event`, а `score` и `score_delta` в результатах считаются в условиях события.

### `robustness(decisions, events=None, data=None) -> dict`

Прогоняет один и тот же план через каждое событие (`events` — список id, по умолчанию все).
План фиксирован и выбран до события: если секвестр урезал бюджет и план в него не влезает,
при этом событии он «не проходит» (`valid: false`, `score: null`). Это худший исход.

```json
{
  "valid": true, "errors": [],
  "plan": "M7 (Нура) + M8 (Нура) + M10 (Нура) + M12 (весь город) + M5 (Сарыарка)",
  "decisions": [...], "score": 56.54, "cost": 95,
  "events": [
    {"event": "E1", "name": "Авария на магистральной теплосети", "district": "almaty",
     "district_name": "Алматы", "budget": 100, "baseline_score": 51.22,
     "valid": true, "errors": [], "score": 55.21, "drop": 1.34, "plan_gain": 3.99,
     "N_crit": 1, "min_district": {"id": "nura", "name": "Нура", "D": 52.96},
     "hardest_hit": {"id": "almaty", "name": "Алматы", "D_drop": 2.0},
     "new_critical": [{"district": "almaty", "indicator": "C1", "value": 35.0, "...": "..."}]},
    {"event": "E6", "name": "Сокращение бюджета", "budget": 85, "valid": false,
     "errors": ["Превышен бюджет: …"], "score": null, "drop": null, "hardest_hit": null, "...": "..."}
  ],
  "worst_case": {"...": "строка худшего события целиком"},
  "worst_score": null,
  "fails_under": ["E6"],
  "average_drop": 2.02,
  "summary": "При событии «Сокращение бюджета» план не проходит: … Худший из выдерживаемых сценариев — «Сильный буран»: Score 52.08 (падение 4.46), …",
  "notes": ["…"]
}
```

- `drop` — насколько Score плана при событии ниже, чем без события.
- `plan_gain` — сколько план даёт поверх города без мер при этом событии.
- `hardest_hit` — район с наибольшим падением оценки D. Если общегородское событие бьёт
  по всем одинаково: `{"id": null, "name": "все районы одинаково", "D_drop": 1.46}`.
- `worst_score` — Score в худшем случае. Равен `null`, если план при каком-то событии
  не проходит.
- `summary` — готовая фраза для интерфейса.
- Строки `events` идут в порядке событий в файле, у валидных и невалидных строк одинаковые ключи.
  Удобно выводить таблицей.

### `optimize(top_n=10, constraints=None, robust=True, robust_events=None)`

Ищет план с **лучшим Score в худшем случае** среди событий (`robust_events`, по умолчанию все).
План обязан проходить при каждом событии. Поэтому при `E6` в списке его стоимость не больше
85 у.е. Формат прежний, плюс:
- на верхнем уровне `"mode": "robust"`, `"robust_events": [...]`, `"robust_budget": 85`;
- в каждом результате `"worst_case": {"event": "E2", "name": "Сильный буран", "score": 54.37, "drop": 2.46, "hardest_hit": {...}}`.

`score` в результатах — официальный Score без событий, результаты отсортированы по
`worst_case.score`. Адаптер UI сейчас пересортировывает планы по `score`, поэтому для этого
режима сортировку нужно оставить как есть. `event_id` вместе с `robust=True` задать нельзя:
вернётся ошибка. Весь режим занимает около 0.6 с.

Пример: лучший по Score план (57.24, 98 у.е.) при секвестре не проходит. Устойчивый план
`M1 (Нура) + M2 (город) + M4 (Нура) + M8 (Нура) + M9 (Нура)` стоит 85 у.е. и даёт 56.83, то есть
на 0.41 меньше лучшего плана, но проходит при всех 8 событиях (в худшем случае, при буране, 54.37).

### `compare(plans, event_id=None, data=None) -> dict`

`plans` — словарь «название → набор решений». В качестве значения подходит и элемент
`optimize()["results"]`, у которого есть поле `decisions`. Каждый план считается через `simulate()`.

```json
{
  "errors": [],
  "baseline_score": 52.56,
  "leader": "Оптимум",
  "ranking": [
    {"name": "Оптимум", "rank": 1, "valid": true, "errors": [], "score": 57.24,
     "delta_vs_baseline": 4.68, "gap_to_leader": 0.0, "cost": 98, "budget_left": 2,
     "N_crit": 0, "min_district": {"id": "nura", "name": "Нура", "D": 54.09},
     "measures": ["M2 (весь город)", "M3 (Нура)", "M8 (Нура)", "M9 (Нура)", "M14 (весь город)"],
     "decisions": [...], "unique_measures": ["M2 (весь город)", "M3 (Нура)", "..."],
     "differs_from_leader": null,
     "top_measure": {"measure": "M8", "district_name": "Нура", "score_contribution": 1.4},
     "focus_districts": {"Нура": 3}, "synergies": []},
    {"name": "Пример ТЗ", "rank": 2, "score": 56.54, "gap_to_leader": 0.7,
     "differs_from_leader": {"leader_has": ["M2 (весь город)", "M3 (Нура)", "..."],
                             "plan_has": ["M7 (Нура)", "M10 (Нура)", "..."]}, "...": "..."},
    {"name": "С ошибкой", "rank": null, "valid": false,
     "errors": ["Нужно ровно 5 решений, а выбрано 4."], "score": null, "...": "..."}
  ],
  "common_measures": ["M8 (Нура)"],
  "summary": ["1 место — «Оптимум»: Score 57.24 (+4.68 к базе), стоимость 98 у.е.", "…"]
}
```

- Места получают только валидные планы, по Score. При равном Score выше более дешёвый план,
  при равных Score и стоимости место общее (1, 1, 3).
- Невалидные планы идут в конце с `rank: null` и причинами в `errors`.
- Отличия считаются по парам «мера + район»: M8 в Нуре и M8 в Есиле — разные решения.
- С `event_id` все планы сравниваются в условиях события, и в ответе появляется блок `event`.

---

## Схемы инструментов для советника

Схемы в стиле `SCHEMAS` из `agent/tools.py`. Сам `agent/` я не трогал, их можно вставить как есть.

```python
EVENT_ID = {"type": ["string", "null"]}
SCHEMAS.update({
    "list_events": ("Каталог городских событий: что случилось, где и как это бьёт по городу.",
                    {"type": "object", "properties": {}, "additionalProperties": False}),
    "robustness": ("Проверить устойчивость плана: Score при каждом событии, падение, "
                   "какой район пострадал сильнее и худший случай.",
                   {"type": "object", "properties": {
                       "decisions": DECISIONS["properties"]["decisions"],
                       "events": {"type": ["array", "null"], "items": {"type": "string"}}},
                    "required": ["decisions"], "additionalProperties": False}),
    "compare": ("Сравнить несколько планов: место, Score, стоимость, разница с базой и отличия.",
                {"type": "object", "properties": {
                    "plans": {"type": "object", "additionalProperties": DECISIONS["properties"]["decisions"]},
                    "event_id": EVENT_ID},
                 "required": ["plans"], "additionalProperties": False}),
})
# simulate/validate: добавить в properties "event_id": EVENT_ID;
# optimize: "event_id": EVENT_ID, "robust": {"type": "boolean"}.
```

## Тесты

```bash
.venv/bin/python -m pytest   # 112 тестов, около 12 с (Windows: .venv\Scripts\python -m pytest)
.venv/bin/python check.py    # эталоны ТЗ, топ-5, устойчивость; код выхода 1 при расхождении
```
