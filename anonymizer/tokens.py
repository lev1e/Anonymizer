"""Формат токенов: читаемые псевдонимы вида Name1, Company3, City2.

Токен живёт в файле, который человек отдаёт внешней языковой модели, поэтому к нему три требования:

* читаемость — `Name1` модель понимает как «человек №1» и не путает с обычным словом;
* устойчивость — один и тот же объект всегда даёт один и тот же токен, в любом файле;
* терпимый разбор — регистр, обратные слэши перед подчёркиванием, кириллические двойники латинских
  букв и склеенное с токеном русское окончание не мешают восстановлению.

Формы одного и того же объекта (`Иванов Иван`, `Иванову И.И.`) получают токены `Name1`, `Name1_2`,
`Name1_3`: связь между ними видна модели, а исходное написание возвращается точно.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Kind:
    code: str
    stem: str
    group: str          # раздел в отчёте
    label: str          # как назвать во множественном числе


# Порядок важен только для отчёта. group объединяет близкие сущности в одну строку сводки.
KINDS: tuple[Kind, ...] = (
    Kind("PERSON", "Name", "names", "имена"),
    Kind("ORG", "Company", "companies", "компании"),
    Kind("PROJECT", "Project", "projects", "проекты и объекты"),
    Kind("CITY", "City", "places", "города и населённые пункты"),
    Kind("REGION", "Region", "places", "регионы"),
    Kind("COUNTRY", "Country", "places", "страны"),
    Kind("ADDRESS", "Address", "places", "адреса"),
    Kind("EMAIL", "Email", "contacts", "email"),
    Kind("PHONE", "Phone", "contacts", "телефоны"),
    Kind("USERNAME", "Handle", "contacts", "аккаунты"),
    Kind("DOMAIN", "Domain", "contacts", "сайты и домены"),
    Kind("FILE", "File", "files", "имена файлов"),
    Kind("CONTRACT", "Contract", "documents", "номера договоров"),
    Kind("PASSPORT", "Passport", "documents", "паспортные данные"),
    Kind("SNILS", "Snils", "documents", "СНИЛС"),
    Kind("INN", "Inn", "documents", "ИНН"),
    Kind("OGRN", "Ogrn", "documents", "ОГРН"),
    Kind("KPP", "Kpp", "documents", "КПП"),
    Kind("BIK", "Bik", "documents", "БИК"),
    Kind("OMS", "Oms", "documents", "полисы"),
    Kind("DRIVER_LICENSE", "License", "documents", "водительские удостоверения"),
    Kind("VEHICLE_PLATE", "Plate", "documents", "госномера"),
    Kind("CARD", "Card", "finance", "банковские карты"),
    Kind("BANK_ACCOUNT", "Account", "finance", "банковские счета"),
    Kind("BIRTH_DATE", "Birthdate", "documents", "даты рождения"),
    Kind("IP_ADDRESS", "Ip", "contacts", "IP-адреса"),
    Kind("TERM", "Term", "custom", "слова из вашего списка"),
    Kind("META", "Meta", "hidden", "свойства файла"),
    Kind("PII", "Data", "documents", "прочие персональные данные"),
    Kind("SECRET", "Secret", "secrets", "пароли и ключи"),
    Kind("BUSINESS", "Biz", "custom", "коммерческие данные"),
)

KIND_BY_CODE = {kind.code: kind for kind in KINDS}
KIND_BY_STEM = {kind.stem.lower(): kind for kind in KINDS}

GROUP_LABELS = {
    "hidden": "Служебные свойства",
    "names": "Имена", "companies": "Компании", "projects": "Проекты", "places": "Города и адреса",
    "contacts": "Контакты", "files": "Имена файлов", "documents": "Документы и номера",
    "finance": "Финансовые реквизиты", "secrets": "Пароли и ключи", "custom": "Прочее",
}

# Вид сущности по категории находки. Неизвестная категория считается прочими данными.
CATEGORY_ALIASES = {
    "POSSIBLE_PERSON": "PERSON", "POSSIBLE_ENTITY": "TERM", "INN_PERSON": "INN", "INN_ORG": "INN",
}

EMAIL_DOMAIN = "example.com"


def kind_of(category: str) -> Kind:
    code = CATEGORY_ALIASES.get(category, category)
    return KIND_BY_CODE.get(code, KIND_BY_CODE["PII"])


# Кириллические двойники латинских букв: русский текст вокруг токена тянет за собой раскладку.
_LATIN_TO_CYR = {
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М", "O": "О", "P": "Р",
    "T": "Т", "X": "Х", "Y": "У", "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х",
    "y": "у", "k": "к", "m": "м", "t": "т", "h": "н", "b": "в",
}


def _stem_pattern(stem: str) -> str:
    """Основа токена: любой регистр и кириллические двойники на месте латинских букв."""
    parts = []
    for ch in stem:
        options = {ch.lower(), ch.upper()}
        for letter in (ch.lower(), ch.upper()):
            twin = _LATIN_TO_CYR.get(letter)
            if twin:
                options.update({twin.lower(), twin.upper()})
        parts.append("[" + "".join(sorted(options)) + "]")
    return "".join(parts)


_GENERAL_STEMS = sorted((k.stem for k in KINDS if k.code != "EMAIL"), key=len, reverse=True)
_STEM_ALTERNATION = "|".join(f"(?:{_stem_pattern(stem)})" for stem in _GENERAL_STEMS)

# Между цифрами и вариантом допускается обратный слэш: markdown экранирует подчёркивание.
_VARIANT = r"(?:\\?_(\d{1,6}))?"

TOKEN_RE = re.compile(
    rf"(?<![A-Za-z0-9_Ѐ-ӿ])({_STEM_ALTERNATION})(\d{{1,7}}){_VARIANT}(?![A-Za-z0-9_])")

# В именах таблиц и листов (`Таблица_Name1`) подчёркивание — обычный разделитель, а невидимый знак в них недопустим:
# здесь токен ищется и после подчёркивания.
TOKEN_RE_AFTER_UNDERSCORE = re.compile(
    rf"(?<![A-Za-z0-9Ѐ-ӿ])({_STEM_ALTERNATION})(\d{{1,7}}){_VARIANT}(?![A-Za-z0-9_])")

# Email-токен сохраняет форму адреса: столбец с почтой остаётся столбцом с почтой.
EMAIL_TOKEN_RE = re.compile(
    rf"(?<![\w.+-])({_stem_pattern('email')})(\d{{1,7}}){_VARIANT}@example\.com(?![\w-])(?!\.\w)", re.I)

# Русское окончание, приклеенное к токену: «Name1у». Токен распознаётся, окончание сохраняется.
ENDING_RE = re.compile(r"[а-яё]{1,3}(?![а-яёA-Za-z0-9])")


def normalize_stem(raw: str) -> str | None:
    """`nаme` (кириллическая «а») → `Name`; None, если это не основа токена."""
    folded = raw.translate(_CYR_TO_LATIN).lower()
    kind = KIND_BY_STEM.get(folded)
    return kind.stem if kind else None


_CYR_TO_LATIN = str.maketrans({twin: latin for latin, twin in _LATIN_TO_CYR.items()}
                              | {twin.lower(): latin.lower() for latin, twin in _LATIN_TO_CYR.items()}
                              | {twin.upper(): latin.upper() for latin, twin in _LATIN_TO_CYR.items()})


def format_token(kind: Kind, number: int, variant: int = 1, extension: str = "") -> str:
    suffix = f"_{variant}" if variant > 1 else ""
    if kind.code == "EMAIL":
        return f"email{number}{suffix}@{EMAIL_DOMAIN}"
    return f"{kind.stem}{number}{suffix}{extension}"


def token_base(kind: Kind, number: int) -> str:
    """Ключ сущности в хранилище — токен первой формы."""
    return format_token(kind, number, 1)


_STEM_KIND_FOR_EMAIL = KIND_BY_CODE["EMAIL"]


def parse_match(match: re.Match[str], email: bool = False) -> tuple[Kind, int, int]:
    """(вид, номер, вариант) из совпадения TOKEN_RE или EMAIL_TOKEN_RE."""
    if email:
        return _STEM_KIND_FOR_EMAIL, int(match.group(2)), int(match.group(3) or 1)
    stem = normalize_stem(match.group(1))
    assert stem is not None
    return KIND_BY_STEM[stem.lower()], int(match.group(2)), int(match.group(3) or 1)


def looks_like_token(text: str) -> bool:
    """Быстрая проверка перед дорогим разбором: есть ли в строке хоть цифра рядом с буквой."""
    return any(ch.isdigit() for ch in text)


def base_of(token: str) -> str | None:
    """`Name7_2` → `Name7`, `email7_2@example.com` → `email7@example.com`; None, если это не токен."""
    m = EMAIL_TOKEN_RE.fullmatch(token)
    if m:
        return token_base(KIND_BY_CODE["EMAIL"], int(m.group(2)))
    m = TOKEN_RE.fullmatch(token)
    if m:
        kind, number, _ = parse_match(m)
        return token_base(kind, number)
    return None
