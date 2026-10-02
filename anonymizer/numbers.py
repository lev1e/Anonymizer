"""Числовые суррогаты для Excel: подставные числа, с которыми модель может считать.

Текстовые данные заменяются токенами, а числа — правдоподобными числами того же порядка. Один и тот же
исходный `1200` всегда даёт один и тот же суррогат, поэтому связи между ячейками сохраняются. При
восстановлении суррогат ищется среди ячеек с введённым вручную значением; ячейки с формулами не
трогаются, иначе расчёт потерял бы связь с данными.

Не заменяются: даты и время, годы (1900–2100), целые числа до 12 (номера месяцев, счётчики, ставки
в единицах), логические значения и результаты формул. Кэшированные результаты формул удаляются:
они были вычислены по исходным числам и раскрыли бы их.
"""

from __future__ import annotations

import re
import secrets
from decimal import Decimal, ROUND_HALF_UP

from lxml import etree

from .vault import Vault, canon_number

_RANDOM = secrets.SystemRandom()

# Встроенные форматы Excel, означающие дату или время.
BUILTIN_DATE_FORMATS = set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59))
_DATE_TOKEN = re.compile(r"(?i)(?<![\\%_])[ymdhs]|AM/PM|上午|下午")


def _local(node) -> str:
    return etree.QName(node).localname


def date_style_indexes(styles_root) -> set[int]:
    """Номера ячеечных стилей, которые показывают значение как дату или время."""
    custom: dict[int, str] = {}
    for node in styles_root.iter():
        if _local(node) == "numFmt":
            try:
                custom[int(node.get("numFmtId"))] = node.get("formatCode") or ""
            except (TypeError, ValueError):
                continue
    dates: set[int] = set()
    for holder in styles_root.iter():
        if _local(holder) != "cellXfs":
            continue
        for index, xf in enumerate(child for child in holder if _local(child) == "xf"):
            try:
                fmt = int(xf.get("numFmtId", "0"))
            except ValueError:
                continue
            if fmt in BUILTIN_DATE_FORMATS:
                dates.add(index)
            elif fmt in custom:
                code = re.sub(r'"[^"]*"|\[[^\]]*\]', "", custom[fmt])
                if _DATE_TOKEN.search(code):
                    dates.add(index)
    return dates


def numeric_cells(root, date_styles: set[int]):
    """Ячейки листа с введённым вручную числом: (ячейка, узел значения, каноническая запись)."""
    for cell in root.iter():
        if _local(cell) != "c":
            continue
        if cell.get("t") not in (None, "n"):
            continue
        children = {_local(child): child for child in cell}
        if "f" in children or "v" not in children or not (children["v"].text or "").strip():
            continue
        try:
            style = int(cell.get("s", "0"))
        except ValueError:
            style = 0
        if style in date_styles:
            continue
        canon = canon_number(children["v"].text)
        if canon is not None:
            yield cell, children["v"], canon


def should_replace(canon: str) -> bool:
    value = Decimal(canon)
    if value == value.to_integral_value():
        whole = int(value)
        if abs(whole) <= 12 or 1900 <= whole <= 2100:
            return False
    return True


def decimals_of(canon: str) -> int:
    return len(canon.split(".")[1]) if "." in canon else 0


def make_surrogate(canon: str, avoid: set[str]):
    """Фабрика суррогатов: тот же порядок величины, тот же знак, тот же формат записи."""
    value = Decimal(canon)
    places = decimals_of(canon)
    is_integer = places == 0

    def make() -> str:
        for _ in range(60):
            factor = Decimal(str(round(_RANDOM.uniform(0.55, 1.85), 4)))
            candidate = value * factor
            if is_integer:
                number = int(candidate.to_integral_value(rounding=ROUND_HALF_UP))
                # Круглые числа выдают догадку и совпадают с числами, которые модель пишет от себя.
                if abs(number) >= 100 and number % 10 == 0:
                    number += _RANDOM.choice((-3, -1, 1, 3, 7))
                if abs(number) < 13 or 1900 <= abs(number) <= 2100:
                    continue
                text = str(number)
            else:
                # Дробным числам добавляем знаки: 0,14 → 0,1387. Так суррогат отличим от чисел модели.
                extra = 2 if abs(value) < 100 else 1
                quantum = Decimal(1).scaleb(-(places + extra))
                text = canon_number(str(candidate.quantize(quantum, rounding=ROUND_HALF_UP))) or ""
            canonical = canon_number(text)
            if canonical and canonical != canon and canonical not in avoid and Decimal(canonical) != 0:
                return canonical
        raise RuntimeError("Не удалось подобрать суррогат числа")

    return make


def anonymize_worksheet(root, date_styles: set[int], vault: Vault, avoid: set[str]) -> tuple[int, int]:
    """Заменяет числа на суррогаты и убирает кэш формул. Возвращает (заменено чисел, очищено формул)."""
    replaced = 0
    for _cell, node, canon in list(numeric_cells(root, date_styles)):
        if not should_replace(canon):
            continue
        surrogate = vault.surrogate_for(canon, make_surrogate(canon, avoid))
        node.text = surrogate
        replaced += 1
    cleared = 0
    for cell in root.iter():
        if _local(cell) != "c":
            continue
        children = list(cell)
        if any(_local(c) == "f" for c in children):
            for child in children:
                if _local(child) == "v":
                    cell.remove(child)
                    cleared += 1
            if cell.get("t") in ("str", "e", "b", "n"):
                # Без кэша тип результата неизвестен; Excel определит его при пересчёте.
                del cell.attrib["t"]
    return replaced, cleared


def anonymize_chart_cache(root, vault: Vault, avoid: set[str]) -> int:
    replaced = 0
    for cache in root.iter():
        if _local(cache) != "numCache":
            continue
        for value in cache.iter():
            if _local(value) == "v" and value.text:
                canon = canon_number(value.text)
                if canon is not None and should_replace(canon):
                    value.text = vault.surrogate_for(canon, make_surrogate(canon, avoid))
                    replaced += 1
    return replaced


def restore_worksheet(root, date_styles: set[int], vault: Vault, restored_log: list[tuple[str, str]]) -> int:
    """Возвращает исходные числа на место суррогатов в ячейках с введённым значением."""
    restored = 0
    for cell, node, canon in list(numeric_cells(root, date_styles)):
        original = vault.original_of_surrogate(canon)
        if original is None:
            continue
        node.text = original
        restored += 1
        restored_log.append((cell.get("r", ""), original))
    return restored


def restore_chart_cache(root, vault: Vault) -> int:
    restored = 0
    for cache in root.iter():
        if _local(cache) != "numCache":
            continue
        for value in cache.iter():
            if _local(value) == "v" and value.text:
                canon = canon_number(value.text)
                original = vault.original_of_surrogate(canon) if canon is not None else None
                if original is not None:
                    value.text = original
                    restored += 1
    return restored


_AFTER_CALC_PR = ("oleSize", "customWorkbookViews", "pivotCaches", "smartTagPr", "smartTagTypes", "webPublishing",
                  "fileRecoveryPr", "webPublishObjects", "extLst")


def request_recalculation(workbook_root) -> None:
    """Excel пересчитает книгу при открытии: результаты формул после подстановки чисел устарели."""
    calc = next((n for n in workbook_root if _local(n) == "calcPr"), None)
    if calc is None:
        namespace = etree.QName(workbook_root).namespace
        calc = etree.Element(f"{{{namespace}}}calcPr")
        anchor = next((n for n in workbook_root if _local(n) in _AFTER_CALC_PR), None)
        if anchor is not None:
            anchor.addprevious(calc)
        else:
            workbook_root.append(calc)
    calc.set("fullCalcOnLoad", "1")
