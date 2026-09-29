from __future__ import annotations

import hashlib
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import fitz
from lxml import etree

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
# Название (`title`) и категория обычно содержат имя клиента или проекта: «Отчёт для ООО «Вектор-Строй»».
METADATA_TAGS = {"creator", "lastModifiedBy", "company", "manager", "comments", "description", "subject", "keywords",
                 "title", "category", "Company", "Manager", "contentStatus", "HyperlinkBase"}
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
IDENTIFIER_ATTRS = {("table", "name"), ("table", "displayName"), ("tableColumn", "name"), ("definedName", "name")}
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
    ("property", "name"),
}
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


EMBEDDED_SUFFIXES = (".xlsx", ".docx", ".pptx")


def is_embedded_package(name: str) -> bool:
    """Книга, документ или презентация внутри файла: данные диаграммы, вставленный объект."""
    lowered = name.lower()
    return "/embeddings/" in lowered and lowered.endswith(EMBEDDED_SUFFIXES)


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
                finding.reason = "Скрыто по решению пользователя"
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
            first.element.text = first.text
            continue
        first.text = prefix + token
        first.element.text = first.text
        for _, _, segment in affected[1:]:
            if segment is last and segment.writable:
                segment.text = suffix
                segment.element.text = suffix
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
            detector.person_values |= person_column_values(dict(trees))
            # Learn the document's own full names before scanning, so its abbreviations resolve.
            detector.harvest("\n".join(_harvest_source(root, name) for name, root in trees), rel)
            for name, root in trees:
                _, part_findings, _, _ = _scan_xml_tree(root, name, rel, detector, scrub_metadata)
                findings.extend(part_findings)
        if inspect_embedded and (snap["media"] or snap["embeddings"]):
            warnings.append("Есть изображения или вложенные объекты, их визуальное содержимое не подтверждено")
        status = FileStatus.REVIEW_REQUIRED if warnings or any(f.decision != Decision.AUTO for f in findings) else FileStatus.CLEAN
        return FileResult(rel, "OOXML", status, findings, warnings, integrity=snap)
    except (zipfile.BadZipFile, OSError) as exc:
        return FileResult(rel, "OOXML", FileStatus.BLOCKED, warnings=[f"Повреждённый или недоступный Office-файл: {type(exc).__name__}"])


HEADER_FOOTER_TAGS = {"oddHeader", "evenHeader", "firstHeader", "oddFooter", "evenFooter", "firstFooter"}
# Коды колонтитулов Excel: &L &C &R (позиция), &"шрифт" &12 (размер), &P &N &D &T &F &A (поля). Они не текст:
# `&Rgladyshev@firma.ru` — это код правой части и адрес, а не адрес с буквой R.
HEADER_CODES = re.compile(r'&(?:[LCR]|"[^"]*"|\d+|[A-Za-z&])')


def _mask_header_codes(text: str) -> str:
    return HEADER_CODES.sub(lambda m: " " * len(m.group(0)), text)


def _metadata_finding(rel: str, loc: str, kind: str, value: str, start: int, reason: str) -> Finding:
    return Finding(finding_id(rel, loc, kind, value), rel, loc, "META", value, start, start + len(value),
                   Decision.AUTO, 1.0, reason=reason)


def _scan_text_groups(root, part: str, rel: str, detector, scrub_metadata: bool):
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
        if scrub_here:
            # Свойство документа обезличивается целиком: часть значения, найденная как имя, не должна оставлять
            # рядом остаток («Mikhail Name1» из «Mikhail V. Zakharov»).
            offset, whole = 0, []
            for segment in segments:
                value = segment.text
                is_metadata = _xml_name(segment.element) in METADATA_TAGS or part.endswith("custom.xml")
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


def person_column_values(parsed: dict) -> set[str]:
    """Тексты ячеек из столбцов, в заголовке которых сказано «ФИО», «Фамилия», «Сотрудник» и подобное.

    Название столбца — самая надёжная подсказка, что в ячейке человек: редкая фамилия без имени и отчества
    («Шин», «Кокубу Масатакэ») иначе неотличима от названия.
    """
    strings = _shared_strings(parsed)
    values: set[str] = set()
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
        headers: dict[str, int] = {}
        for column, row, text in cells:
            if row <= 15 and PERSON_HEADER.search(text) and not NOT_PERSON_HEADER.search(text):
                headers[column] = max(headers.get(column, 0), row)
        for column, row, text in cells:
            if column in headers and row > headers[column] and len(text.split()) <= 4 and re.match(r"^[^\W\d_]", text):
                values.add(fold(text))
    return values


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
            found = detector.scan(value, rel, loc)
            if local_path and scrub_metadata:
                found = [_metadata_finding(rel, loc, "local-path", value, 0, "Путь на компьютере автора")]
            if scrub_metadata and sensitive and not found and not has_token(value):
                found = [_metadata_finding(rel, loc, "metadata-attr", value, 0, "Метаданные автора")]
            if found:
                findings.extend(found)
                groups.append((node, attr, found))
    return findings, groups


def _scan_xml_tree(root, part: str, rel: str, detector, scrub_metadata: bool):
    """Findings for one already-parsed part, so a part is never parsed more than once."""
    text_findings, text_groups = _scan_text_groups(root, part, rel, detector, scrub_metadata)
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
            detector.person_values |= person_column_values(parsed)
            detector.harvest("\n".join(_harvest_source(root, name) for name, root in parsed.items()), rel)
            numbers_on = ctx.settings.numbers and src.suffix.lower() == ".xlsx"
            date_styles: set[int] = set()
            avoid: set[str] = set()
            if numbers_on:
                styles = parsed.get("xl/styles.xml")
                date_styles = numeric.date_style_indexes(styles) if styles is not None else set()
                for name, root in parsed.items():
                    if _is_worksheet(name):
                        avoid.update(canon for _, _, canon in numeric.numeric_cells(root, date_styles))
            with zipfile.ZipFile(tmp_name, "w") as zout:
                for info in zin.infolist():
                    raw = zin.read(info.filename)
                    root = parsed.get(info.filename)
                    if root is None and is_embedded_package(info.filename) and depth < 2:
                        raw, inner = _transform_embedded(raw, info.filename, rel, detector, ctx, depth)
                        findings.extend(inner.findings)
                        warnings.extend(f"{info.filename}: {w}" for w in inner.warnings)
                    if root is not None:
                        _, part_findings, groups, attrs = _scan_xml_tree(root, info.filename, rel, detector,
                                                                         ctx.settings.scrub_metadata)
                        part_findings = ctx.apply_overrides(part_findings)
                        by_id = {f.finding_id: f for f in part_findings}
                        findings.extend(part_findings)
                        for segments, original_group in groups:
                            _replace_across_segments(segments, [by_id[f.finding_id] for f in original_group], ctx)
                        if _is_worksheet(info.filename):
                            _text_into_numeric_cells(root)
                        for node, attr, original_attr in attrs:
                            value = replace_findings(node.get(attr, ""), [by_id[f.finding_id] for f in original_attr], ctx,
                                                     guard=(_xml_name(node), attr) not in IDENTIFIER_ATTRS)
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
    integrity_ok = (before["names"] == after["names"] and before["binary_hashes"] == after["binary_hashes"]
                    and all(before[k] == after[k] for k in structural_keys))
    residual = _residuals(detector, "\n".join(output_text), findings)
    if not integrity_ok:
        warnings.append("Нарушена структурная целостность OOXML")
    if residual:
        warnings.append("После обработки остались критические совпадения")
    notices: list[str] = []
    if ctx.settings.inspect_embedded and (after["media"] or after["embeddings"]):
        notices.append(f"В файле есть изображения или вложенные объекты ({after['media'] + after['embeddings']}). "
                       "Текст на картинках программа не читает: проверьте их вручную")
    status = FileStatus.CLEAN if integrity_ok and not residual and not warnings else FileStatus.REVIEW_REQUIRED
    result = FileResult(rel, "OOXML", status, findings, warnings, str(dst),
                        {"open_ok": True, "structure_equal": integrity_ok, "before": before, "after": after})
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
