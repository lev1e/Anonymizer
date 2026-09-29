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
# Адрес сайта целиком выше известного названия: в `www.site.com/press/brand-news` название внутри ссылки не должно
# разрезать адрес и оставить домен открытым. Ниже email (960): домен почты — часть адреса.
P_DOMAIN = 945
P_FILE = 780
P_REPEATED = 660         # строгий режим: повторяющееся несловарное название
P_GEO = 650
P_GEO_KNOWN = 680
P_GEO_EXPLICIT = 690     # ниже адреса (700): город внутри адреса не должен дробить адрес на части
P_COUNTRY = 650          # ниже компании: «Россия» внутри названия компании заменяется вместе с названием

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

# Слово, после которого несловарное слово с заглавной — название сервиса или компании: «портал ELMA», «вендор Грантек».
SERVICE_CUE = re.compile(
    r"(?<![\w])(?P<c>(?i:портал\w{0,2}|сервис\w{0,2}|платформ\w{1,2}|вендор\w{0,2}|поставщик\w{0,2}|подрядчик\w{0,2}))"
    r"[ \t]+(?P<n>[A-ZА-ЯЁ][\w\-]*(?: [A-Z][\w\-]*)?)")     # без перевода строки: соседняя ячейка — другой текст
# Название с родовым словом в конце: «Сумитек Групп», «Вектрон Холдинг», «Ангарский Грантек Завод».
ORG_TRAILING_NOUN = re.compile(
    r"(?<![\w\-])(?P<n>(?:[A-ZА-ЯЁ][\w\-]* +){1,3}(?:Компани[яиюей]|Корпораци[яиюей]|Групп[аыеуой]?|Холдинг\w{0,2}|"
    r"Завод\w{0,2}|Банк\w{0,2}|Комбинат\w{0,2}|Объединени[еяю]\w{0,2}))(?![\w\-])")
ORG_NOUN_PREFIXES = ("компани", "корпораци", "групп", "холдинг", "завод", "банк", "комбинат", "объединени")

# Разделители пунктов перечня: запятая, точка с запятой, черта, скобки, двоеточие, союз «и».
SIBLING_SPLIT = re.compile(r"\s*(?:[,;|/:()]|\s(?:и|and|&)\s)\s*")
# «ex-» после нормализации смешанных алфавитов может стать кириллическим «ех-».
_ITEM_PREFIX = re.compile(r"^(?:ex-|ех-|экс-|бывш\.\s*|(?:" + _LEGAL_RU + r")\s+)", re.I)

CITY_INTRO = re.compile(
    rf"(?<![\w.])(?<!т\. )(?:г\. ?|гор\. ?|город(?:а|е|у|ом)? +|пос\. ?|пос[её]л(?:ок|ка|ке|ком) +|пгт\.? ?|рп\.? ?|"
    rf"п/ст +|п\. ?|с\. ?|д\. ?|дер\. ?|ст\. ?|станиц(?:а|ы|е|у) +|ст-ца +|хутор(?:а|е)? +|сел(?:о|а|е|ом) +|"
    rf"деревн(?:я|и|е|ю) +|аул +|ОП +|р-не +|районе +)"
    rf"(?P<n>[А-ЯЁ][а-яё]+(?:-[А-ЯЁа-яё]+){{0,3}}(?: +[А-ЯЁ][а-яё]+)?|[А-ЯЁ]{{2,}}(?:-[А-ЯЁ]{{2,}}){{0,2}}(?![\w\-]))")
# «г. Yichang», «город Wuhan»: город латиницей после явного слова «город».
LATIN_CITY_INTRO = re.compile(r"(?<![\w.])(?:г\. ?|город(?:а|е|у|ом)? +|city of +)(?P<n>[A-Z][a-z]+(?:[ \-][A-Z][a-z]+)?)(?![\w\-])")
# Слова-метки подразделения или площадки, после которых прилагательное — название места: «сервис Олекминский».
UNIT_LABEL_WORDS = frozenset("""сервис склад участок филиал поле цех оп база площадка подразделение отделение офис карьер
разрез рудник месторождение""".split())
# «Регион: Северо-Западный», «Федеральный округ — Уральский»: прилагательное-регион как значение подписи.
REGION_LABEL = re.compile(
    r"(?<![\w\-])(?i:регион|макрорегион|(?:федеральный\s+)?округ|территория|дивизион)\s*[:\-–—]?\s+"
    r"(?P<n>[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?(?:ский|ской|цкий|ный|ний|ская|ное|ная))(?![\w\-])")
# Сокращения перед названием, которые означают и другое: «п. 3» — пункт, «с.»/«ст.»/«д.» — страница, статья, дом;
# «в районе» — «около». После них берём только слово, не совпадающее с обычным словом словаря.
_AMBIGUOUS_INTROS = frozenset({"п.", "п", "с.", "с", "д.", "д", "ст.", "ст", "районе", "р-не"})
REGION_ADJ_HEAD = re.compile(
    r"(?<![\w])(?!(?:Федеральн|Автономн|Муниципальн|Городск|Сельск|Административн)\w)"
    r"(?P<n>[А-ЯЁ][а-яё]+(?:-[А-ЯЁа-яё][а-яё]+)?(?:ской|ская|ский|ского|ском|скую|ским|цкой|цкая|цкий|ной|ная|ный|"
    r"ного|ном|ному|ным|овой|овая|овый|инской|инская|инский)) +(?:федеральн\w{2,3} +)?(?:обл\b\.?|области|областью|область|"
    r"край|края|краю|крае|республика|республики|республике|округ|округа|округе|округу|ФО\b|район|района|районе|р-н\w*|"
    r"улус|улуса|улусе)")
# «р-н Беловский», «район Беловский»: вид территории перед прилагательным.
REGION_HEAD_ADJ = re.compile(
    r"(?<![\w\-])(?:р-н[а-я]*\.?|район[а-я]?|улус[а-я]?) +(?P<n>[А-ЯЁ][а-яё]+(?:ский|цкий|ского|цкого|ском|цком|ской|цкой))(?![\w])")
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
# Любой падеж прилагательного-топонима: «Беловском», «Северо-Западного», «Иркутской».
ADJECTIVE_FORM = re.compile(r"^.{2,}(?:[сц]к|н|ов|ин)(?:ий|ый|ой|ого|его|ому|ему|ом|ем|им|ым|ая|яя|ую|юю|ое|ее|ие|ые|их|ых|ей)$", re.I)
SPACE = re.compile(r"\s+")
LATIN_RUN = re.compile(r"(?<![\w\-.@/])[A-Z][A-Za-z&\-']*(?: [A-Z][A-Za-z&\-']*)+(?![\w\-])")
STRONG_SUGGESTION = .65          # подсказка, которую не стоит пропускать: сервис показывает её среди важных

# Сокращения, которые в деловом тексте встречаются постоянно и названием компании не бывают.
LATIN_COUNTRIES = frozenset("""china russia usa america europe asia africa germany france italy spain japan korea india brazil
turkey canada mexico vietnam thailand indonesia kazakhstan belarus ukraine poland england britain london paris berlin
moscow""".split())

ACRONYM_STOP = frozenset(fold(w) for w in """
ООО АО ПАО ЗАО НДС ИНН КПП ОГРН ФИО ФЗ ГК РФ США ЕС ООН СНГ ИТ КПЭ КПЭ ЕБИТДА ТЗ ТК ЖКХ ГОСТ СНИП ТУ ПО ОС НМА ОЗ
ДЗ КЗ БДР БДДС ОПУ ОПР ДДС ЕРП ЦФО НП ТМЦ ТСД ЗУП ДА НЕТ ВСЕГО ИТОГО ПРОЧЕЕ ФОТ ЗП ДМС ОМС ПФР ФСС ФНС ЕГРЮЛ ЕГРИП ИП
ДОГОВОР ПРИЛОЖЕНИЕ ФИЛИАЛ ОТДЕЛ ЦЕХ СКЛАД ГЭС ТЭС ТЭЦ АЭС ФО МСК СПБ СПБ ЛС КВ
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
        self._surface: dict[str, Entity] = {}           # написание → объект (у мест ключ — начальная форма)
        self.keep = {fold(t) for t in (keep_terms or []) if t.strip()}
        self.suggestions: dict[str, dict] = {}
        self._pending_alias: dict[str, str] = {}
        self._pending_heads: list[tuple[str, str]] = []   # (прилагательное-регион, слово после него: филиал, округ, край)
        self._label_forms: dict[str, set[str]] = {}       # «СФ», «ДВ», «СИБ» → ключи мест, которые так сокращают
        self._place_prefixes: dict[str, int] = {}         # «Сервис» в «Сервис Красноярск» → сколько раз перед местом
        self._repeated: set[str] = set()                   # слова-названия, встреченные в документе не раз
        self._lower_words: set[str] = set()                # слова, которые в документе пишут строчными
        self._pending_links: dict[str, str] = {}           # «(Хейнекен)» после компании → ключ этой компании
        self._explicit_orgs = False                        # есть ли компании, опознанные в документах явно
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
        if key is None and kind in {"CITY", "REGION"}:
            # Город и регион — один объект во всех падежах и в виде прилагательного: «Красноярске» и «Красноярск»,
            # «Иркутской» и «Иркутская», «Сибирский» и «Сибирь» получают одну метку с разными номерами написания.
            key = self._place_key(kind, core)
        key = key or entity_key(core)
        if not key or key in self.keep or fold(core) in self.keep:
            return None
        if kind != "TERM" and (fold(core) in geo.COMMON_SOFTWARE or fold(core) in geo.COUNTRIES):
            return None
        entity = self.entities.get(key)
        if entity is None:
            entity = Entity(kind, key, [], explicit, _ordinary(core))
            self.entities[key] = entity
        self._surface[fold(core)] = entity
        if entity.explicit and entity.kind == "ORG":
            self._explicit_orgs = True
        if core in entity.forms:
            return entity
        entity.add(core)
        self._index(entity, core)
        if entity.kind in {"CITY", "REGION"}:
            self._place_forms(entity, core)
        # Обиходное слово («Мост», «Простор») латиницей совпадает с английским словом: его не транслитерируем.
        if entity.explicit and not entity.ordinary and entity.kind in {"ORG", "PROJECT", "CITY"} and not core.isascii():
            for latin in transliterations(core):
                if latin not in entity.forms and fold(latin) not in self.entities:
                    entity.add(latin)
                    self._index(entity, latin, loose=True)
        return entity

    def _place_key(self, kind: str, core: str) -> str:
        """Ключ места: известный объект, справочная начальная форма или начальная форма прилагательного."""
        key = entity_key(core)
        if key in self.entities:
            return key
        words = core.split()
        adjective = len(words) == 1 and bool(ADJECTIVE_FORM.search(core)) and not core.isupper()
        # Прилагательное-регион («Олекминском районе») словарь может прочесть как падеж города: сначала прилагательное.
        if len(words) <= 2 and not (kind == "REGION" and adjective):
            found = self._geo_lookup(core, pair=len(words) == 2)
            if found:
                return found[1]
        if len(words) != 1:
            return key
        entity = self._inflected(fold(core))
        if entity is not None and entity.kind == kind:
            return entity.key
        if adjective:
            lemma = _lemma(core)
            if lemma.endswith(("ий", "ый", "ой")):
                return self._noun_of_adjective(lemma, kind) or lemma
        lemmas = _lemmas(core)
        return lemmas[0] if lemmas else key

    def _noun_of_adjective(self, lemma: str, kind: str) -> str | None:
        """«сибирский» → «сибирь», «кузбасский» → «кузбасс», «уральский» → «урал»: место того же вида, если оно известно."""
        for tail in ("ский", "цкий", "ской", "ный", "ний"):
            if lemma.endswith(tail):
                base = lemma[:-len(tail)]
                break
        else:
            return None
        if len(base) < 3:
            return None
        soft = base.rstrip("ь")
        table = geo.RU_REGIONS if kind == "REGION" else geo.RU_CITIES
        for noun in (base, soft, base + "ь", soft + "ь", base + "ск", base + "с", base + "а", base + "о", base + "ы",
                     base + "и", soft + "ье", soft + "ия", base + "ия", base + "ка"):
            known = self.entities.get(noun)
            if known is not None and known.kind == kind or noun in table:
                return noun
        return None

    def _place_forms(self, entity: Entity, core: str) -> None:
        """«Комсомольск-на-Амуре» в тексте зовут и просто «Комсомольск»: первая часть — написание того же места."""
        head = _hyphen_head(core)
        if head and fold(head) not in self.entities and fold(head) not in self._surface:
            entity.add(head)
            self._index(entity, head)
            self._surface[fold(head)] = entity

    def _index(self, entity: Entity, form: str, loose: bool = False) -> None:
        words = WORD.findall(form)
        if not words:
            return
        # Слова формы разделяет что угодно, кроме букв и цифр: пробел, тире, «&». Короткое сокращение капсом («ДВФ»)
        # ищем только капсом: строчное «двф» — уже не оно.
        flags = 0 if form.isupper() and len(form) <= 5 else re.I
        # Латинское написание русского названия встречается в именах файлов и шаблонов: «5_A4_TeDo basic_blue».
        # Для него подчёркивание — такой же разделитель, как пробел или дефис.
        edge_before, edge_after = (r"(?<![^\W_])", r"(?![^\W_])") if loose else (r"(?<![\w])", r"(?![\w])")
        joiner = r"[^\w]{1,4}" if not loose else r"(?:[^\w]|_){1,4}"
        pattern = re.compile(edge_before + joiner.join(map(_word_pattern, words)) + edge_after, flags)
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
            out.extend(self._cue_names(text))
            out.extend(self._trailing_noun_names(text))
            out.extend(self._siblings(text, out))
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
            elif prev in geo.UNIT_CONTEXT:
                # «участок «Каменка»», «ОП «Северный»»: без юридической формы это площадка — место или объект.
                found = self._geo_lookup(core) if len(core.split()) == 1 else None
                if found or _toponym_like(WORD.findall(core)[0] if WORD.findall(core) else core):
                    kind, reason = (found[0] if found else "CITY"), "Населённый пункт или площадка"
                else:
                    kind, reason = "PROJECT", "Название площадки или объекта"
            elif prev in geo.ORG_CONTEXT or prev2 in geo.ORG_CONTEXT and prev in {"под", "названием", "именем"}:
                kind, reason = "ORG", "Название организации"
            elif _proper_looking(core):
                kind, reason = "ORG", "Название в кавычках"
            if kind:
                out.append((start, end, kind, core, reason))
        return out

    def _cue_names(self, text: str) -> list[tuple[int, int, str, str, str]]:
        """«заявка на портал ELMA», «вендор Грантек»: несловарное слово после такого слова — название."""
        out = []
        for m in SERVICE_CUE.finditer(text):
            name = m.group("n")
            cue = fold(m.group("c"))
            first = name.split()[0]
            if not _brand_like(first) or fold(name) in self.keep:
                continue
            if not first.isascii() and re.match(r"[ \t]+[А-ЯЁ][а-яё]+", text[m.end("n"):m.end("n") + 24]):
                continue          # «сервису Иванов Пётр»: за словом идёт ещё одно с заглавной, это человек, а не название
            project = cue.startswith(("портал", "сервис", "платформ"))
            if project and first.isupper() and not first.isascii():
                continue          # «сервис ЭДО», «портал ГИС» — отраслевые сокращения, а не названия
            if len(name.split()) > 1 and not _brand_like(name.split()[1]):
                name = first
            out.append((m.start("n"), m.start("n") + len(name), "PROJECT" if project else "ORG", name,
                        "Название после слова «портал», «вендор», «подрядчик»"))
        return out

    def _trailing_noun_names(self, text: str) -> list[tuple[int, int, str, str, str]]:
        """«Сумитек Групп», «Вектрон Холдинг»: слова перед родовым словом, хотя бы одно из них — не из словаря
        и не география. «Первая Компания», «Уральский Завод» остаются как есть."""
        out = []
        for m in ORG_TRAILING_NOUN.finditer(text):
            words = m.group("n").split()
            head = words[:-1]
            # Берём с конца: слова перед названием («Отчёт Сумитек Групп») в название не входят.
            keep: list[str] = []
            for word in reversed(head):
                if _brand_like(word) or keep and word[:1].isupper() and not lexicon.is_common_word(word) \
                        and not self._geo_lookup(word):
                    keep.insert(0, word)
                else:
                    break
            if not keep or not any(_brand_like(w) for w in keep):
                continue
            name = " ".join(keep + words[-1:])
            start = m.start("n") + m.group("n").rindex(" ".join(keep))
            if fold(name) in self.keep:
                continue
            out.append((start, start + len(name), "ORG", name, "Организация по родовому слову в названии"))
        return out

    def _siblings(self, text: str, spans) -> list[tuple[int, int, str, str, str]]:
        """Соседи опознанной компании в перечне: «Балтика, Insignis и DFTC», «Heineken (Хейнекен)».

        Если хотя бы один пункт перечня — опознанная компания, другие пункты того же ряда, похожие на название
        (латиница, капс, несловарное слово), — тоже компании. Скобка сразу после компании — её другое написание.
        """
        from . import morphology as mo
        if not SIBLING_SPLIT.search(text):
            return []
        lines = []
        for line in re.finditer(r"[^\n]+", text):
            line_start, line_end = line.span()
            items, position = [], line_start
            for sep in SIBLING_SPLIT.finditer(text, line_start, line_end):
                items.append((position, sep.start(), text[sep.start():sep.end()].strip()))
                position = sep.end()
            items.append((position, line_end, ""))
            capitalised = [(a, b) for a, b, _ in items if text[slice(*_item_core(text, a, b))][:1].isupper()]
            if len(items) >= 2 and len(capitalised) >= 2:
                lines.append((line_start, line_end, items))
        if not lines:
            return []
        anchors = [(start, end, entity_key(core)) for start, end, kind, core, _ in spans if kind == "ORG"]
        # Опорой служит компания, опознанная в этом документе явно; справочная («Балтика» из списка крупных брендов)
        # в перечне рыночных игроков соседей не делает компаниями: там бывают и марки, и сорта, и обычные слова.
        if self._explicit_orgs:
            anchors += [(h.start, h.end, h.key) for h in self._known_hits(text)
                        if h.category == "ORG" and h.key in self.entities and self.entities[h.key].explicit]
        if not anchors:
            return []
        out = []
        for line_start, line_end, items in lines:
            if not any(line_start <= a and b <= line_end for a, b, _ in anchors):
                continue
            anchored = []
            for a, b, _ in items:
                core_a, core_b = _item_core(text, a, b)
                owner = next((key for s, e, key in anchors if s <= core_a and core_b <= e and core_b > core_a), None)
                anchored.append(owner)
            if not any(anchored):
                continue
            for index, (a, b, sep) in enumerate(items):
                if anchored[index]:
                    continue
                core_a, core_b = _item_core(text, a, b)
                core = text[core_a:core_b]
                words = core.split()
                if not 1 <= len(words) <= 3 or not all(w[:1].isupper() or w in {"&", "and"} for w in words):
                    continue
                if fold(core) in self.keep or any(fold(w) in geo.COUNTRIES or fold(w) in geo.COMMON_SOFTWARE for w in words):
                    continue
                if len(words) > 1 and any(mo.is_given_name(w) for w in words):
                    continue          # «Anna Fischer (Siemens)» — человек рядом с компанией, а не компания
                previous = items[index - 1] if index else None
                # «Heineken (Хейнекен)»: одна скобка сразу за компанией — другое написание той же компании. Здесь
                # достаточно, чтобы слово не было обычным: словарь охотно читает транскрипцию бренда как имя.
                alias = (previous is not None and anchored[index - 1] and previous[2] == "(" and sep == ")"
                         and not text[previous[1]:a].strip("( ")
                         and all(w.isascii() or not lexicon.is_common_word(w) and not _lemmas(w) for w in words))
                if not alias and not any(_brand_like(w) for w in words):
                    continue
                if alias:
                    self._pending_links[core] = anchored[index - 1]
                out.append((core_a, core_b, "ORG", core, "Рядом в перечне с названием компании"))
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
            first = name.split()[0]
            if intro in _AMBIGUOUS_INTROS and intro not in {"районе", "р-не"} and self._common_word(first) \
                    and not (intro not in {"п.", "п"} and re.search(r"(?:к[аиеуо]|кой|[ыи]й|ая|ое)$", first)
                             and not re.match(r"\s+[а-яё]{3,}", text[end:])):
                continue          # «ст. Научный сотрудник» — должность, а не станица
            if intro in {"районе", "р-не"} and not (
                    _toponym_like(first) and (not lexicon.is_common_word(first) or _lemmas(first)
                                              or re.search(r"(?:ово|ево|ино|ыно|[внрл]к[аиеуо])$", fold(first)))):
                continue          # «в районе Нового года», «в районе Склада» — «около», а не название
            if name.isupper() or name.split("-")[0].isupper():
                # «ОП САГАН-НУР», «г. МОСКВА»: название капсом — не сокращение из делового языка.
                if fold(name) in ACRONYM_STOP or len(name.replace("-", "")) < 4 and "-" not in name:
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
        for m in LATIN_CITY_INTRO.finditer(text):
            if fold(m.group("n")) not in LATIN_COUNTRIES and fold(m.group("n")) not in geo.COMMON_SOFTWARE:
                out.append((m.start("n"), m.end("n"), "CITY", m.group("n"), "Населённый пункт"))
        for m in REGION_ADJ_HEAD.finditer(text):
            out.append((m.start("n"), m.end("n"), "REGION", m.group("n"), "Регион"))
            self._pending_heads.append((m.group("n"), text[m.end("n"):m.end()].strip()))
        for m in REGION_LABEL.finditer(text):
            adj = m.group("n")
            if fold(adj) in geo.REGION_ADJECTIVES or _COMPOUND_PREFIX.match(fold(adj)) or self._adj_of_city(adj):
                out.append((m.start("n"), m.end("n"), "REGION", adj, "Регион"))
        for m in REGION_HEAD_ADJ.finditer(text):
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
                self._pending_heads.append((adj, text[m.end("n"):m.end()].strip()))
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
        lemma = fold(adj)
        for tail, nominative in (("ского", "ский"), ("ской", "ский"), ("ском", "ский"), ("ская", "ский")):
            if lemma.endswith(tail):
                lemma = lemma[:-len(tail)] + nominative
                break
        # «Томский» → «Томск», «Братский» → «Братск»: короткая основа, но город из справочника.
        if lemma.endswith("ский") and len(lemma) >= 7 and lemma[:-4] + "ск" in geo.RU_CITIES \
                and lemma[:-4] + "ск" not in geo.AMBIGUOUS_CITIES:
            return True
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
        self._pending_heads = []
        spans = self.explicit_spans(text)
        added = self._learn_spans(spans)
        if self._on("geo"):
            self._learn_place_labels(text)
            self._label_hits(text)
            self._adjective_place_hits(text)
        self._learn_words(text)
        if self._on("domains"):
            for m in EMAIL_DOMAIN.finditer(text):
                self._register_domain(m.group("d"))
        return added

    def _learn_spans(self, spans) -> int:
        added = 0
        for start, end, kind, core, _reason in spans:
            before = len(self.entities)
            link = self._pending_links.get(core)
            entity = self.register(kind, core, key=link if link in self.entities else None)
            added += len(self.entities) - before
            if entity is not None and kind == "ORG":
                self._alias_from_tail(entity, core)
        for adjective, head in self._pending_heads:
            entity = self._surface.get(fold(adjective))
            if entity is not None and entity.key in self.entities:
                self._register_initialisms(entity, adjective, head)
        self._pending_heads = []
        self._pending_links = {}
        for abbr, full in self._pending_alias.items():
            full_entity = self.entities.get(entity_key(full))
            abbr_entity = self.entities.get(entity_key(abbr))
            if full_entity and abbr_entity and abbr_entity is not full_entity:
                # Сокращение становится написанием полного названия, а не отдельной компанией.
                self._merge(abbr_entity, full_entity)
        return added

    def _register_initialisms(self, entity: Entity, adjective: str, head: str) -> None:
        """«Дальневосточный филиал» пишут и «ДВФ», «Северо-Западный филиал» — «СЗФ»: это то же место.

        Сокращение из трёх-четырёх букв с «Ф»/«ФО» однозначно и становится написанием места. Двухбуквенные «ДВ», «СФ»,
        «КК» и усечения вроде «СИБ» совпадают с обиходными сокращениями («СФ» — счёт-фактура), поэтому они считаются
        местом только там, где стоят как метка: отдельной ячейкой, пунктом списка, перед «:» или перед словом «филиал».
        """
        strong, labels = _initialisms(adjective, head)
        for form in strong:
            if fold(form) not in self._surface and fold(form) not in self.entities:
                entity.add(form)
                self._index(entity, form)
                self._surface[fold(form)] = entity
        for form in labels:
            self._label_forms.setdefault(form, set()).add(entity.key)

    def _learn_words(self, text: str) -> None:
        """Сведения о словах документа для подсказок: что пишут строчными и какие названия повторяются."""
        counts: dict[str, int] = {}
        for line in text.split("\n"):
            for m in WORD.finditer(line):
                word = m.group(0)
                if word[:1].islower():
                    self._lower_words.add(fold(word))
                    continue
                head = line[:m.start()].rstrip()
                if not head or head[-1] in ".!?…•·*" or not _brand_like(word):
                    continue
                counts[fold(word)] = counts.get(fold(word), 0) + 1
        self._repeated |= {word for word, count in counts.items() if count >= 2}

    def _repeated_hits(self, text: str) -> list[Hit]:
        """Строгий режим: несловарное название, которое встречается в документе не раз, скрывается сразу."""
        out = []
        for m in WORD.finditer(text):
            low = fold(m.group(0))
            if low in self._repeated and low not in self.keep and low not in self._surface:
                out.append(Hit(m.start(), m.end(), "ORG", low, "Название повторяется в документе", .8, P_REPEATED))
        return out

    def _learn_place_labels(self, text: str) -> None:
        """Строки вида «Сервис Красноярск»: слово, после которого в документе стоят места. Если то же слово стоит и
        перед коротким капсом («Сервис КК»), это код места того же ряда — он скрывается во всём документе."""
        pairs = [line.split() for line in text.split("\n")]
        pairs = [words for words in pairs if len(words) == 2 and words[0][:1].isupper() and not words[0].isupper()]
        for first, second in pairs:
            place = second.strip("()")
            known = self._surface.get(fold(place))
            if not place.isupper() and (self._geo_lookup(place) or known is not None and known.kind in {"CITY", "REGION"}):
                self._place_prefixes[fold(first)] = self._place_prefixes.get(fold(first), 0) + 1
        for first, second in pairs:
            code = second.strip("()")
            if (self._place_prefixes.get(fold(first), 0) >= 2 and re.fullmatch(r"[А-ЯЁ]{2,4}", code)
                    and fold(code) not in ACRONYM_STOP and fold(code) not in self._surface):
                self.register("REGION", code)

    def _label_hits(self, text: str) -> list[Hit]:
        """Сокращение места в роли метки: «ДВ», «СИБ -», «Сервис КК» (где в других строках «Сервис Красноярск»)."""
        if not self._label_forms and not self._place_prefixes:
            return []
        out = []
        for m in re.finditer(r"(?<![\w\-.])[А-ЯЁ]{2,4}(?![\w\-])", text):
            form = m.group(0)
            if fold(form) in self._surface or fold(form) in ACRONYM_STOP:
                continue
            keys = {k for k in self._label_forms.get(form, ()) if k in self.entities}
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.end())
            line_end = len(text) if line_end < 0 else line_end
            before = text[line_start:m.start()].strip()
            after = text[m.end():line_end].strip()
            if len(keys) == 1 and _label_context(before, after):
                entity = self.entities[next(iter(keys))]
                # Раз документ пишет место так в роли метки, это его написание и в остальном тексте документа.
                entity.add(form)
                self._index(entity, form)
                self._surface[fold(form)] = entity
            elif (not keys and len(before.split()) == 1 and not after and before[:1].isupper()
                  and self._place_prefixes.get(fold(before), 0) >= 2):
                # «Сервис КК» при «Сервис Красноярск», «Сервис Томск» в других строках: код места того же ряда.
                entity = self.register("REGION", form)
                if entity is None:
                    continue
            else:
                continue
            out.append(Hit(m.start(), m.end(), entity.kind, entity.key, "Сокращение названия места", .9, P_GEO_EXPLICIT))
        return out

    def _adjective_place_hits(self, text: str) -> list[Hit]:
        """Прилагательное-место без слова «район» в роли метки: «20 сервис Олекминский», ячейка «Олекминский».

        Обычное прилагательное («Технический», «Центральный») так не станет местом: основа должна совпасть с местом,
        уже опознанным в документе, или с городом и регионом справочника.
        """
        out = []
        for line in re.finditer(r"[^\n]+", text):
            words = list(WORD.finditer(text, line.start(), line.end()))
            for index, m in enumerate(words):
                word = m.group(0)
                if not word[:1].isupper() or word.isupper() or not ADJECTIVE_FORM.search(word):
                    continue
                if fold(word) in self._surface and self._surface[fold(word)].key in self.entities:
                    continue          # уже известное написание: его найдёт общий поиск
                after = text[m.end():line.end()].strip()
                previous = fold(words[index - 1].group(0)) if index else ""
                whole = not text[line.start():m.start()].strip() and not after
                tail = previous in UNIT_LABEL_WORDS and (not after or after[0] in ",;)" or after[0].isdigit())
                if not (whole or tail):
                    continue
                entity = self._place_of_adjective(word)
                if entity is None:
                    continue
                out.append(Hit(m.start(), m.end(), entity.kind, entity.key, "Прилагательное от названия места",
                               .9, P_GEO_EXPLICIT))
        return out

    def _place_of_adjective(self, word: str) -> Entity | None:
        lemma = _lemma(word)
        if not lemma.endswith(("ий", "ый", "ой")):
            return None
        entity = self.entities.get(lemma)
        if entity is None or entity.kind not in {"CITY", "REGION"}:
            entity = None
            for kind, table in (("REGION", geo.RU_REGIONS), ("CITY", geo.RU_CITIES)):
                noun = self._noun_of_adjective(lemma, kind)
                if noun is None or noun in geo.AMBIGUOUS_CITIES:
                    continue
                known = self.entities.get(noun)
                if known is not None and known.kind in {"CITY", "REGION"} or noun in table:
                    return self.register("REGION", word)
            return None
        # Написание того же места: «Олекминский» при «в Олекминском районе».
        entity.add(word)
        self._index(entity, word)
        self._surface[fold(word)] = entity
        return entity

    def _alias_from_tail(self, entity: Entity, core: str) -> None:
        """«Сумитек интернейшнл» → отдельно «Сумитек» тоже относится к той же компании."""
        words = core.split()
        if len(words) >= 2 and (fold(words[-1]) in geo.ORG_NAME_TAILS or fold(words[-1]).startswith(ORG_NOUN_PREFIXES)
                                and any(_brand_like(w) for w in words[:-1])):
            head = " ".join(words[:-1])
            if head and fold(head) not in geo.COMMON_SOFTWARE and (not _ordinary(head) or all(map(_brand_like, words[:-1]))):
                if head not in entity.forms:
                    entity.add(head)
                    self._index(entity, head)

    def _merge(self, source: Entity, target: Entity) -> None:
        for form in source.forms:
            target.add(form)
            self._index(target, form)
        self.entities.pop(source.key, None)
        for surface, owner in list(self._surface.items()):
            if owner is source:
                self._surface[surface] = target
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
        self._pending_heads = []
        spans = self.explicit_spans(text)
        self._learn_spans(spans)
        if self._on("domains") and "@" in text:
            for m in EMAIL_DOMAIN.finditer(text):
                self._register_domain(m.group("d"))
        hits: list[Hit] = []
        for start, end, kind, core, reason in spans:
            entity = self._surface.get(fold(core.strip(" \t«»“”„\"'"))) or self.entities.get(entity_key(core))
            if entity is not None and entity.key not in self.entities:
                entity = self.entities.get(entity_key(core))
            if entity is None:
                # Название не принято к учёту (например, стоит в списке «не скрывать»).
                continue
            geo_kind = entity.kind in {"CITY", "REGION"}
            hits.append(Hit(start, end, entity.kind, entity.key, reason, .96, P_GEO_EXPLICIT if geo_kind else P_EXPLICIT))
        hits.extend(self._known_hits(text))
        if self._on("geo"):
            hits.extend(self._gazetteer_hits(text))
            hits.extend(self._label_hits(text))
            hits.extend(self._adjective_place_hits(text))
        if self._repeated and self._on("organizations") and getattr(self.settings, "strict", False):
            hits.extend(self._repeated_hits(text))
        if getattr(self.settings, "countries", False):
            hits.extend(country_hits(text))
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
                        or fold(first) in geo.COUNTRIES or first.isdigit() or first.upper() in geo.LEGAL_FORMS:
                    continue
                if any(fold(h) in self.entities for h in (name, first)):
                    continue
                out.append(Hit(m.start("n"), m.end("n"), "POSSIBLE_ENTITY", entity_key(name), reason, .5, 410, "REVIEW"))
        # Несколько латинских слов подряд («Pernod Ricard Rouss») — одно название, а не три подсказки.
        runs = [m.span() for m in LATIN_RUN.finditer(text)] if cyrillic_unit else []
        for a, b in runs:
            words = text[a:b].split()
            if all(fold(w) in self.STOP_CANDIDATES or fold(w) in LATIN_COUNTRIES or fold(w) in self.entities
                   for w in words) or fold(text[a:b]) in self.keep or fold(text[a:b]) in self.entities:
                continue
            out.append(Hit(a, b, "POSSIBLE_ENTITY", entity_key(text[a:b]), "Латинское название: возможно, компания или бренд",
                           .45, 400, "REVIEW"))
        whole_unit = text.strip()
        for m in WORD.finditer(text):
            word = m.group(0)
            if len(word) < 3 or not word[0].isupper() or any(ch.isdigit() for ch in word):
                continue
            if any(a <= m.start() < b for a, b in runs):
                continue
            head = text[:m.start()].rstrip()
            # Ячейка из одного слова — не начало предложения, а метка или название: её тоже надо показать.
            if word != whole_unit and (not head or head[-1] in ".!?…\n•·*" or head[-1] in "–—-" and len(head) < 3):
                continue
            low = fold(word)
            if low in self.STOP_CANDIDATES or low in self.keep or low in self.entities or low in geo.COUNTRIES:
                continue
            parts = re.split(r"[.\-'’]", word)
            if len(parts) > 1 and all(lexicon.is_common_word(p) or p.isascii() and p.islower() for p in parts):
                continue          # «Инженер-механик», «Подразделение.Филиал», «Отчёт.pptx» — составные обычные слова
            latin = word.isascii()
            if latin and not cyrillic_unit:
                continue
            # Капсом («КОМАЦУ») слово — не имя, даже если словарь знает такое имя.
            if not latin and (mo.is_stop_word(word) or not word.isupper() and mo.is_given_name(word)):
                continue
            if not latin and any(lemma in geo.COUNTRIES or lemma in geo.RU_CITIES or lemma in geo.WORLD_CITIES
                                 for lemma in _lemmas(word)):
                continue
            if low in LATIN_COUNTRIES:
                continue
            confidence = STRONG_SUGGESTION if low in self._repeated else .45
            if word.isupper():
                if not 3 <= len(word) <= 7 or low in ACRONYM_STOP:
                    continue
                if not latin:
                    ordinary = lexicon.shape(word.capitalize())
                    if ordinary.lexical and ordinary.known and not ordinary.surname and not ordinary.given:
                        continue
                reason = "Аббревиатура: возможно, название компании"
            else:
                if latin:
                    if low in {"the", "and", "for"} or word[1:].isupper() is False and not re.search(r"[a-z]", word[1:]):
                        continue
                    # Одно латинское слово посреди русского текста — почти всегда название или бренд.
                    confidence = STRONG_SUGGESTION
                else:
                    if _verb_like(word):
                        continue          # «Инвентаризируем» после разрыва таблицы — глагол, а не название
                    shape = lexicon.shape(word)
                    if shape.lexical and shape.known and not shape.surname or lexicon.is_common_word(word):
                        if not self._capitalised_lexeme(text, m, word, whole_unit):
                            continue
                        reason = "Обычное слово с заглавной буквы посреди предложения: возможно, название"
                        out.append(Hit(m.start(), m.end(), "POSSIBLE_ENTITY", entity_key(word), reason, .45, 400, "REVIEW"))
                        continue
                    if shape.given and not shape.surname:
                        continue
                reason = "Похоже на название или фамилию"
            out.append(Hit(m.start(), m.end(), "POSSIBLE_ENTITY", entity_key(word), reason, confidence, 400, "REVIEW"))
        return out

    def _capitalised_lexeme(self, text: str, m: "re.Match[str]", word: str, whole_unit: str) -> bool:
        """Словарное слово с заглавной посреди фразы, которое в документе ни разу не написано строчными («нашими ИТ
        или Топлог»): так пишут название. Заголовки из слов с заглавной («Отчёт По Продажам») не в счёт."""
        if word == whole_unit or len(self._lower_words) < 50 or fold(word) in self._lower_words or _lemmas(word):
            return False
        # Название так и пишут — существительным в начальной форме; «Различия», «Заказчиков», «Коммерческий» — нет.
        morph = lexicon.analyzer()
        tag = morph.parse(word)[0].tag if morph is not None else None
        if tag is None or tag.POS != "NOUN" or not {"sing", "nomn"} <= set(tag.grammemes):
            return False
        if lexicon.shape(word).lemma and fold(lexicon.shape(word).lemma) in self._lower_words:
            return False
        before = WORD.findall(text[max(0, m.start() - 40):m.start()])
        after = WORD.findall(text[m.end():m.end() + 40])
        title = any(w[:1].isupper() and not w.isupper() for w in before[-3:] + after[:1])
        return bool(before) and before[-1][:1].islower() and not title

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
                    continue
            if token[:1].isupper() and token[-1:].isdigit():
                entity = self._numbered_place(token)
                if entity is not None:
                    # «Еруда2» — метка площадки: место с номером. Заменяется слово целиком, как отдельное написание
                    # того же места, чтобы номер не слипся с меткой («City5» + «2») и возврат был точным.
                    out.append(Hit(m.start(), m.end(), entity.kind, entity.key, "Название места с номером",
                                   .9, self._known_priority(entity)))
        return out

    def _numbered_place(self, token: str) -> Entity | None:
        parts = re.fullmatch(r"([^\W\d_]{4,})(\d{1,3})", token)
        if not parts:
            return None
        alpha = fold(parts.group(1))
        entity = self._surface.get(alpha) or self._inflected(alpha)
        if entity is not None and entity.kind in {"CITY", "REGION"} and entity.key in self.entities:
            return entity
        return None

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
        if entity.kind == "DOMAIN":
            return P_DOMAIN
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
        if low in CITY_HEADS and not pair:
            return "CITY", CITY_HEADS[low]
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
                if lemma in CITY_HEADS and not pair:
                    return "CITY", CITY_HEADS[lemma]
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


# -- страны (настройка «Скрывать страны») ---------------------------------------

ADJECTIVE_ENDING = re.compile(r"(?:ий|ый|ой|ая|яя|ое|ее|ие|ые|ого|его|ому|ему|ым|им|ом|ем|ую|юю|ых|их|ыми|ими|ей)$", re.I)
# «Ю. Корея», «С. Корея»: сокращённое прилагательное перед названием.
COUNTRY_INITIAL = re.compile(r"(?<![\w.])([ЮС])\.\s?(Коре[яиеюй]й?)(?![\w-])")
_INITIAL_COUNTRY = {"Ю": "южная корея", "С": "северная корея"}
# Латинские слова, которые чаще значат не страну: имя Jordan, штат Georgia, turkey (индейка).
_LATIN_AMBIGUOUS = {"jordan", "georgia", "turkey"}


@lru_cache(maxsize=32768)
def _normal_forms(word: str) -> frozenset[str]:
    morph = lexicon.analyzer()
    if morph is None or word.isascii():
        return frozenset({fold(word)})
    try:
        return frozenset({fold(word)} | {fold(p.normal_form) for p in morph.parse(word)[:5]})
    except Exception:
        return frozenset({fold(word)})


@lru_cache(maxsize=1)
def _country_index() -> tuple[dict, dict, dict, int]:
    """Слово → страна (кириллица через начальную форму, латиница и сокращения дословно) и многословные названия."""
    single_cyr: dict[str, str] = {}
    exact: dict[str, str] = {}
    phrases: dict[tuple, str] = {}
    longest = 1
    for form, key in geo.COUNTRY_NAMES.items():
        words = form.split()
        if len(words) > 1:
            longest = max(longest, len(words))
            if form.isascii():
                phrases[tuple(fold(w) for w in words)] = key
            else:
                phrases[tuple(_lemma(w) for w in words)] = key
        elif form.isascii() or form.isupper():
            if fold(form) not in _LATIN_AMBIGUOUS:
                exact[form if form.isupper() else fold(form)] = key
        else:
            single_cyr[fold(form)] = key
            single_cyr[_lemma(form)] = key
    return single_cyr, exact, phrases, longest


def _country_word(word: str) -> str | None:
    single_cyr, exact, _, _ = _country_index()
    if word.isupper() and word in exact:
        return exact[word]
    if word.isascii():
        return exact.get(fold(word)) if word[:1].isupper() else None
    forms = _normal_forms(word if not word.isupper() else word.capitalize())
    if word[:1].isupper():
        found = next((single_cyr[f] for f in forms if f in single_cyr), None)
        if found:
            return found
    if ADJECTIVE_ENDING.search(word):
        return next((geo.COUNTRY_ADJECTIVES[f] for f in forms if f in geo.COUNTRY_ADJECTIVES), None)
    return None


def _country_phrase(words: list[str]) -> str | None:
    _, _, phrases, _ = _country_index()
    if not all(w[:1].isupper() for w in words):
        return None
    if all(w.isascii() for w in words):
        return phrases.get(tuple(fold(w) for w in words))
    options = [_normal_forms(w) for w in words]
    for lemmas, key in phrases.items():
        if len(lemmas) == len(words) and all(l in o for l, o in zip(lemmas, options)):
            return key
    return None


def country_hits(text: str) -> list[Hit]:
    """Страна и её формы: «Армении», «армянский», «КНР», «Ю. Корея», «China». Ключ — страна, поэтому у всех форм
    одна метка. Часть слова не заменяется: «Китайгородский» — не Китай."""
    out: list[Hit] = []
    taken: list[tuple[int, int]] = []
    for m in COUNTRY_INITIAL.finditer(text):
        out.append(Hit(m.start(), m.end(), "COUNTRY", _INITIAL_COUNTRY[m.group(1)], "Страна", .95, P_COUNTRY))
        taken.append(m.span())
    tokens = [t for t in WORD.finditer(text) if not any(a <= t.start() < b for a, b in taken)]
    longest = _country_index()[3]
    i = 0
    while i < len(tokens):
        step = 0
        for n in range(min(longest, len(tokens) - i), 1, -1):
            seg = tokens[i:i + n]
            if any(text[a.end():b.start()].strip() for a, b in zip(seg, seg[1:])):
                continue
            key = _country_phrase([t.group(0) for t in seg])
            if key:
                out.append(Hit(seg[0].start(), seg[-1].end(), "COUNTRY", key, "Страна", .95, P_COUNTRY))
                step = n
                break
        if not step:
            key = _country_word(tokens[i].group(0))
            if key:
                out.append(Hit(tokens[i].start(), tokens[i].end(), "COUNTRY", key, "Страна", .95, P_COUNTRY))
            step = 1
        i += step
    return out


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


_TRANSLIT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
             "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
             "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e",
             "ю": "yu", "я": "ya"}
# Второй распространённый вариант: «Хабаровск» — Habarovsk, «Цемент» — Cement, «Юнона» — Iunona.
_TRANSLIT_ALT = {**_TRANSLIT, "х": "h", "ц": "c", "й": "i", "ю": "iu", "я": "ia", "щ": "sch"}


def transliterations(name: str) -> list[str]:
    """Латинские написания русского названия (по двум распространённым схемам), с сохранением заглавных:
    «ТеДо» → TeDo, «Аврора» → Avrora. Короткие и чисто латинские названия не трогаем."""
    letters = [ch for ch in name if ch.isalpha()]
    if len(letters) < 4 or not any("\u0400" <= ch <= "\u04ff" for ch in letters):
        return []
    out = []
    for table in (_TRANSLIT, _TRANSLIT_ALT):
        parts = []
        for word in re.split(r"(\W+)", name):
            upper = len(word) > 1 and word.isupper()
            chunk = []
            for ch in word:
                latin = table.get(ch.lower(), ch)
                if ch.isupper() and latin:
                    latin = latin.upper() if upper else latin[0].upper() + latin[1:]
                chunk.append(latin)
            parts.append("".join(chunk))
        variant = "".join(parts)
        if variant.isascii() and variant not in out:
            out.append(variant)
    return out


@lru_cache(maxsize=32768)
def _verb_like(word: str) -> bool:
    """Глагол или причастие («Инвентаризируем», «Проверяйте»): с заглавной оно только в начале фразы."""
    morph = lexicon.analyzer()
    if morph is None or word.isascii():
        return False
    try:
        return morph.parse(word)[0].tag.POS in {"VERB", "INFN", "GRND", "PRTF", "PRTS"}
    except Exception:
        return False


@lru_cache(maxsize=32768)
def _brand_like(word: str) -> bool:
    """Слово похоже на название, а не на обычное слово или имя: латиница с заглавной, капс из 2–6 букв,
    русское слово, которого нет в словаре. Страны, города, программы и деловые сокращения — нет."""
    low = fold(word)
    if len(word) < 2 or not word[:1].isupper() or any(ch.isdigit() for ch in word):
        return False
    if (low in geo.COMMON_SOFTWARE or low in geo.COUNTRIES or low in LATIN_COUNTRIES or low in ACRONYM_STOP
            or low in geo.QUOTED_NON_NAMES or low in geo.RU_CITIES or low in geo.WORLD_CITIES or low in geo.RU_REGIONS
            or low in geo.LEGAL_FORMS or low in {"the", "and", "for", "of", "ex"}):
        return False
    if word.isascii():
        return len(word) >= 2 and (len(word) >= 3 or word.isupper())
    if word.isupper():
        return 2 <= len(word) <= 6
    if not lexicon.available():
        return False
    from . import morphology as mo
    # Признак «словарь знает слово» у pymorphy ненадёжен: он угадывает разбор по окончанию («Вектрон» — «известное»
    # существительное). Надёжнее проверки на обычное слово в любой форме, имя, фамилию и географию.
    shape = lexicon.shape(word)
    if shape.surname or shape.patronymic or mo.is_given_name(word) or mo.is_stop_word(word):
        return False
    if lexicon.is_common_word(word) or _lemmas(word):
        return False
    return not _verb_like(word)


_COMPOUND_PREFIX = re.compile(r"^(дальне|ближне|северо|юго|южно|западно|восточно|верхне|нижне|средне)-?(.{4,})$")


def _initialisms(adjective: str, head: str) -> tuple[set[str], set[str]]:
    """Сокращения места по прилагательному и слову после него: (однозначные, только в роли метки)."""
    lemma = _lemma(adjective) if not adjective.isascii() else fold(adjective)
    if "-" in lemma:
        parts = [p for p in lemma.split("-") if p]
    else:
        compound = _COMPOUND_PREFIX.match(lemma)
        parts = list(compound.groups()) if compound else [lemma]
    if not parts or not all(parts):
        return set(), set()
    letters = "".join(p[0] for p in parts).upper()
    head = fold(head).split()[-1] if head.split() else ""
    federal = "федеральн" in fold(head) or head == "фо"
    strong: set[str] = set()
    labels: set[str] = set()
    if head.startswith("филиал"):
        if len(parts) > 1:
            strong.add(letters + "Ф")
            labels.update({letters, letters[0] + "Ф"})
        else:
            labels.update({letters + "Ф", lemma[:3].upper()})
    elif head.startswith("округ") or federal:
        strong.add(letters[0] + "ФО")
        if len(parts) > 1:
            strong.add(letters + "ФО")
    elif head.startswith("кра"):
        labels.add(letters[0] + "К")
    stop = {f for f in strong | labels if fold(f) in ACRONYM_STOP}
    return strong - stop, labels - stop


def _label_context(before: str, after: str) -> bool:
    """Слово стоит как метка: ячейка целиком, начало строки перед «:»/«-», пункт списка, перед «филиал», после «ОП»."""
    if not before and not after:
        return True
    if (not before or before[-1] in ".;,:") and after[:1] in {":", "-", "–", "—"}:
        return True
    if (not before or before[-1] in ",;/|(") and (not after or after[0] in ",;/|)"):
        return True
    following = WORD.findall(after[:24])
    if following and fold(following[0]) in geo.BRANCH_HEADS:
        return True
    return bool(before) and fold(before.split()[-1]) == "оп"


def _toponym_like(word: str) -> bool:
    """Слово похоже на название места: есть в справочнике, словарь помечает его географическим, типичное окончание
    названий сёл («Малиновка», «Тальжино») или слова нет в словаре вовсе."""
    low = fold(word)
    if low in geo.RU_CITIES or low in geo.WORLD_CITIES or low in geo.RU_REGIONS or _lemmas(word):
        return True
    if re.search(r"(?:ово|ево|ёво|ино|ыно|ское|цкое|[внрл]к[аиеуо]|ск)$", low):
        return True
    return lexicon.available() and not lexicon.shape(word).known


def _item_core(text: str, start: int, end: int) -> tuple[int, int]:
    """Границы пункта перечня без кавычек, юридической формы и приставки «ex-»."""
    while start < end and text[start] in " \t«»“”„\"'":
        start += 1
    while end > start and text[end - 1] in " \t«»“”„\"'.":
        end -= 1
    prefix = _ITEM_PREFIX.match(text[start:end])
    if prefix:
        start += prefix.end()
        while start < end and text[start] in " «“„\"'":
            start += 1
    return start, end


def _hyphen_head(name: str) -> str | None:
    """Первая часть составного названия, которой место зовут коротко: «Комсомольск-на-Амуре» → «Комсомольск»,
    «Каменск-Уральский» → «Каменск». «Орехово-Зуево» и «Улан-Удэ» так не сокращают."""
    parts = name.split("-")
    if len(parts) < 2 or len(parts[0]) < 5 or not parts[0][:1].isupper() or parts[0].isupper():
        return None
    if not (parts[1].islower() or ADJECTIVE_CORE.match(parts[-1]) and parts[-1][:1].isupper()):
        return None
    head = fold(parts[0])
    if head in geo.RU_CITIES or head in geo.WORLD_CITIES or head in geo.AMBIGUOUS_CITIES or head in geo.COUNTRIES:
        return None
    if head.endswith("о"):
        return None           # «Северо-Енисейский», «Лосино-Петровский»: первая часть — не название
    if lexicon.available():
        shape = lexicon.shape(parts[0])
        if shape.surname or shape.given or shape.lexical and shape.known and not _lemmas(parts[0]):
            return None       # «Камень-на-Оби», «Юрьев-Польский»: первая часть — обычное слово или фамилия
    return parts[0]


def _city_heads() -> dict[str, str]:
    """Короткие имена справочных городов; если первая часть общая у двух городов («Каменск»), её не берём."""
    heads: dict[str, list[str]] = {}
    for city in geo.RU_CITIES:
        head = _hyphen_head("-".join(p if p in {"на", "в", "под", "над", "де"} else p[:1].upper() + p[1:]
                                     for p in city.split("-")))
        if head:
            heads.setdefault(fold(head), []).append(city)
    return {head: cities[0] for head, cities in heads.items() if len(cities) == 1}


def _stem_of(low: str) -> str:
    return low[:-1] if low and low[-1] in "аяоеиыьйю" and len(low) >= 5 else low


@lru_cache(maxsize=4096)
def _pair_lemmas(phrase: str) -> tuple[tuple[str, ...], ...]:
    """Двусловные названия: прилагательное («Нижний») не помечено как географическое, существительное — да."""
    first, second = phrase.split(" ", 1)
    return tuple((a, b) for a in {_lemma(first), fold(first)} for b in (_lemmas(second) or (fold(second),)))


CITY_HEADS = _city_heads()
