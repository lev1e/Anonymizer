"""Целая папка за один раз: обход, один общий проход обучения и зеркальная папка с результатом.

Все файлы папки идут в одно задание `Service`, поэтому одна сущность получает одну метку во всех файлах, а результат
не зависит от порядка. Структура папок повторяется в зеркальной папке рядом с исходной. Имена подпапок тоже данные
(«Клиент Ромашка/Договоры»), поэтому они заменяются нейтральными `Папка{N}`, а исходное имя хранится в хранилище.
Исходная папка не меняется.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .formats import classify
from .service import FORMAT_LABEL, SUFFIX_ANON, SUFFIX_RESTORED, UNSUPPORTED_HELP, Job, PathUpload, Service, safe_name
from .vault import Vault

MAX_FILES = 5000
MAX_TOTAL_BYTES = 4 * 1024 ** 3
DIR_STEM = "Папка"
DIR_MARK = "dir:"
SKIP_FILES = {".ds_store", "thumbs.db", "desktop.ini"}
_DIR_NAME = re.compile(rf"^{DIR_STEM}(\d+)$")
_MIRROR_NAME = re.compile(r"\((?:обезличено|восстановлено)\)(?: \(\d+\))?$")
_COLLISION = re.compile(r"(?P<head>.*(?:\(обезличено\)|\(восстановлено\))) \(\d+\)(?P<ext>\.[^.]*)?$")


@dataclass
class FolderScan:
    root: Path
    files: list[tuple[Path, Path]] = field(default_factory=list)      # (путь внутри папки, путь на диске)
    skipped: list[dict] = field(default_factory=list)
    total_bytes: int = 0
    limit_hit: str = ""

    def public(self) -> dict:
        return {"name": self.root.name, "files": len(self.files), "skipped": len(self.skipped), "bytes": self.total_bytes,
                "note": self.limit_hit}


def scan_folder(root: Path, kind: str) -> FolderScan:
    """Файлы, которые можно обработать, и список пропущенного с причиной. Ничего не читает, кроме размеров."""
    scan = FolderScan(root=root)
    # Результат прошлого запуска, лежащий внутри выбранной папки, не должен обрабатываться повторно.
    own = re.compile(rf"{re.escape((SUFFIX_ANON if kind == 'anonymize' else SUFFIX_RESTORED).strip())}(?: \(\d+\))?$", re.IGNORECASE)

    def walk(directory: Path, rel: Path) -> None:
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name.casefold())
        except OSError:
            scan.skipped.append({"path": rel.as_posix() or ".", "reason": "Папку не удалось прочитать."})
            return
        for entry in entries:
            relative = rel / entry.name
            if entry.is_symlink():
                scan.skipped.append({"path": relative.as_posix(), "reason": "Ярлык или символическая ссылка не обрабатываются."})
                continue
            if entry.is_dir():
                if entry.name.startswith(".") or own.search(entry.name):
                    continue
                walk(entry, relative)
                continue
            if not entry.is_file():
                continue
            lowered = entry.name.casefold()
            if lowered in SKIP_FILES or entry.name.startswith("~$") or entry.name.startswith("."):
                continue                      # служебные файлы систем и Office: их нет смысла перечислять
            suffix = entry.suffix.lower()
            if classify(entry) not in FORMAT_LABEL:
                scan.skipped.append({"path": relative.as_posix(), "reason": UNSUPPORTED_HELP.get(
                    suffix, "Формат не поддерживается: файл остаётся только в исходной папке.")})
                continue
            if len(scan.files) >= MAX_FILES:
                scan.limit_hit = f"В папке больше {MAX_FILES} файлов: обработаны первые {MAX_FILES}."
                return
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            if scan.total_bytes + size > MAX_TOTAL_BYTES:
                scan.limit_hit = "Общий размер папки больше 4 ГБ: обработана часть файлов. Разбейте папку на несколько."
                return
            scan.total_bytes += size
            scan.files.append((relative, entry))

    walk(root, Path())
    return scan


# -- имена папок --------------------------------------------------------------------

def neutral_dir_name(vault: Vault, name: str) -> str:
    return f"{DIR_STEM}{vault.file_number(DIR_MARK + name)}"


def original_dir_name(vault: Vault, name: str) -> str:
    """Имя подпапки до обезличивания, если оно хранится; иначе имя как есть (папка пользователя, не наша)."""
    match = _DIR_NAME.match(name)
    original = vault.original_file_name(int(match.group(1))) if match else None
    return original[len(DIR_MARK):] if original and original.startswith(DIR_MARK) else name


def mirror_root(source: Path, kind: str, vault: Vault) -> Path:
    """Свободное имя зеркальной папки рядом с исходной."""
    if kind == "anonymize":
        stem, suffix = neutral_dir_name(vault, source.name), SUFFIX_ANON
    else:
        base = source.name
        for mark in (SUFFIX_ANON, "_anon"):
            base = base.replace(mark, "")
        stem, suffix = original_dir_name(vault, base.strip()), SUFFIX_RESTORED
    stem = safe_name(stem) or "Папка"
    candidate = source.parent / f"{stem}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = source.parent / f"{stem}{suffix} ({counter})"
        counter += 1
    return candidate


def _free(path: Path) -> Path:
    if not path.exists():
        return path
    counter = 2
    while True:
        candidate = path.with_name(f"{path.stem} ({counter}){path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


@dataclass
class FolderContext:
    scan: FolderScan
    kind: str
    dest: Path | None = None


def _output_name(name: str) -> str:
    """Одноимённые файлы разных подпапок получают в общем списке суффикс « (2)»; в своей папке он не нужен."""
    match = _COLLISION.match(name)
    return f"{match.group('head')}{match.group('ext') or ''}" if match else name


def write_mirror(job: Job, context: FolderContext, vault: Vault) -> dict:
    """Раскладывает готовые файлы по зеркальной структуре. Возвращает сводку для экрана."""
    scan = context.scan
    dest = mirror_root(scan.root, context.kind, vault)
    written = failed = 0
    dir_cache: dict[tuple[str, ...], tuple[str, ...]] = {}
    for (rel, _), outcome in zip(scan.files, job.files):
        outcome.name = rel.as_posix()
        if not outcome.out_path or outcome.status == "error":
            failed += 1
            continue
        parts = rel.parent.parts
        if parts not in dir_cache:
            mapper = (lambda n: neutral_dir_name(vault, n)) if context.kind == "anonymize" else (lambda n: original_dir_name(vault, n))
            dir_cache[parts] = tuple(safe_name(mapper(p)) for p in parts)
        target_dir = dest.joinpath(*dir_cache[parts])
        target_dir.mkdir(parents=True, exist_ok=True)
        preferred = target_dir / _output_name(outcome.out_name)
        target = preferred if not preferred.exists() else _free(target_dir / outcome.out_name)
        shutil.copyfile(outcome.out_path, target)
        outcome.out_name = target.relative_to(dest).as_posix()
        written += 1
    dest.mkdir(parents=True, exist_ok=True)
    context.dest = dest
    return {"out": str(dest), "name": dest.name, "written": written, "failed": failed, "skipped": scan.skipped[:200],
            "skipped_total": len(scan.skipped), "note": scan.limit_hit}


def remove_previous(context: FolderContext) -> None:
    """Повторный проход (с решениями по сомнительным словам) заменяет результат прошлого прохода."""
    if context.dest is not None and context.dest.is_dir() and _MIRROR_NAME.search(context.dest.name):
        shutil.rmtree(context.dest, ignore_errors=True)
    context.dest = None


def uploads_for(scan: FolderScan) -> list[PathUpload]:
    return [PathUpload(safe_name(rel.name), path) for rel, path in scan.files]


def run_folder(service: Service, context: FolderContext, options: dict, job: Job, overrides: dict | None = None) -> None:
    uploads = uploads_for(context.scan)
    if not uploads:
        raise ValueError("В папке нет файлов, которые можно обработать. Поддерживаются Excel, Word, PowerPoint, PDF и текст.")
    job.folder = context
    if context.kind == "anonymize":
        service.anonymize(uploads, options, overrides, job=job)
    else:
        service.restore(uploads, job=job)
    finish_folder(service, job, context)


def finish_folder(service: Service, job: Job, context: FolderContext) -> None:
    job.result["folder"] = write_mirror(job, context, service.vault)
