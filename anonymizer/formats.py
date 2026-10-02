from __future__ import annotations

import copy
import hashlib
import os
import posixpath
import re
import tempfile
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path

import fitz
from lxml import etree

from . import lexicon
from . import numbers as numeric
from .models import Decision, FileResult, FileStatus, Finding, Settings
from .normalize import fold
from .tokens import EMAIL_TOKEN_RE, TOKEN_RE, base_of, kind_of
from .util import atomic_write, decode_text, finding_id, safe_copy, sha256_bytes, sniff_encoding
from .vault import Vault


TEXT_EXTENSIONS = {".txt", ".csv", ".tsv", ".md", ".json", ".xml", ".log", ".htm", ".html", ".ini", ".yaml", ".yml"}
OOXML_EXTENSIONS = {".xlsx", ".docx", ".pptx"}
# OpenDocument — тот же ZIP с XML внутри, и вернуть плейсхолдеры на место в нём получается.
# А вот обезличивать его пока нельзя: текст в ODF лежит прямо в <text:p> вперемешку с хвостами
# вложенных узлов, чего сборщик текста не умеет, и снимок целостности для ODF не считается.
# Поэтому такой файл никогда не объявляется очищенным.
ODF_EXTENSIONS = {".odt", ".ods", ".odp"}
MACRO_EXTENSIONS = {".xlsm", ".docm", ".pptm"}
OLD_OFFICE = {".xls", ".doc", ".ppt"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp", ".heic"}

# Свойства документа. В core.xml имена строчные, в app.xml — с заглавной (`Company`, `Manager`), поэтому перечислены оба вида.
# Название (`title`) и категория обычно содержат имя клиента или проекта: «Отчёт для ООО «Маяк-Строй»».
# `Template` — имя шаблона оформления в app.xml: у корпоративного шаблона в нём название клиента («Клиент_шаблон.potx»).
METADATA_TAGS = {"creator", "lastModifiedBy", "company", "manager", "comments", "description", "subject", "keywords",
                 "title", "category", "Company", "Manager", "contentStatus", "HyperlinkBase", "Template"}
TEXT_TAGS = {
    "t", "text", "delText", "f", "definedName", "oddHeader", "evenHeader", "firstHeader",
    "oddFooter", "evenFooter", "firstFooter", "author", "instrText", "lpwstr", "bstr", "lpstr",
    # Выпадающий список проверки данных и формула условного форматирования держат значения
    # прямо в тексте узла: «"Иванов,Петров,Сидоров"» уезжало наружу нетронутым.
    "formula1", "formula2", "formula",
} | METADATA_TAGS
GROUP_TAGS = {"p", "si", "c", "comment", "definedName", "oddHeader", "evenHeader", "firstHeader",
              "oddFooter", "evenFooter", "firstFooter", "pt", "tx",
              "formula1", "formula2", "formula"}
SEPARATOR_TAGS = {"tab": " ", "br": "\n", "cr": "\n"}

# Средняя ширина глифа относительно кегля. Токены пишутся Helvetica (узкая латиница),
# восстановленный текст — встроенным Unicode-шрифтом, у которого глифы шире.
TOKEN_GLYPH_WIDTH = .50
RESTORED_GLYPH_WIDTH = .55
PLACEHOLDER_FONT_RANGE = (3.0, 8.0)

# Attributes that carry a person's name rather than machine data. Sheet names, shape names and
# alt text all live here and are invisible to any scan that only walks element text.
SENSITIVE_ATTRS = {"author", "initials", "userId", "lastModifiedBy", "creator", "displayName"}
# Имена, которые Excel принимает только как идентификатор: невидимый разделитель в них недопустим.
IDENTIFIER_ATTRS = {("table", "name"), ("table", "displayName"), ("tableColumn", "name"), ("definedName", "name"),
                    ("bookmarkStart", "name"), ("hyperlink", "anchor"), ("tag", "val")}
NAMED_ELEMENT_ATTRS = {
    ("sheet", "name"), ("cNvPr", "name"), ("cNvPr", "descr"), ("cNvPr", "title"),
    ("docPr", "name"), ("docPr", "descr"), ("docPr", "title"), ("cSld", "name"),
    ("table", "displayName"), ("table", "name"), ("chartSpace", "name"), ("pivotCacheDefinition", "name"),
    # Имя столбца умной таблицы обязано совпадать с текстом её заголовка в ячейке, иначе Excel предлагает «восстановить»
    # файл. Заголовок обезличивается, значит и имя столбца должно измениться так же.
    ("tableColumn", "name"), ("cacheField", "name"), ("pivotField", "name"),
    # Подсказки и тексты, которые видит человек: гиперссылка, проверка данных, условное форматирование.
    ("hyperlink", "tooltip"), ("hyperlink", "display"), ("dataValidation", "prompt"), ("dataValidation", "promptTitle"),
    ("dataValidation", "error"), ("dataValidation", "errorTitle"), ("cfRule", "text"),
    ("property", "name"), ("definedName", "name"), ("bookmarkStart", "name"), ("hyperlink", "anchor"),
    # Имена темы, палитры, шрифтовой схемы и эффектов: автор корпоративного шаблона пишет в них название клиента
    # («Клиент ppt color palette»). В документе их не видно, но в файле они лежат открытым текстом.
    # `themeFamily` (расширение Office 2013+) хранит ещё и имя файла шаблона: «Клиент_template_blue.potx».
    ("theme", "name"), ("clrScheme", "name"), ("fontScheme", "name"), ("fmtScheme", "name"), ("themeFamily", "name"),
}
TEMPLATE_NAME_ATTRS = {("theme", "name"), ("clrScheme", "name"), ("fontScheme", "name"), ("fmtScheme", "name"),
                       ("themeFamily", "name")}
# Встроенные имена Office: в них нет ничего о владельце, и заменять их незачем.
BUILTIN_TEMPLATE_NAME = re.compile(
    r"(?i)^\s*(?:(?:тема\s+)?office(?:\s+\d{4}(?:\s*[-–]\s*\d{4})?)?(?:\s+(?:theme|тема))?|normal(?:\.dot[mx]?)?|blank(?:\.potx)?|"
    r"(?:custom|другая|специальная|пользовательская|настраиваемая)(?:\s+\d+)?)\s*$")
# Имена одних и тех же элементов в разных форматах значат разное: `tag` в Word — метка элемента управления, а в PowerPoint —
# служебные данные надстройки (think-cell хранит там целый XML). Поэтому такие атрибуты берутся только в своей части файла.
PART_NAMED_ATTRS = {
    "word/": {("alias", "val"), ("tag", "val"), ("fldSimple", "instr")},
    "ppt/": {("cmAuthor", "name"), ("cmAuthor", "initials"), ("section", "name")},
}
CUSTOM_XML_PREFIX = "customxml/"

# Адреса ссылок. Пространства имён и типы связей (`Type`, `xmlns`) тоже начинаются с http://, но это
# машинные идентификаторы формата: изменить их значит сломать файл.
LINK_ATTRS = {"Target", "href", "url", "link", "address", "location"}

# Путь на компьютере автора (`C:\\Users\\ivanov\\Desktop\\Клиент\\`, `file:///...`): в нём имя пользователя и название клиента.
LOCAL_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|file:|\\\\|/Users/|/home/)")

# Значения длиннее этого — машинные данные (base64, пути, стили), а не текст документа.
MAX_ATTRIBUTE_LENGTH = 10_000

# Сколько чисел-сумм в книге (не дат, не лет, не номеров до 12) уже стоит подсказки «включите замену чисел».
MANY_AMOUNTS = 20


EMBEDDED_SUFFIXES = (".xlsx", ".docx", ".pptx")


def is_embedded_package(name: str) -> bool:
    """Книга, документ или презентация внутри файла: данные диаграммы, вставленный объект."""
    lowered = name.lower()
    return "/embeddings/" in lowered and lowered.endswith(EMBEDDED_SUFFIXES)


# Вложения, которые программа не разбирает: их содержимое уходит наружу как есть.
EMBEDDED_LABELS = {".xlsb": "встроенный .xlsb", ".xls": "встроенный .xls", ".xlsm": "встроенный .xlsm",
                   ".doc": "встроенный .doc", ".docm": "встроенный .docm", ".ppt": "встроенный .ppt",
                   ".pptm": "встроенный .pptm", ".pdf": "встроенный PDF", ".bin": "объект OLE (.bin)"}


def unprocessed_embeddings(names, depth: int = 0) -> dict[str, int]:
    """Сколько вложений каждого вида останется необезличенным: двоичные книги, объекты OLE, PDF."""
    counts: dict[str, int] = {}
    for name in names:
        lowered = name.lower()
        if "/embeddings/" not in lowered or lowered.endswith((".xml", ".rels")) or "/_rels/" in lowered:
            continue
        if is_embedded_package(name) and depth < 2:
            continue
        label = EMBEDDED_LABELS.get(Path(lowered).suffix, f"вложение {Path(lowered).suffix or 'без расширения'}")
        counts[label] = counts.get(label, 0) + 1
    return counts


def embedded_warning(counts: dict[str, int]) -> str:
    listing = ", ".join(f"{label} ({n})" if n > 1 else label for label, n in counts.items())
    return (f"Вложенные объекты не обезличены: {listing}. Их содержимое осталось как есть и может содержать исходные "
            "данные: удалите эти объекты из файла или проверьте их вручную")


def removal_notice(removal: "EmbeddedRemoval") -> str:
    listing = ", ".join(f"{label} ({n})" if n > 1 else label for label, n in removal.labels.items())
    what = []
    if removal.charts:
        what.append(f"диаграммы ({removal.charts}) сохранили свой вид, но изменить или обновить их данные больше нельзя")
    if removal.ole:
        what.append(f"вставленные объекты ({removal.ole}) заменены их картинками и больше не редактируются")
    tail = "; ".join(what)
    return (f"Удалены вложения, которые программа не умеет обезличить: {listing}. В них были исходные данные. "
            + (tail[:1].upper() + tail[1:] + "." if tail else ""))


THUMBNAIL_REL = "/metadata/thumbnail"


def drop_thumbnails(parsed: dict, names: list[str]) -> set[str]:
    """Эскиз первой страницы (docProps/thumbnail.jpeg) — картинка титульного слайда или листа.

    Текст на картинке заменить нельзя, а название клиента на ней читается. Эскиз удаляется вместе со ссылкой
    на него и записью о типе: Office создаст новый при следующем сохранении. Возвращает имена удалённых частей.
    """
    dropped = {n for n in names if n.lower().startswith("docprops/thumbnail")}
    rels = parsed.get("_rels/.rels")
    if rels is not None:
        for rel in list(rels):
            target = (rel.get("Target") or "").lstrip("/").lower()
            if (rel.get("Type") or "").endswith(THUMBNAIL_REL):
                dropped.update(n for n in names if n.lower() == target)
            if (rel.get("Type") or "").endswith(THUMBNAIL_REL) or target in {d.lower() for d in dropped}:
                rels.remove(rel)
    types = parsed.get("[Content_Types].xml")
    if types is not None and dropped:
        parts = {"/" + n.lower() for n in dropped}
        for node in list(types):
            if (node.get("PartName") or "").lower() in parts:
                types.remove(node)
    return dropped


R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
R_ID = f"{{{R_NS}}}id"


@dataclass
class EmbeddedRemoval:
    """Что убрано из вложений: удалённые части, число диаграмм без книги данных и объектов OLE, ставших картинкой."""
    parts: set[str] = field(default_factory=set)
    charts: int = 0
    ole: int = 0
    labels: dict[str, int] = field(default_factory=dict)


def _unprocessable(name: str, depth: int) -> bool:
    """Вложение, которое программа не обезличивает: двоичная книга, объект OLE, PDF, слишком глубокая вложенность."""
    lowered = name.lower()
    if "/embeddings/" not in lowered or lowered.endswith((".xml", ".rels")) or "/_rels/" in lowered:
        return False
    return not (is_embedded_package(name) and depth < 2)


def _rels_of(part: str) -> str:
    folder, base = posixpath.split(part)
    return posixpath.join(folder, "_rels", base + ".rels")


def _rel_target(part: str, rel) -> str:
    target = rel.get("Target") or ""
    if rel.get("TargetMode") == "External":
        return ""
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(part), target))


def _ole_picture(frame):
    """Объект OLE на слайде превращается в свою же картинку-заместитель (её PowerPoint и так показывает).

    Имя, номер и служебные метки берутся у рамки объекта, изображение и размеры — у картинки из `mc:Fallback`.
    Без готовой картинки безопасной замены нет: такой объект остаётся, а о нём предупреждают.
    """
    picture = next((node for node in frame.iter() if _xml_name(node) == "pic"), None)
    frame_props = next((node for node in frame if _xml_name(node) == "nvGraphicFramePr"), None)
    if picture is None or frame_props is None:
        return None
    picture = copy.deepcopy(picture)
    picture_props = next((node for node in picture if _xml_name(node) == "nvPicPr"), None)
    if picture_props is None:
        return None
    for node in list(picture_props):
        own = next((x for x in frame_props if _xml_name(x) == _xml_name(node)), None)
        if own is not None and _xml_name(node) in {"cNvPr", "nvPr"}:
            picture_props.replace(node, copy.deepcopy(own))
    shape = next((node for node in picture if _xml_name(node) == "spPr"), None)
    frame_xfrm = next((node for node in frame if _xml_name(node) == "xfrm"), None)
    if shape is not None and frame_xfrm is not None and not any(_xml_name(x) == "xfrm" for x in shape):
        a_ns = etree.QName(frame_xfrm[0]).namespace if len(frame_xfrm) else "http://schemas.openxmlformats.org/drawingml/2006/main"
        xfrm = etree.Element(f"{{{a_ns}}}xfrm", dict(frame_xfrm.attrib))
        xfrm.extend(copy.deepcopy(x) for x in frame_xfrm)
        shape.insert(0, xfrm)
    return picture


def drop_embedded_payloads(parsed: dict, names: list[str], depth: int = 0) -> EmbeddedRemoval:
    """Убирает вложения, которые нельзя обезличить, но без которых документ открывается и выглядит так же.

    * Книга данных диаграммы (`c:externalData` → .xlsb/.xls): в самой диаграмме остаются последние значения
      (`c:numCache`/`c:strCache`), их текст обезличивается как обычно. Диаграмма рисуется, но править данные нельзя.
    * Объект OLE на слайде (`p:oleObj` → .bin) заменяется своей картинкой-заместителем из `mc:Fallback`.
    * Объект OLE в Word (`o:OLEObject`/`w:objectEmbed` в `w:object`) теряет вложение, картинка `v:imagedata` остаётся.

    Объект без картинки, объекты OLE Excel и вложения без ссылок на них не трогаются: о них по-прежнему предупреждение.
    Книги, документы и презентации внутри файла обезличиваются отдельно и здесь не удаляются.
    """
    removal = EmbeddedRemoval()
    actual = {n.lower(): n for n in names}
    released: set[str] = set()
    for part, root in list(parsed.items()):
        if part.lower().endswith(".rels") or part == "[Content_Types].xml":
            continue
        rels = parsed.get(_rels_of(part))
        if rels is None:
            continue
        targets = {rel.get("Id"): actual.get(_rel_target(part, rel).lower(), "") for rel in rels}

        def payload(node) -> str:
            target = targets.get(node.get(R_ID) or "", "")
            return target if target and _unprocessable(target, depth) else ""

        candidates: set[str] = set()
        for node in list(root.iter()):
            name = _xml_name(node)
            if name == "externalData" and "/charts/" in part.lower() and payload(node):
                candidates.add(node.get(R_ID))
                node.getparent().remove(node)
                removal.charts += 1
            elif name == "graphicFrame":
                data = next((x for x in node.iter() if _xml_name(x) == "graphicData"), None)
                objects = [x for x in node.iter() if _xml_name(x) == "oleObj"]
                if data is None or not (data.get("uri") or "").endswith("/ole") or not objects:
                    continue
                if not all(payload(x) for x in objects):
                    continue
                picture = _ole_picture(node)
                if picture is None:
                    continue
                node.getparent().replace(node, picture)
                candidates.update(x.get(R_ID) for x in objects)
                removal.ole += 1
            elif name in {"OLEObject", "objectEmbed"} and payload(node):
                holder = node.getparent()
                if holder is None or not any(_xml_name(x) in {"imagedata", "blip"} for x in holder.iter()):
                    continue
                candidates.add(node.get(R_ID))
                holder.remove(node)
                removal.ole += 1
        if not candidates:
            continue
        # Ссылка удаляется, только если на неё в части больше ничего не указывает.
        still_used = {value for node in root.iter() for value in node.attrib.values()}
        for rel in list(rels):
            rid = rel.get("Id")
            if rid in candidates and rid not in still_used:
                released.add(targets.get(rid, ""))
                rels.remove(rel)
    released.discard("")
    # Одно вложение может быть нужно и другой части (общий объект в макете и на слайде): тогда оно остаётся.
    referenced = {_rel_target(_rels_owner(part), rel).lower()
                  for part, rels in parsed.items() if part.lower().endswith(".rels") for rel in rels}
    removal.parts = {name for name in released if name.lower() not in referenced}
    for name in removal.parts:
        label = EMBEDDED_LABELS.get(Path(name.lower()).suffix, f"вложение {Path(name.lower()).suffix or 'без расширения'}")
        removal.labels[label] = removal.labels.get(label, 0) + 1
    types = parsed.get("[Content_Types].xml")
    if types is not None and removal.parts:
        gone = {"/" + n.lower() for n in removal.parts}
        gone_ext = {Path(n.lower()).suffix.lstrip(".") for n in removal.parts}
        left_ext = {Path(n.lower()).suffix.lstrip(".") for n in names if n not in removal.parts}
        for node in list(types):
            extension = (node.get("Extension") or "").lower()
            if (node.get("PartName") or "").lower() in gone or \
                    (_xml_name(node) == "Default" and extension in gone_ext and extension not in left_ext):
                types.remove(node)
    return removal


def _rels_owner(rels_part: str) -> str:
    """`ppt/slides/_rels/slide1.xml.rels` → `ppt/slides/slide1.xml`; `_rels/.rels` → корень пакета."""
    folder, base = posixpath.split(rels_part)
    return posixpath.join(posixpath.dirname(folder), base[:-len(".rels")])


def classify(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in TEXT_EXTENSIONS:
        return "TEXT"
    if ext in OOXML_EXTENSIONS:
        return "OOXML"
    if ext in ODF_EXTENSIONS:
        return "ODF"
    if ext in MACRO_EXTENSIONS:
        return "MACRO_OFFICE"
    if ext in OLD_OFFICE:
        return "OLD_OFFICE"
    if ext == ".pdf":
        return "PDF"
    if ext in IMAGE_EXTENSIONS:
        return "IMAGE"
    return "UNSUPPORTED"


def _xml_name(element) -> str:
    return etree.QName(element).localname


def _parser():
    return etree.XMLParser(resolve_entities=False, no_network=True, recover=False,
                           remove_blank_text=False, huge_tree=True)


# ---------------------------------------------------------------------------
# Replacement plumbing
# ---------------------------------------------------------------------------

@dataclass
class TransformContext:
    """Всё, что нужно замене: хранилище токенов, найденные люди и решения пользователя.

    Токен выдаёт хранилище, а не файл: один и тот же объект в любом файле получает один и тот же токен.
    """
    vault: Vault
    settings: Settings
    detector: object
    overrides: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)          # группа отчёта → число замен
    tokens: dict[str, set[str]] = field(default_factory=dict)     # группа отчёта → уникальные токены
    replaced: dict[str, dict] = field(default_factory=dict)       # исходное → сведения для экрана «Что заменено»
    taken_paths: dict[str, str] = field(default_factory=dict)
    numeric: dict[str, int] = field(default_factory=dict)
    auto_hidden: set[str] = field(default_factory=set)            # скрыто строгим режимом, а не человеком

    def apply_overrides(self, findings: list[Finding]) -> list[Finding]:
        """Решения человека: `hide` заменяет сомнительное, `keep` оставляет найденное как есть."""
        for finding in findings:
            # Однофамильцы («Ilin Sergey» при двух Ильиных Сергеях, «Иванов И.» при двух Ивановых): кто из двоих, не
            # угадать, но оставлять имя открытым нельзя. Оно заменяется отдельной меткой, а возврат подставит написание
            # из хранилища дословно, поэтому ошибиться в человеке нельзя.
            if (finding.category == "PERSON" and finding.decision == Decision.REVIEW and finding.candidates
                    and "опечат" not in finding.reason):
                finding.decision = Decision.AUTO
                finding.person_id = None
                finding.key = fold(finding.original)
                finding.reason = "Совпадает с несколькими людьми: заменено отдельной меткой"
        if not self.overrides:
            return findings
        for finding in findings:
            choice = self.overrides.get(fold(finding.original))
            if choice == "keep":
                finding.decision = Decision.REVIEW
                finding.reason = "Оставлено по решению пользователя"
            elif choice == "hide":
                finding.decision = Decision.AUTO
                if finding.category == "POSSIBLE_PERSON":
                    finding.category = "PERSON"
                finding.confidence = 1.0
                finding.reason = ("Скрыто: включено «Скрывать и сомнительные слова»" if fold(finding.original) in self.auto_hidden
                                  else "Скрыто по решению пользователя")
        return findings

    def token_for(self, finding: Finding) -> str:
        kind = kind_of(finding.category)
        key = finding.key
        person_id = finding.person_id
        if kind.code == "PERSON" and person_id:
            resolver = getattr(self.detector, "resolve_person", None)
            key = (resolver(person_id) if resolver else person_id) or person_id
        if kind.code == "SECRET":
            secret = not self.settings.retain_secrets_for_restore
            token = self.vault.token_for("SECRET", key, None if secret else finding.original, secret=secret)
        else:
            token = self.vault.token_for(kind.code, key or fold(finding.original), finding.original)
        self.counts[kind.group] = self.counts.get(kind.group, 0) + 1
        self.tokens.setdefault(kind.group, set()).add(base_of(token) or token)
        if kind.code != "SECRET":
            entry = self.replaced.setdefault(fold(finding.original), {
                "original": finding.original, "token": token, "kind": kind.code, "group": kind.group,
                "count": 0, "reason": finding.reason})
            entry["count"] += 1
        return token


def replace_findings(text: str, findings: list[Finding], ctx: TransformContext, guard: bool = True) -> str:
    accepted = sorted((x for x in findings if x.decision == Decision.AUTO), key=lambda x: x.start)
    # Номера выдаются слева направо, чтобы человек №1 был первым в документе, а замена идёт
    # справа налево, чтобы не сбить ещё не использованные позиции.
    tokens = {id(f): ctx.token_for(f) for f in accepted}
    # Склейка одним проходом. Пересборка строки на каждой находке давала квадратичный рост:
    # файл в несколько мегабайт с десятками тысяч замен обрабатывался бы минутами.
    pieces: list[str] = []
    cursor = 0
    for f in accepted:
        if f.start < cursor:
            continue
        pieces.append(text[cursor:f.start])
        pieces.append(guard_token(text, f.start, f.end, tokens[id(f)]) if guard else tokens[id(f)])
        cursor = f.end
    pieces.append(text[cursor:])
    return "".join(pieces)


ZWSP = "\u200b"


def _glues(ch: str) -> bool:
    return ch.isascii() and (ch.isalnum() or ch == "_")


def guard_token(text: str, start: int, end: int, token: str) -> str:
    """Токен не должен слипаться с соседними символами: `Name5` + `1` читалось бы как `Name51`.

    Между ними ставится невидимый разделитель; при восстановлении он удаляется вместе с токеном.
    """
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    if before in "LCR" and text[start - 2:start - 1] == "&":
        before = ""            # код колонтитула Excel (&R, &L, &C), а не буква слова
    if before and _glues(before):
        token = ZWSP + token
    if after and _glues(after):
        token = token + ZWSP
    return token


# ---------------------------------------------------------------------------
# Plain text
# ---------------------------------------------------------------------------

def scan_text_file(path: Path, rel: str, detector) -> FileResult:
    try:
        raw = path.read_bytes()
        text = decode_text(raw)
        detector.harvest(text, rel)
        findings = detector.scan(text, rel, "text")
        status = FileStatus.REVIEW_REQUIRED if any(f.decision != Decision.AUTO for f in findings) else FileStatus.CLEAN
        return FileResult(rel, "TEXT", status, findings,
                          integrity={"sha256": sha256_bytes(raw), "bytes": len(raw), "lines": text.count("\n") + 1})
    except Exception as exc:
        return FileResult(rel, "TEXT", FileStatus.BLOCKED, warnings=[f"Ошибка чтения: {type(exc).__name__}"])


def transform_text_file(src: Path, dst: Path, rel: str, detector, ctx: TransformContext) -> FileResult:
    raw = src.read_bytes()
    enc = sniff_encoding(raw)
    text = raw.decode(enc, errors="strict")
    detector.harvest(text, rel)
    findings = ctx.apply_overrides(detector.scan(text, rel, "text"))
    transformed = replace_findings(text, findings, ctx)
    try:
        payload = transformed.encode(enc)
    except UnicodeEncodeError:
        payload, enc = transformed.encode("utf-8"), "utf-8"
    atomic_write(dst, payload)
    critical = _residuals(detector, transformed, findings)
    warnings = ["После обработки в файле остались данные, похожие на исходные"] if critical else []
    status = FileStatus.CLEAN if not critical else FileStatus.REVIEW_REQUIRED
    return FileResult(rel, "TEXT", status, findings, warnings, str(dst),
                      {"original_lines": text.count("\n") + 1, "output_lines": transformed.count("\n") + 1, "open_ok": True})


def _residuals(detector, output_text: str, findings: list[Finding]) -> list[str]:
    """Что пережило замену: исходное значение всё ещё читается как отдельное слово или шаблон снова срабатывает.

    Проверяются уникальные значения, а не каждая находка: одно и то же ФИО встречается в
    документе сотни раз, и поиск по всему тексту на каждое вхождение — квадратичная работа.
    Короткое значение внутри другого слова («Ким» в «Кимберли») остатком не считается.
    """
    unique = {f.original for f in findings
              if f.decision == Decision.AUTO and f.original and f.category not in {"TERM", "FILE", "META"}}
    leftovers = []
    for value in unique:
        start = output_text.find(value)
        while start != -1:
            before = output_text[start - 1] if start else " "
            after = output_text[start + len(value)] if start + len(value) < len(output_text) else " "
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                leftovers.append(value)
                break
            start = output_text.find(value, start + 1)
    leftovers.extend(f.original for f in detector.verify(output_text))
    return leftovers


# ---------------------------------------------------------------------------
# OOXML
# ---------------------------------------------------------------------------

def _zip_snapshot(path: Path) -> dict:
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        binary: dict[str, str] = {}
        formulas = 0
        for name in names:
            lowered = name.lower()
            if lowered.endswith(".xml"):
                formulas += len(re.findall(br"<(?:[A-Za-z0-9_]+:)?f(?:\s|>)", z.read(name)))
            elif not lowered.endswith(".rels") and not is_embedded_package(name):
                binary[name] = hashlib.sha256(z.read(name)).hexdigest()
        return {
            "entries": len(names), "names": sorted(names), "binary_hashes": binary,
            "uncompressed_bytes": sum(i.file_size for i in z.infolist()),
            "sheets": sum(n.startswith("xl/worksheets/sheet") and n.endswith(".xml") for n in names),
            "slides": sum(n.startswith("ppt/slides/slide") and n.endswith(".xml") for n in names),
            "formulas": formulas,
            "tables": sum("/tables/" in n and n.endswith(".xml") for n in names),
            "charts": sum("/charts/" in n and n.endswith(".xml") for n in names),
            "comments": sum("comment" in n.lower() and n.endswith(".xml") for n in names),
            "media": sum("/media/" in n for n in names),
            "embeddings": sum("/embeddings/" in n and not is_embedded_package(n) for n in names),
            "packages": sum(is_embedded_package(n) for n in names),
            "macros": any(n.lower().endswith("vbaproject.bin") for n in names),
            "signatures": any("_xmlsignatures/" in n.lower() for n in names),
        }


@dataclass(slots=True)
class Segment:
    """One contribution to a paragraph's text: either an editable run or a structural break."""
    element: object
    text: str
    writable: bool


def _cell_value_is_text(node) -> bool:
    """`<v>` holds a shared-string index for normal cells but literal text for formula results.

    Rewriting the index would corrupt the workbook; leaving the literal alone would leak the
    name a formula produced, which is exactly the cached value Excel displays.
    """
    parent = node.getparent()
    if parent is None:
        return False
    if _xml_name(parent) == "c":
        if parent.get("t") in ("str", "inlineStr"):
            return True
        # Телефон, карта и СНИЛС, записанные в ячейку числом, — такие же данные, как и текстом.
        return parent.get("t") in (None, "n") and _sensitive_number(node.text or "")
    return _xml_name(parent) in ("pt", "tx", "v")


def _sensitive_number(text: str) -> bool:
    from .detectors import valid_luhn, valid_snils
    digits = text.strip()
    if not digits.isdigit() or digits.startswith("0"):
        return False
    if len(digits) == 11:
        return (digits[0] in "78" and digits[1] == "9") or valid_snils(digits)
    return 13 <= len(digits) <= 19 and valid_luhn(digits)


NUMERIC_TEXT = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")


def _text_into_numeric_cells(root) -> None:
    """Число в ячейке заменено меткой: ячейка становится текстовой, иначе Excel сочтёт файл повреждённым."""
    for cell in list(root.iter()):           # дерево меняется по ходу: обход по копии списка
        if _xml_name(cell) != "c" or cell.get("t") not in (None, "n"):
            continue
        v = next((c for c in cell if _xml_name(c) == "v"), None)
        if v is None or v.text is None or NUMERIC_TEXT.match(v.text.strip()):
            continue
        text = v.text
        cell.remove(v)
        cell.set("t", "inlineStr")
        ns = etree.QName(cell).namespace
        inline = etree.SubElement(cell, f"{{{ns}}}is")
        etree.SubElement(inline, f"{{{ns}}}t").text = text


def _is_text_node(node, part: str) -> bool:
    name = _xml_name(node)
    if part.lower().startswith(CUSTOM_XML_PREFIX):
        return True
    if name in TEXT_TAGS:
        return True
    if name == "v":
        return "/charts/" in part or _cell_value_is_text(node)
    return False


def _groups(root, part: str) -> list[tuple[str, list[Segment]]]:
    grouped: dict[int, tuple[object, list[Segment]]] = {}
    for node in root.iter():
        name = _xml_name(node)
        separator = SEPARATOR_TAGS.get(name)
        is_text = _is_text_node(node, part) and node.text
        if not separator and not is_text:
            continue
        parent = node
        if name not in METADATA_TAGS | {"lpwstr", "bstr", "lpstr"}:
            while parent.getparent() is not None and _xml_name(parent) not in GROUP_TAGS:
                parent = parent.getparent()
        if separator and id(parent) not in grouped:
            # A leading break carries no text of its own; only breaks inside a run group matter.
            continue
        entry = grouped.setdefault(id(parent), (parent, []))
        entry[1].append(Segment(node, separator if separator else (node.text or ""), not separator))
    return [(f"{_xml_name(parent)}:{i}", segments) for i, (parent, segments) in enumerate(grouped.values())]


def _part_text(root, part: str) -> str:
    return "\n".join("".join(s.text for s in segments) for _, segments in _groups(root, part))


XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


def write_text(element, value: str) -> None:
    """Записывает текст узла. Пробел в начале или конце Word и Excel отбрасывают, если нет xml:space="preserve":
    метка, разбитая на два фрагмента («Company» и «1 ждёт»), после замены оставляла «Ромашкаждёт»."""
    element.text = value
    if value and (value != value.strip(" \t\n\r") or "  " in value):
        namespace = etree.QName(element).namespace or ""
        if namespace.endswith(("/wordprocessingml/2006/main", "/spreadsheetml/2006/main")) and etree.QName(element).localname in (
                "t", "delText", "instrText"):
            element.set(XML_SPACE, "preserve")


def splice_segments(segments: list[Segment], edits: list[tuple[int, int, str]]) -> None:
    """Подставляет `edits` = (начало, конец, текст) в сегменты абзаца справа налево.

    Замена помещается в первый затронутый текстовый run, остальные затронутые участки очищаются, а
    поглощённые разрывы строк и табуляции удаляются. Незатронутые runs сохраняют своё оформление.
    """
    for start, end, token in sorted(edits, key=lambda e: e[0], reverse=True):
        spans, position = [], 0
        for segment in segments:
            spans.append((position, position + len(segment.text), segment))
            position += len(segment.text)
        affected = [(a, b, s) for a, b, s in spans if start < b and end > a]
        if not affected:
            continue
        writable = [item for item in affected if item[2].writable]
        if not writable:
            continue
        first_a, _, first = writable[0]
        last_a, last_b, last = affected[-1]
        prefix = first.text[:max(0, start - first_a)]
        suffix = last.text[max(0, end - last_a):] if last.writable else ""
        if first is last:
            first.text = prefix + token + suffix
            write_text(first.element, first.text)
            continue
        first.text = prefix + token
        write_text(first.element, first.text)
        for _, _, segment in affected[1:]:
            if segment is last and segment.writable:
                segment.text = suffix
                write_text(segment.element, suffix)
            elif segment.writable:
                segment.text = ""
                segment.element.text = ""
            else:
                parent = segment.element.getparent()
                if parent is not None:
                    parent.remove(segment.element)
                segment.text = ""


def _replace_across_segments(segments: list[Segment], findings: list[Finding], ctx: TransformContext) -> None:
    accepted = sorted((x for x in findings if x.decision == Decision.AUTO), key=lambda x: x.start)
    text = "".join(seg.text for seg in segments)
    splice_segments(segments, [(f.start, f.end, guard_token(text, f.start, f.end, ctx.token_for(f))) for f in accepted])


def scan_ooxml(path: Path, rel: str, detector, scrub_metadata: bool, inspect_embedded: bool) -> FileResult:
    warnings: list[str] = []
    try:
        snap = _zip_snapshot(path)
        if snap["signatures"]:
            return FileResult(rel, "OOXML", FileStatus.BLOCKED, warnings=["Документ содержит цифровую подпись; изменение нарушит её"], integrity=snap)
        if snap["macros"]:
            return FileResult(rel, "MACRO_OFFICE", FileStatus.REVIEW_REQUIRED, warnings=["Документ содержит VBA; автоматическая перезапись отключена"], integrity=snap)
        findings: list[Finding] = []
        with zipfile.ZipFile(path) as z:
            parts = [n for n in z.namelist() if n.lower().endswith((".xml", ".rels"))]
            trees: list[tuple[str, object]] = []
            for name in parts:
                try:
                    trees.append((name, etree.fromstring(z.read(name), _parser())))
                except etree.XMLSyntaxError:
                    warnings.append(f"Не удалось безопасно разобрать {name}")
            hints = learn_package(dict(trees), detector, rel)
            for name, root in trees:
                _, part_findings, _, _ = _scan_xml_tree(root, name, rel, detector, scrub_metadata, hints)
                findings.extend(part_findings)
        if inspect_embedded and (snap["media"] or snap["embeddings"]):
            warnings.append("Есть изображения или вложенные объекты, их визуальное содержимое не подтверждено")
        status = FileStatus.REVIEW_REQUIRED if warnings or any(f.decision != Decision.AUTO for f in findings) else FileStatus.CLEAN
        return FileResult(rel, "OOXML", status, findings, warnings, integrity=snap)
    except (zipfile.BadZipFile, OSError) as exc:
        return FileResult(rel, "OOXML", FileStatus.BLOCKED, warnings=[f"Повреждённый или недоступный Office-файл: {type(exc).__name__}"])


HEADER_FOOTER_TAGS = {"oddHeader", "evenHeader", "firstHeader", "oddFooter", "evenFooter", "firstFooter"}
# Коды колонтитулов Excel: &L &C &R (позиция), &"шрифт" &12 (размер), &P &N &D &T &F &A (поля). Они не текст:
# `&Rgusakov@firma.ru` — это код правой части и адрес, а не адрес с буквой R.
HEADER_CODES = re.compile(r'&(?:[LCR]|"[^"]*"|\d+|[A-Za-z&])')


def _mask_header_codes(text: str) -> str:
    return HEADER_CODES.sub(lambda m: " " * len(m.group(0)), text)


def _metadata_finding(rel: str, loc: str, kind: str, value: str, start: int, reason: str) -> Finding:
    return Finding(finding_id(rel, loc, kind, value), rel, loc, "META", value, start, start + len(value),
                   Decision.AUTO, 1.0, reason=reason)


def _is_metadata_value(segment: Segment, part: str, hints: "PackageHints") -> bool:
    """Значение свойства документа, которое заменяется целиком. Встроенные имена шаблонов Office не трогаем."""
    tag = _xml_name(segment.element)
    if tag == "Template" or (part == "docProps/app.xml" and segment.text.strip() in hints.templates):
        return not BUILTIN_TEMPLATE_NAME.match(segment.text)
    return tag in METADATA_TAGS or part.endswith("custom.xml")


def _scan_text_groups(root, part: str, rel: str, detector, scrub_metadata: bool, hints: "PackageHints | None" = None):
    hints = hints or PackageHints()
    findings: list[Finding] = []
    groups: list[tuple[list[Segment], list[Finding]]] = []
    scrub_here = scrub_metadata and part.startswith("docProps/")
    for group_name, segments in _groups(root, part):
        text = "".join(s.text for s in segments)
        if not text.strip():
            continue
        loc = f"{part}::{group_name}"
        scan_text = _mask_header_codes(text) if group_name.split(":")[0] in HEADER_FOOTER_TAGS else text
        found = detector.scan(scan_text, rel, loc)
        if hints.person_lists and fold(text.strip()) in hints.person_lists:
            found = _with_list_items(found, scan_text, rel, loc, detector)
        if scrub_here:
            # Свойство документа обезличивается целиком: часть значения, найденная как имя, не должна оставлять
            # рядом остаток («Mikhail Name1» из «Mikhail V. Zakharov»).
            offset, whole = 0, []
            for segment in segments:
                value = segment.text
                is_metadata = _is_metadata_value(segment, part, hints)
                if value and segment.writable and is_metadata and not has_token(value):
                    whole.append(_metadata_finding(rel, loc, "metadata", value, offset, "Метаданные документа"))
                offset += len(value)
            if whole:
                found = whole if len(whole) == len(segments) else found + [
                    w for w in whole if not any(f.start < w.end and f.end > w.start for f in found)]
        if found:
            findings.extend(found)
            groups.append((segments, found))
    return findings, groups


def _is_named_attr(element_name: str, attr_name: str, pivot_cache: bool, part: str = "") -> bool:
    if (element_name, attr_name) in NAMED_ELEMENT_ATTRS or (pivot_cache and element_name == "s" and attr_name == "v"):
        return True
    lowered = part.lower()
    return any(lowered.startswith(prefix) and (element_name, attr_name) in pairs for prefix, pairs in PART_NAMED_ATTRS.items())


PERSON_HEADER = re.compile(r"(?i)\bфио\b|ф\.?\s?и\.?\s?о\b|фамили|сотрудник|владел|ответственн|участник|исполнител|"
                           r"контактное лицо|full name|surname|last name|first name|employee")
NOT_PERSON_HEADER = re.compile(r"(?i)должност|подраздел|отдел|филиал|почт|e-?mail|телефон|числен|кол-?во|количеств|описани|"
                               r"комментар|функци|статус|роль|назван|процесс")
CELL_REF = re.compile(r"^([A-Z]+)(\d+)$")


def _shared_strings(parsed: dict) -> list[str]:
    root = parsed.get("xl/sharedStrings.xml")
    if root is None:
        return []
    out = []
    for si in root:
        if _xml_name(si) != "si":
            continue
        out.append("".join(t.text or "" for t in si.iter() if _xml_name(t) == "t"
                           and not any(_xml_name(a) == "rPh" for a in t.iterancestors())))
    return out


def _sheet_cells(parsed: dict):
    """Текстовые ячейки каждого листа: [(столбец, строка, текст)]."""
    strings = _shared_strings(parsed)
    for name, root in parsed.items():
        if not _is_worksheet(name):
            continue
        cells: list[tuple[str, int, str]] = []
        for cell in root.iter():
            if _xml_name(cell) != "c":
                continue
            match = CELL_REF.match(cell.get("r", ""))
            if not match:
                continue
            kind = cell.get("t")
            text = ""
            if kind == "s":
                v = next((c for c in cell if _xml_name(c) == "v"), None)
                if v is not None and (v.text or "").strip().isdigit() and int(v.text) < len(strings):
                    text = strings[int(v.text)]
            elif kind == "inlineStr":
                text = "".join(t.text or "" for t in cell.iter() if _xml_name(t) == "t")
            if text.strip():
                cells.append((match.group(1), int(match.group(2)), text.strip()))
        yield cells


def _column_values(cells, header: re.Pattern, not_header: re.Pattern):
    """(заголовок, текст) ячеек ниже подходящего заголовка того же столбца."""
    headers: dict[str, tuple[int, str]] = {}
    for column, row, text in cells:
        # «Участники: Орлов, Соколова» — значение с вводным словом, а не заголовок столбца.
        if re.search(r":\s*\S|;", text):
            continue
        if row <= 15 and len(text.split()) <= 5 and header.search(text) and not not_header.search(text):
            headers[column] = max(headers.get(column, (0, "")), (row, text))
    for column, row, text in cells:
        if column in headers and row > headers[column][0]:
            yield headers[column][1], text


# Разделители перечня в одной ячейке: «Иванов И.И.; Петров П.П.», «Участники: Орлов, Соколова».
LIST_SEPARATOR = re.compile(r"\s*(?:[;\n]|,(?=\s*[^\W\d_]))\s*")
LIST_PREFIX = re.compile(r"^[^:;,\n]{1,40}:\s*")


def list_items(text: str) -> list[tuple[int, int]]:
    """Границы элементов перечня. Вводная часть до двоеточия («Участники:») элементом не считается."""
    prefix = LIST_PREFIX.match(text)
    position = prefix.end() if prefix else 0
    spans = []
    for separator in [*LIST_SEPARATOR.finditer(text, position), None]:
        end = separator.start() if separator else len(text)
        item = text[position:end]
        if item.strip():
            left = position + len(item) - len(item.lstrip())
            spans.append((left, left + len(item.strip())))
        if separator:
            position = separator.end()
    return spans


def _person_columns(parsed: dict) -> tuple[set[str], set[str]]:
    values: set[str] = set()
    lists: set[str] = set()
    for cells in _sheet_cells(parsed):
        for _, text in _column_values(cells, PERSON_HEADER, NOT_PERSON_HEADER):
            if len(text.split()) <= 4 and re.match(r"^[^\W\d_]", text):
                values.add(fold(text))
            if not LIST_SEPARATOR.search(text):
                continue
            # Перечень людей в одной ячейке: каждый элемент — такой же человек, как одиночное значение столбца.
            items = [text[a:b] for a, b in list_items(text)]
            if len(items) >= 2 and all(len(i.split()) <= 4 and re.match(r"^[^\W\d_]", i) for i in items):
                values.update(fold(i) for i in items)
                lists.add(fold(text))
    return values, lists


def person_column_values(parsed: dict) -> set[str]:
    """Тексты ячеек из столбцов, в заголовке которых сказано «ФИО», «Фамилия», «Сотрудник» и подобное.

    Название столбца — самая надёжная подсказка, что в ячейке человек: редкая фамилия без имени и отчества
    («Шин», «Сугимото Масатакэ») иначе неотличима от названия. Ячейка-перечень («А; Б; В») даёт каждый свой элемент.
    """
    return _person_columns(parsed)[0]


# Столбцы с географией: филиал, регион, город, площадка. Их значения выдают, где работает клиент.
# «Подразделение», «Отдел», «Должность» сюда не относятся: там обычные слова, а не названия мест.
PLACE_HEADER = re.compile(r"(?i)филиал|регион|город|площадк|территори|локаци|населённ|населенн")
CITY_HEADER = re.compile(r"(?i)город|площадк|локаци|населённ|населенн")
NOT_PLACE_HEADER = re.compile(r"(?i)подраздел|отдел|должност|адрес|телефон|почт|e-?mail|\bкод|кол-?во|количеств|числен|"
                              r"руковод|директор|начальник|менеджер|ответствен|контакт|сотрудник|фио|описани|комментар|"
                              r"статус|доля|сумм|%")
PLACE_STOP = {"не указано", "не указан", "н/д", "н.д.", "нет", "да", "все", "-", "—", "итого", "всего", "прочие", "прочее",
              "другое", "другие", "общий", "общее", "нет данных", "не определено", "без филиала"}
# Строчные слова, допустимые в названии места: «г. Москва», «Иркутская область», «Нерюнгринский р-н».
GEO_DESIGNATOR = re.compile(r"(?i)^(?:г|гор|с|п|пос|пгт|д|ст|рп|обл|р-н|область|края?|района?|округа?|республика)\.?$")
DOTTED_INITIALS = re.compile(r"\b[А-ЯЁA-Z]\.\s?[А-ЯЁA-Z]\.")


# Кириллическая форма состояния: «Выполняется», «Проводится», «Согласовано», «Оплачена».
STATUS_ENDING = re.compile(r"(?i)(?:ется|ится|ено|ана)$")


def _place_like(value: str, header: str) -> bool:
    """Значение из географического столбца похоже на название места, а не на статус, число или обычное слово."""
    words = value.split()
    if not words or len(words) > 3 or fold(value) in PLACE_STOP or fold(value) == fold(header):
        return False
    if not re.match(r"^[^\W\d_]", value) or DOTTED_INITIALS.search(value):
        return False
    if not all(w[:1].isupper() or GEO_DESIGNATOR.match(w) for w in words):
        return False
    core = [w for w in words if not GEO_DESIGNATOR.match(w)]
    for word in core:
        # Окончание формы состояния не касается известных городов: «Астана» остаётся местом.
        if lexicon.is_status_word(word) or (re.search("[а-яё]", word, re.I) and STATUS_ENDING.search(word)
                                            and not lexicon.is_toponym(word)):
            return False
    # Словарное слово годится только как прилагательное места («Дальневосточный», «Кузбасский»): «Код», «Выручка»,
    # «Основной» из соседней таблицы под тем же столбцом — обычные слова.
    return not all(lexicon.is_common_word(w) and not lexicon.is_geo_adjective(w) for w in core)


def _geo_cue(value: str, text: str) -> bool:
    """Значение где-то ещё в книге стоит рядом с географическим словом: «г. Х», «в Х», «филиал Х», «Х район»."""
    name = re.escape(value)
    return bool(re.search(rf"(?i)(?:\b(?:г|гор|пос|пгт|с|д|ст)\.\s*|\b(?:город\w*|филиал\w*|регион\w*|в|во|из)\s+){name}(?!\w)"
                          rf"|(?<!\w){name}\s+(?:област|кра[йяею]|район|округ)", text))


def place_column_values(parsed: dict) -> dict[str, str]:
    """Значение из столбца «Филиал», «Регион», «Город», «Площадка» → вид (CITY или REGION).

    Столбец засчитывается, только если в нём хотя бы два разных похожих на место значения: одиночное значение под
    случайным заголовком («Регион выполнения» над столбцом статусов) — не довод. Одиночное принимается, если оно
    ещё где-то в книге стоит рядом с географическим словом.
    """
    out: dict[str, str] = {}
    for cells in _sheet_cells(parsed):
        columns: dict[str, list[str]] = {}
        for header, text in _column_values(cells, PLACE_HEADER, NOT_PLACE_HEADER):
            value = " ".join(text.split())
            if _place_like(value, header) and value not in columns.setdefault(header, []):
                columns[header].append(value)
        everything = None
        for header, values in columns.items():
            if len(values) < 2:
                everything = everything if everything is not None else "\n".join(t for _, _, t in cells)
                values = [v for v in values if _geo_cue(v, everything)]
            for value in values:
                out.setdefault(value, "CITY" if CITY_HEADER.search(header) else "REGION")
    return out


def register_place_values(parsed: dict, detector, rel: str) -> None:
    """Значения географических столбцов становятся известными названиями для всей книги, в том числе для текста.

    Что детектор и так скрывает целиком (город из справочника, известная организация), не трогаем: у него уже есть
    свой вид. Опознанный человек в таком столбце тоже не место; а «похоже на фамилию» у прилагательного на -ский
    («Заречинский») в столбце «Филиал» — ошибка формы слова, а не человек.
    """
    entities = getattr(detector, "entities", None)
    if entities is None:
        return
    for value, kind in place_column_values(parsed).items():
        if fold(value) in detector.person_values:
            continue
        probe = detector.scan(value, rel, "column:place")
        if any(f.category == "PERSON" and f.decision == Decision.AUTO for f in probe):
            continue
        if any(f.decision == Decision.AUTO and f.start <= 0 and f.end >= len(value) for f in probe):
            continue
        entities.register(kind, value, explicit=True)


def _with_list_items(found: list[Finding], text: str, rel: str, loc: str, detector) -> list[Finding]:
    """Ячейка-перечень людей: каждый элемент проверяется ещё и как отдельное значение столбца.

    Внутри длинной строки редкая фамилия без имени («Громов») не узнаётся, а как одиночное значение столбца
    «Участники» — узнаётся. Находка по элементу заменяет находки внутри него; частично пересекающиеся не трогаем.
    """
    result = list(found)
    for start, end in list_items(text):
        if any(f.decision == Decision.AUTO and f.start <= start and f.end >= end for f in result):
            continue
        for f in detector.scan(text[start:end], rel, loc):
            a, b = f.start + start, f.end + start
            inside = [g for g in result if g.start >= a and g.end <= b]
            if any(g.start < b and g.end > a and g not in inside for g in result):
                continue
            if inside and f.decision != Decision.AUTO:
                continue
            result = [g for g in result if g not in inside]
            result.append(replace(f, start=a, end=b, finding_id=finding_id(f.file, f.location, a, b, f.original)))
    return sorted(result, key=lambda f: f.start)


@dataclass
class PackageHints:
    """Что известно о файле целиком ещё до разбора частей."""
    templates: set[str] = field(default_factory=set)      # имена тем и шаблона, придуманные автором
    person_lists: set[str] = field(default_factory=set)   # ячейки-перечни людей (свёрнутый текст)


def template_names(parsed: dict) -> set[str]:
    """Имена тем, палитр и шаблона, кроме встроенных: они же повторяются в перечне частей app.xml."""
    names: set[str] = set()
    for part, root in parsed.items():
        lowered = part.lower()
        if "/theme/" not in lowered and lowered != "docprops/app.xml":
            continue
        for node in root.iter():
            if not isinstance(node.tag, str):
                continue
            element = _xml_name(node)
            if element == "Template" and node.text:
                names.add(node.text.strip())
            names.update(node.get(attr) for el, attr in TEMPLATE_NAME_ATTRS if el == element and node.get(attr))
    names = {n.strip() for n in names if n.strip() and not BUILTIN_TEMPLATE_NAME.match(n)}
    # В перечне частей имя шаблона пишется без расширения.
    return names | {re.sub(r"(?i)\.(?:pot|dot|xlt)[xm]?$", "", n) for n in names}


def learn_package(parsed: dict, detector, rel: str) -> PackageHints:
    """Обучение детектора на файле целиком до замены: люди из столбцов «ФИО», места из столбцов «Филиал», полные имена."""
    persons, lists = _person_columns(parsed)
    detector.person_values |= persons
    # Learn the document's own full names before scanning, so its abbreviations resolve.
    detector.harvest("\n".join(_harvest_source(root, name) for name, root in parsed.items()), rel)
    register_place_values(parsed, detector, rel)
    return PackageHints(template_names(parsed), lists)


def _harvest_source(root, part: str) -> str:
    """Текст части плюс значения проверяемых атрибутов.

    Полное ФИО может стоять только в имени листа или в кэше сводной таблицы. Если не показать
    его сборщику имён, человек остаётся неопознанным, и вместо замены выходит ручная проверка.
    """
    pivot_cache = "/pivotcache/" in part.lower()
    values = [_part_text(root, part)]
    for node in root.iter():
        element_name = _xml_name(node)
        for attr, value in node.attrib.items():
            if not value or len(value) > MAX_ATTRIBUTE_LENGTH:
                continue
            attr_name = etree.QName(attr).localname if attr.startswith("{") else attr
            if _is_named_attr(element_name, attr_name, pivot_cache, part) or attr_name in SENSITIVE_ATTRS:
                values.append(value)
    return "\n".join(values)


def ooxml_text(path: Path) -> str:
    """Видимое содержимое документа без разметки.

    Для сверки возврата с оригиналом сырой XML не годится: пересборка контейнера меняет
    порядок атрибутов и кавычки, и байты расходятся даже при безупречном возврате.
    """
    parts: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for name in sorted(archive.namelist()):
            if not name.lower().endswith(".xml"):
                continue
            try:
                root = etree.fromstring(archive.read(name), _parser())
            except etree.XMLSyntaxError:
                continue
            parts.append(_harvest_source(root, name))
    return "\n".join(parts)


def has_token(value: str) -> bool:
    """Значение уже обезличено: токен искать в нём ещё раз не нужно."""
    return bool(TOKEN_RE.search(value) or EMAIL_TOKEN_RE.search(value))


def _scan_attributes(root, part: str, rel: str, detector, scrub_metadata: bool):
    findings: list[Finding] = []
    groups: list[tuple[object, str, list[Finding]]] = []
    # Кэш сводной таблицы держит вторую копию исходных ячеек в атрибуте: <s v="Иванов"/>.
    # Значения листа обезличивались, а кэш уезжал наружу нетронутым.
    pivot_cache = "/pivotcache/" in part.lower()
    for node in root.iter():
        element_name = _xml_name(node)
        for attr, value in node.attrib.items():
            if not value or len(value) > MAX_ATTRIBUTE_LENGTH:
                continue
            attr_name = etree.QName(attr).localname if attr.startswith("{") else attr
            named = _is_named_attr(element_name, attr_name, pivot_cache, part) and not value.lstrip().startswith("<")
            sensitive = attr_name in SENSITIVE_ATTRS
            link = attr_name in LINK_ATTRS and value.startswith(("http://", "https://", "mailto:", "tel:"))
            local_path = attr_name in LINK_ATTRS and bool(LOCAL_PATH.match(value)) and not has_token(value)
            if not (named or sensitive or link or local_path):
                continue
            loc = f"{part}::attribute:{element_name}:{attr_name}"
            # Имена диапазонов, таблиц и столбцов пишут через подчёркивание («Клиент_Ромашка»): для разбора оно разделитель
            # слов, а не часть слова. Длина строки не меняется, поэтому позиции находок остаются верными.
            found = detector.scan(value.replace("_", " ") if (element_name, attr_name) in IDENTIFIER_ATTRS else value, rel, loc)
            if local_path and scrub_metadata:
                found = [_metadata_finding(rel, loc, "local-path", value, 0, "Путь на компьютере автора")]
            if scrub_metadata and sensitive and not found and not has_token(value):
                found = [_metadata_finding(rel, loc, "metadata-attr", value, 0, "Метаданные автора")]
            if scrub_metadata and (element_name, attr_name) in TEMPLATE_NAME_ATTRS and not has_token(value) \
                    and not BUILTIN_TEMPLATE_NAME.match(value):
                # Имя темы — ярлык шаблона, а не содержимое: заменяется целиком, смысл документа от этого не страдает.
                found = [_metadata_finding(rel, loc, "template-name", value, 0, "Имя темы или шаблона оформления")]
            if found:
                findings.extend(found)
                groups.append((node, attr, found))
    return findings, groups


def _scan_xml_tree(root, part: str, rel: str, detector, scrub_metadata: bool, hints: PackageHints | None = None):
    """Findings for one already-parsed part, so a part is never parsed more than once."""
    text_findings, text_groups = _scan_text_groups(root, part, rel, detector, scrub_metadata, hints)
    attr_findings, attr_groups = _scan_attributes(root, part, rel, detector, scrub_metadata)
    return root, text_findings + attr_findings, text_groups, attr_groups


DC_NS = "http://purl.org/dc/elements/1.1/"
MARKER_PREFIX = "anonymizer:"


def stamp_vault_marker(root, vault_id: str) -> None:
    """Пометка в свойствах документа: каким хранилищем сделан файл. Существующий идентификатор документа не затирается."""
    for node in root.iter(f"{{{DC_NS}}}identifier"):
        if (node.text or "").strip() and not (node.text or "").startswith(MARKER_PREFIX):
            return
        node.text = MARKER_PREFIX + vault_id
        return
    node = etree.SubElement(root, f"{{{DC_NS}}}identifier", nsmap={"dc": DC_NS})
    node.text = MARKER_PREFIX + vault_id


def strip_vault_marker(root) -> bool:
    removed = False
    for node in list(root.iter(f"{{{DC_NS}}}identifier")):
        if (node.text or "").startswith(MARKER_PREFIX):
            node.getparent().remove(node)
            removed = True
    return removed


def read_vault_marker(path: Path) -> str:
    """Идентификатор хранилища, которым обезличен файл, или пустая строка (пометка пропала при пересохранении)."""
    if path.suffix.lower() == ".pdf":
        try:
            with fitz.open(path) as doc:
                producer = (doc.metadata or {}).get("producer") or ""
            return producer[len(MARKER_PREFIX):].strip() if producer.startswith(MARKER_PREFIX) else ""
        except Exception:
            return ""
    try:
        with zipfile.ZipFile(path) as z:
            if "docProps/core.xml" not in z.namelist():
                return ""
            root = etree.fromstring(z.read("docProps/core.xml"), _parser())
    except Exception:
        return ""
    for node in root.iter(f"{{{DC_NS}}}identifier"):
        if (node.text or "").startswith(MARKER_PREFIX):
            return node.text[len(MARKER_PREFIX):].strip()
    return ""


def transform_ooxml(src: Path, dst: Path, rel: str, detector, ctx: TransformContext, depth: int = 0) -> FileResult:
    before = _zip_snapshot(src)
    if before["macros"] or before["signatures"]:
        safe_copy(src, dst)
        reason = "VBA" if before["macros"] else "цифровая подпись"
        return FileResult(rel, "OOXML", FileStatus.REVIEW_REQUIRED if before["macros"] else FileStatus.BLOCKED,
                          warnings=[f"Автоматическая обработка отключена: {reason}"], output_path=str(dst), integrity=before)
    findings: list[Finding] = []
    warnings: list[str] = []
    output_text: list[str] = []
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".anon-office-", suffix=src.suffix, dir=dst.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(src, "r") as zin:
            parsed: dict[str, object] = {}
            for info in zin.infolist():
                if info.filename.lower().endswith((".xml", ".rels")):
                    try:
                        parsed[info.filename] = etree.fromstring(zin.read(info.filename), _parser())
                    except etree.XMLSyntaxError:
                        warnings.append(f"Не обработан внутренний XML: {info.filename}")
            hints = learn_package(parsed, detector, rel)
            thumbnails = drop_thumbnails(parsed, zin.namelist()) if ctx.settings.scrub_metadata else set()
            removal = drop_embedded_payloads(parsed, zin.namelist(), depth) if ctx.settings.scrub_metadata \
                else EmbeddedRemoval()
            dropped = thumbnails | removal.parts
            numbers_on = ctx.settings.numbers and src.suffix.lower() == ".xlsx"
            date_styles: set[int] = set()
            avoid: set[str] = set()
            amounts = 0
            if src.suffix.lower() == ".xlsx":
                styles = parsed.get("xl/styles.xml")
                date_styles = numeric.date_style_indexes(styles) if styles is not None else set()
            if numbers_on:
                for name, root in parsed.items():
                    if _is_worksheet(name):
                        avoid.update(canon for _, _, canon in numeric.numeric_cells(root, date_styles))
            elif src.suffix.lower() == ".xlsx" and depth == 0:
                amounts = sum(numeric.should_replace(canon) for name, root in parsed.items() if _is_worksheet(name)
                              for _, _, canon in numeric.numeric_cells(root, date_styles))
            with zipfile.ZipFile(tmp_name, "w") as zout:
                for info in zin.infolist():
                    if info.filename in dropped:
                        continue
                    raw = zin.read(info.filename)
                    root = parsed.get(info.filename)
                    if root is None and is_embedded_package(info.filename) and depth < 2:
                        raw, inner = _transform_embedded(raw, info.filename, rel, detector, ctx, depth)
                        findings.extend(inner.findings)
                        warnings.extend(f"{info.filename}: {w}" for w in inner.warnings)
                    if root is not None:
                        _, part_findings, groups, attrs = _scan_xml_tree(root, info.filename, rel, detector,
                                                                         ctx.settings.scrub_metadata, hints)
                        part_findings = ctx.apply_overrides(part_findings)
                        by_id = {f.finding_id: f for f in part_findings}
                        findings.extend(part_findings)
                        for segments, original_group in groups:
                            _replace_across_segments(segments, [by_id[f.finding_id] for f in original_group], ctx)
                        if _is_worksheet(info.filename):
                            _text_into_numeric_cells(root)
                        for node, attr, original_attr in attrs:
                            value = replace_findings(node.get(attr, ""), [by_id[f.finding_id] for f in original_attr], ctx,
                                                     guard=(_xml_name(node), etree.QName(attr).localname) not in IDENTIFIER_ATTRS)
                            node.set(attr, value)
                        if numbers_on:
                            lowered = info.filename.lower()
                            if _is_worksheet(info.filename):
                                done, cleared = numeric.anonymize_worksheet(root, date_styles, ctx.vault, avoid)
                                ctx.numeric["replaced"] = ctx.numeric.get("replaced", 0) + done
                                ctx.numeric["formulas_cleared"] = ctx.numeric.get("formulas_cleared", 0) + cleared
                            elif "/charts/" in lowered and lowered.endswith(".xml"):
                                ctx.numeric["replaced"] = ctx.numeric.get("replaced", 0) + \
                                    numeric.anonymize_chart_cache(root, ctx.vault, avoid)
                            elif lowered == "xl/workbook.xml":
                                numeric.request_recalculation(root)
                        output_text.append(_part_text(root, info.filename))
                        if depth == 0 and info.filename == "docProps/core.xml" and getattr(ctx.vault, "vault_id", ""):
                            stamp_vault_marker(root, ctx.vault.vault_id)
                        raw = etree.tostring(root, xml_declaration=raw.lstrip().startswith(b"<?xml"),
                                             encoding="UTF-8", standalone=None)
                    zout.writestr(info, raw)
        os.replace(tmp_name, dst)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    after = _zip_snapshot(dst)
    structural_keys = ("sheets", "slides", "formulas", "tables", "charts", "comments", "media", "embeddings", "macros", "signatures")
    # Удалённые эскиз и вложения — намеренное отличие, а не порча структуры.
    expected_names = [n for n in before["names"] if n not in dropped]
    expected_hashes = {n: h for n, h in before["binary_hashes"].items() if n not in dropped}
    expected = {**before, "embeddings": before["embeddings"] - len(removal.parts)}
    integrity_ok = (expected_names == after["names"] and expected_hashes == after["binary_hashes"]
                    and all(expected[k] == after[k] for k in structural_keys))
    residual = _residuals(detector, "\n".join(output_text), findings)
    if not integrity_ok:
        warnings.append("Нарушена структурная целостность OOXML")
    if residual:
        warnings.append("После обработки остались критические совпадения")
    embedded = unprocessed_embeddings(after["names"], depth) if ctx.settings.inspect_embedded else {}
    if embedded:
        warnings.append(embedded_warning(embedded))
    notices: list[str] = []
    if thumbnails:
        notices.append("Эскиз первой страницы удалён: на этой картинке читалось исходное содержимое. "
                       "Office создаст новый эскиз при следующем сохранении")
    if removal.parts:
        notices.append(removal_notice(removal))
    if ctx.settings.inspect_embedded and after["media"]:
        notices.append(f"В файле есть изображения ({after['media']}). "
                       "Текст на картинках программа не читает: проверьте их вручную")
    if amounts >= MANY_AMOUNTS:
        notices.append(f"Числа в таблицах не обезличены (значений: {amounts}). Если суммы и показатели — коммерческая "
                       "тайна, включите «Заменять числа в Excel» и обезличьте файл заново")
    status = FileStatus.CLEAN if integrity_ok and not residual and not warnings else FileStatus.REVIEW_REQUIRED
    result = FileResult(rel, "OOXML", status, findings, warnings, str(dst),
                        {"open_ok": True, "structure_equal": integrity_ok, "before": before, "after": after,
                         "dropped": sorted(dropped)})
    result.notices = notices
    return result


def _transform_embedded(raw: bytes, name: str, rel: str, detector, ctx: TransformContext, depth: int):
    """Вложенная книга (данные диаграммы) обезличивается тем же способом, что и сам файл."""
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / ("in" + Path(name).suffix)
        target = Path(tmp) / ("out" + Path(name).suffix)
        source.write_bytes(raw)
        try:
            result = transform_ooxml(source, target, rel, detector, ctx, depth + 1)
        except Exception:
            return raw, FileResult(rel, "OOXML", FileStatus.BLOCKED,
                                   warnings=["Вложенный файл не удалось обезличить: его содержимое осталось как есть"])
        if result.status == FileStatus.BLOCKED or not target.exists():
            return raw, result
        return target.read_bytes(), result


def _is_worksheet(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith("xl/worksheets/") and lowered.endswith(".xml") and "/_rels/" not in lowered


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def _pdf_warnings(images: int, chars: int, pages: int, annotations: int, links: int, embedded: int) -> list[str]:
    """Проблемы, из-за которых нельзя утверждать, что файл безопасен."""
    warnings = []
    if images and chars < max(30, pages * 20):
        warnings.append("Похоже, это скан: текста в нём почти нет, а изображения программа не читает. "
                        "Имена на изображении останутся видимыми")
    if annotations:
        warnings.append("В PDF есть примечания или комментарии: их содержимое не проверялось")
    if embedded:
        warnings.append("В PDF есть вложенные файлы: они не обезличиваются")
    return warnings


def _pdf_notices(images: int, chars: int, pages: int, links: int) -> list[str]:
    notices = []
    if images and chars >= max(30, pages * 20):
        notices.append(f"В PDF есть изображения ({images}). Текст на них программа не читает: проверьте вручную")
    if links:
        notices.append("В PDF есть гиперссылки: адреса в них не изменялись")
    return notices


# Свойства документа PDF. Имя автора живёт здесь, а не на странице, и до сих пор уезжало
# наружу нетронутым: файл без единого имени в тексте объявлялся чистым вместе с «/Author».
PDF_METADATA_FIELDS = ("title", "author", "subject", "keywords", "creator", "producer")


def _pdf_metadata(doc) -> dict[str, str]:
    meta = doc.metadata or {}
    return {field: meta[field] for field in PDF_METADATA_FIELDS if meta.get(field)}


def scan_pdf(path: Path, rel: str, detector) -> FileResult:
    findings: list[Finding] = []
    try:
        doc = fitz.open(path)
        if getattr(doc, "get_sigflags", lambda: -1)() > 0:
            doc.close()
            return FileResult(rel, "PDF", FileStatus.BLOCKED, warnings=["PDF содержит цифровую подпись; изменение нарушит её"])
        pages, images, chars, annotations, links = len(doc), 0, 0, 0, 0
        embedded = getattr(doc, "embfile_count", lambda: 0)()
        page_text = []
        for page in doc:
            text = page.get_text("text")
            page_text.append(text)
            chars += len(text.strip())
            images += len(page.get_images(full=True))
            annotations += sum(1 for _ in (page.annots() or []))
            links += len(page.get_links())
        metadata = _pdf_metadata(doc)
        doc.close()
        detector.harvest("\n".join(page_text), rel)
        for i, text in enumerate(page_text):
            findings.extend(detector.scan(text, rel, f"page:{i + 1}"))
        for field, value in metadata.items():
            findings.extend(detector.scan(value, rel, f"metadata:{field}"))
        warnings = _pdf_warnings(images, chars, pages, annotations, links, embedded)
        status = FileStatus.REVIEW_REQUIRED if warnings else FileStatus.CLEAN
        return FileResult(rel, "PDF", status, findings, warnings,
                          integrity={"pages": pages, "images": images, "text_chars": chars,
                                     "annotations": annotations, "links": links, "embedded": embedded})
    except Exception as exc:
        return FileResult(rel, "PDF", FileStatus.BLOCKED, warnings=[f"PDF не открывается: {type(exc).__name__}"])


PDF_PERSONAL_FIELDS = ("title", "author", "subject", "keywords")


def _pdf_extra_texts(doc) -> list[str]:
    """Текст вне страниц: закладки, примечания, свойства документа."""
    texts = list(_pdf_metadata(doc).values())
    try:
        texts.extend(str(entry[1]) for entry in doc.get_toc(simple=False))
    except Exception:
        pass
    for page in doc:
        for annot in (page.annots() or []):
            try:
                texts.extend(str(v) for k, v in (annot.info or {}).items() if k in ("content", "title", "subject") and v)
            except Exception:
                continue
        try:
            texts.extend(str(w.field_value) for w in (page.widgets() or []) if isinstance(w.field_value, str) and w.field_value)
        except Exception:
            continue
    return texts


def _scrub_pdf_extras(doc, rel: str, detector, ctx: TransformContext, findings: list[Finding]) -> int:
    """Закладки, XMP и текст примечаний: такое же содержимое файла, как страницы, но в других местах.

    Возвращает, сколько примечаний обработано (их текст обезличен).
    """
    try:
        doc.del_xml_metadata()        # XMP дублирует автора и название и не покрыт обычными свойствами
    except Exception:
        pass
    try:
        toc = doc.get_toc(simple=False)
        changed = False
        for entry in toc:
            found = ctx.apply_overrides(detector.scan(entry[1], rel, "toc"))
            if found:
                findings.extend(found)
                entry[1] = replace_findings(entry[1], found, ctx)
                changed = True
        if changed:
            doc.set_toc(toc)
    except Exception:
        pass
    done = 0
    for page in doc:
        try:
            for widget in (page.widgets() or []):
                value = widget.field_value
                if not isinstance(value, str) or not value.strip() or has_token(value):
                    continue
                found = ctx.apply_overrides(detector.scan(value, rel, "form-field"))
                if found:
                    findings.extend(found)
                    widget.field_value = replace_findings(value, found, ctx)
                    widget.update()
        except Exception:
            pass
        for annot in (page.annots() or []):
            try:
                info = dict(annot.info or {})
                new_info, touched = {}, False
                for key in ("content", "title", "subject"):
                    value = info.get(key) or ""
                    if not value.strip() or has_token(value):
                        continue
                    found = ctx.apply_overrides(detector.scan(value, rel, f"annotation:{key}"))
                    if found:
                        findings.extend(found)
                        new_info[key] = replace_findings(value, found, ctx)
                        touched = True
                if touched:
                    annot.set_info(**new_info)
                    annot.update()
                if not (info.get("content") or "").strip() or touched:
                    done += 1
            except Exception:
                continue
    return done


def _search_rects(page, value: str):
    rects = page.search_for(value)
    if not rects and value != " ".join(value.split()):
        rects = page.search_for(" ".join(value.split()))
    return rects


def transform_pdf(src: Path, dst: Path, rel: str, detector, ctx: TransformContext) -> FileResult:
    doc = fitz.open(src)
    if getattr(doc, "get_sigflags", lambda: -1)() > 0:
        doc.close()
        safe_copy(src, dst)
        return FileResult(rel, "PDF", FileStatus.BLOCKED, warnings=["PDF содержит цифровую подпись; файл не изменялся"], output_path=str(dst))
    findings: list[Finding] = []
    warnings: list[str] = []
    images = chars = annotations = links = 0
    embedded = getattr(doc, "embfile_count", lambda: 0)()
    page_text = [page.get_text("text") for page in doc]
    # Имена из закладок, примечаний и свойств учатся так же, как имена со страниц: полное ФИО там тоже бывает единственным.
    detector.harvest("\n".join([*page_text, *_pdf_extra_texts(doc)]), rel)
    for i, page in enumerate(doc):
        text = page_text[i]
        chars += len(text.strip())
        images += len(page.get_images(full=True))
        annotations += sum(1 for _ in (page.annots() or []))
        links += len(page.get_links())
        page_findings = ctx.apply_overrides(detector.scan(text, rel, f"page:{i + 1}"))
        findings.extend(page_findings)
        page_findings = [f for f in page_findings if f.decision == Decision.AUTO]
        # One search per distinct value, one token per occurrence: searching per finding would
        # multiply tokens by the number of repeats on the page.
        by_value: dict[str, list[Finding]] = {}
        for f in sorted(page_findings, key=lambda x: x.start):
            by_value.setdefault(f.original, []).append(f)
        redacted = False
        for value, group in by_value.items():
            rects = _search_rects(page, value)
            if not rects:
                warnings.append(f"Страница {i + 1}: не найдены координаты фрагмента")
                continue
            for index, rect in enumerate(rects):
                token = ctx.token_for(group[min(index, len(group) - 1)])
                fitted = rect.width / max(1.0, len(token) * TOKEN_GLYPH_WIDTH)
                font_size = max(PLACEHOLDER_FONT_RANGE[0], min(PLACEHOLDER_FONT_RANGE[1], fitted))
                page.add_redact_annot(rect, text=token, fontname="helv", fontsize=font_size,
                                      fill=(1, 1, 1), text_color=(0, 0, 0))
                redacted = True
        if redacted:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS,
                                  graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                                  text=fitz.PDF_REDACT_TEXT_REMOVE)
    metadata = dict(doc.metadata or {})
    scrubbed = False
    for field, value in _pdf_metadata(doc).items():
        if has_token(value):
            continue
        if field in PDF_PERSONAL_FIELDS and ctx.settings.scrub_metadata:
            # Название, автор, тема и ключевые слова обезличиваются целиком, как свойства документа Office.
            found = [_metadata_finding(rel, f"metadata:{field}", "metadata", value, 0, "Метаданные документа")]
        else:
            found = ctx.apply_overrides(detector.scan(value, rel, f"metadata:{field}"))
        if not found:
            continue
        findings.extend(found)
        metadata[field] = replace_findings(value, found, ctx)
        scrubbed = True
    if getattr(ctx.vault, "vault_id", ""):
        metadata["producer"] = MARKER_PREFIX + ctx.vault.vault_id
        scrubbed = True
    if scrubbed:
        doc.set_metadata(metadata)
    annotations_done = _scrub_pdf_extras(doc, rel, detector, ctx, findings)
    dst.parent.mkdir(parents=True, exist_ok=True)
    pages_before = len(doc)
    doc.save(dst, garbage=4, deflate=True, clean=True)
    doc.close()
    verify_doc = fitz.open(dst)
    verify_text = "\n".join(page.get_text("text") for page in verify_doc)
    pages_after = len(verify_doc)
    verify_doc.close()
    residual = _residuals(detector, verify_text, findings)
    if residual:
        warnings.append("Исходный текст всё ещё извлекается из PDF")
    if pages_before != pages_after:
        warnings.append("Изменилось количество страниц")
    for warning in _pdf_warnings(images, chars, pages_before, max(0, annotations - annotations_done), links, embedded):
        if warning not in warnings:
            warnings.append(warning)
    status = FileStatus.CLEAN if not warnings else FileStatus.REVIEW_REQUIRED
    result = FileResult(rel, "PDF", status, findings, warnings, str(dst),
                        {"pages_before": pages_before, "pages_after": pages_after, "original_text_removed": not residual})
    result.notices = _pdf_notices(images, chars, pages_before, links)
    return result


def extract_text(path: Path) -> str:
    """Читаемый текст файла: для проверки «уже обезличен ли файл» и для сверки результата."""
    kind = classify(path)
    if kind == "TEXT":
        return decode_text(path.read_bytes())
    if kind == "OOXML":
        return ooxml_text(path)
    if kind == "PDF":
        doc = fitz.open(path)
        try:
            return "\n".join(page.get_text("text", sort=True) for page in doc)
        finally:
            doc.close()
    return ""
