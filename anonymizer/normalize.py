from __future__ import annotations

import re
import unicodedata
from array import array


# Invisible characters that survive copy-paste from the web and from PDF and would
# otherwise split a name in two for every lookup.
INVISIBLE = frozenset(
    "\u200b\u200c\u200d\u2060\ufeff"   # zero width space / non-joiner / joiner / word joiner / BOM
    "\u00ad\u180e\u034f"                # soft hyphen, Mongolian vowel separator, combining grapheme joiner
)

# Latin letters that render identically to a Cyrillic letter. Applied only inside a
# word that already mixes the two scripts, so genuine Latin text is left alone.
LATIN_TO_CYRILLIC = {
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "I": "І", "K": "К", "M": "М",
    "O": "О", "P": "Р", "S": "Ѕ", "T": "Т", "X": "Х", "Y": "У",
    "a": "а", "c": "с", "e": "е", "i": "і", "o": "о", "p": "р", "s": "ѕ", "x": "х", "y": "у",
}
CYRILLIC_TO_LATIN = {
    "А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H", "К": "K", "М": "M", "О": "O",
    "Р": "P", "Т": "T", "Х": "X", "У": "Y", "а": "a", "с": "c", "е": "e", "о": "o",
    "р": "p", "х": "x", "у": "y",
}

# Знаки, которые пишущий воспринимает как дефис. Мягкий перенос сюда не входит: он уже
# вычеркнут вместе с прочими невидимками.
DASHES = frozenset("\u2010\u2011\u2012\u2013\u2014\u2015\u2212\uff0d\u02d7\u058a\u1806")

CYRILLIC_RANGE = ("Ѐ", "ӿ")


def _is_cyrillic(ch: str) -> bool:
    return CYRILLIC_RANGE[0] <= ch <= CYRILLIC_RANGE[1]


def _is_latin(ch: str) -> bool:
    return ("A" <= ch <= "Z") or ("a" <= ch <= "z")


class NormalizedText:
    """Cleaned-up text plus the mapping needed to point back at the raw document.

    Detection runs on `text`; every span is translated back to raw coordinates with
    `to_original`, so replacements always land on the exact original bytes.
    """

    __slots__ = ("text", "_starts", "_ends", "_identity")

    def __init__(self, text: str, starts: "array[int] | None", ends: "array[int] | None", identity: bool = False):
        self.text = text
        self._starts = starts
        self._ends = ends
        self._identity = identity

    def to_original(self, start: int, end: int) -> tuple[int, int]:
        if self._identity:
            # Текст не менялся: координаты совпадают, отображение не строилось.
            if start >= end or not self.text:
                return 0, 0
            start = max(0, min(start, len(self.text) - 1))
            return start, max(start + 1, min(end, len(self.text)))
        if start >= end or not self._starts:
            return 0, 0
        start = max(0, min(start, len(self._starts) - 1))
        end = max(start + 1, min(end, len(self._ends)))
        return self._starts[start], self._ends[end - 1]

    def __len__(self) -> int:
        return len(self.text)


def normalize(raw: str) -> NormalizedText:
    """Fold away the differences that break naive matching but not the ones that carry meaning.

    Whitespace runs collapse to one character, keeping a newline when the run held one so
    line-anchored patterns still work. Invisible characters vanish. Mixed-script words are
    pulled onto whichever script already dominates them.
    """
    if _needs_no_normalization(raw):
        return NormalizedText(raw, None, None, identity=True)
    chars: list[str] = []
    # Machine integers, not Python objects: the map holds one entry per character and a
    # megabyte of text would otherwise allocate two million boxed ints.
    starts, ends = array("i"), array("i")
    index = 0
    length = len(raw)
    while index < length:
        ch = raw[index]
        if ch in INVISIBLE:
            index += 1
            continue
        if ch.isspace():
            run_end = index
            has_newline = False
            while run_end < length and raw[run_end].isspace():
                has_newline = has_newline or raw[run_end] in "\n\r"
                run_end += 1
            chars.append("\n" if has_newline else " ")
            starts.append(index)
            ends.append(run_end)
            index = run_end
            continue
        # Сложить «е» и надстрочный знак в «ё» можно только вместе, поэтому знак разбирается
        # не поодиночке, а со своей буквой. Иначе текст с macOS, где «ё» и «й» записаны двумя
        # символами, рассыпался на «Корол» и «в»: фамилия не опознавалась совсем, а файл
        # объявлялся чистым и уходил наружу с именем в открытом виде.
        cluster_end = index + 1
        while cluster_end < length and unicodedata.combining(raw[cluster_end]):
            cluster_end += 1
        folded = unicodedata.normalize("NFKC", raw[index:cluster_end])
        # Знак, которому слитной буквы не нашлось, — надстрочное ударение и подобный шум:
        # он только рвёт слово. Отображение смотрит на весь исходный кусок, поэтому замена
        # накроет и его, а возврат вернёт исходное написание посимвольно.
        folded = "".join(piece for piece in folded if not unicodedata.combining(piece))
        if not folded:
            index = cluster_end
            continue
        for piece in folded:
            chars.append(piece)
            starts.append(index)
            ends.append(cluster_end)
        index = cluster_end
    text = _unify_scripts(_unify_dashes("".join(chars)))
    return NormalizedText(text, starts, ends)


# Всё, что нормализация могла бы изменить: невидимые знаки, знаки-модификаторы, тире, любые пробельные символы,
# кроме одиночного пробела, и подряд идущие пробелы.
_NEEDS_WORK = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad\u180e\u034f\u0300-\u036f\u2010-\u2015\u2212\uff0d\u02d7"
                         "\u058a\u1806]|[^\\S ]|  ")
_LATIN = re.compile("[A-Za-z]")
_CYRILLIC = re.compile("[\u0400-\u04ff]")


def _needs_no_normalization(raw: str) -> bool:
    """Обычный текст без невидимых знаков, лишних пробелов и смешения алфавитов проходит без разбора по символам.

    Так обрабатывается почти каждая ячейка таблицы, а разбор по символам в Python — самая дорогая часть проверки.
    """
    if not raw or _NEEDS_WORK.search(raw) or not unicodedata.is_normalized("NFKC", raw):
        return False
    return not (_LATIN.search(raw) and _CYRILLIC.search(raw))


def _unify_dashes(text: str) -> str:
    """Свести тире внутри слова к обычному дефису; длина та же, отображение не сдвигается.

    Word сам меняет дефис на короткое тире, а неразрывный дефис ставят как раз в двойных
    фамилиях, чтобы те не рвались по строкам. Для разбора это был другой знак, слово
    распадалось надвое, и «Римский‑Корсаков» терял первую половину: в файл уходило
    «Римский[[метка]]». Половина замены хуже пропуска — выглядит обработанным.

    Тире вне слова не трогаем: «Договор — это соглашение» должно остаться тире, иначе
    склеятся соседние слова.
    """
    out = list(text)
    for i in range(1, len(out) - 1):
        if out[i] in DASHES and out[i - 1].isalpha() and out[i + 1].isalpha():
            out[i] = "-"
    return "".join(out)


def _unify_scripts(text: str) -> str:
    """Rewrite homoglyphs inside mixed-script words; same length, so the offset map holds."""
    out = list(text)
    start = None
    for i in range(len(out) + 1):
        letter = i < len(out) and (out[i].isalpha() or out[i] in "-'’")
        if letter and start is None:
            start = i
        elif not letter and start is not None:
            _unify_word(out, start, i)
            start = None
    return "".join(out)


def _unify_word(out: list[str], start: int, end: int) -> None:
    cyrillic = sum(1 for i in range(start, end) if _is_cyrillic(out[i]))
    latin = sum(1 for i in range(start, end) if _is_latin(out[i]))
    if not cyrillic or not latin:
        return
    if cyrillic >= latin:
        table, check = LATIN_TO_CYRILLIC, _is_latin
    else:
        table, check = CYRILLIC_TO_LATIN, _is_cyrillic
    # Only convert when every foreign letter has a lookalike; otherwise the word is
    # genuinely bilingual and rewriting it would corrupt real content.
    if any(check(out[i]) and out[i] not in table for i in range(start, end)):
        return
    for i in range(start, end):
        if check(out[i]):
            out[i] = table[out[i]]


def fold(value: str) -> str:
    """Case- and ё-insensitive key used for every dictionary lookup."""
    return value.casefold().replace("\u0451", "\u0435").replace("\u0301", "")  # ё -> e, drop combining acute
