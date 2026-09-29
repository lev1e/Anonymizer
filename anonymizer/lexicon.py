"""Словарный разбор слова: фамилия, имя, отчество — или обычное слово.

Суффиксных правил на русский не хватает в обе стороны. «Ивановичу» и «Смирновой» по
окончанию не опознаются, хотя это очевидные отчество и фамилия в косвенном падеже; «Логист»,
«Поставка» и «Выгрузка» по окончанию неотличимы от фамилии, хотя это обычные слова из
делового текста. Оба промаха стоят дорого: первый оставляет ФИО в документе открытым, второй
заводит несуществующего человека и вырезает по всему проекту нормальное слово.

Здесь и то и другое решается словарём OpenCorpora через pymorphy3: у него есть пометки Surn,
Name и Patr на всех падежных формах и, что не менее важно, признак «слово вообще есть в
словаре как обычная лексема». Отсутствие слова в словаре — тоже сигнал: незнакомое слово с
заглавной буквы гораздо вероятнее фамилия, чем существительное.

Пакет опциональный. Без него модуль отдаёт пустой разбор, и вызывающий код откатывается на
суффиксные правила: качество ниже, но программа работает.
"""

from __future__ import annotations

import threading
from functools import lru_cache
from typing import NamedTuple

# Части речи обычной лексики. Служебные (PREP, CONJ, PRCL) сюда входят намеренно: «Для»,
# «Или», «Уже» в начале ячейки стоят с заглавной и фамилией не бывают.
LEXICAL_POS = frozenset({
    "NOUN", "ADJF", "ADJS", "VERB", "INFN", "PRTF", "PRTS", "GRND", "ADVB",
    "NPRO", "PRED", "PREP", "CONJ", "PRCL", "INTJ", "NUMR", "COMP",
})

NAME_TAGS = frozenset({"Surn", "Name", "Patr"})

MALE, FEMALE, UNKNOWN = "m", "f", "?"


class Shape(NamedTuple):
    """Что словарь знает о слове. Все поля независимы: слово бывает и фамилией, и словом."""

    surname: bool       # есть разбор как фамилия (в любом падеже)
    given: bool         # есть разбор как личное имя
    patronymic: bool    # есть разбор как отчество
    lexical: bool       # есть словарный разбор как обычное слово
    known: bool         # слово нашлось в словаре, а не достроено по образцу
    nominative: bool    # среди разборов есть именительный падеж
    gender: str         # род по разбору-имени, если он однозначен
    lemma: str          # начальная форма


EMPTY = Shape(False, False, False, False, False, False, UNKNOWN, "")

_analyzer = None
_tried = False
_load_lock = threading.Lock()


def analyzer():
    """Ленивый и одноразовый: словарь весит 16 МБ и грузится 30 мс, но только при первом ФИО.

    Загрузка идёт под блокировкой, и флаг «уже пробовали» ставится ПОСЛЕ неё. Файлы
    проверяются в несколько потоков, и без этого второй поток заставал флаг уже поднятым, а
    словарь ещё не готовым, получал None и тихо откатывался на суффиксные правила. Наружу это
    выходило как плавающий результат: одно и то же ФИО в косвенном падеже то распознавалось,
    то нет, в зависимости от того, какой файл успел первым.
    """
    global _analyzer, _tried
    if _tried:
        return _analyzer
    with _load_lock:
        if _tried:
            return _analyzer
        try:
            import pymorphy3
            _analyzer = pymorphy3.MorphAnalyzer()
        except Exception:
            # Отсутствие пакета, битый словарь, несовместимая версия — во всех случаях
            # откатываемся на суффиксные правила, а не роняем разбор документа.
            _analyzer = None
        _tried = True
    return _analyzer


def available() -> bool:
    return analyzer() is not None


# Части речи, которые склоняются. Для них «обычное слово» засчитывается только в словарной
# форме — см. `_is_ordinary`.
DECLINED_POS = frozenset({"NOUN", "ADJF", "PRTF", "NUMR"})


def _is_ordinary(parse) -> bool:
    """Разбор говорит, что перед нами обычное слово, а не имя собственное.

    Для склоняемых частей речи требуется именительный падеж. Иначе фамилии на -ых/-их и -ов
    попадают под разбор прилагательного или существительного в косвенном падеже и объявляются
    обычным словом: «Петропавловских» словарь честно читает как «петропавловский» в
    родительном множественного, после чего фамилия перестаёт распознаваться вовсе.

    На несклоняемых частях речи (наречие, частица, союз, предлог, глагол) такого требования
    нет — «Только», «Если», «Сейчас» надо гасить в любой форме.
    """
    pos = parse.tag.POS
    if pos not in LEXICAL_POS:
        return False
    if pos in DECLINED_POS:
        return "nomn" in parse.tag.grammemes
    return True


def _is_dictionary(parse) -> bool:
    """Разбор взят из словаря, а не предсказан по окончанию.

    Предсказанный разбор годится как слабый довод «похоже на фамилию», но как довод
    «это обычное слово» — нет: предсказатель охотно объявляет существительным что угодно.
    """
    stack = getattr(parse, "methods_stack", ())
    return bool(stack) and type(stack[0][0]).__name__ == "DictionaryAnalyzer"


@lru_cache(maxsize=32768)
def shape(word: str) -> Shape:
    """Разбор слова. Кэш обязателен: scan идёт по каждой ячейке, слова повторяются тысячами."""
    morph = analyzer()
    if morph is None or not word:
        return EMPTY
    try:
        parses = morph.parse(word)
    except Exception:
        return EMPTY
    if not parses:
        return EMPTY

    surname = given = patronymic = lexical = known = nominative = False
    gender = UNKNOWN
    for parse in parses:
        grammemes = set(parse.tag.grammemes)
        tags = NAME_TAGS & grammemes
        from_dict = _is_dictionary(parse)
        known = known or from_dict
        if "Surn" in tags:
            surname = True
        if "Name" in tags:
            given = True
        if "Patr" in tags:
            patronymic = True
        if tags and gender == UNKNOWN:
            if parse.tag.gender == "femn":
                gender = FEMALE
            elif parse.tag.gender == "masc":
                gender = MALE
        if tags and "nomn" in grammemes:
            nominative = True
        if from_dict and not tags and _is_ordinary(parse):
            lexical = True
    return Shape(surname, given, patronymic, lexical, known, nominative, gender,
                 parses[0].normal_form)


APOSTROPHE = frozenset("'\u2019\u02bc")


@lru_cache(maxsize=16384)
def to_nominative(word: str, role: str, preferred: frozenset = frozenset()) -> tuple[str, str]:
    """Именительный падеж слова в заданной роли плюс род: ('Смирновой','Surn') -> ('Смирнова','f').

    Приводить к начальной форме нужно ради тождества человека, а не ради текста: «Иванову»,
    «Иванова» и «Ивановым» обязаны попасть в одну запись, иначе один сотрудник размножится
    на три и каждое упоминание станет неоднозначным.

    `preferred` — множество известных начальных форм (свёрнутых через fold). Оно разрешает
    ничьи: «Анне» словарь разбирает и как дательный от «Анна», и как несклоняемое
    французское «Анне», причём с одинаковым весом, и без подсказки побеждает второе.
    """
    morph = analyzer()
    if morph is None or not word:
        return word, UNKNOWN
    try:
        parses = morph.parse(word)
    except Exception:
        return word, UNKNOWN

    # Слово уже в известной начальной форме — трогать нечего. Без этой проверки склонение
    # назад калечит нормальные имена: «Созон» превращался в «Созона», «Виталия» в мужское
    # «Виталий», «Жеральдина» в «Жеральдин». Приводить к начальной форме нужно только то,
    # что в ней ещё не стоит.
    if preferred and word.casefold() in preferred:
        return word, UNKNOWN

    # Фамилия с частицей и апострофом словарю неизвестна целиком, и предсказатель разбирает
    # её наугад: «О'Коннор» он читает как родительный падеж выдуманного «о'коннора». Человек
    # заносился в список под этой формой — чужой падеж и потерянная заглавная внутри слова, —
    # а настоящее написание в указатель не попадало вовсе, и второе упоминание в том же
    # документе оставалось необезличенным. Проверить догадку нечем: ни один разбор не
    # словарный. Оставляем написание как есть.
    if (APOSTROPHE & set(word)) and not any(parse.is_known for parse in parses):
        return word, UNKNOWN

    best = None
    nominative_reading = False
    for parse in parses:
        if role not in parse.tag.grammemes:
            continue
        if {"nomn", "sing"} <= set(parse.tag.grammemes):
            nominative_reading = True
        candidate = _nominative_of(parse)
        if candidate is None:
            continue
        gender = FEMALE if parse.tag.gender == "femn" else MALE if parse.tag.gender == "masc" else UNKNOWN
        if preferred and candidate.casefold() in preferred:
            return _match_case(word, candidate), gender
        if best is None:
            best = (candidate, gender)
    # Разбор в именительном единственного есть — значит слово может быть начальной формой, и
    # переписывать его нельзя: «Хасанов» иначе становился «Хасан», а «Сергеева» — «Сергеев».
    # Число обязательно: «Дмитриевны» словарь читает и как именительный МНОЖЕСТВЕННОГО, и
    # без этой оговорки отчество оставалось в родительном падеже.
    if nominative_reading:
        return word, best[1] if best else UNKNOWN
    if best is not None:
        return _match_case(word, best[0]), best[1]

    # Роль не нашлась совсем: «Ковалем» словарь знает только как обычное существительное.
    # Начальная форма всё равно нужна — иначе человек осядет в списке под косвенной формой.
    #
    # Только по словарным разборам. Предсказатель на незнакомом слове выдумывает парадигму и
    # отрезает лишнее: якутское «Ыгыа» он читал как родительный падеж и превращал в «Ыгы»,
    # после чего заученное имя переставало совпадать с тем, что написано в документе.
    dictionary = [p for p in parses if _is_dictionary(p) and not (NAME_TAGS & set(p.tag.grammemes))]
    if any({"nomn", "sing"} <= set(p.tag.grammemes) for p in dictionary):
        return word, UNKNOWN
    for parse in dictionary:
        candidate = _nominative_of(parse)
        if candidate and candidate.casefold() != word.casefold():
            return _match_case(word, candidate), UNKNOWN
    return word, UNKNOWN


def _nominative_of(parse) -> str | None:
    """Именительная форма разбора. Склонение назад иногда возвращает ту же форму — тогда
    берём лемму, а если и она не изменилась, разбор нам ничего не дал."""
    form = parse.inflect({"nomn", "sing"})
    if form is not None and "nomn" in form.tag.grammemes:
        return form.word
    return parse.normal_form or None


def _match_case(source: str, result: str) -> str:
    """Словарь отдаёт всё строчными, а в индекс и в список людей форма попадает как есть."""
    if source.isupper():
        return result.upper()
    if source[:1].isupper():
        return result[:1].upper() + result[1:]
    return result


_PROPER_TAGS = frozenset({"Name", "Surn", "Patr", "Geox", "Orgn", "Trad"})


@lru_cache(maxsize=32768)
def is_common_word(word: str) -> bool:
    """Обычное слово в любой форме: «Клиента», «Договоров». Собственные имена и названия сюда не относятся.

    Отличие от `Shape.lexical`: там для склоняемых слов нужен именительный падеж, чтобы фамилии в косвенном
    падеже не принимались за обычные слова. Здесь смотрят на все словарные разборы сразу.
    """
    morph = analyzer()
    if morph is None or not word:
        return False
    try:
        parses = morph.parse(word)
    except Exception:
        return False
    dictionary = [p for p in parses if _is_dictionary(p)]
    if not dictionary:
        return False
    if any(_PROPER_TAGS & set(p.tag.grammemes) for p in dictionary):
        return False
    return any(p.tag.POS in LEXICAL_POS for p in dictionary)
