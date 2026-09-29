"""Возврат исходных значений в файл, который вернулся из внешней модели.

Файл после модели редко совпадает с отправленным: порядок данных другой, часть строк добавлена,
листы перенесены. Поэтому восстановление не сопоставляет структуры, а ищет токены в любом месте
любого текстового узла и подставляет значение из хранилища. Формулы, форматирование и всё, что в
токенах не участвует, остаётся как вернула модель.

Токен, которого нет в хранилище, не угадывается: подставить вместо `Name77` похожее `Name7` значило бы
приписать документу чужое имя. Такой токен остаётся в файле и попадает в отчёт.
"""

from __future__ import annotations

import os
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import fitz
from lxml import etree

from . import numbers as numeric
from .formats import (HEADER_FOOTER_TAGS, _groups, _is_worksheet, _mask_header_codes, _parser, _xml_name, _zip_snapshot,
                      is_embedded_package, splice_segments, strip_vault_marker)
from .tokens import EMAIL_TOKEN_RE, TOKEN_RE, TOKEN_RE_AFTER_UNDERSCORE, parse_match
from .util import atomic_write, sniff_encoding
from .vault import Vault

MAX_SHEET_NAME = 31
INVALID_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")


@dataclass
class RestoreStats:
    found: int = 0
    restored: int = 0
    unknown: dict[str, int] = field(default_factory=dict)        # токен → сколько раз
    removed: dict[str, int] = field(default_factory=dict)        # токены удалённых секретов
    variant_fallback: int = 0
    endings: int = 0
    numbers: int = 0
    number_cells: list[str] = field(default_factory=list)
    sheet_fixes: list[str] = field(default_factory=list)
    entities: set[str] = field(default_factory=set)              # какие токены встретились и были восстановлены

    def merge(self, other: "RestoreStats") -> None:
        self.found += other.found
        self.restored += other.restored
        for name in ("unknown", "removed"):
            target = getattr(self, name)
            for token, count in getattr(other, name).items():
                target[token] = target.get(token, 0) + count
        self.variant_fallback += other.variant_fallback
        self.endings += other.endings
        self.numbers += other.numbers
        self.number_cells.extend(other.number_cells)
        self.sheet_fixes.extend(other.sheet_fixes)
        self.entities |= other.entities


class Restorer:
    """Подставляет значения вместо токенов и считает, что удалось, а что нет."""

    def __init__(self, vault: Vault):
        self.vault = vault
        self.stats = RestoreStats()
        self._cache: dict[str, str | None] = {}

    # -- поиск ----------------------------------------------------------------

    def matches(self, text: str, after_underscore: bool = False) -> list[tuple[int, int, str]]:
        """(начало, конец, замена) для каждого токена, который можно вернуть; остальное учитывается в отчёте."""
        out: list[tuple[int, int, str]] = []
        if not any(ch.isdigit() for ch in text):
            return out
        for regex, is_email in ((TOKEN_RE_AFTER_UNDERSCORE if after_underscore else TOKEN_RE, False), (EMAIL_TOKEN_RE, True)):
            for m in regex.finditer(text):
                replacement = self._resolve(m, is_email)
                if replacement is not None:
                    start, end = m.start(), m.end()
                    # Невидимый разделитель, поставленный при обезличивании, уходит вместе с токеном.
                    if start > 0 and text[start - 1] == "\u200b":
                        start -= 1
                    if end < len(text) and text[end] == "\u200b":
                        end += 1
                    out.append((start, end, replacement))
        return out

    def substitute(self, text: str, after_underscore: bool = False) -> str:
        edits = self.matches(text, after_underscore)
        if not edits:
            return text
        edits.sort()
        pieces, cursor = [], 0
        for start, end, replacement in edits:
            if start < cursor:
                continue
            pieces.append(text[cursor:start])
            pieces.append(replacement)
            cursor = end
        pieces.append(text[cursor:])
        return "".join(pieces)

    def _resolve(self, m: re.Match[str], is_email: bool) -> str | None:
        raw = m.group(0)
        self.stats.found += 1
        following = m.string[m.end():m.end() + 1]
        if following and "а" <= following <= "я" and following.islower():
            self.stats.endings += 1
        kind, number, variant = parse_match(m, email=is_email)
        result = self.vault.lookup(kind, number, variant)
        if result.status == "ok":
            self.stats.restored += 1
            self.stats.entities.add(result.base)
            return result.original
        if result.status == "variant_missing":
            # `Name1_2024`: суффикс не номер написания, а часть текста. Возвращаем первое написание и суффикс.
            self.stats.variant_fallback += 1
            self.stats.restored += 1
            self.stats.entities.add(result.base)
            variant_text = m.group(3) if m.lastindex and m.lastindex >= 3 and m.group(3) else ""
            return (result.original or "") + (f"_{variant_text}" if variant_text else "")
        if result.status == "removed":
            self.stats.removed[result.base] = self.stats.removed.get(result.base, 0) + 1
            return None
        canonical = f"{kind.stem}{number}" + (f"_{variant}" if variant > 1 else "")
        self.stats.unknown[canonical] = self.stats.unknown.get(canonical, 0) + 1
        return None


# ---------------------------------------------------------------------------
# Текстовые форматы
# ---------------------------------------------------------------------------

def restore_text_file(src: Path, dst: Path, restorer: Restorer) -> None:
    raw = src.read_bytes()
    enc = sniff_encoding(raw)
    text = restorer.substitute(raw.decode(enc, errors="replace"))
    try:
        payload = text.encode(enc)
    except UnicodeEncodeError:
        payload = text.encode("utf-8")
    atomic_write(dst, payload)


# ---------------------------------------------------------------------------
# OOXML
# ---------------------------------------------------------------------------

def restore_ooxml(src: Path, dst: Path, restorer: Restorer, numbers: bool = True, depth: int = 0) -> bool:
    """Возвращает True, если структура контейнера не пострадала."""
    before = _zip_snapshot(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".restore-office-", suffix=src.suffix, dir=dst.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(src) as zin:
            parsed: dict[str, object] = {}
            for info in zin.infolist():
                if info.filename.lower().endswith((".xml", ".rels")):
                    try:
                        parsed[info.filename] = etree.fromstring(zin.read(info.filename), _parser())
                    except etree.XMLSyntaxError:
                        pass
            use_numbers = numbers and src.suffix.lower() == ".xlsx" and bool(restorer.vault.numbers_back)
            date_styles: set[int] = set()
            if use_numbers and parsed.get("xl/styles.xml") is not None:
                date_styles = numeric.date_style_indexes(parsed["xl/styles.xml"])
            numbers_restored = 0
            sheet_renames: list[tuple[str, str]] = []
            with zipfile.ZipFile(tmp_name, "w") as zout:
                for info in zin.infolist():
                    raw = zin.read(info.filename)
                    root = parsed.get(info.filename)
                    if root is None and is_embedded_package(info.filename) and depth < 2:
                        raw = _restore_embedded(raw, info.filename, restorer, numbers, depth)
                    if root is not None:
                        changed = _restore_tree(root, info.filename, restorer)
                        if depth == 0 and info.filename == "docProps/core.xml":
                            changed = strip_vault_marker(root) or changed
                        if use_numbers:
                            if _is_worksheet(info.filename):
                                done = numeric.restore_worksheet(root, date_styles, restorer.vault,
                                                                 [])  # ячейки в отчёт не выводим поимённо
                                restorer.stats.numbers += done
                                numbers_restored += done
                                changed = changed or bool(done)
                            elif "/charts/" in info.filename.lower():
                                done = numeric.restore_chart_cache(root, restorer.vault)
                                changed = changed or bool(done)
                        if info.filename.lower() == "xl/workbook.xml":
                            renames = _fix_sheet_names(root)
                            sheet_renames.extend(renames)
                            restorer.stats.sheet_fixes.extend(f"{old} → {new}" for old, new in renames)
                            changed = True
                        if changed:
                            raw = etree.tostring(root, xml_declaration=raw.lstrip().startswith(b"<?xml"),
                                                 encoding="UTF-8")
                    zout.writestr(info, raw)
            if numbers_restored:
                _request_recalc_in_zip(tmp_name)
            if sheet_renames:
                _rewrite_sheet_references(tmp_name, sheet_renames)
        os.replace(tmp_name, dst)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    after = _zip_snapshot(dst)
    return before["names"] == after["names"] and before["binary_hashes"] == after["binary_hashes"]


def _restore_embedded(raw: bytes, name: str, restorer: Restorer, numbers: bool, depth: int) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / ("in" + Path(name).suffix)
        target = Path(tmp) / ("out" + Path(name).suffix)
        source.write_bytes(raw)
        try:
            restore_ooxml(source, target, restorer, numbers, depth + 1)
        except Exception:
            return raw
        return target.read_bytes() if target.exists() else raw


def _request_recalc_in_zip(path: str) -> None:
    """Excel пересчитает книгу при открытии: формулы после возврата чисел ссылаются на исходные значения."""
    with zipfile.ZipFile(path) as zin:
        items = [(info, zin.read(info.filename)) for info in zin.infolist()]
    with zipfile.ZipFile(path, "w") as zout:
        for info, data in items:
            if info.filename.lower() == "xl/workbook.xml":
                root = etree.fromstring(data, _parser())
                numeric.request_recalculation(root)
                data = etree.tostring(root, xml_declaration=data.lstrip().startswith(b"<?xml"), encoding="UTF-8")
            zout.writestr(info, data)


def _restore_tree(root, part: str, restorer: Restorer) -> bool:
    """Токен мог быть разорван форматированием, поэтому текст абзаца собирается целиком,
    но правятся только затронутые участки — оформление остальных runs сохраняется."""
    changed = False
    covered: set[int] = set()
    keep_alive = []      # lxml создаёт обёртки узлов по требованию: без ссылки id() освобождается и повторяется
    for group_name, segments in _groups(root, part):
        joined = "".join(s.text for s in segments)
        if group_name.split(":")[0] in HEADER_FOOTER_TAGS:
            joined = _mask_header_codes(joined)       # `&Remail1@example.com`: R — код колонтитула, а не буква адреса
        edits = restorer.matches(joined) if any(ch.isdigit() for ch in joined) else []
        for segment in segments:
            covered.add(id(segment.element))
            keep_alive.append(segment.element)
        if not edits:
            continue
        splice_segments(segments, edits)
        changed = True
    for node in root.iter():
        if id(node) not in covered and node.text and any(ch.isdigit() for ch in node.text) and \
                not isinstance(node, etree._Comment):
            replaced = restorer.substitute(node.text)
            if replaced != node.text:
                node.text = replaced
                changed = True
        for attr, value in list(node.attrib.items()):
            if any(ch.isdigit() for ch in value):
                replaced = restorer.substitute(value, after_underscore=True)
                if replaced != value:
                    node.set(attr, replaced)
                    changed = True
    return changed


def _rewrite_sheet_references(path: str, renames: list[tuple[str, str]]) -> None:
    """Лист переименован под ограничения Excel: формулы, имена и диаграммы должны ссылаться на новое имя, а не на несуществующее."""
    with zipfile.ZipFile(path) as zin:
        items = [(info, zin.read(info.filename)) for info in zin.infolist()]
    with zipfile.ZipFile(path, "w") as zout:
        for info, data in items:
            name = info.filename.lower()
            if name.endswith(".xml") and (_is_worksheet(info.filename) or name == "xl/workbook.xml" or "/charts/" in name):
                root = etree.fromstring(data, _parser())
                changed = False
                for node in root.iter():
                    if _xml_name(node) in {"f", "definedName", "formula1", "formula2", "formula"} and node.text:
                        text = node.text
                        for old, new in renames:
                            quoted_old, quoted_new = "'" + old.replace("'", "''") + "'!", "'" + new.replace("'", "''") + "'!"
                            text = text.replace(quoted_old, quoted_new)
                            if old != new:
                                # Имя без кавычек: при подстановке значения вместо метки кавычки не ставились.
                                plain_new = new if re.fullmatch(r"[A-Za-zА-Яа-яЁё_][\w.]*", new) else "'" + new.replace("'", "''") + "'"
                                text = re.sub(rf"(?<![\w.'])({re.escape(old)})!", lambda _m, n=plain_new: n + "!", text)
                        if text != node.text:
                            node.text, changed = text, True
                if changed:
                    data = etree.tostring(root, xml_declaration=data.lstrip().startswith(b"<?xml"), encoding="UTF-8")
            zout.writestr(info, data)


def _fix_sheet_names(workbook_root) -> list[tuple[str, str]]:
    """Имя листа Excel — до 31 знака и без `[]:*?/\\`. Подстановка исходных значений могла его испортить."""
    fixes: list[tuple[str, str]] = []
    seen: set[str] = set()
    for node in workbook_root.iter():
        if _xml_name(node) != "sheet":
            continue
        name = node.get("name") or ""
        fixed = INVALID_SHEET_CHARS.sub("_", name).strip("'")[:MAX_SHEET_NAME] or "Лист"
        candidate, counter = fixed, 2
        while candidate.casefold() in seen:
            suffix = f"_{counter}"
            candidate = fixed[:MAX_SHEET_NAME - len(suffix)] + suffix
            counter += 1
        seen.add(candidate.casefold())
        if candidate != name:
            node.set("name", candidate)
            fixes.append((name, candidate))
    return fixes


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

RESTORE_FONT = "china-s"
PLACEHOLDER_FONT_RANGE = (3.0, 8.0)


def _fit_font_size(text: str, width: float) -> float:
    size = PLACEHOLDER_FONT_RANGE[1]
    while size > PLACEHOLDER_FONT_RANGE[0]:
        if fitz.get_text_length(text, fontname=RESTORE_FONT, fontsize=size) <= width:
            return size
        size -= .25
    return PLACEHOLDER_FONT_RANGE[0]


def restore_pdf(src: Path, dst: Path, restorer: Restorer) -> bool:
    doc = fitz.open(src)
    metadata = dict(doc.metadata or {})
    restored_meta = {k: restorer.substitute(v) for k, v in metadata.items() if isinstance(v, str) and v}
    if str(restored_meta.get("producer", "")).startswith("anonymizer:"):
        restored_meta["producer"] = ""       # служебная пометка хранилища; прежнее значение не сохранялось
    if any(restored_meta[k] != metadata.get(k) for k in restored_meta):
        doc.set_metadata({**metadata, **restored_meta})
    for page in doc:
        text = page.get_text("text")
        found = {m.group(0) for regex in (TOKEN_RE, EMAIL_TOKEN_RE) for m in regex.finditer(text)}
        pending: list[tuple[fitz.Rect, str]] = []
        # Длинные токены первыми: `Name1` находится и внутри `Name10`, а замена должна лечь на весь токен.
        for token in sorted(found, key=len, reverse=True):
            original = restorer.substitute(token)
            if original == token:
                continue
            for rect in page.search_for(token):
                if not any(rect.intersects(other) for other, _ in pending):
                    pending.append((rect, original))
        if not pending:
            continue
        for rect, _ in pending:
            page.add_redact_annot(rect, fill=(1, 1, 1))
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                              text=fitz.PDF_REDACT_TEXT_REMOVE)
        for rect, original in pending:
            available = max(20.0, page.rect.x1 - rect.x0 - 12.0)
            size = _fit_font_size(original, available)
            page.insert_text((rect.x0, rect.y1 - size * .2), original, fontname=RESTORE_FONT, fontsize=size,
                             color=(0, 0, 0))
    dst.parent.mkdir(parents=True, exist_ok=True)
    pages = len(doc)
    doc.save(dst, garbage=4, deflate=True, clean=True)
    doc.close()
    check = fitz.open(dst)
    ok = len(check) == pages
    check.close()
    return ok


def count_tokens(text: str) -> int:
    return sum(1 for _ in TOKEN_RE.finditer(text)) + sum(1 for _ in EMAIL_TOKEN_RE.finditer(text))
