"""Организации, проекты, города, домены и имена файлов.

Человека находит морфология, а компанию по одному виду слова не отличить от обычного слова:
«Напитки Вместе» — два обиходных слова. Поэтому здесь работает контекст и накопление знаний
внутри документа:

1. Явные признаки. Юридическая форма («ООО «Аврора»», «АО Сумитек»), слово-подсказка перед
   кавычками («компания «…»», «ГЭС «…»»), «г. Ковдор», «п/ст Тальжино», «Ленинградской обл.».
2. Распространение. Как только название опознано хотя бы раз, все его упоминания в документе
   заменяются одинаково — и в кавычках, и без, и в косвенном падеже.
3. Справочники. Крупные компании, города и регионы находятся и без контекста.
4. Пользовательские списки: «всегда скрывать» и «никогда не скрывать».

Всё, что опознано неуверенно, не заменяется: оно попадает в список для проверки.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from . import geo_data as geo
from . import lexicon
from .normalize import fold
from .tokens import EMAIL_TOKEN_RE, TOKEN_RE

# Приоритеты при перекрытии. Явное упоминание организации выше человека: в «ООО «Иванов и партнёры»»
# слово «Иванов» — часть названия, а не отдельный человек.
P_TERM = 1100
P_EXPLICIT = 950
P_KNOWN = 940
P_DOMAIN = 820
P_FILE = 780
P_GEO = 650
P_GEO_KNOWN = 680
P_GEO_EXPLICIT = 690     # ниже адреса (700): город внутри адреса не должен дробить адрес на части

WORD = re.compile(r"[^\W_]+(?:[.\-'’][^\W_]+)*", re.UNICODE)
CAP = r"[A-ZА-ЯЁ][\w\-]*"
QUOTE_OPEN = "«“„\""
QUOTE_CLOSE = "»”“\""

_forms_sorted = sorted({f.replace(".", r"\.") for f in geo.LEGAL_FORMS if f not in {"РФ ООО"}}, key=len, reverse=True)
_LEGAL_RU = "|".join(re.escape(f) for f in sorted((f for f in geo.LEGAL_FORMS if f != "РФ ООО"), key=len, reverse=True))
_LEGAL_LAT = "|".join(re.escape(f) for f in sorted(geo.LATIN_LEGAL_FORMS, key=len, reverse=True))

def _legal_quoted(forms: str, dot: str = r"\.?") -> "re.Pattern[str]":
    """Юридическая форма и название в кавычках: «ёлочки» (с вложенными кавычками), парные или одиночные апострофы."""
    return re.compile(
        rf"(?<![\w\-])(?:{forms}){dot} *(?:«(?P<n>[^«»\n]{{1,80}}?)»|[“„\"](?P<m>[^«»“”„\"\n]{{1,80}}?)[”“\"]"
        rf"|['‘](?P<k>[^'‘’\n]{{1,80}}?)['’])")


LEGAL_QUOTED = _legal_quoted(_LEGAL_RU)
# Юридическая форма словами: «Акционерное общество «Технопарк Сибирь»». Без этого название из обычных слов (или со словом-регионом)
# не признавалось организацией, и часть его оставалась в файле.
LEGAL_LONG_FORMS = (r"(?i:(?:(?:публичн|непубличн|открыт|закрыт)\w{2,3}\s+)?акционерн\w{2,3}\s+обществ\w{1,2}"
                    r"|обществ\w{1,2}\s+с\s+ограниченной\s+ответственностью|товарищество\s+с\s+ограниченной\s+ответственностью)")
LEGAL_LONG_QUOTED = _legal_quoted(LEGAL_LONG_FORMS, dot=r"\s*")
LEGAL_LATIN_QUOTED = _legal_quoted(_LEGAL_LAT, dot="")
# Без кавычек: «ООО Сумитек интернейшнл». Берём заглавные слова и, при наличии, хвостовое слово вроде «интернейшнл».
_TAILS = "|".join(sorted(map(re.escape, geo.ORG_NAME_TAILS), key=len, reverse=True))
# Форма после названия: «Ромашка ООО выставила счёт». Одно слово: перед ним может стоять обычное слово («Заказчик Ромашка ООО»).
LEGAL_RU_TRAILING = re.compile(
    rf"(?<![\w\-])(?P<n>{CAP}) +(?:ООО|АО|ЗАО|ПАО|ОАО)(?![\w])(?! *[«“„\"'‘A-ZА-ЯЁ0-9])")
# Слово, после которого называют компанию или проект. Автозамены нет: слово может быть и человеком, поэтому это предложение
# для проверки, но оно показывается всегда, даже если само слово обычное («Тихая Гавань»).
ORG_CUE = re.compile(
    rf"(?<![\w])(?i:компани\w{{1,2}}|корпораци\w{{1,2}}|фирм\w{{1,2}}|холдинг\w*|клиент\w*|заказчик\w*|контрагент\w*|поставщик\w*|"
    rf"партн[её]р\w*|подрядчик\w*|покупател\w+|customer|client|supplier|vendor)\s*[:—–\-]?\s+(?P<n>{CAP}(?: +{CAP}){{0,2}})")
PROJECT_CUE = re.compile(
    rf"(?<![\w])(?i:проект\w*|программ\w+|продукт\w*|инициатив\w+|project|program)\s*[:—–\-]?\s+(?P<n>{CAP}(?: +{CAP}){{0,1}})")
LEGAL_BARE = re.compile(
    rf"(?<![\w\-])(?:ООО|ОАО|ЗАО|ПАО|НАО|АО|ГК|ФГУП|МУП|ГУП|АНО|НКО|ТОО|ЧОП) +"
    rf"(?P<n>{CAP}(?: +{CAP}){{0,2}}(?: +(?:{_TAILS})(?![\w]))?)")
LEGAL_LATIN_BARE = re.compile(
    rf"(?<![\w\-])(?:{_LEGAL_LAT}) +(?P<n>[A-Z][\w&\-]*(?: +[A-Z][\w&\-]*){{0,2}})")
LATIN_LEGAL_TRAILING = re.compile(
    rf"(?<![\w\-])(?P<n>[A-Z][\w&\-]*(?: +[A-Z][\w&\-]*){{0,2}}),? +(?:{_LEGAL_LAT})(?![\w])")

# Внутри «ёлочек» бывают вложенные кавычки: «Ромашка "Люкс"». Закрывает такое название только »; прочие кавычки — только парные.
QUOTED = re.compile(r"«(?P<n>[^«»\n]{2,70}?)»|[“„\"](?P<m>[^«»“”„\"\n]{2,70}?)[”“\"]")

# Полное название с сокращением в скобках: «Технологии Доверия (ТеДо)».
ABBREVIATION = re.compile(rf"(?P<full>{CAP}(?: +{CAP}){{1,4}}) *\((?P<abbr>[A-ZА-ЯЁ][\w\-]{{1,11}})\)")

CITY_INTRO = re.compile(
    rf"(?<![\w.])(?:г\. ?|гор\. ?|город(?:а|е|у|ом)? +|пос\. ?|поселок +|посёлок +|пгт +|п/ст +|"
    rf"п\. ?|станица +|ст-ца +|хутор +|село +|деревня +|ОП +)"
    rf"(?P<n>[А-ЯЁ][а-яё]+(?:-[А-ЯЁа-яё]+){{0,3}}(?: +[А-ЯЁ][а-яё]+)?)")
REGION_ADJ_HEAD = re.compile(
    r"(?<![\w])(?P<n>[А-ЯЁ][а-яё]+(?:-[а-яё]+)?(?:ской|ская|ский|ского|ском|скую|ским|цкой|цкая|цкий|ной|ная|ный|"
    r"овой|овая|овый|инской|инская|инский)) +(?:обл\b\.?|области|областью|область|край|края|краю|республика|"
    r"республики|республике|округ|округа|округе|район|района|районе)")
REPUBLIC = re.compile(r"(?<![\w])Республика +(?P<n>[А-ЯЁ][а-яё]+(?:-[А-ЯЁа-яё]+)?(?: +[А-ЯЁ][а-яё]+)?)")
BRANCH_ADJ = re.compile(
    rf"(?<![\w])(?P<n>[А-ЯЁ][а-яё]+(?:-[А-ЯЁа-яё]+)?(?:ский|ой|ий|ый|ая|ое|ского|ской|ском|ской)) +"
    rf"(?:{'|'.join(map(re.escape, sorted(geo.BRANCH_HEADS, key=len, reverse=True)))})(?![\w])", re.I)

EMAIL_DOMAIN = re.compile(r"@(?P<d>[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9\-]+)+)")
_TLDS = ("ru su рф com net org io info biz eu de uk cn kz by ua us co tech ai app dev online store shop pro "
         "me tv cc pl fr it es nl ch at cz jp kr in br tr ae sg hk xyz cloud site ltd group world global").split()
DOMAIN = re.compile(
    rf"(?<![\w@.\-])(?:(?:https?://)?(?:www\.)?)(?P<d>(?:[A-Za-z0-9][A-Za-z0-9\-]*\.)+(?:{'|'.join(_TLDS)}))(?![\w\-])",
    re.I)

URL_TAIL = re.compile(r"[/?#][^\s<>\"'«»]+")

FILE_EXTENSIONS = ("xlsx", "xlsm", "xls", "docx", "docm", "doc", "pptx", "pptm", "ppt", "pdf", "csv", "txt", "md",
                   "json", "xml", "zip", "rar", "7z", "png", "jpg", "jpeg", "msg", "eml", "odt", "ods", "odp", "rtf")
FILE_NAME = re.compile(
    rf"(?<![\w.\-/\\])(?P<n>[\w](?:[\w \-.,()№&+]{{0,90}}?[\w)])?)(?P<e>\.(?:{'|'.join(FILE_EXTENSIONS)}))(?![\w])", re.I)
FILE_KEYWORD = re.compile(r"(?i)(?:файл\w*|вложени\w+|приложени\w+|документ\w*|таблиц\w+|презентаци\w+|file|attachment|attached)\s+")
NAME_CONNECTORS = frozenset("и в на для по от с о к из за".split())

# Падежные окончания единственного числа: у названия из одного слова они не меняют его сути.
ENDINGS = ("а", "у", "е", "ы", "и", "ом", "ем", "ой", "ей", "ам", "ов", "ах", "ами", "ою", "ею", "я", "ю")
# Название-прилагательное («Трехсосенский», «Северная»): основа без окончания и падежные окончания прилагательных.
ADJECTIVE_CORE = re.compile(r"^.{3,}(?:ский|цкий|ный|ний|ой|ый|ий|ая|яя|ое|ее)$", re.I)
ADJECTIVE_ENDINGS = ("ий", "ый", "ой", "ого", "его", "ому", "ему", "им", "ым", "ом", "ем", "ая", "яя", "ей", "ую", "юю",
                     "ое", "ее", "ые", "ие", "ых", "их", "ими", "ыми")
SPACE = re.compile(r"\s+")

# Сокращения, которые в деловом тексте встречаются постоянно и названием компании не бывают.
LATIN_COUNTRIES = frozenset("""china russia usa america europe asia africa germany france italy spain japan korea india brazil
turkey canada mexico vietnam thailand indonesia kazakhstan belarus ukraine poland england britain london paris berlin
moscow""".split())

ACRONYM_STOP = frozenset(fold(w) for w in """
ООО АО ПАО ЗАО НДС ИНН КПП ОГРН ФИО ФЗ ГК РФ США ЕС ООН СНГ ИТ КПЭ КПЭ ЕБИТДА ТЗ ТК ЖКХ ГОСТ СНИП ТУ ПО ОС НМА ОЗ
ДЗ КЗ БДР БДДС ОПУ ОПР ДДС ЕРП ЦФО НП ТМЦ ТСД ЗУП ДА НЕТ ВСЕГО ИТОГО ПРОЧЕЕ ФОТ ЗП ДМС ОМС ПФР ФСС ФНС ЕГРЮЛ ЕГРИП ИП
ДОГОВОР ПРИЛОЖЕНИЕ ФИЛИАЛ ОТДЕЛ ЦЕХ СКЛАД ГЭС ТЭС ТЭЦ АЭС ФО МСК СПБ СПБ ЛС КВ ДВФ СЗФ
KPI ERP CRM IT PDF XML CSV API SLA OKR NPV IRR ROI ROE ROA CAPEX OPEX EBITDA WACC FMCG B2B B2C SKU MVP
""".split())


@dataclass(slots=True)
class Entity:
    kind: str
    key: str
    forms: list[str] = field(default_factory=list)
    explicit: bool = True
    ordinary: bool = False      # ядро совпадает с обиходным словом: сравниваем с учётом регистра

    def add(self, form: str) -> None:
        if form not in self.forms:
            self.forms.append(form)


@dataclass(slots=True)
class Hit:
    start: int
    end: int
    category: str
    key: str
    reason: str
    confidence: float
    priority: int
    decision: str = "AUTO"


def entity_key(core: str) -> str:
    return SPACE.sub(" ", fold(core.strip(" \t«»“”„\"'.,;:"))).strip()


@lru_cache(maxsize=32768)
def _lemma(word: str) -> str:
    """Начальная форма слова по самому вероятному разбору: «Технологий» → «технология»."""
    morph = lexicon.analyzer()
    if morph is None or word.isascii():
        return fold(word)
    try:
        return fold(morph.parse(word)[0].normal_form)
    except Exception:
        return fold(word)


@lru_cache(maxsize=32768)
def _lemmas(word: str) -> tuple[str, ...]:
    """Начальные формы слова по словарю: «Красноярске» → «красноярск». Пусто, если словаря нет."""
    morph = lexicon.analyzer()
    if morph is None:
        return ()
    try:
        parses = morph.parse(word)[:4]
    except Exception:
        return ()
    # Только разборы, помеченные словарём как географические: «Курганов» — фамилия, а не форма города Курган.
    return tuple(dict.fromkeys(fold(p.normal_form) for p in parses if "Geox" in p.tag))


def _ordinary(core: str) -> bool:
    """Ядро названия — обиходное слово: «Простор», «Успех». Такие названия ищем с учётом регистра."""
    words = WORD.findall(core)
    if len(words) != 1:
        return False
    shape = lexicon.shape(words[0])
    return shape.lexical and shape.known


def _quoted_span(m: "re.Match[str]") -> tuple[int, int]:
    """Границы названия внутри кавычек: у шаблонов группы называются n, m или k — по виду кавычек."""
    for name in ("n", "m", "k"):
        if name in m.re.groupindex and m.group(name) is not None:
            return m.span(name)
    return m.span()


def _proper_looking(core: str) -> bool:
    """Название без контекста: похоже ли оно на имя собственное, а не на заголовок или оборот речи."""
    words = WORD.findall(core)
    if not words:
        return False
    if any(any(ch.isdigit() for ch in w) for w in words):
        return True
    for word in words:
        if re.search(r"[A-Za-z]", word):
            return True
        if len(word) >= 3 and word.isupper():
            return True
        if "-" in word and all(len(part) >= 3 for part in word.split("-")):
            return True
        if any(ch.isupper() for ch in word[1:]):
            return True          # ГидроВолга, ТеДо
        shape = lexicon.shape(word)
        if lexicon.available() and not shape.known:
            return True          # слова нет в словаре: скорее название, чем слово
    return False


NUMBER_FORMAT = re.compile(r"[0#?.,%$€₽\-+/:\s_*()\[\]]*|[dDmMyYhHsSeE]+(?:[.,:/\-\s\[\]]+[dDmMyYhHsSeE]+)+|[ДМГЧСдмгчс]+(?:[.,:/\-\s]+[ДМГЧСдмгчс]+)+")


def _looks_like_number_format(core: str) -> bool:
    """Строка формата, а не название: `0.00`, `0,0%`, `#,##0`, `dd.mm.yyyy`, `ДД.ММ.ГГГГ`."""
    return bool(NUMBER_FORMAT.fullmatch(core)) and bool(re.search(r"[0#dDmMyYhHsSеЕДМГЧС]", core))


def _looks_like_sentence(core: str) -> bool:
    words = WORD.findall(core)
    return len(words) > 6 or bool(re.search(r"[.!?;:]\s|[.!?]$", core)) or core.islower()


class EntityRecognizer:
    def __init__(self, settings=None, hide_terms: list[str] | None = None, keep_terms: list[str] | None = None):
        self.settings = settings
        self.entities: dict[str, Entity] = {}           # key -> Entity
        self._by_first: dict[str, list[tuple[Entity, re.Pattern[str], int]]] = {}
        self._by_stem: dict[str, list[Entity]] = {}
        self._by_adj_stem: dict[str, list[Entity]] = {}
        self._by_lemmas: dict[tuple[str, ...], Entity] = {}
        self._max_words = 1
        self._domains: dict[str, Entity] = {}
        self.keep = {fold(t) for t in (keep_terms or []) if t.strip()}
        self.suggestions: dict[str, dict] = {}
        self._pending_alias: dict[str, str] = {}
        if self._on("organizations"):
            for name in sorted(geo.KNOWN_ORGS):
                original = self._original_spelling(name)
                self.register("ORG", original, explicit=False, key=name)
        for term in hide_terms or []:
            self.register("TERM", term.strip(), explicit=True)

    @staticmethod
    def _original_spelling(folded: str) -> str:
        """Справочник хранит свёрнутые названия; ищем их независимо от регистра, а первым написанием берём заглавное."""
        return folded[:1].upper() + folded[1:]

    # -- настройки ------------------------------------------------------------

    def _on(self, name: str) -> bool:
        return True if self.settings is None else bool(getattr(self.settings, name, True))

    # -- регистрация ----------------------------------------------------------

    def seed(self, kind: str, key: str, spellings: list[str]) -> None:
        """Знания из хранилища: объект, замеченный в другом документе, ищется и здесь."""
        for form in spellings[:1] or [key]:
            self.register(kind, form, explicit=True, key=key)
        for form in spellings[1:]:
            self.register(kind, form, explicit=True, key=key)

    def register(self, kind: str, core: str, *, explicit: bool = True, key: str | None = None) -> Entity | None:
        core = core.strip(" \t«»“”„\"'")
        if len(core) < 2:
            return None
        key = key or entity_key(core)
        if not key or key in self.keep or fold(core) in self.keep:
            return None
        if kind != "TERM" and (fold(core) in geo.COMMON_SOFTWARE or fold(core) in geo.COUNTRIES):
            return None
        entity = self.entities.get(key)
        if entity is None:
            entity = Entity(kind, key, [], explicit, _ordinary(core))
            self.entities[key] = entity
        if core in entity.forms:
            return entity
        entity.add(core)
        self._index(entity, core)
        return entity

    def _index(self, entity: Entity, form: str) -> None:
        words = WORD.findall(form)
        if not words:
            return
        # Слова формы разделяет что угодно, кроме букв и цифр: пробел, тире, «&».
        pattern = re.compile(r"(?<![\w])" + r"[^\w]{1,4}".join(map(_word_pattern, words)) + r"(?![\w])", re.I)
        bucket = self._by_first.setdefault(fold(words[0]), [])
        bucket.append((entity, pattern, len(form)))
        bucket.sort(key=lambda item: -item[2])     # длинное название раньше короткого: «Аврора-3» до «Аврора»
        if 2 <= len(words) <= 5 and all("\u0400" <= w[0] <= "\u04ff" for w in words) and entity.kind != "TERM":
            self._by_lemmas[tuple(_lemma(w) for w in words)] = entity
            self._max_words = max(self._max_words, len(words))
        if len(words) == 1 and self._may_inflect(form, entity):
            if ADJECTIVE_CORE.match(form):
                stem = fold(form)[:-2] if fold(form).endswith(("ая", "яя", "ое", "ее", "ий", "ый", "ой")) else fold(form)
                bucket = self._by_adj_stem.setdefault(stem, [])
            else:
                bucket = self._by_stem.setdefault(_stem_of(fold(form)), [])
            if entity not in bucket:
                bucket.append(entity)

    @staticmethod
    def _may_inflect(form: str, entity: Entity) -> bool:
        return (len(form) >= 5 and (not entity.ordinary or form[0].isupper()) and "Ѐ" <= form[0] <= "ӿ"
                and "-" not in form and entity.kind in {"ORG", "PROJECT", "CITY", "REGION", "TERM"})

    # -- явные признаки -------------------------------------------------------

    def explicit_spans(self, text: str) -> list[tuple[int, int, str, str, str]]:
        """(начало, конец, вид, ядро названия, причина) по явным признакам в тексте."""
        out: list[tuple[int, int, str, str, str]] = []
        if self._on("organizations"):
            for pattern, reason in ((LEGAL_QUOTED, "Организация с юридической формой"),
                                    (LEGAL_LONG_QUOTED, "Организация с юридической формой"),
                                    (LEGAL_LATIN_QUOTED, "Организация с юридической формой"),
                                    (LEGAL_BARE, "Организация с юридической формой"),
                                    (LEGAL_LATIN_BARE, "Организация с юридической формой"),
                                    (LEGAL_RU_TRAILING, "Организация с юридической формой"),
                                    (LATIN_LEGAL_TRAILING, "Организация с юридической формой")):
                for m in pattern.finditer(text):
                    span = _quoted_span(m)
                    core = text[span[0]:span[1]]
                    if core.strip() and fold(core) not in geo.QUOTED_NON_NAMES \
                            and not (TOKEN_RE.fullmatch(core.strip()) or EMAIL_TOKEN_RE.fullmatch(core.strip())):
                        out.append((span[0], span[1], "ORG", core, reason))
            out.extend(self._quoted(text))
            out.extend(self._abbreviations(text))
        if self._on("geo"):
            out.extend(self._geo_intros(text))
        return out

    def _quoted(self, text: str) -> list[tuple[int, int, str, str, str]]:
        out = []
        for m in QUOTED.finditer(text):
            start, end = _quoted_span(m)
            core = text[start:end]
            stripped = core.strip()
            if not stripped:
                continue
            lead = len(core) - len(core.lstrip())
            start += lead
            end = start + len(stripped)
            core = stripped
            if _looks_like_sentence(core) or fold(core) in geo.QUOTED_NON_NAMES or fold(core) in geo.COMMON_SOFTWARE:
                continue
            if TOKEN_RE.fullmatch(core) or EMAIL_TOKEN_RE.fullmatch(core):
                continue          # уже обезличенное: Company1 в кавычках — метка, а не новое название
            if _looks_like_number_format(core) or re.fullmatch(r"[\d.,:/\-\s%]+|(?i:utf|iso|win|cp)[-_ ]?\d+|v?\d+(?:\.\d+)+", core):
                continue          # «0.00», «ДД.ММ.ГГГГ», «1.0», «32687», «UTF-16» — формат, версия или число, а не название
            if not (core[0].isupper() or core[0].isdigit()):
                continue
            before = WORD.findall(text[max(0, m.start() - 48):m.start()])
            prev = fold(before[-1]) if before else ""
            prev2 = fold(before[-2]) if len(before) > 1 else ""
            kind = ""
            if prev in geo.PROJECT_CONTEXT or (prev in {"название", "названием", "именем"} and prev2 in geo.PROJECT_CONTEXT):
                kind, reason = "PROJECT", "Название проекта или объекта"
            elif prev in geo.ORG_CONTEXT or prev2 in geo.ORG_CONTEXT and prev in {"под", "названием", "именем"}:
                kind, reason = "ORG", "Название организации"
            elif _proper_looking(core):
                kind, reason = "ORG", "Название в кавычках"
            if kind:
                out.append((start, end, kind, core, reason))
        return out

    def _abbreviations(self, text: str) -> list[tuple[int, int, str, str, str]]:
        out = []
        for m in ABBREVIATION.finditer(text):
            full, abbr = m.group("full"), m.group("abbr")
            if fold(abbr) in geo.COMMON_SOFTWARE or fold(abbr) in geo.COUNTRIES or fold(full) in geo.COUNTRIES:
                continue
            if abbr[0].casefold() != full[0].casefold():
                continue
            words = full.split()
            if any(fold(w) in geo.QUOTED_NON_NAMES for w in words):
                continue
            # Определение вида «Полное название (АББР)» уверенно только для названий, а не для
            # обычных словосочетаний: нужен признак имени собственного хотя бы в одном из слов.
            if not (_proper_looking(full) or (abbr.isupper() is False and any(c.isupper() for c in abbr[1:])) or
                    re.match(r"(?i)(?:компани|групп|холдинг|корпораци)", text[max(0, m.start() - 12):m.start()].strip()[-9:] or "")):
                continue
            out.append((m.start("full"), m.end("full"), "ORG", full, "Название организации с сокращением"))
            out.append((m.start("abbr"), m.end("abbr"), "ORG", abbr, "Сокращение названия организации"))
            # Оба написания — один объект: сокращение регистрируется как вариант полного названия.
            self._pending_alias[abbr] = full
        return out

    def _geo_intros(self, text: str) -> list[tuple[int, int, str, str, str]]:
        out = []
        for m in CITY_INTRO.finditer(text):
            start, end = m.span("n")
            name = m.group("n")
            intro = text[m.start():start].strip().lower()
            # «2025 г. Выручка» — год, а не город; «см. п. Условия оплаты» — пункт договора, а не посёлок.
            if intro.startswith("г") and re.search(r"\d\s*$", text[:m.start()]):
                continue
            if intro in {"п.", "п"} and self._common_word(name.split()[0]):
                continue
            words = name.split()
            # Второе слово берётся, только если это «Нижний Новгород»: прилагательное перед существительным.
            if len(words) == 2 and not re.search(r"(?:ий|ый|ая|яя|ое)$", words[0]) and \
                    fold(name) not in geo.RU_CITIES:
                end = start + len(words[0])
                name = words[0]
            if fold(name) in geo.COUNTRIES:
                continue
            out.append((start, end, "CITY", name, "Населённый пункт"))
        for m in REGION_ADJ_HEAD.finditer(text):
            out.append((m.start("n"), m.end("n"), "REGION", m.group("n"), "Регион"))
        for m in REPUBLIC.finditer(text):
            name = m.group("n")
            if fold(name) in geo.COUNTRIES:
                continue
            out.append((m.start("n"), m.end("n"), "REGION", name, "Регион"))
        for m in BRANCH_ADJ.finditer(text):
            adj = m.group("n")
            if fold(adj) in geo.REGION_ADJECTIVES or self._adj_of_city(adj):
                out.append((m.start("n"), m.end("n"), "REGION", adj, "Регион в названии подразделения"))
        return out

    @staticmethod
    def _common_word(word: str) -> bool:
        """Обычное слово словаря (не название и не фамилия): после «п.» оно означает пункт, а не населённый пункт."""
        if fold(word) in geo.RU_CITIES or fold(word) in geo.WORLD_CITIES:
            return False
        if re.search(r"(?:ово|ево|ёво|ино|ыно|ское|цкое)$", fold(word)) and len(word) >= 6:
            return False           # окончание названий сёл и посёлков: Кольцово, Тальжино, Никольское
        if not lexicon.available():
            return False
        shape = lexicon.shape(word)
        return bool(shape.lexical and shape.known and not shape.surname and not shape.given)

    @staticmethod
    def _adj_of_city(adj: str) -> bool:
        """«Кемеровский», «Красноярская» → город или регион по основе слова."""
        stem = fold(adj)
        for tail in ("ского", "ской", "ском", "ский", "ская", "ое", "ий", "ый", "ой", "ая"):
            if stem.endswith(tail):
                stem = stem[:-len(tail)]
                break
        stem = re.sub(r"(?:цк|нск|ск|овск|евск|инск)$", "", stem)
        if len(stem) < 5:
            return False
        for city in geo.RU_CITIES | geo.RU_REGIONS:
            base = city.rstrip("аяоеиыью")
            if len(base) >= 5 and (base.startswith(stem) or stem.startswith(base)) and abs(len(base) - len(stem)) <= 4:
                return True
        return False

    # -- обучение -------------------------------------------------------------

    def learn(self, text: str) -> int:
        """Запомнить названия, введённые в тексте явно, — чтобы найти их упоминания везде."""
        self._pending_alias = {}
        spans = self.explicit_spans(text)
        added = self._learn_spans(spans)
        if self._on("domains"):
            for m in EMAIL_DOMAIN.finditer(text):
                self._register_domain(m.group("d"))
        return added

    def _learn_spans(self, spans) -> int:
        added = 0
        for start, end, kind, core, _reason in spans:
            before = len(self.entities)
            entity = self.register(kind, core)
            added += len(self.entities) - before
            if entity is not None and kind == "ORG":
                self._alias_from_tail(entity, core)
        for abbr, full in self._pending_alias.items():
            full_entity = self.entities.get(entity_key(full))
            abbr_entity = self.entities.get(entity_key(abbr))
            if full_entity and abbr_entity and abbr_entity is not full_entity:
                # Сокращение становится написанием полного названия, а не отдельной компанией.
                self._merge(abbr_entity, full_entity)
        return added

    def _alias_from_tail(self, entity: Entity, core: str) -> None:
        """«Сумитек интернейшнл» → отдельно «Сумитек» тоже относится к той же компании."""
        words = core.split()
        if len(words) >= 2 and fold(words[-1]) in geo.ORG_NAME_TAILS:
            head = " ".join(words[:-1])
            if head and fold(head) not in geo.COMMON_SOFTWARE and not _ordinary(head):
                if head not in entity.forms:
                    entity.add(head)
                    self._index(entity, head)

    def _merge(self, source: Entity, target: Entity) -> None:
        for form in source.forms:
            target.add(form)
            self._index(target, form)
        self.entities.pop(source.key, None)
        for bucket in self._by_first.values():
            bucket[:] = [item for item in bucket if item[0] is not source]
        for bucket in self._by_stem.values():
            bucket[:] = [e for e in bucket if e is not source]

    def _register_domain(self, domain: str) -> None:
        domain = domain.lower()
        if fold(domain) in geo.PUBLIC_MAIL_DOMAINS:
            return
        parts = domain.split(".")
        # Поддомены mail.sumitec.ru и www.sumitec.ru — тот же объект.
        root = ".".join(parts[-2:]) if len(parts) > 2 and parts[-2] not in {"co", "com", "org", "net"} else domain
        if fold(root) in geo.PUBLIC_MAIL_DOMAINS or fold(root) in geo.INFRASTRUCTURE_DOMAINS:
            return
        entity = self.register("DOMAIN", root)
        if entity is not None:
            self._domains[root] = entity

    # -- поиск ----------------------------------------------------------------

    def find(self, text: str) -> list[Hit]:
        if not text.strip():
            return []
        self._pending_alias = {}
        spans = self.explicit_spans(text)
        self._learn_spans(spans)
        if self._on("domains") and "@" in text:
            for m in EMAIL_DOMAIN.finditer(text):
                self._register_domain(m.group("d"))
        hits: list[Hit] = []
        for start, end, kind, core, reason in spans:
            entity = self.entities.get(entity_key(core))
            if entity is None:
                # Название не принято к учёту (например, стоит в списке «не скрывать»).
                continue
            geo_kind = entity.kind in {"CITY", "REGION"}
            hits.append(Hit(start, end, entity.kind, entity.key, reason, .96, P_GEO_EXPLICIT if geo_kind else P_EXPLICIT))
        hits.extend(self._known_hits(text))
        if self._on("geo"):
            hits.extend(self._gazetteer_hits(text))
        if self._on("domains"):
            hits.extend(self._domain_hits(text))
        if self._on("filenames"):
            hits.extend(self._file_hits(text))
        return [h for h in hits if fold(text[h.start:h.end]) not in self.keep]

    STOP_CANDIDATES = geo.COMMON_SOFTWARE | geo.QUOTED_NON_NAMES

    def suggest_hits(self, text: str, covered: list[tuple[int, int]] | None = None) -> list[Hit]:
        """Слова, похожие на имя собственное, которые программа не заменила: пусть человек решит сам.

        Не в начале предложения, с заглавной буквы, не обиходные слова. Аббревиатуры тоже попадают сюда:
        «ОПХ» и «МПК» могут быть названиями компаний, а могут — сокращениями из отраслевого языка.
        """
        from . import morphology as mo
        out: list[Hit] = []
        cyrillic_unit = bool(re.search(r"[А-Яа-яЁё]", text))
        for cue, reason in ((ORG_CUE, "Названа после слова «клиент», «компания»: возможно, название компании"),
                            (PROJECT_CUE, "Названо после слова «проект»: возможно, название проекта")):
            for m in cue.finditer(text):
                name = m.group("n")
                first = name.split()[0]
                if fold(first) in self.keep or fold(name) in self.keep or fold(first) in self.STOP_CANDIDATES \
                        or fold(first) in geo.COUNTRIES or first.isdigit() or fold(first) in geo.LEGAL_FORMS:
                    continue
                if any(fold(h) in self.entities for h in (name, first)):
                    continue
                out.append(Hit(m.start("n"), m.end("n"), "POSSIBLE_ENTITY", entity_key(name), reason, .5, 410, "REVIEW"))
        for m in WORD.finditer(text):
            word = m.group(0)
            if len(word) < 3 or not word[0].isupper() or any(ch.isdigit() for ch in word):
                continue
            head = text[:m.start()].rstrip()
            if not head or head[-1] in ".!?…:;\n•·*" or head[-1] in "–—-" and len(head) < 3:
                continue
            low = fold(word)
            if low in self.STOP_CANDIDATES or low in self.keep or low in self.entities or low in geo.COUNTRIES:
                continue
            latin = word.isascii()
            if latin and not cyrillic_unit:
                continue
            if not latin and (mo.is_stop_word(word) or mo.is_given_name(word)):
                continue
            if not latin and any(lemma in geo.COUNTRIES or lemma in geo.RU_CITIES or lemma in geo.WORLD_CITIES
                                 for lemma in _lemmas(word)):
                continue
            if low in LATIN_COUNTRIES:
                continue
            if word.isupper():
                if not 3 <= len(word) <= 7 or low in ACRONYM_STOP:
                    continue
                if not latin:
                    ordinary = lexicon.shape(word.capitalize())
                    if ordinary.lexical and ordinary.known and not ordinary.surname:
                        continue
                reason = "Аббревиатура: возможно, название компании"
            else:
                if latin:
                    if low in {"the", "and", "for"} or word[1:].isupper() is False and not re.search(r"[a-z]", word[1:]):
                        continue
                else:
                    shape = lexicon.shape(word)
                    if shape.lexical and shape.known and not shape.surname:
                        continue
                    if lexicon.is_common_word(word):
                        continue
                    if shape.given and not shape.surname:
                        continue
                reason = "Похоже на название или фамилию"
            out.append(Hit(m.start(), m.end(), "POSSIBLE_ENTITY", entity_key(word), reason, .45, 400, "REVIEW"))
        return out

    def _known_hits(self, text: str) -> list[Hit]:
        if not self.entities:
            return []
        out: list[Hit] = []
        tokens = list(WORD.finditer(text))
        position = 0
        for index, m in enumerate(tokens):
            if m.start() < position:
                continue
            token = m.group(0)
            low = fold(token)
            matched = False
            for start_at, key in self._starts(token, m.start()):
                for entity, pattern, _ in self._by_first.get(key, ()):
                    found = pattern.match(text, start_at)
                    if found and self._case_ok(entity, found.group(0)):
                        end = found.end()
                        if entity.kind == "DOMAIN":
                            tail = URL_TAIL.match(text, end)      # путь и параметры ссылки на известный домен — тоже часть адреса
                            if tail:
                                end += len(tail.group(0).rstrip(".,;:!?)]}»\"'"))
                        out.append(Hit(found.start(), end, entity.kind, entity.key,
                                       "Уже встречалось в документе", .95, self._known_priority(entity)))
                        position = end
                        matched = True
                        break
                if matched:
                    break
            if matched:
                continue
            if self._by_lemmas and token[:1].isupper():
                hit = self._lemma_phrase(text, tokens, index)
                if hit is not None:
                    out.append(hit)
                    position = hit.end
                    continue
            if (self._by_stem or self._by_adj_stem) and token[:1].isupper():
                entity = self._inflected(low)
                if entity is not None:
                    out.append(Hit(m.start(), m.end(), entity.kind, entity.key, "Другая форма известного названия",
                                   .92, self._known_priority(entity)))
        return out

    @staticmethod
    def _starts(token: str, offset: int):
        """Где в слове может начинаться известное название: слово целиком, затем каждая часть между дефисами.

        «ex-PwC» — это PwC с приставкой, а «PwC-консультант» — PwC с определением: часть слова должна найтись так же,
        как отдельное слово.
        """
        yield offset, fold(token)
        if "-" not in token:
            return
        position = 0
        for part in token.split("-"):
            if len(part) >= 2:
                yield offset + position, fold(part)
            position += len(part) + 1

    def _lemma_phrase(self, text: str, tokens: list, index: int) -> Hit | None:
        """«Технологий Доверия» — то же название, что «Технологии Доверия», только в другом падеже."""
        for size in range(min(self._max_words, len(tokens) - index), 1, -1):
            window = tokens[index:index + size]
            if any(text[a.end():b.start()].strip() for a, b in zip(window, window[1:])):
                continue
            if not all(w.group(0)[:1].isupper() for w in window[:1]):
                continue
            entity = self._by_lemmas.get(tuple(_lemma(w.group(0)) for w in window))
            if entity is not None:
                return Hit(window[0].start(), window[-1].end(), entity.kind, entity.key,
                           "Другая форма известного названия", .92, self._known_priority(entity))
        return None

    @staticmethod
    def _known_priority(entity: Entity) -> int:
        if entity.kind == "TERM":
            return P_TERM
        return P_GEO_KNOWN if entity.kind in {"CITY", "REGION"} else P_KNOWN

    @staticmethod
    def _case_ok(entity: Entity, surface: str) -> bool:
        if entity.ordinary:
            return surface in entity.forms
        return True

    def _inflected(self, low: str) -> Entity | None:
        for ending in ENDINGS:
            if low.endswith(ending) and len(low) - len(ending) >= 4:
                for entity in self._by_stem.get(low[:-len(ending)], ()):
                    return entity
        for ending in ADJECTIVE_ENDINGS:
            if low.endswith(ending) and len(low) - len(ending) >= 4:
                for entity in self._by_adj_stem.get(low[:-len(ending)], ()):
                    return entity
        return None

    def _gazetteer_hits(self, text: str) -> list[Hit]:
        out: list[Hit] = []
        tokens = list(WORD.finditer(text))
        i = 0
        while i < len(tokens):
            token = tokens[i]
            word = token.group(0)
            if not word[:1].isupper() and not word.isupper():
                i += 1
                continue
            # Двусловные названия: «Нижний Новгород», «Набережные Челны», «Дальний Восток».
            if i + 1 < len(tokens) and text[token.end():tokens[i + 1].start()].strip() == "" \
                    and tokens[i + 1].group(0)[:1].isupper():
                pair = self._geo_lookup(f"{word} {tokens[i + 1].group(0)}", pair=True)
                if pair:
                    out.append(Hit(token.start(), tokens[i + 1].end(), pair[0], pair[1], "Город или регион", .93, P_GEO))
                    i += 2
                    continue
            found = self._geo_lookup(word)
            if found:
                before = text[max(0, token.start() - 6):token.start()].lower()
                marked = bool(re.search(r"(?:г\.|город|пос\.|п/ст)\s*$", before))
                if fold(found[1]) in geo.AMBIGUOUS_CITIES and not marked:
                    i += 1
                    continue
                out.append(Hit(token.start(), token.end(), found[0], found[1], "Город или регион", .93, P_GEO))
            i += 1
        return out

    @staticmethod
    def _geo_lookup(phrase: str, pair: bool = False) -> tuple[str, str] | None:
        low = fold(phrase)
        if low in geo.CITY_ABBREVIATIONS and not pair:
            low = geo.CITY_ABBREVIATIONS[low]
            return "CITY", low
        if low in geo.COUNTRIES:
            return None
        for table, kind in ((geo.RU_CITIES, "CITY"), (geo.WORLD_CITIES, "CITY"), (geo.RU_REGIONS, "REGION")):
            if low in table:
                return kind, low
        if phrase.isascii() and low in geo.LATIN_CITIES:
            return "CITY", low
        if not phrase.isascii():
            candidates = []
            if pair:
                candidates = [" ".join(x) for x in _pair_lemmas(phrase)]
            else:
                candidates = list(_lemmas(phrase))
            for lemma in candidates:
                if lemma in geo.COUNTRIES:
                    return None
                for table, kind in ((geo.RU_CITIES, "CITY"), (geo.WORLD_CITIES, "CITY"), (geo.RU_REGIONS, "REGION")):
                    if lemma in table:
                        return kind, lemma
        return None

    def _domain_hits(self, text: str) -> list[Hit]:
        out = []
        for m in DOMAIN.finditer(text):
            domain = m.group("d").lower()
            parts = domain.split(".")
            root = ".".join(parts[-2:]) if len(parts) > 2 and parts[-2] not in {"co", "com", "org", "net"} else domain
            if fold(root) in geo.PUBLIC_MAIL_DOMAINS or fold(domain) in geo.PUBLIC_MAIL_DOMAINS:
                continue
            if fold(root) in geo.INFRASTRUCTURE_DOMAINS:
                continue
            # Пропускаем «www.» и поддомен: в файл возвращается тот же корень.
            if fold(root) in geo.COMMON_SOFTWARE:
                continue
            start = m.start("d") + len(domain) - len(root)
            end = m.end("d")
            # Путь и параметры называют человека или клиента не реже, чем сам домен: «/users/ivan-ivanov», «?u=ryabinin».
            tail = URL_TAIL.match(text, end)
            if tail:
                end += len(tail.group(0).rstrip(".,;:!?)]}»\"'"))
            out.append(Hit(start, end, "DOMAIN", root, "Адрес сайта", .9, P_DOMAIN))
        return out

    def _file_hits(self, text: str) -> list[Hit]:
        out = []
        for m in FILE_NAME.finditer(text):
            name, start = m.group("n"), m.start("n")
            whole_unit = text.strip() == name + m.group("e")
            if not whole_unit:
                keyword = None
                for keyword in FILE_KEYWORD.finditer(name):
                    pass
                if keyword is not None and keyword.end() < len(name):
                    start += keyword.end()
                    name = name[keyword.end():]
                else:
                    offset = self._name_run_start(name)
                    start += offset
                    name = name[offset:]
            name = name.strip(" .,")
            if len(name) < 2 or not re.search(r"\w", name):
                continue
            start = text.index(name, start) if name in text[start:] else start
            out.append(Hit(start, start + len(name), "FILE", entity_key(name), "Имя файла", .85, P_FILE))
        return out

    @staticmethod
    def _name_run_start(name: str) -> int:
        """Где в цепочке слов перед расширением начинается имя файла, а не предложение вокруг него.

        Имя — это последний непрерывный ряд слов с заглавной буквы, цифрами или знаками файловых имён;
        между ними допускаются короткие служебные слова («Отчёт по продажам»)."""
        words = list(re.finditer(r"\S+", name))
        if not words:
            return 0

        def namelike(word: str) -> bool:
            return word[:1].isupper() or any(ch.isdigit() or ch in "_-.()" for ch in word)

        first = len(words) - 1
        if not namelike(words[first].group(0)) and " " in name:
            # Имя из строчных слов без единого признака («report final») — отдельное слово, а не предложение.
            return words[first].start() if len(words) > 1 else 0
        index = first
        while index > 0:
            previous = words[index - 1].group(0)
            if namelike(previous):
                index -= 1
            elif previous.lower() in NAME_CONNECTORS and index > 1 and namelike(words[index - 2].group(0)):
                index -= 2
            else:
                break
        return words[index].start()


def _word_pattern(word: str) -> str:
    """Слово как регулярное выражение, безразличное к «е» и «ё»."""
    out = []
    for ch in word:
        if ch in "её":
            out.append("[её]")
        elif ch in "ЕЁ":
            out.append("[ЕЁ]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def _stem_of(low: str) -> str:
    return low[:-1] if low and low[-1] in "аяоеиыьйю" and len(low) >= 5 else low


@lru_cache(maxsize=4096)
def _pair_lemmas(phrase: str) -> tuple[tuple[str, ...], ...]:
    """Двусловные названия: прилагательное («Нижний») не помечено как географическое, существительное — да."""
    first, second = phrase.split(" ", 1)
    return tuple((a, b) for a in {_lemma(first), fold(first)} for b in (_lemmas(second) or (fold(second),)))
