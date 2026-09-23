"""Числа из ответов движка: ссылки и точно скопированные числовые значения."""

from __future__ import annotations

from decimal import Decimal
import math
import re


REFERENCE = re.compile(r"\{\{(e\d+):(/[^{}\s]*)\}\}")
# Конечные формы числительных: «сотрудники» и «пятна» — обычные слова,
# поэтому проверка по широким корням сот*/пят* здесь непригодна.
_NUMERAL_FORMS = """
ноль нуль нуля нолю нулю нулём нулем нуле нолём нолем ноли нули нулей нолей
один одна одно одного одной одною одному одну одним одними одном одни одних
два две двух двум двумя три трёх трех трём трем тремя
четыре четырёх четырех четырём четырем четырьмя
пять пяти пятью шесть шести шестью семь семи семью восемь восьми восемью восьмью
девять девяти девятью десять десяти десятью
одиннадцать одиннадцати одиннадцатью двенадцать двенадцати двенадцатью
тринадцать тринадцати тринадцатью четырнадцать четырнадцати четырнадцатью
пятнадцать пятнадцати пятнадцатью шестнадцать шестнадцати шестнадцатью
семнадцать семнадцати семнадцатью восемнадцать восемнадцати восемнадцатью
девятнадцать девятнадцати девятнадцатью двадцать двадцати двадцатью
тридцать тридцати тридцатью сорок сорока
пятьдесят пятидесяти пятьюдесятью шестьдесят шестидесяти шестьюдесятью
семьдесят семидесяти семьюдесятью восемьдесят восьмидесяти восемьюдесятью восьмьюдесятью
девяносто девяноста сто ста
двести двухсот двумстам двумястами двухстах
триста трёхсот трехсот трёмстам тремстам тремястами трёхстах трехстах
четыреста четырёхсот четырехсот четырёмстам четыремстам четырьмястами четырёхстах четырехстах
пятьсот пятисот пятистам пятьюстами пятистах шестьсот шестисот шестистам шестьюстами шестистах
семьсот семисот семистам семьюстами семистах восемьсот восьмисот восьмистам восемьюстами восьмьюстами восьмистах
девятьсот девятисот девятистам девятьюстами девятистах
сотня сотни сотню сотней сотнею сотне сотен сотням сотнями сотнях
тысяча тысячи тысячу тысячей тысячею тысяче тысяч тысячам тысячами тысячах
миллион миллиона миллиону миллионом миллионе миллионы миллионов миллионам миллионами миллионах
миллиард миллиарда миллиарду миллиардом миллиарде миллиарды миллиардов миллиардам миллиардами миллиардах
двое двоих двоим двоими трое троих троим троими четверо четверых четверым четверыми
пятеро пятерых пятерым пятерыми шестеро шестерых шестерым шестерыми
семеро семерых семерым семерыми восьмеро восьмерых восьмерым восьмерыми
девятеро девятерых девятерым девятерыми десятеро десятерых десятерым десятерыми
вдвое втрое вчетверо впятеро вшестеро всемеро ввосьмеро вдевятеро вдесятеро
половина половины половину половиной половиною половине половин половинам половинами половинах
треть трети третью третей третям третями третях
четверть четверти четвертью четвертей четвертям четвертями четвертях
полтора полторы полутора полтораста полутораста
zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen
sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety
hundred hundreds thousand thousands million millions billion billions twice thrice half quarter
""".split()
NUMBER_WORDS = re.compile(r"\b(?:" + "|".join(sorted(set(_NUMERAL_FORMS))) + r")\b", re.IGNORECASE)
_IDENTIFIER = re.compile(r"\b(?:M\d+|[TESBC]\d+)\b", re.IGNORECASE)
_DECIMAL_TEXT = r"[+\-−]?[0-9]+(?:[.,][0-9]+)?"
_LITERAL = re.compile(r"(?<![\w.,])" + _DECIMAL_TEXT + r"(?![\w]|[.,][0-9])")
# Обычные малые количества можно копировать из движка словами: это другая
# запись того же scalar, а не вычисление. Составные числительные не разбираем.
_SMALL_NUMERALS = {
    word: Decimal(value)
    for value, forms in enumerate((
        "ноль нуль нуля нолю нулю нулём нулем нуле нолём нолем ноли нули нулей нолей",
        "один одна одно одного одной одною одному одну одним одними одном одни одних",
        "два две двух двум двумя",
        "три трёх трех трём трем тремя",
        "четыре четырёх четырех четырём четырем четырьмя",
        "пять пяти пятью",
        "шесть шести шестью",
        "семь семи семью",
        "восемь восьми восемью восьмью",
        "девять девяти девятью",
        "десять десяти десятью",
    ))
    for word in forms.split()
}


class EvidenceError(ValueError):
    """Ответ содержит неподтверждённое число или неверную ссылку."""


def format_value(value: int | float | str) -> str:
    """Меняем только запись числа; новых вычислений и округления здесь нет."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise EvidenceError("Ссылка должна указывать на число или строку.")
    if isinstance(value, float) and not math.isfinite(value):
        raise EvidenceError("Движок вернул неконечное число.")
    if isinstance(value, str):
        if len(value) > 500:
            raise EvidenceError("Для ответа нужна ссылка на короткое поле.")
        # Данные не могут добавлять HTML, ссылки или форматирование в ответ.
        return re.sub(r"([\\`*_{}\[\]()<>#!|])", r"\\\1", value)
    return format(Decimal(str(value)), "f").replace(".", ",")


def resolve_pointer(result: dict, pointer: str):
    """JSON Pointer к ответу движка; допускаем известную лишнюю обёртку /result/."""
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise EvidenceError("Путь к полю должен начинаться с /.")
    # Инструмент отдаёт модели {evidence_id, result}, а evidence хранит сам result.
    # Не удаляем префикс, если result — настоящее поле исходного ответа.
    if isinstance(result, dict) and "result" not in result and pointer.startswith("/result/"):
        pointer = pointer[len("/result"):]
    value = result
    traversed = ""
    for part in pointer.split("/")[1:]:
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", key):
                raise EvidenceError(f"По пути «{traversed or '/'}» нужен индекс списка, получено «{key}».")
            index = int(key)
            if index >= len(value):
                raise EvidenceError(f"Индекс «{key}» отсутствует по пути «{traversed or '/'}».")
            value = value[index]
        elif isinstance(value, dict):
            if key not in value:
                available = ", ".join(str(k) for k in list(value)[:12]) or "нет полей"
                raise EvidenceError(
                    f"Поле «{key}» отсутствует по пути «{traversed or '/'}». Доступно: {available}."
                )
            value = value[key]
        else:
            raise EvidenceError(f"По пути «{traversed or '/'}» находится значение, у него нет поля «{key}».")
        traversed += "/" + part
    return value


def _known_identifiers(evidence: dict) -> set[str]:
    """Коды мер/показателей разрешены только при наличии в ответах движка."""
    identifiers: set[str] = set()

    def collect(value):
        if isinstance(value, str):
            identifiers.update(match[0].upper() for match in _IDENTIFIER.finditer(value))
        elif isinstance(value, dict):
            for key, item in value.items():
                collect(key)
                collect(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item)

    collect(evidence)
    return identifiers


def _numeric_facts(evidence: dict) -> list[tuple[Decimal, str, str]]:
    """Только конечные числовые scalar-поля, никогда цифры внутри строк."""
    facts = []

    def collect(value, evidence_id, pointer):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = Decimal(str(value))
            if number.is_finite():
                facts.append((number, "{{" + evidence_id + ":" + pointer + "}}", pointer))
        elif isinstance(value, dict):
            for key, item in value.items():
                part = str(key).replace("~", "~0").replace("/", "~1")
                collect(item, evidence_id, pointer + "/" + part)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                collect(item, evidence_id, pointer + "/" + str(index))

    for evidence_id, result in evidence.items():
        collect(result, evidence_id, "")
    return facts


def _as_decimal(text: str) -> Decimal:
    return Decimal(text.replace(",", ".").replace("−", "-"))


def _fact_hints(facts, limit=4) -> str:
    return "; ".join(f"{reference} = {number}" for number, reference, _ in facts[:limit]) or "числовых полей нет"


def _check_number_roles(answer: str, evidence: dict, facts: list) -> None:
    """Две явные роли чисел: лимит поиска и абсолютный Score; это не NLP-анализ."""
    plain = re.sub(r"[*`_]", "", answer)
    limit = None
    for evidence_id, result in reversed(list(evidence.items())):
        constraints = result.get("constraints") if isinstance(result, dict) else None
        if isinstance(constraints, dict):
            budget = constraints.get("budget")
            if isinstance(budget, (int, float)) and not isinstance(budget, bool):
                number = Decimal(str(budget))
                if number.is_finite():
                    limit = (number, "{{" + evidence_id + ":/constraints/budget}}")
                    break
    if limit is not None:
        pattern = r"\bбюджет(?:а|ом|е|у)?\b(?P<between>[^\n.!?;]{0,60}?)\bдо\s*(?P<value>" + _DECIMAL_TEXT + r")"
        for match in re.finditer(pattern, plain, re.IGNORECASE):
            context = plain[max(0, match.start() - 32):match.start()] + match["between"]
            if re.search(r"\b(?:общий|общего|общем|общим|общему|официальн\w*)\b", context, re.IGNORECASE):
                continue
            if _as_decimal(match["value"]) != limit[0]:
                raise EvidenceError(f"Лимит поиска подменён: бюджет до {match['value']}; используй {limit[1]} = {limit[0]}.")

    # delta.score и score_contribution — изменения/вклады, не абсолютная оценка.
    scores = [fact for fact in facts if "score" in fact[2].split("/")[-1].lower()
              and not any(word in fact[2].lower() for word in ("delta", "contribution", "gain", "drop", "gap", "efficiency"))]
    allowed_scores = {fact[0] for fact in scores}
    pattern = (r"\bScore\b\s*(?:(?:плана|города)\s*)?(?:(?:равен|составит|составляет)\s*)?"
               r"[:=—]?\s*(?P<value>" + _DECIMAL_TEXT + r")")
    for match in re.finditer(pattern, plain, re.IGNORECASE):
        before = re.split(r"[.!?;\n]", plain[:match.start()])[-1][-60:]
        if re.search(r"\b(?:изменени\w*|прирост\w*|дельта|разниц\w*)\b", before, re.IGNORECASE):
            continue
        if _as_decimal(match["value"]) not in allowed_scores:
            raise EvidenceError(f"Score {match['value']} не подтверждён полем оценки. Доступно: {_fact_hints(scores)}.")


def render_grounded_answer(draft: str, evidence: dict[str, dict]) -> str:
    if not isinstance(draft, str) or not draft.strip():
        raise EvidenceError("Пустой ответ советника.")
    prose = REFERENCE.sub("", draft)
    if "{{" in prose or "}}" in prose:
        raise EvidenceError("Некорректная ссылка: нужен формат {{e3:/score}} без пробелов внутри.")
    identifiers = _known_identifiers(evidence)

    def hide_identifier(match):
        if match[0].upper() not in identifiers:
            raise EvidenceError(f"Код «{match[0]}» отсутствует в полученных данных движка.")
        return ""

    prose = _IDENTIFIER.sub(hide_identifier, prose)
    facts = _numeric_facts(evidence)
    allowed_values = {fact[0] for fact in facts}

    def hide_literal(match):
        if re.match(r"\s*%", prose[match.end():]):
            raise EvidenceError("Нельзя превращать значение движка в проценты.")
        if _as_decimal(match[0]) not in allowed_values:
            raise EvidenceError(f"Число «{match[0]}» отсутствует в числовых полях движка. Используй факт: {_fact_hints(facts)}.")
        return ""

    remaining = _LITERAL.sub(hide_literal, prose)
    for token in remaining.split():
        if any(char.isnumeric() for char in token):
            raise EvidenceError(f"Числовая запись «{token[:80]}» не поддерживается: используй десятичное значение или ссылку без преобразований.")
    numerals = list(NUMBER_WORDS.finditer(prose))
    for left, right in zip(numerals, numerals[1:]):
        if re.fullmatch(r"[\s\-–—]+", prose[left.end():right.start()]):
            raise EvidenceError("Составное числительное нельзя проверять по частям. Используй ссылку на одно поле движка.")
    for numeral in numerals:
        number = _SMALL_NUMERALS.get(numeral[0].lower())
        if number is None:
            raise EvidenceError(f"Числительное «{numeral[0]}» требует ссылки на поле движка.")
        if number not in allowed_values:
            raise EvidenceError(f"Количество «{numeral[0]}» отсутствует в числовых полях движка. Используй факт: {_fact_hints(facts)}.")

    def substitute(match):
        reference = match[0]
        if match[1] not in evidence:
            available = ", ".join(evidence) or "нет результатов"
            raise EvidenceError(f"Ссылка {reference}: результат {match[1]} отсутствует. Доступно: {available}.")
        try:
            value = resolve_pointer(evidence[match[1]], match[2])
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                before, after = draft[:match.start()], draft[match.end():]
                # Нельзя превратить число источника в другое приставкой знака,
                # склейкой значений, экспонентой или знаком процента.
                if (re.search(r"[+\-−]\s*$|\w$", before) or re.match(r"\w|\s*%|\{\{|[.,]\{\{", after)):
                    raise EvidenceError("Числовая ссылка должна использоваться без преобразований.")
            return format_value(value)
        except EvidenceError as exc:
            raise EvidenceError(f"Ссылка {reference}: {exc}") from exc

    answer = REFERENCE.sub(substitute, draft).strip()
    # Роли проверяем на цифровой записи; в показанном ответе сохраняем
    # естественное «два показателя» / «с нулём критических значений».
    role_text = NUMBER_WORDS.sub(
        lambda match: str(_SMALL_NUMERALS[match[0].lower()])
        if match[0].lower() in _SMALL_NUMERALS else match[0], answer,
    )
    _check_number_roles(role_text, evidence, facts)
    return answer
