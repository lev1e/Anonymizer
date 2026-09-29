from __future__ import annotations

import base64
import bisect
import datetime as dt
import difflib
import hashlib
import re
import threading
from collections import defaultdict
from dataclasses import dataclass

from . import lexicon
from . import morphology as mo
from .entities import EntityRecognizer
from .models import Decision, Finding, Person, Settings
from .normalize import fold, normalize
from .tokens import EMAIL_TOKEN_RE, TOKEN_RE, parse_match
from .util import finding_id


# ---------------------------------------------------------------------------
# Structured personal data
# ---------------------------------------------------------------------------

def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def valid_snils(value: str) -> bool:
    d = _digits(value)
    if len(d) != 11 or len(set(d[:9])) == 1:
        return False
    total = sum(int(d[i]) * (9 - i) for i in range(9))
    checksum = 0 if total in (100, 101) else total if total < 100 else (0 if total % 101 in (100, 101) else total % 101)
    return checksum == int(d[9:])


def valid_inn(value: str) -> bool:
    d = _digits(value)
    if len(d) == 10:
        weights = (2, 4, 10, 3, 5, 9, 4, 6, 8)
        return sum(int(d[i]) * weights[i] for i in range(9)) % 11 % 10 == int(d[9])
    if len(d) == 12:
        w1 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
        w2 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
        first = sum(int(d[i]) * w1[i] for i in range(10)) % 11 % 10
        second = sum(int(d[i]) * w2[i] for i in range(11)) % 11 % 10
        return first == int(d[10]) and second == int(d[11])
    return False


def valid_luhn(value: str) -> bool:
    d = _digits(value)
    if not 13 <= len(d) <= 19:
        return False
    total, alt = 0, False
    for ch in reversed(d):
        n = int(ch)
        if alt:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        alt = not alt
    return total % 10 == 0


def valid_ogrn(value: str) -> bool:
    d = _digits(value)
    if len(d) == 13:
        return int(d[:12]) % 11 % 10 == int(d[12])
    if len(d) == 15:
        return int(d[:14]) % 13 % 10 == int(d[14])
    return False


def valid_iban(value: str) -> bool:
    compact = re.sub(r"\s", "", value).upper()
    if not 15 <= len(compact) <= 34 or not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]+", compact):
        return False
    rearranged = compact[4:] + compact[:4]
    number = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(number) % 97 == 1


def valid_account(value: str) -> bool:
    """Russian bank accounts carry a check digit computed against the BIK; without the BIK
    we can only insist on a plausible balance-account prefix, which the pattern already does."""
    return len(_digits(value)) == 20


@dataclass(frozen=True, slots=True)
class Rule:
    category: str
    pattern: re.Pattern[str]
    confidence: float
    reason: str
    group: int = 0
    validator: object = None


# Чем адрес кончается: номер квартиры, дома, корпуса, строения, офиса, помещения — либо
# номер дома сразу после названия улицы («ул. Тверская, 7»). Без такого замыкания правило
# «Адрес: …» дотягивалось до конца строки и уносило с собой остаток предложения.
ADDRESS_TAIL = (r"(?:кв\.\s?\d+[а-я]?|д(?:ом)?\.?\s?\d+[а-я]?|корп(?:ус)?\.?\s?\d+[а-я]?"
                r"|стр(?:оение)?\.?\s?\d+[а-я]?|оф(?:ис)?\.?\s?\d+[а-я]?|пом\.\s?\d+[а-я]?"
                r"|(?:ул|улица|просп|проспект|пер|переулок|наб|шоссе|бул|бульвар|пл|площадь)"
                r"\.?\s[^\n;,]{2,40},\s?\d+[а-я]?)(?![\w])")

PII_RULES: tuple[Rule, ...] = (
    # Точка после адреса — конец предложения, а не часть домена: «Пишите: ivan@firma.ru.» Раньше такой адрес не находился вовсе,
    # а домен заменялся отдельно, и локальная часть оставалась в файле.
    # Опечатка перед «@» («ivan;@yandex.ru») — всё ещё адрес: без неё логин оставался открытым, а «@yandex» уходил в учётную запись.
    Rule("EMAIL", re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]+[;:,]?@[A-Z0-9.-]+\.[A-Z]{2,24}(?![\w-])(?!\.[\w-])", re.I), .995, "Адрес электронной почты"),
    Rule("PHONE", re.compile(r"(?<![\d\w\-])(?:\+\d{1,3}|8)[\s()\-]*\d{3,4}[\s()\-]*\d{2,3}[\s\-]*\d{2}[\s\-]*\d{2}(?![\d\w\-])"), .97, "Номер телефона"),
    # Российский номер без кода страны: «(495) 123-45-72», «495 123-45-73», «916-123-45-81», «7(495)1234571», «79161234567».
    Rule("PHONE", re.compile(r"(?<![\d\w\-])7\s?\(\d{3}\)\s?\d{7}(?![\d\w])"), .93, "Номер телефона"),
    Rule("PHONE", re.compile(r"(?<![\d\w\-])\(\d{3,5}\)\s?\d{1,3}[\s\-]?\d{2}[\s\-]?\d{2}(?![\d\w\-])"), .92, "Номер телефона"),
    Rule("PHONE", re.compile(r"(?<![\d\w\-])[3489]\d{2}[\s\-]\d{3}\-\d{2}\-\d{2}(?![\d\w\-])"), .90, "Номер телефона"),
    Rule("PHONE", re.compile(r"(?<![\d\w\-])[78]9\d{9}(?![\d\w\-])"), .88, "Номер мобильного телефона"),
    # Не российский номер: страна и 7–12 цифр в любых группах. Число с разрядами («+12 345 678») и сумма («+1 250 000 000 ₽») — не телефон.
    Rule("PHONE", re.compile(r"(?<![\d\w])\+\d{1,3}(?:[\s().\-]*\d){7,12}(?![\d\w])(?!\s?(?:руб|₽|%|тыс|млн|млрд|usd|eur|\$|€))", re.I),
         .93, "Номер телефона", validator=lambda v: not re.fullmatch(r"\+\d{1,3}(?: \d{3}){2,}", v.strip())),
    Rule("PHONE", re.compile(r"(?i)(?:тел(?:ефон)?|моб|факс|whatsapp|telegram)\.?\s*(?:[:№]|No\.?)?\s*((?:\+?\d[\s()\-]*){7,15})"), .95, "Номер телефона", group=1),
    Rule("CARD", re.compile(r"(?<!\d)(?:\d{4}[ -]?){3}\d{4}(?:[ -]?\d{3})?(?!\d)"), .97, "Номер банковской карты", validator=valid_luhn),
    Rule("CARD", re.compile(r"(?i)(?:карт\w*|card|pan)\b[^\n]{0,20}?((?<!\d)(?:\d{4}[ -]){3}\d{4}(?!\d))"), .92, "Номер банковской карты", group=1),
    Rule("SNILS", re.compile(r"(?<!\d)\d{3}[- ]?\d{3}[- ]?\d{3}[ -]?\d{2}(?!\d)"), .99, "СНИЛС", validator=valid_snils),
    Rule("SNILS", re.compile(r"(?<!\d)\d{3}-\d{3}-\d{3}[ -]\d{2}(?!\d)"), .90, "СНИЛС (по формату записи)"),
    Rule("SNILS", re.compile(r"(?i)\bСНИЛС\b[^\n]{0,20}?((?<!\d)\d{3}[- ]?\d{3}[- ]?\d{3}[ -]?\d{2}(?!\d))"), .93, "СНИЛС", group=1),
    # Контрольную сумму ИНН проходит примерно каждое сотое двенадцатизначное число, поэтому
    # без слова «ИНН» рядом правило выкашивало обычные номера заказов и накладных. Все прочие
    # правила со слабым признаком (ПАСПОРТ, ОМС, В/У) устроены так же: сначала ключевое слово.
    # Слово «ИНН» рядом с числом — достаточный довод: неверно записанный номер всё равно остаётся номером.
    Rule("INN_PERSON", re.compile(r"(?i)\bИНН\b[^\n]{0,20}?((?<!\d)\d{12}(?!\d))"), .97, "ИНН физического лица", group=1),
    # Слово «ИНН» и число могут разделять слова («ИНН организации 7707123458») и косая черта («ИНН/КПП 7707123458/770701001»).
    Rule("INN_ORG", re.compile(r"(?i)\bИНН\b[^\n\d]{0,30}?(\d{10}|\d{12})(?!\d)"), .96, "ИНН", group=1),
    Rule("KPP", re.compile(r"(?i)\bИНН\s*/\s*КПП\b[^\n\d]{0,20}\d{10,12}\s*/\s*(\d{9})(?!\d)"), .95, "КПП", group=1),
    Rule("OGRN", re.compile(r"(?i)\bОГРНИ?П?\s*(?:[:№]|No\.?)?\s*(\d{13}|\d{15})\b"), .96, "ОГРН", group=1),
    Rule("KPP", re.compile(r"(?i)\bКПП\b[^\n\d]{0,30}?(\d{9})(?!\d)"), .95, "КПП", group=1),
    Rule("BANK_ACCOUNT", re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?(?![A-Za-z0-9])"), .93,
         "Международный номер счёта (IBAN)", validator=lambda v: valid_iban(v)),
    Rule("BIK", re.compile(r"(?i)\bБИК\s*(?:[:№]|No\.?)?\s*(0\d{8})\b"), .95, "БИК", group=1),
    Rule("BANK_ACCOUNT", re.compile(r"(?i)(?:р/с|р\.\s?с\.?|расч[её]тн\w*\s+сч[её]т\w*|к/с|корр\w*\.?\s+сч[её]т\w*|сч[её]т)\s*(?:[:№]|No\.?)?\s*(\d{20})(?!\d)"),
         .96, "Банковский счёт", group=1),
    Rule("BANK_ACCOUNT", re.compile(r"(?<!\d)(?:40702|40802|40817|40820|30101|30102)\d{15}(?!\d)"), .95, "Банковский счёт"),
    Rule("CONTRACT", re.compile(r"(?i)\b(?:договор|контракт|соглашени|доверенност|счёт-фактур|счет-фактур)\w*(?: [\wА-Яа-яЁё«»\"-]+){0,3}? ?(?:№|No|N|#) ?([A-Za-zА-ЯЁа-яё0-9_./-]{2,})"),
         .94, "Номер договора или документа", group=1),
    Rule("PASSPORT", re.compile(r"(?i)(?:паспорт|серия\s+и\s+номер|документ,?\s+удостоверяющий)[^\n]{0,40}?((?:\d{2}\s?\d{2}|\d{4})\s?(?:[-№ ]|No)?\s?\d{6})(?!\d)"), .97, "Паспортные данные", group=1),
    Rule("PASSPORT", re.compile(r"(?<!\d)\d{2}\s\d{2}\s\d{6}(?!\d)"), .93, "Паспортные данные"),
    Rule("OMS", re.compile(r"(?i)(?:полис|ОМС|ДМС)[^\n]{0,30}?((?<!\d)\d{16}(?!\d))"), .95, "Полис медицинского страхования", group=1),
    Rule("DRIVER_LICENSE", re.compile(r"(?i)(?:водительск\w+\s+удостоверени\w+|в/у)[^\n]{0,20}?((?<!\d)\d{2}\s?\d{2}\s?\d{6}(?!\d))"), .95, "Водительское удостоверение", group=1),
    Rule("VEHICLE_PLATE", re.compile(r"(?<![\w])[АВЕКМНОРСТУХ]\s?\d{3}\s?[АВЕКМНОРСТУХ]{2}\s?\d{2,3}(?![\w])"), .93, "Государственный регистрационный знак"),
    Rule("BIRTH_DATE", re.compile(r"(?i)(?:дат\w+\s+рождени\w+|год\s+рождения|родил(?:ся|ась)|д\.\s*р\.|date\s+of\s+birth|\bdob\b)\s*[:—–-]?\s*(\d{1,2}[./-]\d{1,2}[./-](?:19|20)\d{2}|\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+(?:19|20)\d{2}(?:\s*г(?:ода|\.)?)?|(?:19|20)\d{2})"), .96, "Дата рождения", group=1),
    Rule("ADDRESS", re.compile(r"(?i)(?:адрес\w*|мест\w+\s+жительства|проживает|зарегистрирован(?:а|ы)?)\s*[:—–-]\s*([^\n;]{0,180}" + ADDRESS_TAIL + ")"),
         .90, "Почтовый адрес", group=1),
    Rule("ADDRESS", re.compile(r"(?i)(?<![\w])(?:г\.|город|ул\.|улица|просп\.|проспект|пер\.|переулок|наб\.|шоссе|д\.\s?\d)[^\n;]{10,150}?(?:кв\.\s?\d+|д\.\s?\d+[а-я]?|дом\s?\d+)(?![\w])"), .88, "Почтовый адрес"),
    # Улица с названием даже без «д.» и без города рядом: «на ул. Мира, 7», «улица Большая Садовая». Название — слова с заглавной буквы.
    Rule("ADDRESS", re.compile(
        r"(?<![\w])(?i:ул|улица|просп|проспект|пер|переулок|наб|набережная|бул|бульвар|пл|площадь|проезд)(?:\.|(?<=[а-яa-z]{4}))\s+"
        r"(?:\d{1,2}\s+)?[A-ZА-ЯЁ][\w-]*(?:\s+[A-ZА-ЯЁ][\w-]*){0,2}"
        r"(?:,\s*(?:д\.?\s*)?\d+[а-яa-z]?(?:/\d+)?)?"), .86, "Улица"),
    # «Ленинский пр-т, 32а», «Большая Садовая улица, 5»: определение стоит перед названием вида улицы.
    Rule("ADDRESS", re.compile(
        r"(?<![\w])[А-ЯЁ][а-яё]+(?:ский|ской|ая|ое|ий|ый|ой|ная|ное)\s+(?i:пр-т|пр-кт|просп\.|проспект|ул\.|улица|пер\.|переулок|наб\.|набережная|бул\.|бульвар|шоссе|пл\.|площадь|проезд)"
        r"(?:,?\s*(?:д\.?\s*)?\d+[а-яa-z]?(?:/\d+)?)?"), .86, "Улица"),
    # Почтовый индекс перед городом: «119991, Москва».
    Rule("ADDRESS", re.compile(r"(?<![\d\w])\d{6}(?=,\s*[А-ЯЁ][а-яё]+)"), .80, "Почтовый индекс"),
    # Профили: путь после домена называет человека («t.me/vector_co», «linkedin.com/in/ivan-ivanov»).
    Rule("USERNAME", re.compile(r"(?i)(?<![\w@/.])(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|vk\.com|ok\.ru|facebook\.com|instagram\.com|linkedin\.com/in|github\.com|twitter\.com|x\.com|wa\.me)/[\w.\-]{2,64}"), .90, "Ссылка на профиль"),
    # «@yandex.ru» — хвост адреса почты, а не учётная запись.
    Rule("USERNAME", re.compile(r"(?<![\w@/])@[A-Za-z][A-Za-z0-9_]{4,31}(?!\w)(?!\.[A-Za-z]{2,})"), .90, "Учётная запись мессенджера"),
    Rule("IP_ADDRESS", re.compile(r"(?<![\w.])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?!\w)(?!\.\d)"), .88, "IP-адрес"),
)

SECRET_RULES: tuple[Rule, ...] = (
    Rule("SECRET", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY(?: BLOCK)?-----[\s\S]*?-----END (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY(?: BLOCK)?-----"), 1.0, "PRIVATE_KEY"),
    Rule("SECRET", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"), 1.0, "BEARER"),
    Rule("SECRET", re.compile(r"(?<![\w-])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}(?![\w-])"), 1.0, "JWT"),
    Rule("SECRET", re.compile(r"(?i)\b(?:Server|Host|Data Source)\s*=.+?(?:Password|Pwd)\s*=\s*[^;\s]+(?:;[^\n]*)?"), 1.0, "CONNECTION_STRING"),
    Rule("SECRET", re.compile(r"(?i)\b(?:password|passwd|pwd|пароль|api[_ -]?key|secret[_ -]?key|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|private[_ -]?token|session[_ -]?id)\s*[:=]\s*[\"']?[^\s,;\"']{8,}"), 1.0, "CREDENTIAL"),
    Rule("SECRET", re.compile(r"https?://[^\s<>\"']+[?&](?:token|key|signature|sig|auth|access_token|api_key)=[^&\s<>\"']+", re.I), 1.0, "TOKENIZED_URL"),
    Rule("SECRET", re.compile(r"(?<![\w-])(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9]{20,})(?![\w-])"), 1.0, "PROVIDER_TOKEN"),
)

BUSINESS_RULES: tuple[Rule, ...] = (
    Rule("BUSINESS", re.compile(r"(?i)\b(?:зарплат\w*|оклад\w*|преми\w+|маржа|маржинальн\w+|kpi|бонус\w*|выручка|себестоимость)\b[^\n;]{0,80}"), .92, "COMPENSATION"),
)

LONE_DATE = re.compile(r"^\s*(\d{1,2})[./-](\d{1,2})[./-]((?:19|20)\d{2})\s*$")
BIRTH_CONTEXT = re.compile(r"(?i)дат\w*\s*рождени|год\s+рождения|\bд\.\s*р\.|date\s+of\s+birth|\bdob\b|родил(?:ся|ась)")

# Categories whose match is a container: when something more specific sits inside them the
# remaining fragments are still sensitive, so they get split rather than dropped.
SPLITTABLE = frozenset({"ADDRESS", "BUSINESS"})

# Полное ФИО — максимум три слова, поэтому окна сопоставления и обучения смотрят на три токена.
NAME_WINDOW = 3
# Сколько символов вокруг находки показать человеку при ручной проверке.
CONTEXT_CHARS = 90
# Насколько похожей должна быть фамилия, чтобы считаться опечаткой, и потолок уверенности
# для такой находки: она всегда идёт на ручную проверку и не должна выглядеть надёжнее точной.
FUZZY_THRESHOLD = .85
FUZZY_MAX_CONFIDENCE = .89
# Потолок на случай справочника, где сотни фамилий совпадают и по префиксу, и по длине:
# поиск опечатки не должен становиться главной статьёй расхода времени.
MAX_FUZZY_COMPARISONS = 120

NOMINATIVE_MASCULINE_ENDINGS = ("ов", "ев", "ёв", "ин", "ын", "ский", "цкий")
NOMINATIVE_FEMININE_ENDINGS = ("ова", "ева", "ёва", "ина", "ына", "ская", "цкая")

PRIORITY = {"SECRET": 1000, "EMAIL": 960, "PERSON": 900, "POSSIBLE_PERSON": 500}
DEFAULT_PRIORITY = 800
CONTAINER_PRIORITY = 700


class Spans:
    """Sorted, merged interval set. Overlap tests and gap queries stay logarithmic, which
    matters on documents that produce thousands of findings."""

    __slots__ = ("_starts", "_ends")

    def __init__(self) -> None:
        self._starts: list[int] = []
        self._ends: list[int] = []

    def overlaps(self, start: int, end: int) -> bool:
        i = bisect.bisect_right(self._ends, start)
        return i < len(self._starts) and self._starts[i] < end

    def free_ranges(self, start: int, end: int) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        cursor = start
        i = bisect.bisect_right(self._ends, start)
        while i < len(self._starts) and self._starts[i] < end:
            if self._starts[i] > cursor:
                out.append((cursor, self._starts[i]))
            cursor = max(cursor, self._ends[i])
            i += 1
        if cursor < end:
            out.append((cursor, end))
        return out

    def add(self, start: int, end: int) -> None:
        i = bisect.bisect_left(self._starts, start)
        self._starts.insert(i, start)
        self._ends.insert(i, end)
        # Merge backwards then forwards so the list stays non-overlapping and sorted by both keys.
        while i > 0 and self._starts[i] <= self._ends[i - 1]:
            self._starts[i - 1] = min(self._starts[i - 1], self._starts[i])
            self._ends[i - 1] = max(self._ends[i - 1], self._ends[i])
            del self._starts[i], self._ends[i]
            i -= 1
        while i + 1 < len(self._starts) and self._starts[i + 1] <= self._ends[i]:
            self._ends[i] = max(self._ends[i], self._ends[i + 1])
            del self._starts[i + 1], self._ends[i + 1]


# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------

WORD_RE = re.compile(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*", re.UNICODE)


@dataclass(slots=True)
class Token:
    text: str
    start: int
    end: int
    initial: bool
    key: str
    letters: tuple[str, ...] = ()
    labelled: bool = False
    glued: bool = False
    weak: bool = False


# Слитная пара заглавных — это инициалы («Богдан НВ»). Три буквы уже аббревиатура
# подразделения (ДВФ, ГСМ, ЕНС), и трогать их нельзя.
GLUED_INITIALS = 2
# Двухбуквенные сокращения, которые не бывают инициалами: «ИП Абдуллаев Рустам Камилович» — предприниматель, а не «И.П.».
NOT_INITIALS = frozenset({"ИП", "ЧП", "АО", "ОП", "ГК", "РФ", "ТД", "ПК", "ТК", "НП", "УК", "СП", "ЖК", "БЦ", "ТЦ", "СК", "ЦБ", "ФЛ", "ЮЛ"})


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    for m in WORD_RE.finditer(text):
        word, start, end = m.group(0), m.start(), m.end()
        dotted = end < len(text) and text[end] == "."
        letters: tuple[str, ...] = ()
        weak = False
        if len(word) == 1 and word.isalpha():
            # Строчная буква с точкой тоже бывает инициалом («денисов д.д.»), но так же
            # выглядят «и т.д.» и «в т.ч.», поэтому такой инициал считается слабым и
            # принимается только рядом с уже известной фамилией.
            weak = not word.isupper()
            letters = (word.upper(),)
            if dotted:
                end += 1
            elif weak:
                letters = ()
                weak = False
        glued = False
        if not letters and len(word) == GLUED_INITIALS and word.isalpha() and word.isupper():
            letters = tuple(word)
            glued = not dotted
            if dotted:
                end += 1
        # The folded form is looked up against four tables per token; fold once here.
        labelled = end < len(text) and text[end] == ":"
        tokens.append(Token(word, start, end, bool(letters), fold(word), letters, labelled, glued, weak))
    _settle_dotless_initials(tokens, text)
    _release_abbreviations(tokens, text)
    return tokens


def _release_abbreviations(tokens: list[Token], text: str) -> None:
    """«ИП Абдуллаев Рустам Камилович»: ИП перед полным ФИО — форма собственности, а не инициалы «И.П.»."""
    for index, token in enumerate(tokens[:-2]):
        if token.text not in NOT_INITIALS or not token.initial:
            continue
        rest = tokens[index + 1:index + 3]
        if all(not t.initial and t.text[:1].isupper() and len(t.text) > 2 for t in rest) \
                and not text[token.end:rest[0].start].strip(" ") and not text[rest[0].end:rest[1].start].strip(" "):
            token.initial, token.letters, token.glued = False, (), False


def _settle_dotless_initials(tokens: list[Token], text: str) -> None:
    """A lone capital is only an initial in the company of another one.

    "Богдан Н.В" drops the final dot and "Пекшев А.С" never had it; both are still initials.
    A stray capital in ordinary prose is not, so it needs a neighbour to qualify.
    """
    for index, token in enumerate(tokens):
        if not token.initial or len(token.text) != 1:
            continue
        if text[token.end - 1] == ".":
            continue
        previous = tokens[index - 1] if index else None
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        supported = (previous is not None and previous.initial) or (following is not None and following.initial)
        if not supported:
            token.initial = False
            token.letters = ()


# Фамилия с приставкой-частицей: «д'Артаньян», «о'Нил», «л'Эстрандж». Частица пишется со
# строчной буквы, и проверка «первое слово начинается с заглавной» отбрасывала такое слово
# целиком: имя и отчество рядом находились как ни в чём не бывало, а фамилия уходила в
# выгрузку открытым текстом. «О'Коннор» работал и раньше — там заглавная стоит первой.
PARTICLE_PREFIX = re.compile(r"^\w{1,2}['\u2019]\w", re.UNICODE)


def name_capitalised(word: str) -> bool:
    """Слово начинается как имя собственное: с заглавной буквы или с частицы перед апострофом.

    Шире обычного `word[0].isupper()` ровно на один вид слов — строчная частица, апостроф,
    заглавная буква. В деловом тексте такого сочетания не бывает ни у одного обычного слова,
    поэтому на ложные срабатывания правило не влияет.
    """
    if not word:
        return False
    if word[0].isupper():
        return True
    match = PARTICLE_PREFIX.match(word)
    return bool(match) and word[match.end() - 1].isupper()


# ---------------------------------------------------------------------------
# Name index
# ---------------------------------------------------------------------------

class NameIndex:
    """Maps every inflected, transliterated and abbreviated spelling of a name to its people.

    Forms are generated once per person and looked up by dictionary hit, so scan time depends
    on the length of the document and not on the size of the directory.
    """

    def __init__(self) -> None:
        self.surname: dict[str, set[str]] = defaultdict(set)
        self.given: dict[str, set[str]] = defaultdict(set)
        self.patronymic: dict[str, set[str]] = defaultdict(set)
        self.people: dict[str, Person] = {}
        self.phrases: dict[str, str] = {}
        self.initials: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
        # Формы фамилий разложены по (первые три буквы, длина). Опечатка почти никогда не
        # трогает все три первые буквы и меняет длину не больше чем на единицу, поэтому
        # сравнивать приходится с горсткой форм, а не со всем справочником: в каталоге, где
        # тысяча фамилий начинается на «ива», один префикс давал тысячу сравнений на токен.
        self.surname_buckets: dict[tuple[str, int], set[str]] = defaultdict(set)
        # Обратный индекс: под какими ключами человек лежит в таблице фамилий. Нужен, чтобы
        # слить две записи в одну, не перебирая весь словарь.
        self.surname_keys: dict[str, set[str]] = defaultdict(set)
        # Одно и то же слово ищется в документе сотни раз; результат подбора не меняется,
        # пока индекс не пополнился.
        self.fuzzy_cache: dict[str, str] = {}
        self._lock = threading.Lock()

    def absorb(self, candidates: set[str], surname: str, given: str, patronymic: str) -> bool:
        """Сливает все записи-по-инициалам, относящиеся к этому человеку, в одну полноценную.

        «Иванову И.И.» и «Ивановым И.И.» из разных файлов заводят две записи — падежи фамилии
        разные, а человек один. Полное ФИО достраивает первую и переводит остальные на неё,
        иначе каждое упоминание навсегда осталось бы неоднозначным.
        """
        with self._lock:
            targets = [pid for pid in candidates
                       if pid in self.people and not self.people[pid].given_name]
        if not targets:
            return False
        keeper = targets[0]
        self.complete(keeper, surname, given, patronymic)
        with self._lock:
            for extra in targets[1:]:
                for key in self.surname_keys.pop(extra, set()):
                    holders = self.surname.get(key)
                    if holders and extra in holders:
                        holders.discard(extra)
                        holders.add(keeper)
                        self.surname_keys[keeper].add(key)
                self.people.pop(extra, None)
                self.initials.pop(extra, None)
        return True

    def complete(self, person_id: str, surname: str, given: str, patronymic: str) -> bool:
        """Turn an initials-only record into a full one; returns False if there is none.

        The abbreviated record may have been created from an oblique form ("Иванову И.И."),
        so the nominative surname replaces it and its own forms are indexed onto the same id.
        """
        with self._lock:
            person = self.people.get(person_id)
            if person is None or person.given_name:
                return False
            person.surname, person.given_name, person.patronymic = surname, given, patronymic
            person.full_name = " ".join(x for x in (surname, given, patronymic) if x)
            self.fuzzy_cache.clear()
            gender = mo.gender_of(surname, given, patronymic)
            for form in mo.decline_surname(surname, gender) | mo.transliterate(surname):
                key = fold(form)
                self.surname[key].add(person_id)
                self.surname_keys[person_id].add(key)
                if len(key) >= 4:
                    self.surname_buckets[(key[:3], len(key))].add(key)
            for form in mo.decline_given(given, gender) | mo.transliterate(given):
                self.given[fold(form)].add(person_id)
            for form in mo.decline_patronymic(patronymic, gender):
                self.patronymic[fold(form)].add(person_id)
            self.initials[person_id] = (_initial_letters(given), _initial_letters(patronymic))
            return True

    def add(self, person: Person, initials: tuple[frozenset[str], ...] | None = None) -> None:
        with self._lock:
            if person.person_id in self.people:
                return
            self.people[person.person_id] = person
            self.fuzzy_cache.clear()
            gender = mo.gender_of(person.surname, person.given_name, person.patronymic)
            for form in mo.decline_surname(person.surname, gender):
                key = fold(form)
                self.surname[key].add(person.person_id)
                self.surname_keys[person.person_id].add(key)
                if len(key) >= 4:
                    self.surname_buckets[(key[:3], len(key))].add(key)
            for form in mo.transliterate(person.surname):
                self.surname[fold(form)].add(person.person_id)
            for form in mo.decline_given(person.given_name, gender):
                self.given[fold(form)].add(person.person_id)
            for form in mo.transliterate(person.given_name):
                self.given[fold(form)].add(person.person_id)
            for form in mo.decline_patronymic(person.patronymic, gender):
                self.patronymic[fold(form)].add(person.person_id)
            if initials is not None:
                given_letters = initials[0] if initials else frozenset()
                patronymic_letters = initials[1] if len(initials) > 1 else frozenset()
                self.initials[person.person_id] = (given_letters, patronymic_letters)
            else:
                self.initials[person.person_id] = (_initial_letters(person.given_name),
                                                   _initial_letters(person.patronymic))
            for alias in person.aliases:
                if len(alias) >= 3:
                    self.phrases[fold(alias)] = person.person_id
            if person.employee_number and len(person.employee_number) >= 3:
                self.phrases[fold(person.employee_number)] = person.person_id
            if person.email:
                self.phrases[fold(person.email)] = person.person_id

    def knows(self, person_id: str) -> bool:
        return person_id in self.people

    def add_surfaces(self, person_ids: set[str], surname: str, given: str, patronymic: str) -> None:
        """Написания в том виде, как они стоят в документе.

        Приведение к именительному падежу держится на словаре и иногда ошибается: «Шин» становится «Шина»,
        «Куповой» — «Купов». Форма, которую программа видела своими глазами, ищется всегда, даже если начальная
        форма выведена неверно.
        """
        with self._lock:
            for pid in person_ids:
                if pid not in self.people:
                    continue
                for value, table in ((surname, self.surname), (given, self.given), (patronymic, self.patronymic)):
                    if value and len(value) >= 2:
                        # Латинская запись того же человека во втором столбце строится от увиденной формы, а не только
                        # от выведенной начальной: «Орёл» не должен зависеть от того, во что его превратил словарь.
                        forms = {value} | (mo.transliterate(value) if table is not self.patronymic
                                           and "Ѐ" <= value[0] <= "ӿ" else set())
                        for form in forms:
                            key = fold(form)
                            table[key].add(pid)
                            if table is self.surname:
                                self.surname_keys[pid].add(key)

    def learn_patronymic(self, person_ids: set[str], patronymic: str) -> None:
        """A directory holding only "Мария Иванова" still gets to match "Иванова М.С." once the
        document itself supplies the patronymic."""
        with self._lock:
            for pid in person_ids:
                person = self.people.get(pid)
                if not person or person.patronymic:
                    continue
                person.patronymic = patronymic
                gender = mo.gender_of(person.surname, person.given_name, patronymic)
                for form in mo.decline_patronymic(patronymic, gender):
                    self.patronymic[fold(form)].add(pid)
                given_letters, _ = self.initials.get(pid, (frozenset(), frozenset()))
                self.initials[pid] = (given_letters, _initial_letters(patronymic))


def _initial_letters(name: str) -> frozenset[str]:
    """First letters a person's initial can legitimately take, Cyrillic and transliterated."""
    if not name:
        return frozenset()
    letters = {fold(name[0])}
    letters.update(fold(form[0]) for form in mo.transliterate(name) if form)
    return frozenset(letters)


def identity_key(category: str, value: str) -> str:
    """Одно и то же значение в разной записи — одна сущность: телефон без пробелов и со скобками совпадает."""
    if category in {"PHONE", "CARD", "SNILS", "INN_PERSON", "INN_ORG", "OGRN", "PASSPORT", "OMS", "DRIVER_LICENSE",
                    "BANK_ACCOUNT"}:
        digits = _digits(value)
        return digits[-10:] if category == "PHONE" and len(digits) > 10 else digits
    if category == "EMAIL":
        return value.strip().lower()
    if category == "SECRET":
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
    return re.sub(r"\s+", " ", fold(value)).strip()


def _edit_distance(left: str, right: str, limit: int) -> int:
    """Расстояние Левенштейна с ранним выходом: всё, что больше `limit`, возвращается как `limit + 1`."""
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous = list(range(len(right) + 1))
    for row, a in enumerate(left, 1):
        current = [row]
        for col, b in enumerate(right, 1):
            current.append(min(previous[col] + 1, current[col - 1] + 1, previous[col - 1] + (a != b)))
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def local_person_id(surname: str, given: str, patronymic: str) -> str:
    key = fold(f"{surname}|{given}|{patronymic}")
    return "LOCAL" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:19].upper()


def initials_person_id(surname: str, letters: tuple[str, ...]) -> str:
    """Identity for somebody the documents only ever abbreviate.

    Deterministic in the surname and the initials, so every "Волкова Е.В." in the project
    collapses onto one person instead of a fresh anonymous token per occurrence.
    """
    key = fold(surname) + "|" + "".join(fold(letter) for letter in letters)
    return "INIT" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20].upper()


def local_person_token(person_id: str) -> str:
    """Same alphabet and length as a directory token, otherwise the placeholder would not
    match TOKEN_RE and everything learned from the documents would be unrestorable."""
    digest = hashlib.sha256(person_id.encode("ascii")).digest()
    return base64.b32encode(digest).decode("ascii")[:12]


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

class Detector:
    def __init__(self, people: list[Person], settings: Settings):
        self.settings = settings
        self.index = NameIndex()
        self.by_id: dict[str, Person] = {}
        for person in people:
            self.index.add(person)
            self.by_id[person.person_id] = person
        self.directory_ids = set(self.by_id)
        # Documents that mention a birth date somewhere. A bare old date in a cell only counts
        # as a possible birth date there — otherwise a register of 2008 contracts would bury
        # the whole file in review items.
        self._birth_context: set[str] = set()
        # Решения человека о том, кто есть кто. Разбор их не переопределяет.
        self.merged: dict[str, str] = {}
        self.spelling_owner: dict[str, str] = {}
        self.entities = EntityRecognizer(settings, getattr(settings, "hide_terms", None), getattr(settings, "keep_terms", None))
        # Знает ли хранилище токен: токен, которого там нет, — обычный текст документа и сам подлежит замене.
        self.token_known = None
        # Тексты, которые стоят в столбцах с ФИО: заполняется по заголовкам таблиц (см. formats._person_column_values).
        self.person_values: set[str] = set()
        # Места, найденные разбором названий в тексте, который сейчас разбирается (координаты нормализованного текста).
        self._geo_spans: list[tuple[int, int]] = []
        # Слитные пары заглавных («ОП», «СЦ»), стоящие перед разными словами с заглавной: это сокращения, а не инициалы.
        self._caps_followers: dict[str, set[str]] = defaultdict(set)
        self.abbreviations: set[str] = set()
        # Латинские ФИО, отложенные до конца прохода обучения (см. `_register`).
        self._deferred: list[tuple[str, str, str]] | None = None

    # -- people discovered inside the documents themselves --------------------

    @property
    def people(self) -> list[Person]:
        return list(self.index.people.values())

    @property
    def discovered(self) -> dict[str, Person]:
        return {pid: p for pid, p in self.index.people.items()
                if pid not in self.directory_ids and pid not in self.merged}

    @property
    def visible_people(self) -> list[Person]:
        """Люди, которых видит человек: слитые записи не показываются отдельно."""
        return sorted((p for pid, p in self.index.people.items() if pid not in self.merged),
                      key=lambda p: p.full_name)

    def resolve_person(self, person_id: str | None) -> str | None:
        """Куда в итоге указывает запись после ручных объединений."""
        seen = set()
        while person_id in self.merged and person_id not in seen:
            seen.add(person_id)
            person_id = self.merged[person_id]
        return person_id

    def merge_people(self, person_ids: list[str]) -> str | None:
        """Свести несколько записей в одну: это один и тот же человек."""
        targets = [self.resolve_person(pid) for pid in person_ids]
        targets = [pid for pid in dict.fromkeys(targets) if pid and pid in self.index.people]
        if len(targets) < 2:
            return targets[0] if targets else None
        keeper = targets[0]
        for extra in targets[1:]:
            self.merged[extra] = keeper
            for spelling, owner in list(self.spelling_owner.items()):
                if owner == extra:
                    self.spelling_owner[spelling] = keeper
        return keeper

    def detach_spellings(self, spellings: list[str], label: str = "") -> str:
        """Отделить написания в отдельного человека: это был не он."""
        key = "|".join(sorted(fold(s) for s in spellings))
        person_id = "SPLIT" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:19].upper()
        if person_id not in self.index.people:
            shown = label or (spellings[0] if spellings else "Отдельный человек")
            self.index.add(Person(person_id, local_person_token(person_id), shown, shown.split()[0]))
        for spelling in spellings:
            self.spelling_owner[fold(spelling)] = person_id
        return person_id

    def undo_person_edits(self) -> None:
        self.merged.clear()
        self.spelling_owner.clear()

    def harvest(self, text: str, file: str = "") -> int:
        """Learn the full names written in the document so its own abbreviations resolve.

        This is what links "Иванов И.И." to "Иванов Иван Иванович" without any directory:
        the full form anchors the person, the initials then match against it.
        """
        norm = normalize(text)
        masked = norm.text
        self._geo_spans = []
        if self.settings.organizations or self.settings.geo:
            self.entities.learn(norm.text)
            masked = self._mask_entities(norm.text)
        if not self.settings.personal_data:
            return 0
        if BIRTH_CONTEXT.search(text):
            self._birth_context.add(file)
        if not self.settings.learn_names:
            return 0
        tokens = tokenize(masked)
        self._learn_abbreviations(tokens, masked)
        self._drop_abbreviations(tokens)
        added = 0
        i = 0
        self._deferred = []
        while i < len(tokens):
            consumed = self._harvest_at(tokens, i, masked)
            if consumed:
                added += 1
                i += consumed
            else:
                i += 1
        deferred, self._deferred = self._deferred, None
        for names in deferred:
            self._register(*names)
        self._harvest_honorifics(tokens, masked)
        # Second sweep, after every full name in the text is known: whoever is left with only
        # initials gets an identity of their own so their mentions stay linked to each other.
        i = 0
        while i < len(tokens):
            shape = self._initials_shape(tokens, i, masked)
            if not shape:
                i += 1
                continue
            length, _ = shape
            head = tokens[i]
            if not head.initial:
                _, letters = self._collect_initials(tokens, i + 1)
                self._register_initials(head.text, letters)
            else:
                consumed, letters = self._collect_initials(tokens, i)
                self._register_initials(tokens[i + consumed].text, letters)
            i += length
        return added

    # Сколько разных слов с заглавной должно стоять после слитной пары, чтобы считать её сокращением.
    ABBREVIATION_EVIDENCE = 3

    def _learn_abbreviations(self, tokens: list[Token], text: str) -> None:
        """«ОП Тальжино», «ОП Кутаново», «ОП Берёзовка»: пара заглавных перед тремя разными словами — сокращение.

        Инициалы одного человека стоят перед одной и той же фамилией; перед разными — это вид подразделения,
        и считать его инициалами значит завести по «человеку» на каждое название.
        """
        for token, following in zip(tokens, tokens[1:]):
            if not token.glued or following.initial or not following.text[:1].isupper():
                continue
            if text[token.end:following.start].strip(" \t "):
                continue
            seen = self._caps_followers[token.text]
            seen.add(following.key)
            if len(seen) >= self.ABBREVIATION_EVIDENCE:
                self.abbreviations.add(token.text)

    def _drop_abbreviations(self, tokens: list[Token]) -> None:
        if not self.abbreviations:
            return
        for token in tokens:
            if token.glued and token.text in self.abbreviations:
                token.initial, token.letters, token.glued = False, (), False

    HONORIFICS = frozenset({
        "mr", "mrs", "ms", "miss", "mx", "dr", "prof", "sir", "madam", "г-н", "г-на", "г-ну", "г-ном", "г-не", "г-жа", "г-жи",
        "г-же", "г-жу", "г-жой", "господин", "господина", "господину", "господином", "господине", "госпожа", "госпожи",
        "госпоже", "госпожу", "госпожой"})

    def _harvest_honorifics(self, tokens: list[Token], text: str) -> None:
        """«Mr. Johnson», «г-ну Ковалёву»: слово после обращения — фамилия, даже если такой человек нигде не назван полностью.

        Обращение — достаточное свидетельство, а найденная фамилия затем узнаётся и без обращения («Johnson позвонил»).
        """
        for i, token in enumerate(tokens[:-1]):
            if token.key not in self.HONORIFICS:
                continue
            following = tokens[i + 1]
            gap = text[token.end:following.start]
            if "\n" in gap or gap.strip(" .\t\u00a0"):
                continue
            word = following.text
            if following.initial or len(word) < 3 or not name_capitalised(word) or "-" in word and word.split("-")[0].isupper():
                continue
            if mo.is_stop_word(word) or lexicon.is_common_word(word) or mo.is_given_name(word):
                continue
            self._register_surname_only(word)

    def _register_surname_only(self, surname: str) -> None:
        nominative = self._to_nominative(surname, "", "")[0] if self._cyrillic(surname) else surname
        if len(nominative) < 3:
            return
        known = self.index.surname.get(fold(nominative), set())
        if known:
            self.index.add_surfaces(known, surname, "", "")
            return
        person_id = initials_person_id(nominative, ())
        if not self.index.knows(person_id):
            self.index.add(Person(person_id, local_person_token(person_id), nominative, nominative), initials=())
        self.index.add_surfaces({person_id}, surname, "", "")

    def _mask_entities(self, text: str) -> str:
        """Названия организаций и мест не должны попадать в разбор ФИО: «Северный Ветер» — не человек."""
        # Справочные города («Иванова» — родительный от Иваново) не маскируются: это может быть фамилия.
        hits = self.entities.find(text)
        self._geo_spans = [(h.start, h.end) for h in hits if h.category in {"CITY", "REGION"}]
        spans = [(h.start, h.end) for h in hits
                 if h.category in {"ORG", "PROJECT", "CITY", "REGION", "TERM"} and h.priority >= 680]
        if not spans:
            return text
        chars = list(text)
        for start, end in spans:
            for k in range(start, min(end, len(chars))):
                chars[k] = " "
        return "".join(chars)

    def _harvest_at(self, tokens: list[Token], i: int, text: str = "") -> int:
        # The window has to start at the current token. Skipping filler inside it would pull a
        # later word into the window and drop the patronymic that follows it.
        words: list[Token] = []
        for token in tokens[i:i + NAME_WINDOW]:
            if token.initial or len(token.text) < 2 or not name_capitalised(token.text):
                break
            # Текст файла склеен из ячеек и абзацев через перевод строки. ФИО не переходит из одной
            # ячейки в другую: «Логист ВЭД» и следующая за ней «Белавина Михайлович» — разные записи.
            # Знак препинания тоже разделяет записи: «Ольга Николаевна! Направляю» — не ФИО из трёх слов.
            if words and text and text[words[-1].end:token.start].strip(" \t\u00a0"):
                break
            words.append(token)
        if len(words) < 2:
            return 0
        texts = [w.text for w in words]
        if len(texts) >= NAME_WINDOW:
            triple = self._harvest_triple(texts)
            if triple:
                self._register(*triple)
                return NAME_WINDOW
        # Пара распознаётся только тогда, когда следом не стоит отчество: иначе от тройки
        # откусываются два слова, а отчество остаётся началом следующего окна и путает разбор.
        if len(texts) >= NAME_WINDOW and mo.looks_like_patronymic(texts[2]):
            return 0
        if any(mo.is_stop_word(x) and not mo.is_given_name(x) and not mo.is_ambiguous_given(x) for x in texts[:2]):
            return 0
        pair = texts[:2]
        # «John Smith»: латинская пара, где первое слово — известное имя, а второе — слово с заглавной буквы.
        if all(x.isascii() for x in pair) and mo.is_given_name(pair[0]) and not mo.is_given_name(pair[1]) \
                and self._latin_name_word(words[1]) and mo.is_english_given(pair[0]) \
                and not self._continues_capitalised(tokens, i, text):
            self._register(pair[1], pair[0], "")
            return 2
        named = [mo.is_given_name(x) for x in pair]
        # Фамилия опознаётся в любом падеже: «Смирновой Анне» и «Ковалем Игорем» встречаются
        # в приказах чаще именительного. К начальной форме её приводит уже `_register`.
        shaped = [mo.looks_like_surname_inflected(x) for x in pair]
        # «Иванов Роман»: имя-омоним обычного слова принимается рядом со словом фамильного вида.
        for slot in (0, 1):
            if not named[slot] and shaped[1 - slot] and mo.is_ambiguous_given(pair[slot]) \
                    and not mo.is_ambiguous_given(pair[1 - slot]):
                # «Роман» само по себе выглядит и фамилией («-ман»), но рядом с настоящей фамилией это имя.
                named[slot], shaped[slot] = True, False
        tail = texts[2] if len(texts) >= NAME_WINDOW and mo.maybe_patronymic(texts[2]) else ""
        consumed = NAME_WINDOW if tail else 2
        if named[0] and shaped[1] and not named[1]:
            self._register(pair[1], pair[0], tail)
            return consumed
        if named[1] and shaped[0] and not named[0]:
            self._register(pair[0], pair[1], tail)
            return consumed
        return 0

    @staticmethod
    def _continues_capitalised(tokens: list[Token], i: int, text: str) -> bool:
        """За парой сразу следует ещё одно слово с заглавной буквы: «Corel Draw Suite» — название, а не имя."""
        if i + 2 >= len(tokens):
            return False
        following = tokens[i + 2]
        gap = text[tokens[i + 1].end:following.start] if text else " "
        return gap.strip() == "" and "\n" not in gap and following.text.isascii() and following.text[:1].isupper()

    @staticmethod
    def _harvest_triple(texts: list[str]) -> tuple[str, str, str] | None:
        """Три слова — это ФИО, только если отчество стоит на своём месте.

        Фамилии на -ович (Воронович, Рабинович) по форме неотличимы от отчества, поэтому
        решает позиция: отчество третье в «Фамилия Имя Отчество» и второе в «Имя Отчество
        Фамилия» — но во втором случае первое слово обязано быть известным именем, иначе это
        просто хвост предыдущего ФИО, попавший в окно.
        """
        if mo.looks_like_patronymic(texts[2]):
            surname, given, patronymic = texts[0], texts[1], texts[2]
        elif mo.looks_like_patronymic(texts[1]) and mo.is_given_name(texts[0]):
            surname, given, patronymic = texts[2], texts[0], texts[1]
        else:
            return None
        # Стоп-лист остаётся только для фамилии: рядом с настоящим отчеством «Мая» и «Август»
        # это имена, а не месяцы, и отменять из-за них разбор нельзя. А вот прилагательное
        # фамилией не бывает: «Уважаемый Пётр Алексеевич» — это обращение, а не ФИО.
        # Название города, совпадающее с фамилией («Ростов Иван Андреевич», «Киров Пётр Сергеевич»): рядом стоят имя и
        # отчество, и фамилия с фамильным окончанием — это человек, а не место.
        if mo.is_stop_word(surname) and not mo.looks_like_surname(surname):
            return None
        return surname, given, patronymic

    @staticmethod
    def _to_nominative(surname: str, given: str, patronymic: str) -> tuple[str, str, str]:
        """Привести выученное ФИО к именительному падежу.

        Заучивать имя в той форме, в какой оно попалось, нельзя по двум причинам. Склонение
        строится вперёд от именительного, и от «Иванову» оно даст «Иванову-а, Иванову-ым» —
        мусор, которым в документе не найдётся ничего. И тождество человека рассыпается:
        «Иванову», «Иванова» и «Ивановым» заводят три записи вместо одной, после чего каждое
        упоминание становится неоднозначным и уходит в ручную проверку.

        Род берётся из отчества и навязывается фамилии: словарь приводит фамилию к мужской
        лемме, и «Смирновой Анне Петровне» иначе превратилась бы в «Смирнов Анна Петровна».
        """
        # Фамилии на -ых/-их не склоняются: словарь принимает «Добрых» за родительный падеж прилагательного «добрый».
        # Мужская фамилия на -ов/-ев/-ин уже в именительном: словарь читает «Бабунов» как родительный множественного
        # от «бабун» и возвращал «Бабун». Женскую форму на -ова/-ина от родительного мужской не отличить по одному
        # слову: решает пол по имени и отчеству.
        low = fold(surname)
        gender_hint = mo.gender_of("", given, patronymic)
        if low.endswith(("ых", "их") + NOMINATIVE_MASCULINE_ENDINGS):
            surname_gender = mo.UNKNOWN
        elif low.endswith(NOMINATIVE_FEMININE_ENDINGS) and gender_hint != mo.MALE:
            surname_gender = mo.UNKNOWN
        elif lexicon.available() and not lexicon.shape(surname).surname:
            # Словарь не знает слово как фамилию («Мулык»): достраивать за него начальную форму нельзя,
            # предсказатель выдумывает «Мулыка» и человек перестаёт находиться.
            surname_gender = mo.UNKNOWN
        else:
            surname, surname_gender = lexicon.to_nominative(surname, "Surn")
        if given and (mo.is_ambiguous_given(given) or (
                lexicon.available() and fold(given) not in mo.GIVEN_NAMES
                and not (lexicon.shape(given).given and lexicon.shape(given).known))):
            # «Роман» уже в начальной форме, а редкое имя («Айаал», «Сулустан») словарю неизвестно: склонять его
            # назад нельзя, предсказатель превращает его в «Айаала».
            given, _ = given, mo.UNKNOWN
        else:
            given, _ = lexicon.to_nominative(given, "Name", mo.GIVEN_NAMES) if given else ("", mo.UNKNOWN)
        patronymic, _ = lexicon.to_nominative(patronymic, "Patr") if patronymic else ("", mo.UNKNOWN)
        # Род снимается уже с приведённых форм. По косвенным он врёт: «Сергея» кончается на
        # «я», и правило «-а/-я значит женское» объявляло Сергея Владимировича женщиной, после
        # чего отчество переписывалось в «Владимировна».
        gender = mo.gender_of(surname, given, patronymic)
        if gender == mo.UNKNOWN:
            gender = surname_gender
        if gender != mo.UNKNOWN:
            surname = mo.to_gender(surname, gender)
            patronymic = mo.to_gender_patronymic(patronymic, gender)
        return surname, given, patronymic

    def _register(self, surname: str, given: str, patronymic: str) -> None:
        if self._deferred is not None and surname.isascii() and given.isascii():
            # Латинская запись ждёт конца прохода: к этому времени кириллическая строка того же человека уже заучена.
            self._deferred.append((surname, given, patronymic))
            return
        if surname.isascii() and given.isascii():
            near = {pid for pid in self.index.surname.get(fold(surname), set())
                    if pid in self.index.people and not self.index.people[pid].surname.isascii()
                    and self._close_to_given(pid, fold(given))}
            if not near:
                # «Demenstein Elena» при «Деменштейн Елена»: фамилию латиницей записали не по правилам транслитерации.
                # Имя совпало, фамилия в двух правках от одного из написаний — тот же человек, если он такой один.
                near = {pid for pid in self.index.given.get(fold(given), set())
                        if pid in self.index.people and not self.index.people[pid].surname.isascii()
                        and self._close_to_surname(pid, fold(surname))}
                near = near if len(near) == 1 else set()
            if near:
                self.index.add_surfaces(near, surname, given, patronymic)
                return
        raw = (surname, given, patronymic)
        surname, given, patronymic = self._to_nominative(surname, given, patronymic)
        if len(surname) < 2 or len(given) < 2:
            return
        person_id = local_person_id(surname, given, patronymic)
        if self.index.knows(person_id):
            self.index.add_surfaces({person_id}, *raw)
            return
        # The same person may already be here under their initials alone; fill that record in
        # rather than creating a rival one that would make every mention ambiguous.
        letters = (given[0],) + ((patronymic[0],) if patronymic else ())
        gender = mo.gender_of(surname, given, patronymic)
        candidates = {initials_person_id(form, letters)
                      for form in mo.decline_surname(surname, gender) | {surname}}
        if self.index.absorb(candidates, surname, given, patronymic):
            self.index.add_surfaces({person_id}, *raw)
            return
        # Somebody the directory already covers must not gain a shadow entry: two records for
        # one person turn every mention of them into an ambiguous review item.
        known = self.index.surname.get(fold(surname), set()) & self.index.given.get(fold(given), set())
        if known and patronymic:
            # «Иванов Александр Викторович» и «Иванов Александр Сергеевич» — два человека. Слить их значило бы
            # оставить отчество второго в документе открытым: находка накрыла бы только фамилию и имя.
            differs = all(self.index.people[k].patronymic
                          and fold(self.index.people[k].patronymic) != fold(patronymic) for k in known
                          if k in self.index.people)
            if differs:
                known = set()
        if known:
            if patronymic:
                self.index.learn_patronymic(known, patronymic)
            self.index.add_surfaces(known, *raw)
            return
        full = " ".join(x for x in (surname, given, patronymic) if x)
        self.index.add(Person(person_id, local_person_token(person_id), full, surname, given, patronymic))
        self.index.add_surfaces({person_id}, *raw)

    def _register_initials(self, surname: str, letters: tuple[str, ...]) -> None:
        if len(surname) < 3 or not letters:
            return
        if self._match_initials(self.index.surname.get(fold(surname), set()), letters):
            return
        person_id = initials_person_id(surname, letters)
        if self.index.knows(person_id):
            return
        shown = surname + " " + "".join(f"{letter}." for letter in letters)
        person = Person(person_id, local_person_token(person_id), shown, surname)
        self.index.add(person, initials=tuple(frozenset({fold(letter)}) for letter in letters))

    # -- scanning -------------------------------------------------------------

    def scan(self, text: str, file: str, location: str) -> list[Finding]:
        if not text:
            return []
        norm = normalize(text)
        candidates: list[tuple[int, Finding]] = []
        if self.settings.secrets:
            candidates.extend(self._rule_findings(norm, text, file, location, SECRET_RULES))
        candidates.extend(self._entity_findings(norm, text, file, location))
        if self.settings.personal_data:
            # One tokenisation feeds all three name passes: scan() runs per cell and per
            # paragraph, so repeating the regex walk here is paid thousands of times over.
            tokens = tokenize(norm.text)
            self._drop_abbreviations(tokens)
            candidates.extend(self._name_findings(tokens, norm, text, file, location))
            candidates.extend(self._rule_findings(norm, text, file, location, PII_RULES))
            candidates.extend(self._lone_date(norm, text, file, location))
            candidates.extend(self._unknown_names(tokens, norm, text, file, location))
            candidates.extend(self._fuzzy_names(tokens, norm, text, file, location))
        if self.settings.business_confidential:
            candidates.extend(self._rule_findings(norm, text, file, location, BUSINESS_RULES))
        found = self._resolve(candidates, text, file, location)
        if self.settings.personal_data:
            found = self._complete_unit_name(found, norm, text, file, location)
            found = self._complete_list_names(found, norm, text, file, location)
        keep = self.entities.keep
        if keep:
            found = [f for f in found if fold(f.original) not in keep]
        return found

    SINGLE_NAME = re.compile(r"^[A-ZА-ЯЁ][^\W\d_]{2,}(?:-[^\W\d_]+)?$")
    WHOLE_NAME = re.compile(r"^[A-ZА-ЯЁ][^\W\d_]+(?:[-'’][^\W\d_]+)*(?: [A-ZА-ЯЁ][^\W\d_]+(?:[-'’][^\W\d_]+)*){1,3}$")

    def _complete_unit_name(self, found, norm, text, file, location):
        """Ячейка или строка, целиком состоящая из ФИО, скрывается целиком.

        Редкая фамилия рядом с найденными именем и отчеством («Шин Эмма Борисовна») сама по себе не находится:
        слова нет в словаре, окончания фамильного нет. Но если весь текст ячейки — два-четыре слова с заглавной буквы
        и среди них есть имя или отчество, а в заголовке столбца стоит «ФИО», это человек, и остаток нельзя оставлять.
        """
        stripped = norm.text.strip()
        single = bool(self.SINGLE_NAME.match(stripped)) and fold(stripped) in self.person_values \
            and not mo.is_stop_word(stripped) and not mo.is_common_word(stripped)
        if not self.WHOLE_NAME.match(stripped) and not single:
            return found
        offset = len(norm.text) - len(norm.text.lstrip())
        start, end = norm.to_original(offset, offset + len(stripped))
        if end <= start:
            return found
        inside = [f for f in found if f.start >= start and f.end <= end]
        if any(f.category in {"ORG", "PROJECT", "CITY", "REGION", "DOMAIN", "FILE", "TERM"} and f.decision == Decision.AUTO
               and not (fold(stripped) in self.person_values) for f in inside):
            return found
        # Неоднозначность (однофамильцы) и опечатки решает человек, а не эта эвристика.
        if any(f.decision == Decision.REVIEW and (f.candidates or "опечат" in f.reason) for f in inside):
            return found
        persons = [f for f in inside if f.category == "PERSON" and f.decision == Decision.AUTO]
        if persons and persons[0].start == start and persons[0].end == end:
            return found
        words = stripped.split()
        hinted = fold(stripped) in self.person_values
        if not hinted and (not self.settings.learn_names or not persons):
            # Без подсказки столбца доверяем только уже опознанному человеку: остальное — «форма ФИО» на проверку.
            return found
        if not hinted:
            if any(mo.is_stop_word(w) and not mo.is_given_name(w) and not mo.is_ambiguous_given(w) for w in words):
                return found
            evidence = any(mo.is_given_name(w) or mo.looks_like_patronymic(w) for w in words)
            ordinary = [w for w in words if lexicon.shape(w).lexical and lexicon.shape(w).known
                        and not mo.is_given_name(w) and not lexicon.shape(w).surname and not lexicon.shape(w).given]
            if not evidence or ordinary:
                return found
        person_id = persons[0].person_id if len(persons) == 1 else None
        merged = self._make("PERSON", start, end, text, file, location, Decision.AUTO, .9, person_id=person_id,
                            reason="Столбец с ФИО" if hinted else "Ячейка целиком похожа на ФИО")
        return sorted([f for f in found if f not in inside] + [merged], key=lambda f: (f.start, -(f.end - f.start)))

    LIST_ITEM = re.compile(r"[^;,\n]+")

    def _complete_list_names(self, found, norm, text, file, location):
        """Список людей через «;», «,» или перевод строки: «Иванов И.И.; Калина Д.В.; Орлов, Соколова».

        Когда хотя бы два пункта списка уже опознаны людьми (или столбец назван «ФИО»), остальные короткие пункты того же
        вида — фамилия в любом падеже, «Слово И.О.», пара, отложенная на проверку, — тоже люди, даже если фамилия
        совпадает с обычным словом. Без соседей такие слова так и остаются на ручной проверке.
        """
        body = norm.text
        if not any(sep in body for sep in ";,\n"):
            return found
        persons = [f for f in found if f.category == "PERSON" and f.decision == Decision.AUTO]
        hinted_cell = fold(body.strip()) in self.person_values
        if len(persons) < 2 and not hinted_cell and not self.person_values:
            return found
        items: list[tuple[int, int]] = []
        for m in self.LIST_ITEM.finditer(body):
            start, end = m.start(), m.end()
            colon = body.rfind(":", start, end)
            if colon >= 0:
                start = colon + 1          # «Участники: Иванов» — подпись перед двоеточием не пункт списка
            while start < end and body[start].isspace():
                start += 1
            while end > start and body[end - 1].isspace():
                end -= 1
            if end > start + 1 and body[end - 1] == "." and body[end - 2].islower():
                end -= 1                   # точка в конце предложения, а не после инициала
            if end - start >= 3:
                items.append((start, end))
        if len(items) < 2:
            return found
        spans = [norm.to_original(start, end) for start, end in items]
        confirmed = 0
        for (start, end), (nstart, nend) in zip(spans, items):
            if any(f.start >= start and f.end <= end for f in persons) or fold(body[nstart:nend]) in self.person_values:
                confirmed += 1
        if confirmed < 2 and not hinted_cell:
            return found
        promoted = []
        for (start, end), (nstart, nend) in zip(spans, items):
            if end <= start or any(f.decision == Decision.AUTO and f.start < end and start < f.end for f in found):
                continue
            inside = [f for f in found if f.start < end and start < f.end]
            flagged = any(f.category == "POSSIBLE_PERSON" and f.start <= start and f.end >= end
                          and f.reason != "Возможная опечатка в фамилии" for f in inside)
            if not flagged and not self._list_person_shape(body[nstart:nend], nstart):
                continue
            ids = self.index.surname.get(fold(body[nstart:nend]), set())
            person_id = self.resolve_person(next(iter(ids))) if len(ids) == 1 else None
            promoted.append((start, end, person_id))
        if not promoted:
            return found
        for start, end, _ in promoted:
            found = [f for f in found if not (f.start < end and start < f.end)]
        found.extend(self._make("PERSON", start, end, text, file, location, Decision.AUTO, .88, person_id=person_id,
                                reason="Фамилия в списке людей") for start, end, person_id in promoted)
        return sorted(found, key=lambda f: (f.start, -(f.end - f.start)))

    def _list_person_shape(self, item: str, offset: int) -> bool:
        """Пункт списка сам по себе похож на человека: одна фамилия в любом падеже или «Слово И.О.» с точками."""
        tokens = tokenize(item)
        if not tokens or len(tokens) > 3:
            return False
        head = tokens[0]
        word = head.text
        if head.initial or len(word) < 3 or not name_capitalised(word) or word.isupper() or not self._cyrillic(word):
            return False
        if mo.is_stop_word(word) or head.key in self.ROLE_MARKERS:
            return False
        if any(start < offset + head.end and offset + head.start < end for start, end in self._geo_spans):
            return False
        rest = tokens[1:]
        if not rest:
            info = lexicon.shape(word)
            # «Сибирский», «Центральной» — прилагательное с полным окончанием; «Сомов» (краткая форма) — фамилия.
            adjective = (head.key.endswith(("ий", "ый", "ая", "яя", "ое", "ее", "ые", "ие") + self.ADJECTIVE_OBLIQUE)
                         and info.known and not info.surname and info.lemma.endswith(("ий", "ый", "ой")))
            return (mo.looks_like_surname_inflected(word) and not mo.is_given_name(word)
                    and not self._toponym_shaped(word, info) and not adjective)
        if len(rest) == 1 and not rest[0].initial:
            # «Лоури Геннадий»: два слова с заглавной, одно из них — имя, второе не обиходное слово.
            other = rest[0].text
            return any(mo.is_given_name(a) and self._bare_name_word(b, a) for a, b in ((word, other), (other, word)))
        return all(len(t.text) == 1 and t.initial and not t.weak and t.end > t.start + 1 for t in rest)

    def _entity_findings(self, norm, text, file, location):
        out = []
        hits = self.entities.find(norm.text)
        self._geo_spans = [(h.start, h.end) for h in hits if h.category in {"CITY", "REGION"}]
        for hit in hits:
            start, end = norm.to_original(hit.start, hit.end)
            if end <= start:
                continue
            out.append((hit.priority, self._make(hit.category, start, end, text, file, location, Decision.AUTO,
                                                 hit.confidence, reason=hit.reason, key=hit.key)))
        if self.token_known is not None:
            out.extend(self._literal_tokens(norm, text, file, location))
        if getattr(self.settings, "suggest", False) and self.settings.organizations:
            for hit in self.entities.suggest_hits(norm.text):
                start, end = norm.to_original(hit.start, hit.end)
                if end > start:
                    out.append((hit.priority, self._make("POSSIBLE_ENTITY", start, end, text, file, location,
                                                         Decision.REVIEW, hit.confidence, reason=hit.reason,
                                                         key=hit.key)))
        return out

    def _literal_tokens(self, norm, text, file, location):
        """Текст документа, случайно совпавший с токеном: `Project1` в исходнике не должен восстановиться
        в чужое значение, поэтому такая строка сама заменяется на новый токен и возвращается как была."""
        out = []
        for regex, is_email in ((TOKEN_RE, False), (EMAIL_TOKEN_RE, True)):
            for m in regex.finditer(norm.text):
                kind, number, variant = parse_match(m, email=is_email)
                if self.token_known(kind, number, variant):
                    continue
                start, end = norm.to_original(m.start(), m.end())
                # Самый низкий приоритет: адрес company0.ru — это часть email, а не «токен», и дробить его нельзя.
                out.append((100, self._make("TERM", start, end, text, file, location, Decision.AUTO, 1.0,
                                             reason="Текст, совпадающий с форматом токена", key=fold(text[start:end]))))
        return out

    def verify(self, text: str) -> list[Finding]:
        """Post-rewrite check. Runs only the pattern rules: they are position-independent, so
        concatenated document text cannot produce the false adjacencies name matching would."""
        if not text:
            return []
        norm = normalize(text)
        rules = (SECRET_RULES if self.settings.secrets else ()) + (PII_RULES if self.settings.personal_data else ())
        found = self._rule_findings(norm, text, "verify", "verify", rules)
        # Токен email1@example.com сам похож на адрес, но это результат замены, а не остаток.
        return [f for _, f in found if f.decision == Decision.AUTO and not EMAIL_TOKEN_RE.fullmatch(f.original)]

    def _make(self, category: str, start: int, end: int, text: str, file: str, location: str,
              decision: Decision, confidence: float, *, person_id: str | None = None,
              candidates: list[str] | None = None, reason: str = "", permanent: bool = False,
              key: str = "") -> Finding:
        original = text[start:end]
        context = text[max(0, start - CONTEXT_CHARS):min(len(text), end + CONTEXT_CHARS)].replace("\n", " ")
        if not key:
            key = person_id or identity_key(category, original)
        return Finding(finding_id(file, location, start, end, original), file, location, category, original, start, end, decision, confidence,
                       person_id, candidates or [], reason, context, permanent, key)

    def _rule_findings(self, norm, text, file, location, rules):
        out = []
        for rule in rules:
            for m in rule.pattern.finditer(norm.text):
                span = m.span(rule.group) if rule.group else m.span()
                if span[0] < 0:
                    continue
                value = m.group(rule.group) if rule.group else m.group(0)
                if rule.validator is not None and not rule.validator(value):
                    continue
                start, end = norm.to_original(*span)
                if end <= start:
                    continue
                permanent = rule.category == "SECRET" and not self.settings.retain_secrets_for_restore
                if rule.category == "EMAIL" and self.token_known is not None:
                    tm = EMAIL_TOKEN_RE.fullmatch(value)
                    if tm and self.token_known(*parse_match(tm, email=True)):
                        continue       # адрес email1@example.com — уже метка, а не адрес
                person_id = self.index.phrases.get(fold(value)) if rule.category == "EMAIL" else None
                category = "PERSON" if person_id else rule.category
                priority = PRIORITY.get(category, CONTAINER_PRIORITY if category in SPLITTABLE else DEFAULT_PRIORITY)
                out.append((priority, self._make(category, start, end, text, file, location, Decision.AUTO,
                                                 rule.confidence, person_id=person_id, reason=rule.reason,
                                                 permanent=permanent)))
        return out

    def _lone_date(self, norm, text, file, location):
        if file not in self._birth_context:
            return []
        m = LONE_DATE.match(norm.text)
        if not m:
            return []
        year = int(m.group(3))
        if not 1920 <= year <= dt.datetime.now(dt.UTC).year - 16:
            return []
        start, end = norm.to_original(m.start(1), m.end(3))
        return [(DEFAULT_PRIORITY, self._make("BIRTH_DATE", start, end, text, file, location, Decision.REVIEW,
                                              .60, reason="Отдельная дата — возможна дата рождения"))]

    # -- names ----------------------------------------------------------------

    def _name_findings(self, tokens, norm, text, file, location):
        out: list[tuple[int, Finding]] = []
        i = 0
        while i < len(tokens):
            match = self._match_at(tokens, i, norm.text)
            if not match:
                i += 1
                continue
            length, ids, confidence, reason = match
            start, end = norm.to_original(tokens[i].start, tokens[i + length - 1].end)
            owner = self.spelling_owner.get(fold(text[start:end]))
            if owner:
                ids = {owner}
            if not ids:
                # Фамилия узнана, а соседнее имя не подошло никому: пара скрывается отдельной меткой.
                out.append((PRIORITY["PERSON"], self._make("PERSON", start, end, text, file, location,
                                                           Decision.AUTO, confidence, reason=reason)))
            elif len(ids) == 1:
                person_id = self.resolve_person(next(iter(ids)))
                out.append((PRIORITY["PERSON"], self._make("PERSON", start, end, text, file, location,
                                                           Decision.AUTO, confidence, person_id=person_id, reason=reason)))
            else:
                merged = sorted({self.resolve_person(pid) for pid in ids})
                if len(merged) == 1:
                    out.append((PRIORITY["PERSON"], self._make("PERSON", start, end, text, file, location,
                                                               Decision.AUTO, .95, person_id=merged[0],
                                                               reason="Объединено вручную")))
                else:
                    out.append((PRIORITY["PERSON"], self._make("PERSON", start, end, text, file, location,
                                                               Decision.REVIEW, .55, candidates=merged,
                                                               reason="Совпадает с несколькими сотрудниками")))
            i += length
        return out

    def _common_word_alone(self, tokens: list[Token], i: int) -> bool:
        """Фамилия, совпадающая с обычным словом, без имени рядом: «Мастер участка», «Корпоративный Казначей».

        У человека по фамилии Мастер в документе есть должность мастера, и без этой проверки каждое
        «мастер» превращалось в его имя. Полное ФИО и «фамилия + инициалы» разбираются другими правилами и сюда
        не попадают. Латиница без соседнего имени — тоже обычное слово: транслитерация «Мастер» это «Master».
        """
        word = tokens[i].text
        neighbours = [tokens[j] for j in (i - 1, i + 1) if 0 <= j < len(tokens)]
        if any(t.initial or mo.is_given_name(t.text) or t.key in self.index.given for t in neighbours):
            return False
        if word.isascii():
            # Фамилия человека, названного латиницей в самом документе («John Smith»), узнаётся и без имени.
            ids = self.index.surname.get(tokens[i].key, set())
            if any(self.index.people[pid].given_name.isascii() for pid in ids if pid in self.index.people):
                return False
            return not mo.looks_like_latin_surname(word)
        if mo.looks_like_surname_inflected(word):
            return False
        shape = lexicon.shape(word)
        return shape.lexical and shape.known

    def _lookup(self, table: dict[str, set[str]], token: Token) -> set[str]:
        return table.get(token.key, frozenset())

    @staticmethod
    def _collect_initials(tokens: list[Token], start: int, limit: int = 2,
                          allow_weak: bool = False) -> tuple[int, tuple[str, ...]]:
        """Инициалы подряд, независимо от того, разделены они точками, склеены или без точки."""
        letters: list[str] = []
        consumed = 0
        while start + consumed < len(tokens) and len(letters) < limit:
            token = tokens[start + consumed]
            if not token.initial or (token.weak and not allow_weak):
                break
            # «Петров А.. ДВ - Сидоров Д.»: склеенная пара после инициала — уже метка подразделения, а не отчество,
            # и токен, который не помещается в лимит целиком, чужой: трёх инициалов у имени не бывает.
            if letters and (token.glued or len(letters) + len(token.letters) > limit):
                break
            letters.extend(token.letters)
            consumed += 1
        return consumed, tuple(letters[:limit])

    def _leading_initials_ok(self, tokens: list[Token], i: int, consumed: int, text: str) -> bool:
        """Может ли группа начинаться с инициалов.

        В «Иванов И.И., Петров П.П.» инициалы принадлежат фамилии СЛЕВА. Без этой проверки
        разбор хватает «И.И., Петров» и приписывает Петрову чужие инициалы.
        """
        previous = tokens[i - 1] if i else None
        if previous is not None and (previous.initial or previous.key in self.index.surname
                                     or mo.looks_like_surname_inflected(previous.text)):
            return False
        tail = tokens[i + consumed]
        # «Leonid V. Vyazkov»: слева имя того же человека — инициал здесь отчество, а не начало нового имени.
        if previous is not None and self._lookup(self.index.given, previous) & self._lookup(self.index.surname, tail):
            return False
        # «Метла Д.В.; Жаров А.А.»: после точки с запятой начинается другой пункт, инициалы остались в предыдущем.
        gap = text[tokens[i + consumed - 1].end:tail.start]
        return "\n" not in gap and not any(sep in gap for sep in ";,|")

    def _match_initials(self, ids: set[str], letters: tuple[str, ...]) -> set[str]:
        for slot, letter in enumerate(letters[:2]):
            ids = self._by_initial(ids, letter, slot)
            if not ids:
                break
        return ids

    def _by_initial(self, ids: set[str], letter: str, slot: int) -> set[str]:
        """Keep the people whose initial matches. An unrecorded patronymic matches anything —
        the directory not knowing it is no reason to leave "Иванов А.Б." in the clear."""
        letter = fold(letter)
        keep = set()
        for pid in ids:
            letters = self.index.initials.get(pid, (frozenset(), frozenset()))[slot]
            if not letters or letter in letters:
                keep.add(pid)
        return keep

    # A surname on its own is real PII, but after "ул." or "г." it is almost always a place.
    GEO_MARKERS = frozenset({"ул", "улица", "пер", "переулок", "просп", "проспект", "пл", "площадь",
                             "наб", "набережная", "ш", "шоссе", "бул", "бульвар", "г", "город",
                             "пос", "посёлок", "поселок", "с", "село", "д", "деревня", "мкр", "проезд",
                             "станция", "метро", "им", "имени"})

    # Место называется и наоборот — определением впереди, а не сокращением: «Московская
    # область», «Краснодарский край», «Тверская улица». Слева тут нет ничего, и по одному
    # только левому соседу такая строка выглядела как фамилия после глагола.
    GEO_HEADS = frozenset({"область", "области", "областью", "край", "края", "краем",
                           "район", "района", "округ", "округа", "республика", "республики",
                           "улица", "улицы", "площадь", "набережная", "шоссе", "губерния",
                           "волость", "слобода", "застава", "дорога", "магистраль"})

    def _geographic(self, tokens: list[Token], i: int) -> bool:
        if i + 1 < len(tokens) and tokens[i + 1].key in self.GEO_HEADS:
            return True
        if i == 0:
            return False
        prev = tokens[i - 1]
        if prev.key not in self.GEO_MARKERS:
            return False
        # Однобуквенный маркер адресный только с точкой. «с.» — село, «с» — предлог, и после
        # него стоит человек: «согласовано с Ковалёвой», «получено с Иванова». То же у «г», «д»
        # и «ш». Без этой проверки любая фамилия после предлога «с» молча пропадала.
        return len(prev.text) > 1 or prev.end > prev.start + 1

    def _match_at(self, tokens: list[Token], i: int, text: str) -> tuple[int, set[str], float, str] | None:
        remaining = len(tokens) - i
        t0 = tokens[i]
        t1 = tokens[i + 1] if remaining > 1 else None
        t2 = tokens[i + 2] if remaining > 2 else None
        # ФИО не переходит через границу пункта списка: «Сур Игорь; Камышанский Игорь» — «Игорь; Камышанский» не пара.
        if t1 is not None and self._list_break(t0, t1, text):
            t1 = t2 = None
        elif t2 is not None and self._list_break(t1, t2, text):
            t2 = None

        if t1 is not None and t2 is not None and not (t0.initial or t1.initial or t2.initial):
            ids = self._lookup(self.index.surname, t0) & self._lookup(self.index.given, t1) & self._lookup(self.index.patronymic, t2)
            if ids:
                return 3, ids, .995, "Полное ФИО"
            ids = self._lookup(self.index.given, t0) & self._lookup(self.index.patronymic, t1) & self._lookup(self.index.surname, t2)
            if ids:
                return 3, ids, .995, "Полное ФИО"

        # «Leonid V. Vyazkov»: имя, инициал отчества и фамилия — так пишут по-английски. Без этого правила имя уходило
        # к тёзке по правилу одиночного имени, а «V. Vyazkov» заводил ещё одного человека.
        if t1 is not None and t1.initial and not t0.initial:
            consumed, letters = self._collect_initials(tokens, i + 1)
            tail = tokens[i + 1 + consumed] if i + 1 + consumed < len(tokens) else None
            if len(letters) == 1 and tail is not None and not tail.initial:
                ids = self._lookup(self.index.given, t0) & self._lookup(self.index.surname, tail)
                ids = self._by_initial(ids, letters[0], 1)
                if ids:
                    return 2 + consumed, ids, .96, "Имя, инициал и фамилия"

        if not t0.initial:
            known = self._lookup(self.index.surname, t0)
            consumed, letters = self._collect_initials(tokens, i + 1, allow_weak=bool(known))
            if letters:
                ids = self._match_initials(known, letters)
                if ids:
                    reason = "Фамилия и инициалы" if len(letters) > 1 else "Фамилия и инициал"
                    return 1 + consumed, ids, .97 if len(letters) > 1 else .93, reason

        if t0.initial:
            consumed, letters = self._collect_initials(tokens, i)
            tail = tokens[i + consumed] if i + consumed < len(tokens) else None
            if (letters and tail is not None and not tail.initial
                    and self._leading_initials_ok(tokens, i, consumed, text)):
                ids = self._match_initials(self._lookup(self.index.surname, tail), letters)
                if ids:
                    reason = "Инициалы и фамилия" if len(letters) > 1 else "Инициал и фамилия"
                    return consumed + 1, ids, .97 if len(letters) > 1 else .93, reason

        if t1 is not None and not t0.initial and not t1.initial:
            ids = self._lookup(self.index.surname, t0) & self._lookup(self.index.given, t1)
            if ids:
                return 2, ids, .98, "Фамилия и имя"
            ids = self._lookup(self.index.given, t0) & self._lookup(self.index.surname, t1)
            if ids:
                return 2, ids, .98, "Имя и фамилия"
            ids = self._lookup(self.index.given, t0) & self._lookup(self.index.patronymic, t1)
            if ids:
                return 2, ids, .97, "Имя и отчество"
            if t0.text.isascii() and t1.text.isascii():
                latin = self._latin_fio(tokens, i, text)
                if latin:
                    return latin

        # Три буквы, а не четыре: здесь слово уже совпало с фамилией человека, которого
        # программа встретила в этом же документе. Порог в четыре буквы означал, что
        # «Ким Иван Иванович» в шапке обезличивался, а «Ким» в подписи ниже — нет.
        if not t0.initial and len(t0.text) >= 3 and name_capitalised(t0.text) and not mo.is_stop_word(t0.text):
            ids = self._lookup(self.index.surname, t0)
            # Слово может быть и именем, и фамилией (Богдан, Роман). Раз оно совпало с фамилией
            # известного человека — это фамилия: иначе она осталась бы в документе открытой.
            if ids and not self._geographic(tokens, i) and not self._common_word_alone(tokens, i):
                return 1, ids, .90, "Фамилия"
            phrase = self.index.phrases.get(t0.key)
            if phrase:
                return 1, {phrase}, .99, "Известный вариант написания"
            # «Hi Maria»: имя без фамилии относится к единственному человеку с таким именем в документе.
            ids = self._lookup(self.index.given, t0)
            # «Ольга Николаевна»: имя с отчеством — отдельный человек, а не «Ольга» из другого места документа.
            followed_by_patronymic = (t1 is not None and not t1.initial and name_capitalised(t1.text)
                                      and mo.looks_like_patronymic(t1.text))
            if (len(ids) == 1 and not followed_by_patronymic and not mo.is_ambiguous_given(t0.text) and not self._geographic(tokens, i)
                    and (t0.text.isascii() or mo.is_given_name(t0.text)) and mo.is_given_name(t0.text)
                    and not lexicon.is_common_word(t0.text) and t0.key not in self.index.surname):
                person = self.index.people.get(next(iter(ids)))
                if (person is not None and person.given_name and len(person.given_name) >= 3
                        and not self._foreign_neighbour(tokens, i, text, person.person_id)):
                    return 1, ids, .82, "Имя человека, названного в документе"
        return None

    @staticmethod
    def _list_break(left: Token, right: Token, text: str) -> bool:
        # Перевод строки границей не считается: в ячейке длинное ФИО переносится на вторую строку.
        return bool(text) and any(sep in text[left.end:right.start] for sep in ";|•")

    def _latin_fio(self, tokens: list[Token], i: int, text: str) -> tuple[int, set[str], float, str] | None:
        """«Barabanov Aleksander», «Medvedev Eugeniy»: фамилия латиницей узнана, а имя записано не так, как его даёт
        транслитерация (лишняя буква, «Eu-» вместо «Ev-», опечатка). Имя в пределах двух правок от написаний имени
        этого человека — это он; из однофамильцев выбирается тот, чьё имя совпало. Если не совпало ни у кого, пара
        всё равно скрывается целиком отдельной меткой: оставить рядом с замаскированной фамилией имя открытым нельзя.
        """
        t0, t1 = tokens[i], tokens[i + 1]
        if text and text[t0.end:t1.start].strip(" \t "):
            return None
        for surname, name in ((t0, t1), (t1, t0)):
            ids = self._lookup(self.index.surname, surname)
            if not ids or not self._latin_name_word(name) or name.key in self.index.surname:
                continue
            near = {pid for pid in ids if self._close_to_given(pid, name.key)}
            if near:
                return 2, near, .9, "Фамилия и имя латиницей"
            if surname is t0 and not self._continues_capitalised(tokens, i, text) \
                    and any(self.index.people[pid].given_name for pid in ids if pid in self.index.people):
                return 2, set(), .85, "Фамилия латиницей рядом с именем"
        # Имя узнано, а фамилия записана не по правилам («Lavrenchyk Evgenia»): тот же человек, если он такой один.
        for surname, name in ((t0, t1), (t1, t0)):
            ids = self._lookup(self.index.given, name)
            if not ids or len(ids) > 200 or not self._latin_name_word(surname) or surname.key in self.index.given:
                continue
            near = {pid for pid in ids if self._close_to_surname(pid, surname.key)}
            if len(near) == 1:
                return 2, near, .88, "Фамилия и имя латиницей"
        return None

    def _close_to_given(self, person_id: str, key: str) -> bool:
        person = self.index.people.get(person_id)
        if person is None or not person.given_name or len(key) < 3:
            return False
        limit = 2 if len(key) >= 5 else 1
        return any(_edit_distance(key, fold(form), limit) <= limit
                   for form in mo.transliterate(person.given_name) | {person.given_name})

    def _close_to_surname(self, person_id: str, key: str) -> bool:
        person = self.index.people.get(person_id)
        if person is None or not person.surname or len(key) < 4:
            return False
        limit = 2 if len(key) >= 6 else 1
        return any(_edit_distance(key, fold(form), limit) <= limit for form in mo.transliterate(person.surname))

    def _foreign_neighbour(self, tokens: list[Token], i: int, text: str, person_id: str) -> bool:
        """Рядом с именем стоит чужая фамилия: «Сур Игорь» — не тот Игорь, что назван в документе полностью.

        Правило «имя без фамилии — единственный человек с таким именем» годится только для одинокого имени.
        Слово с заглавной вплотную к нему (через пробел), которое не обиходное и не часть ФИО этого человека,
        — это его собственная фамилия, и привязка к тёзке слила бы двух разных людей в один токен.
        """
        for j in (i - 1, i + 1):
            if not 0 <= j < len(tokens):
                continue
            other = tokens[j]
            left, right = (other, tokens[i]) if j < i else (tokens[i], other)
            gap = text[left.end:right.start] if text else " "
            if gap.strip(" \t ") or other.initial or len(other.text) < 2 or not name_capitalised(other.text):
                continue
            if other.text.isupper() or mo.is_stop_word(other.text) or mo.is_common_word(other.text):
                continue
            shape = lexicon.shape(other.text)
            if shape.lexical and shape.known and not shape.surname:
                continue            # «Уважаемая Мария», «Привет Мария»: обиходное слово рядом не мешает
            if any(person_id in table.get(other.key, ()) for table in
                   (self.index.surname, self.index.given, self.index.patronymic)):
                continue
            return True
        return False

    # -- names that are not in any directory ----------------------------------

    def _unknown_names(self, tokens, norm, text, file, location):
        """Flag person-shaped word groups that no directory explains.

        The shape test is deliberately narrow: at least one token has to look like a real
        surname or patronymic, or be a known given name. Two arbitrary capitalised words —
        "Российская Федерация", "Договор Поставки" — are not enough.
        """
        out = []
        # Латинское имя ищем только там, где текст в целом русский: в английском документе
        # признак «два слова латиницей с заглавной» есть у каждой второй пары слов.
        cyrillic = any(self._cyrillic(t.text) for t in tokens)
        i = 0
        while i < len(tokens):
            shape = self._initials_shape(tokens, i, norm.text)
            if shape:
                length, reason = shape
                start, end = norm.to_original(tokens[i].start, tokens[i + length - 1].end)
                out.append((PRIORITY["PERSON"],
                            self._make("PERSON", start, end, text, file, location,
                                       Decision.AUTO, .93, reason=reason)))
                i += length
                continue
            group = self._unknown_at(tokens, i, norm.text)
            if not group:
                latin = self._latin_pair(tokens, i, norm.text) if cyrillic else ""
                if latin:
                    start, end = norm.to_original(tokens[i].start, tokens[i + 1].end)
                    out.append((PRIORITY["POSSIBLE_PERSON"],
                                self._make("POSSIBLE_PERSON", start, end, text, file, location,
                                           Decision.REVIEW, .55, reason=latin)))
                    i += 2
                    continue
                lone, confidence = self._lone_surname(tokens, i, norm.text)
                if not lone:
                    lone, confidence = self._lone_given(tokens, i), .60
                if lone:
                    start, end = norm.to_original(tokens[i].start, tokens[i].end)
                    # После должности («Директор Сидоров») фамилия заменяется сразу; после глагола и в прозе — ручная проверка.
                    if lone == "Фамилия после должности":
                        out.append((PRIORITY["PERSON"], self._make("PERSON", start, end, text, file, location,
                                                                   Decision.AUTO, .85, reason=lone)))
                    else:
                        out.append((PRIORITY["POSSIBLE_PERSON"],
                                    self._make("POSSIBLE_PERSON", start, end, text, file, location,
                                               Decision.REVIEW, confidence, reason=lone)))
                i += 1
                continue
            length, confidence, reason = group
            start, end = norm.to_original(tokens[i].start, tokens[i + length - 1].end)
            out.append((PRIORITY["POSSIBLE_PERSON"],
                        self._make("POSSIBLE_PERSON", start, end, text, file, location,
                                   Decision.REVIEW, confidence, reason=reason)))
            i += length
        return out

    def _initials_shape(self, tokens: list[Token], i: int, text: str) -> tuple[int, str] | None:
        """`Фамилия И.И.` in either order, for a person no directory and no document explains.

        The shape is unmistakably a reference to a human being, so it gets replaced rather than
        parked for review — otherwise a file that only ever abbreviates names would ship clean.
        """
        head = tokens[i]
        if not head.initial and (self._surname_like(head) or self._common_before_initials(tokens, i)):
            consumed, letters = self._collect_initials(tokens, i + 1)
            # Две заглавные без точек — это чаще аббревиатура (АО, ОК, РФ), чем инициалы.
            # Принимаем их, только когда фамилия уже известна по надёжной форме.
            if (consumed and tokens[i + 1].glued and head.key not in self.index.surname
                    and not mo.looks_like_surname_inflected(head.text)):
                return None
            if letters and not self._same_script(head.text, tokens[i + 1].text):
                return None
            if consumed and tokens[i + 1].glued and self._place_not_person(head):
                return None
            # Фамилия может быть известна, но с другими инициалами: это однофамилец, и накрыть
            # надо всю форму — иначе совпадёт одна фамилия, а инициалы останутся открытыми.
            if letters and not self._match_initials(self._lookup(self.index.surname, head), letters):
                return 1 + consumed, ("Фамилия с инициалами вне справочника" if len(letters) > 1
                                      else "Фамилия с инициалом вне справочника")
        if head.initial:
            consumed, letters = self._collect_initials(tokens, i)
            tail = tokens[i + consumed] if i + consumed < len(tokens) else None
            if (letters and tail is not None and not tail.initial and self._surname_like(tail)
                    and not (head.glued and self._place_not_person(tail))
                    and not self._match_initials(self._lookup(self.index.surname, tail), letters)
                    and self._leading_initials_ok(tokens, i, consumed, text)):
                return consumed + 1, "Инициалы с фамилией вне справочника"
        # Обращение по имени-отчеству: фамилии рядом нет, но человек назван однозначно.
        following = tokens[i + 1] if i + 1 < len(tokens) else None
        if (not head.initial and mo.is_given_name(head.text) and following is not None
                and not following.initial and mo.looks_like_patronymic(following.text)
                and not (self._lookup(self.index.given, head) & self._lookup(self.index.patronymic, following))):
            # Имя может быть знакомым по другому человеку («Ольга Викторовна» ≠ «Ольга Николаевна»): важна пара целиком.
            return 2, "Имя и отчество вне справочника"
        return None

    def _common_before_initials(self, tokens: list[Token], i: int) -> bool:
        """«Калина Д.В.»: фамилия-омоним обычного слова, за которой стоят два инициала с точками.

        Обычное слово словарь отвергает раньше, чем правило успевает посмотреть на инициалы. Два инициала с точками —
        довод сильный, но «Логист А.П.» и «Поставка К.Т.» (заголовок ячейки и чьи-то инициалы) выглядят так же,
        поэтому слово обязано ещё и иметь фамильное окончание и не быть служебным или названием должности.
        """
        head = tokens[i]
        word = head.text
        if head.labelled or len(word) < 3 or not name_capitalised(word) or word.isupper() or not self._cyrillic(word):
            return False
        if mo.is_stop_word(word) or head.key in self.ROLE_MARKERS or not mo.looks_like_surname_inflected(word):
            return False
        pair = tokens[i + 1:i + 3]
        return len(pair) == 2 and all(len(t.text) == 1 and t.initial and not t.weak and not t.glued
                                      and t.end > t.start + 1 for t in pair)

    def _place_not_person(self, token: Token) -> bool:
        """Слово рядом со слитной парой заглавных («ОП Тальжино») — место: его нашёл разбор мест или у него окончание
        названия села. Две заглавные без точек слишком слабый довод, чтобы спорить с этим."""
        if any(start < token.end and token.start < end for start, end in self._geo_spans):
            return True
        return self._toponym_shaped(token.text, lexicon.shape(token.text))

    @staticmethod
    def _surname_like(token: Token) -> bool:
        """Может ли слово рядом с инициалами быть фамилией.

        Раньше здесь проходило любое слово с заглавной буквы, и это была главная течь во всём
        разборе: «Excel A.B.» заводил человека по фамилии Excel, после чего каждое «Excel» во
        всех файлах проекта вырезалось как ФИО. Причём необратимо — запись оставалась в
        индексе до конца прогона. Ровно так же гибли «Логист А.П.», «Поставка К.Т.»,
        «Выгрузка И.И.»: заголовок ячейки с заглавной плюс чьи-то инициалы рядом.

        Теперь мало не быть обиходным словом — нужен положительный довод, что слово вообще
        может быть фамилией. Годится любой из трёх, и все три дешёвые:
          * окончание фамилии в любом падеже (-ов/-ин/-ко/-ян/-швили…);
          * словарь OpenCorpora знает слово как фамилию («Гнатюк», «Мкртчян», «Цой»);
          * слова нет в словаре вообще — незнакомое кириллическое слово с заглавной буквы
            куда вероятнее редкая фамилия («Шпац», «Петропавловских»), чем термин.

        Латиница под третий довод не подпадает намеренно: русского словаря для неё нет, и
        «незнакомость» там не значит ничего — под неё попадает любое английское слово.
        """
        word = token.text
        if token.labelled or len(word) < 2 or not name_capitalised(word) or word.isupper():
            return False
        if mo.is_stop_word(word) or mo.is_common_word(word):
            return False
        if mo.looks_like_surname_inflected(word):
            return True
        # «О'Коннор», «д'Артаньян»: частица с апострофом внутри слова — форма, которой у
        # обычного русского слова не бывает. Добавить «-ор» в список фамильных окончаний
        # нельзя (директор, договор, монитор, коридор), а сама частица говорит достаточно.
        if PARTICLE_PREFIX.match(word):
            return True
        info = lexicon.shape(word)
        if info.surname:
            return True
        # Двухбуквенное слово принимается только по прямому доводу выше: «Ли С.П.» и «Ян О.В.»
        # это люди, а «Их А.Б.» и «Об И.И.» — нет.
        return len(word) >= 3 and not info.known and Detector._cyrillic(word)

    @staticmethod
    def _cyrillic(word: str) -> bool:
        return bool(word) and "Ѐ" <= word[0] <= "ӿ"

    @staticmethod
    def _same_script(left: str, right: str) -> bool:
        """Фамилия и инициалы пишутся одним алфавитом: «Excel ВБ» — не человек."""
        cyrillic = "\u0400" <= left[0] <= "\u04ff"
        return cyrillic == ("\u0400" <= right[0] <= "\u04ff")

    @staticmethod
    def _joined(tokens: list[Token], i: int, text: str) -> int:
        """Сколько слов подряд, начиная с i, разделены только пробелами: знак препинания и перевод строки обрывают имя."""
        count = 1
        while i + count < len(tokens):
            gap = text[tokens[i + count - 1].end:tokens[i + count].start]
            if gap.strip(" ") or "\n" in gap:
                break
            count += 1
        return count

    def _unknown_at(self, tokens: list[Token], i: int, text: str = "") -> tuple[int, float, str] | None:
        window = tokens[i:i + min(NAME_WINDOW, self._joined(tokens, i, text) if text else NAME_WINDOW)]
        words = [t for t in window if not t.initial]
        if len(words) < 2 or words[0] is not window[0]:
            return None
        if any(len(w.text) < 3 or not name_capitalised(w.text) for w in words[:2]):
            return None
        if any(mo.is_stop_word(w.text) and not mo.is_given_name(w.text) for w in words[:2]):
            return None
        texts = [w.text for w in words]
        if len(texts) >= NAME_WINDOW and not mo.is_stop_word(texts[2]) and texts[2][0].isupper() and mo.looks_like_patronymic(texts[2]):
            if mo.looks_like_surname_inflected(texts[0]) or mo.is_given_name(texts[0]) or mo.is_given_name(texts[1]):
                return NAME_WINDOW, .82, "ФИО отсутствует в справочнике"
        # Фамилию проверяем в любом падеже. Требовать именительный можно при заучивании имени,
        # где от него строится склонение, но не при поиске: «Кузнецовой Ольги» и «Гнатюка
        # Сергея» — обычная форма для приказов и доверенностей, и раньше они не находились.
        strong = [mo.looks_like_surname_inflected(t) for t in texts[:2]]
        known = [mo.is_given_name(t) for t in texts[:2]]
        if (strong[0] and known[1]) or (known[0] and strong[1]):
            return 2, .78, "ФИО отсутствует в справочнике"
        # Имя рядом со словом, которое словарь фамилией не считает: «Дмитрий Пак», «Андрей
        # Шпак». Довод слабее — отсюда и оценка ниже, и обязательная ручная проверка.
        if known[0] and self._plausible_surname(texts[1]):
            return 2, .70, "Возможное ФИО вне справочника"
        # «Лоури Геннадий», «Сур Игорь»: редкая фамилия без фамильного окончания рядом с именем. Второе слово не обиходное
        # и не служебное — иначе «Игорь Приказ» и «Отдел Павел» стали бы людьми.
        for slot in (0, 1):
            if known[slot] and self._bare_name_word(texts[1 - slot], texts[slot]):
                return 2, .70, "Возможное ФИО вне справочника"
        # Пара «слово-на-ова + слово-на-ов» без имени рядом опознанию не поддаётся: «Снова
        # Смирнов» и «Причина Иванова» выглядят так же, как две настоящие фамилии подряд.
        # Такая находка давала больше шума в ручной проверке, чем пользы.
        return None

    # Слова, после которых стоящее следом имя собственное — почти всегда человек. Формы
    # выписаны явно: морфология здесь ничего не добавила бы, а список короткий и проверяемый.
    ACTOR_MARKERS = frozenset({
        "получено", "получил", "получила", "принято", "принял", "приняла", "подписал",
        "подписала", "подписано", "утвердил", "утвердила", "утверждено", "согласовал",
        "согласовала", "согласовано", "исполнил", "исполнила", "проверил", "проверила",
        "составил", "составила", "выдал", "выдала", "выдано", "направлено", "направил",
        "направила", "представил", "представила", "сдал", "сдала", "оформил", "оформила",
        "разработал", "разработала", "передано", "передал", "передала", "заполнил",
        "заполнила", "внёс", "внес", "внесла", "уволен", "уволена", "принят", "принята",
        "назначен", "назначена", "ответственный", "ответственная", "исполнитель", "от",
    })

    # Должность или роль перед фамилией: «Директор Сидоров», «Ответственный: Абдуллаев». За такой подписью стоит человек, и
    # оставлять фамилию открытой хуже, чем заменить; в отличие от глагола, роль не бывает частью обычной фразы.
    ROLE_MARKERS = frozenset({
        "директор", "директора", "директору", "руководитель", "руководителя", "начальник", "начальника", "менеджер",
        "менеджера", "бухгалтер", "бухгалтера", "председатель", "председателя", "заместитель", "заместителя",
        "куратор", "куратора", "ответственный", "ответственная", "ответственного", "ответственному", "исполнитель",
        "исполнителя", "подписант", "подписанта", "представитель", "представителя", "контакт", "контакта", "инженер",
        "инженера", "специалист", "специалиста", "координатор", "координатора", "супервайзер", "автор", "автора",
        "заказчик", "заказчика", "подрядчик", "подрядчика",
    })

    def _role_context(self, tokens: list[Token], i: int) -> bool:
        return i > 0 and tokens[i - 1].key in self.ROLE_MARKERS

    def _person_context(self, tokens: list[Token], i: int) -> bool:
        """Слева стоит слово, после которого имя собственное — почти всегда человек.

        Список выше остался ради того, чего морфология не покрывает: «исполнитель», «от»,
        «ответственный». Всё остальное решает разбор — иначе список приходится дописывать
        под каждый новый глагол, а пропущенный глагол означает фамилию в открытом виде.
        """
        if i == 0:
            return False
        previous = tokens[i - 1]
        return previous.key in self.ACTOR_MARKERS or mo.is_actor_verb(previous.text)

    def _lone_surname(self, tokens: list[Token], i: int, text: str) -> tuple[str, float]:
        """Фамилия сама по себе, без имени и без инициалов: «получено Сидоровым».

        В колонке «Ответственный» или в тексте приказа это сплошь и рядом единственное
        упоминание человека, и пропустить его — оставить ПДн в документе. Автозамена сюда не
        годится: у слова нет ни одного соседа, который подтвердил бы, что это человек, —
        поэтому находка всегда уходит на ручную проверку.

        Одной фамильной формы мало — она есть у «Тинькофф», у «Жукова» в названии проспекта и
        у любого родительного падежа множественного числа. Довод нужен ещё один, и подходят
        два разных.

        Первый — контекст, в котором стоять может только человек: либо слово занимает ячейку
        целиком (колонка «Ответственный», строка подписи), либо слева стоит глагол передачи
        действия — «получено Сидоровым», «подписал Петров».

        Второй — сама фамилия. «Сегодня Воронцов представил отчёт» не подходит ни под одно из
        контекстных правил: слева наречие, справа обычный глагол, — и такое упоминание раньше
        не давало вообще никакой находки, то есть человек уезжал в выгрузку молча. Здесь
        опорой служит словарь: слово разобрано как фамилия, взято из словаря, а не достроено
        предсказателем по окончанию (`known`), и обычного чтения у него нет (`lexical`).
        Каждое из трёх условий закрывает свой промах: без `known` находкой становится
        «Тинькофф» и любое незнакомое слово на -ов, без `lexical` — «Московская Область».
        Отдельно гасятся топонимы: у «Кирова» и «Пушкина» фамильный разбор ровно такой же,
        а человека за ними нет.
        """
        token = tokens[i]
        word = token.text
        # Порог в три буквы, а не в четыре: «Ким» и «Цой» — фамилии целого региона, и
        # отбрасывать их по длине значит терять их всегда. Двухбуквенные слова остаются за
        # бортом: «Ли» словарь знает и как частицу, и слишком часто это она и есть.
        if token.initial or token.labelled or len(word) < 3 or word.isupper():
            return "", 0.
        if not name_capitalised(word) or self._geographic(tokens, i):
            return "", 0.
        # Признак «бывает именем» отсюда убран: он ничего не решал сам по себе, а «Ким»
        # словарь знает и как имя, и как фамилию — слово вылетало по первому же признаку,
        # и «Договор подписал Ким» не давал ни одной находки. Ниже стоит проверка сильнее:
        # слово обязано быть фамилией по словарю, иначе «Иван» и «Мария» прошли бы тоже.
        if mo.is_stop_word(word) or mo.is_common_word(word):
            return "", 0.
        if token.key in self.index.surname:
            return "", 0.
        info = lexicon.shape(word)
        # «Директор Кузбасского филиала», «Начальник Коммерческого отдела»: прилагательное при названии подразделения,
        # а не фамилия. Одна такая находка отравляла хранилище — «Коммерческого» заменялось как человек во всех файлах.
        if self._unit_adjective(tokens, i, text, info):
            return "", 0.
        # «Тальжино», «Кольцово»: название села, а не фамилия, если словарь не знает его фамилией.
        if self._toponym_shaped(word, info):
            return "", 0.
        actor = len(tokens) == 1 or self._person_context(tokens, i)
        role = self._role_context(tokens, i)
        if not info.surname:
            # Фамилия, которой нет в словаре («Абдуллаев»), после должности всё равно человек.
            if not (role and not info.lexical and mo.looks_like_surname_inflected(word)):
                return "", 0.
        actor = actor or role
        # Отчество само по себе не стоит, поэтому слово такого вида обычно отбрасываем. Но в
        # строке подписи оно почти всегда фамилия: «Станкевич», «Макаревич», «Богданович»,
        # «Климович» — их в России целый пласт, и все они молчали. Довод контекста здесь
        # сильнее вида слова: после «подписал» и «утверждаю» стоит человек.
        if mo.looks_like_patronymic(word) and not actor:
            return "", 0.
        # Три буквы — довод слабый, и один он не тянет: подпереть его обязан контекст.
        # «Договор подписал Ким» и ячейка, в которой кроме «Ким» ничего нет, — да;
        # то же слово посреди прозы — нет.
        if len(word) < 4 and not actor:
            return "", 0.
        # «Иванов», «Попов», «Балашов» словарь считает и обиходными словами (иванов день,
        # попов сын), поэтому строка «Подписал Иванов.» не давала ни одной находки — самая
        # обычная подпись под приказом уезжала наружу в открытом виде. Пропускаем такое
        # слово только вместе с двумя доводами сразу: перед ним стоит глагол-деятель
        # («подписал», «согласовал», «исполнитель»), и словарь возводит слово к нему же
        # самому. Второй довод отсекает прилагательные-топонимы: «Московская» сводится к
        # «московский», «Нижегородская» — к «нижегородский», а «Иванов» — к «иванов».
        if info.lexical and not (actor and self._reduces_to_a_surname(word, info)):
            return "", 0.
        if role:
            return "Фамилия после должности", .8
        if actor:
            return "Одиночная фамилия вне справочника", .65
        if not info.known or mo.is_toponym(word) or self._surname_shaped_neighbour(tokens, i, text):
            return "", 0.
        # Довод слабее контекстного — отсюда и оценка ниже. Решение в обоих случаях одно:
        # ручная проверка. Автозамена по одному только виду слова изуродовала бы документ.
        return "Одиночная фамилия вне справочника", .55

    # Косвенные окончания прилагательного и названия подразделений, перед которыми оно стоит.
    ADJECTIVE_OBLIQUE = ("ого", "его", "ому", "ему", "ой", "ей", "ым", "им", "ом", "ем", "ую", "юю", "ых", "их")
    UNIT_NOUNS = ("филиал", "департамент", "отдел", "завод", "офис", "участк", "участок", "цех", "склад", "управлен",
                  "служб", "подразделен", "направлен", "дивизион", "регион", "сектор", "центр", "бюро", "комбинат",
                  "представительств", "дирекци", "лаборатори", "хозяйств", "терминал", "площадк", "предприяти",
                  "производств", "магазин", "комплекс", "округ", "район", "област", "станци", "холдинг", "компани")
    UNIT_WORDS = frozenset({"край", "края", "краю", "краем", "крае", "база", "базы", "базе", "базу", "базой",
                            "блок", "блока", "блоку", "блоком", "группа", "группы", "группе", "группу", "группой"})

    def _unit_adjective(self, tokens: list[Token], i: int, text: str, info) -> bool:
        """Прилагательное в косвенном падеже, а не фамилия: следом название подразделения, или словарь знает слово
        только прилагательным, или оно образовано от известного города/региона («Кузбасского», «Московского»)."""
        word = tokens[i].key
        if len(word) < 5 or not word.endswith(self.ADJECTIVE_OBLIQUE):
            return False
        following = tokens[i + 1] if i + 1 < len(tokens) else None
        if following is not None and "\n" not in text[tokens[i].end:following.start]:
            if following.key in self.UNIT_WORDS or following.key.startswith(self.UNIT_NOUNS):
                return True
        if info.surname and info.known:
            return False           # «Директор Троицкого»: словарь знает фамилию, соседа-подразделения нет
        if info.known and info.lemma.endswith(("ий", "ый", "ой")):
            return True            # «Директор Технического»: словарное прилагательное
        if not re.search(r"[сц]к(?:ого|ому|им|ом|ой|ую|их|ими)$", word):
            return False
        adj_of_city = getattr(EntityRecognizer, "_adj_of_city", None)
        return bool(adj_of_city and adj_of_city(tokens[i].text))

    @staticmethod
    def _toponym_shaped(word: str, info) -> bool:
        """Окончание названий сёл и посёлков (-ово/-ево/-ино/-ыно), а словарь фамилией слово не знает."""
        return len(word) >= 5 and fold(word).endswith(("ово", "ево", "ёво", "ино", "ыно")) and not (info.surname and info.known)

    @staticmethod
    def _reduces_to_a_surname(word: str, info) -> bool:
        """Слово возводится к фамилии, а не к обиходному слову.

        Проверка нужна затем, что словарь считает обиходными словами и «Иванов» (иванов
        день), и «Московская» (московская область). Различает их начальная форма: у «Иванов»
        она он сам, у «Московская» — прилагательное «московский».

        Одного равенства мало. Женская фамилия сводится к мужской — «Иванова» к «иванов»,
        «Цветкова» к «цветков», — и по строгому равенству каждая женская подпись оставалась
        неопознанной. Поэтому годится и второй случай: начальная форма сама фамилия. Топоним
        оттуда исключён, иначе «Московская» вернулась бы через «московский».
        """
        if info.lemma == word.casefold():
            return True
        return bool(info.lemma) and lexicon.shape(info.lemma).surname and not mo.is_toponym(info.lemma)

    def _surname_shaped_neighbour(self, tokens: list[Token], i: int, text: str) -> bool:
        """Слева через пробел стоит ещё одно слово фамильного вида: «Снова Смирнов».

        Пару таких слов от двух настоящих фамилий подряд не отличить, и `_unknown_at` её
        намеренно пропускает — шума от неё больше, чем пользы. Через одиночное правило тот же
        шум вернулся бы вторым словом пары, поэтому здесь стоит та же заглушка.

        Разделитель решает всё. «Петров, Сидоров и Мельников» — это список, и каждая запись в
        нём фамилия; так выглядит строка подписей, столбец согласующих и выпадающий список в
        Excel. Заглушка по одному только виду соседа съедала такой список целиком, оставляя
        одну последнюю фамилию, — а файл при этом объявлялся чистым.
        """
        if i == 0:
            return False
        prev = tokens[i - 1]
        gap = text[prev.end:tokens[i].start]
        if gap.strip() or "\n" in gap or "\r" in gap:
            return False
        return (len(prev.text) >= 4 and prev.text[0].isupper() and not prev.initial
                and mo.looks_like_surname_inflected(prev.text))

    def _lone_given(self, tokens: list[Token], i: int) -> str:
        """Уменьшительное имя само по себе: «заходил Саша», «Вова обещал перезвонить».

        В переписке и служебных записках это сплошь и рядом единственное, как человека
        называют, и до сих пор такое упоминание не находилось вовсе: одиночное слово не
        складывается ни в ФИО, ни в пару «фамилия — инициалы», а одиночная фамилия его не
        берёт — она сама отсеивает всё, что словарь считает именем.

        Довод слабее полного ФИО: у «Саши» нет ни фамилии рядом, ни отчества, и подтвердить
        его нечем. Поэтому находка всегда уходит на ручную проверку, а не в автозамену.
        """
        token = tokens[i]
        word = token.text
        if token.initial or token.labelled or word.isupper():
            return ""
        if len(word) < 3 or not word[0].isupper():
            return ""
        if not mo.is_diminutive_name(word):
            return ""
        # «Слава», «Света», «Поля» в список не входят, но словоформы совпадают у многих:
        # обиходное чтение всегда сильнее.
        if mo.is_stop_word(word) or mo.is_common_word(word):
            return ""
        if self._geographic(tokens, i):
            return ""
        if token.key in self.index.given or token.key in self.index.surname:
            return ""
        return "Уменьшительное имя вне справочника"

    # Латиница, которая пишется как имя, но именем не бывает: марки, продукты, юридические
    # формы, города и служебная лексика договоров. Список короткий намеренно — он закрывает
    # то, что реально встречается в русских документах, а не английский словарь целиком.
    LATIN_NON_NAMES = frozenset({
        "microsoft", "office", "windows", "server", "oracle", "adobe", "acrobat", "reader",
        "google", "apple", "amazon", "excel", "word", "outlook", "power", "powerpoint",
        "point", "visual", "studio", "basic", "teams", "azure", "cloud", "linux", "ubuntu",
        "android", "chrome", "firefox", "photoshop", "autocad", "intel", "cisco", "samsung",
        "huawei", "xerox", "canon", "epson", "hewlett", "packard", "dell", "lenovo", "asus",
        "nvidia", "siemens", "bosch", "sony", "toshiba", "kaspersky", "yandex", "docker",
        "ltd", "limited", "inc", "llc", "plc", "gmbh", "corp", "corporation", "company",
        "group", "holding", "trading", "bank", "service", "services", "system", "systems",
        "solution", "solutions", "technology", "technologies", "software", "hardware",
        "data", "center", "centre", "manager", "management", "report", "project", "client",
        "order", "invoice", "total", "sales", "business", "global", "international",
        "enterprise", "professional", "standard", "premium", "advanced", "home", "edition",
        "version", "release", "update", "pro", "plus", "max", "mini", "air", "user", "admin",
        "new", "york", "los", "angeles", "san", "francisco", "united", "states", "great",
        "britain", "north", "south", "east", "west", "city", "street", "avenue", "road",
        "state", "national", "world", "europe", "asia", "america", "russia", "moscow",
        "saint", "petersburg", "the", "and", "for", "with", "from", "all", "any", "one",
        "two", "first", "last", "next", "best", "top", "main", "open", "close", "start",
        "end", "high", "low", "full", "free", "real", "true", "false", "test", "demo",
    })

    def _latin_pair(self, tokens: list[Token], i: int, text: str = "") -> str:
        """Латинское имя в русском тексте: «Контракт подписал John Smith».

        Весь кириллический разбор проходит мимо: ни склонения, ни словаря, ни списка имён
        для латиницы нет, и до сих пор такая пара не давала находки ни при каких условиях.
        Опознать её можно только по форме — два слова подряд, каждое с одной заглавной, — а
        форма эта общая у «John Smith» и у «Microsoft Office». Поэтому отсекаем список
        названий, третье слово подряд (названия длиннее, имена короче) и заглавные целиком:
        «IBM» и «USA» именами не бывают. Довод остаётся слабым, решение — ручная проверка.
        """
        if i + 1 >= len(tokens):
            return ""
        if text and self._joined(tokens, i, text) < 2:
            return ""
        if not self._latin_name_word(tokens[i]) or not self._latin_name_word(tokens[i + 1]):
            return ""
        before = tokens[i - 1] if i else None
        after = tokens[i + 2] if i + 2 < len(tokens) else None
        if before is not None and self._latin_name_word(before):
            return ""
        if after is not None and self._latin_name_word(after):
            return ""
        return "Латинское имя вне справочника"

    @classmethod
    def _latin_name_word(cls, token: Token) -> bool:
        word = token.text
        if token.labelled or len(word) < 3 or not word.isascii() or not word.isalpha():
            return False
        if not word[0].isupper() or not word[1:].islower():
            return False
        return fold(word) not in cls.LATIN_NON_NAMES

    @classmethod
    def _bare_name_word(cls, word: str, given: str) -> bool:
        """Слово рядом с известным именем, которое ничем, кроме имени собственного, быть не может.

        Второе имя не мешает: «Лоури Геннадий» — фамилия, которую словарь достраивает как имя, рядом с настоящим именем.
        """
        if len(word) < 3 or not word[0].isupper() or word.isupper() or not (cls._cyrillic(word) and cls._cyrillic(given)):
            return False
        if mo.is_stop_word(word) or mo.looks_like_patronymic(word) or mo.is_common_word(word):
            return False
        return not lexicon.shape(word).lexical

    @staticmethod
    def _plausible_surname(word: str) -> bool:
        """Слово, которое может оказаться фамилией, когда рядом стоит опознанное имя.

        Одного этого признака мало — «Дмитрий Приказ» им тоже удовлетворяет по форме, — но
        рядом с настоящим именем он даёт находку на ручную проверку, а не автозамену.
        """
        if len(word) < 2 or not word[0].isupper() or word.isupper():
            return False
        if mo.is_stop_word(word) or mo.is_given_name(word) or mo.looks_like_patronymic(word):
            return False
        if mo.is_surname_homonym(word):
            return True
        info = lexicon.shape(word)
        return info.surname or not info.known

    # -- typos ----------------------------------------------------------------

    def _fuzzy_names(self, tokens, norm, text, file, location):
        """Catch a misspelled surname the exact index cannot see.

        Only tokens that already sit in a person-shaped context are tested, so an ordinary
        Russian word that happens to resemble a surname does not become a review item.
        """
        if not self.index.surname_buckets:
            return []
        out = []
        for i, token in enumerate(tokens):
            word = token.text
            if token.initial or len(word) < 5 or not word[0].isupper():
                continue
            key = token.key
            if key in self.index.surname or mo.is_stop_word(word) or mo.is_given_name(word):
                continue
            if not (mo.looks_like_surname(word) or self._name_context(tokens, i)):
                continue
            cached = self.index.fuzzy_cache.get(key)
            if cached is not None:
                best_form, best_ratio = (cached, 1.0) if cached else ("", 0.0)
                if not cached:
                    continue
                ids = sorted(self.index.surname.get(best_form, ()))
                if ids:
                    start, end = norm.to_original(token.start, token.end)
                    out.append((PRIORITY["POSSIBLE_PERSON"] + 50,
                                self._make("POSSIBLE_PERSON", start, end, text, file, location, Decision.REVIEW,
                                           FUZZY_MAX_CONFIDENCE, candidates=ids,
                                           reason="Возможная опечатка в фамилии")))
                continue
            best_ratio, best_form = 0.0, ""
            compared = 0
            for length in (len(key), len(key) - 1, len(key) + 1):
                for form in self.index.surname_buckets.get((key[:3], length), ()):
                    compared += 1
                    if compared > MAX_FUZZY_COMPARISONS:
                        break
                    ratio = difflib.SequenceMatcher(None, key, form).ratio()
                    if ratio > best_ratio:
                        best_ratio, best_form = ratio, form
                if compared > MAX_FUZZY_COMPARISONS:
                    break
            self.index.fuzzy_cache[key] = best_form if best_ratio >= FUZZY_THRESHOLD else ""
            if best_ratio < FUZZY_THRESHOLD:
                continue
            ids = sorted(self.index.surname.get(best_form, ()))
            if not ids:
                continue
            start, end = norm.to_original(token.start, token.end)
            out.append((PRIORITY["POSSIBLE_PERSON"] + 50,
                        self._make("POSSIBLE_PERSON", start, end, text, file, location, Decision.REVIEW,
                                   min(FUZZY_MAX_CONFIDENCE, best_ratio), candidates=ids,
                                   reason="Возможная опечатка в фамилии")))
        return out

    def _name_context(self, tokens: list[Token], i: int) -> bool:
        for neighbour in (tokens[i - 1] if i > 0 else None, tokens[i + 1] if i + 1 < len(tokens) else None):
            if neighbour is None:
                continue
            if neighbour.initial or mo.is_given_name(neighbour.text) or mo.looks_like_patronymic(neighbour.text):
                return True
            if neighbour.key in self.index.given:
                return True
        return False

    # -- overlap resolution ---------------------------------------------------

    def _resolve(self, candidates: list[tuple[int, Finding]], text: str, file: str, location: str) -> list[Finding]:
        accepted: list[Finding] = []
        occupied = Spans()
        candidates.sort(key=lambda item: (-item[0], -(item[1].end - item[1].start),
                                          -item[1].confidence, item[1].start))
        for priority, finding in candidates:
            if not occupied.overlaps(finding.start, finding.end):
                accepted.append(finding)
                occupied.add(finding.start, finding.end)
                continue
            if finding.category not in SPLITTABLE:
                continue
            # A container that lost part of its span still hides the rest; keep the fragments.
            for start, end in occupied.free_ranges(finding.start, finding.end):
                if end - start < 4:
                    continue
                fragment = self._make(finding.category, start, end, text, file, location,
                                      finding.decision, finding.confidence, reason=finding.reason)
                accepted.append(fragment)
                occupied.add(start, end)
        accepted.sort(key=lambda f: (f.start, -(f.end - f.start)))
        return accepted
