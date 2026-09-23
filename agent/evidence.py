"""Подстановка фактов из движка: числам, написанным моделью, не доверяем."""

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
    for token in prose.split():
        if any(char.isnumeric() for char in token):
            raise EvidenceError(f"Число «{token[:80]}» написано без ссылки. Замени его ссылкой на поле движка.")
    numeral = NUMBER_WORDS.search(prose)
    if numeral:
        raise EvidenceError(f"Числительное «{numeral[0]}» написано без ссылки. Используй поле движка.")

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
                if (re.search(r"[+\-−]\s*$|\w$", before) or re.match(r"\w|%|\{\{|[.,]\{\{", after)):
                    raise EvidenceError("Числовая ссылка должна использоваться без преобразований.")
            return format_value(value)
        except EvidenceError as exc:
            raise EvidenceError(f"Ссылка {reference}: {exc}") from exc

    return REFERENCE.sub(substitute, draft).strip()
