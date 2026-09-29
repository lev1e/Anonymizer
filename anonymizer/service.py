"""Сценарии продукта: обезличить файл, вернуть исходные данные, показать, что заменено.

Слой не знает про HTTP. Интерфейс получает из него готовые для показа результаты: статус каждого
файла, сообщения на русском языке, сводку по видам данных и список того, что стоит проверить.
"""

from __future__ import annotations

import re
import shutil
import threading
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import restore as rs
from .detectors import Detector
from .formats import (TransformContext, classify, extract_text, read_vault_marker, replace_findings, scan_ooxml,
                      scan_pdf, scan_text_file, transform_ooxml, transform_pdf, transform_text_file)
from .models import Decision, Settings
from .normalize import fold
from .tokens import EMAIL_TOKEN_RE, GROUP_LABELS, KINDS, TOKEN_RE, format_token, parse_match
from .vault import Vault
from . import verify

Progress = Callable[[float, str], None]

SUFFIX_ANON = " (обезличено)"
SUFFIX_RESTORED = " (восстановлено)"
MAX_UPLOAD_BYTES = 300 * 1024 * 1024
KNOWLEDGE_KINDS = {"ORG", "PROJECT", "CITY", "REGION", "DOMAIN", "TERM"}

UNSUPPORTED_HELP = {
    ".xls": "Старый формат Excel. Откройте файл в Excel и сохраните как .xlsx.",
    ".doc": "Старый формат Word. Откройте файл в Word и сохраните как .docx.",
    ".ppt": "Старый формат PowerPoint. Откройте файл и сохраните как .pptx.",
    ".xlsm": "Файл с макросами: автоматическая обработка отключена, чтобы не повредить макросы. "
             "Сохраните копию без макросов (.xlsx).",
    ".docm": "Файл с макросами: сохраните копию без макросов (.docx).",
    ".pptm": "Файл с макросами: сохраните копию без макросов (.pptx).",
    ".rtf": "Формат RTF не поддерживается. Сохраните файл как .docx.",
    ".odt": "Формат OpenDocument не поддерживается. Сохраните файл как .docx.",
    ".ods": "Формат OpenDocument не поддерживается. Сохраните файл как .xlsx.",
    ".odp": "Формат OpenDocument не поддерживается. Сохраните файл как .pptx.",
    ".png": "Изображения не обезличиваются: текст на картинках программа не читает.",
    ".jpg": "Изображения не обезличиваются: текст на картинках программа не читает.",
    ".jpeg": "Изображения не обезличиваются: текст на картинках программа не читает.",
}

FORMAT_LABEL = {"TEXT": "Текст", "OOXML": "Office", "PDF": "PDF"}


@dataclass
class Upload:
    name: str
    data: bytes


@dataclass
class FileOutcome:
    name: str
    out_name: str = ""
    out_path: str = ""
    status: str = "ok"                     # ok | attention | error
    format: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    total: int = 0
    messages: list[dict] = field(default_factory=list)

    def add(self, level: str, text: str) -> None:
        if not any(m["text"] == text for m in self.messages):
            self.messages.append({"level": level, "text": text})
        if level == "error":
            self.status = "error"
        elif level == "warn" and self.status == "ok":
            self.status = "attention"

    def public(self) -> dict:
        return {"name": self.name, "out_name": self.out_name, "status": self.status, "format": self.format,
                "counts": self.counts, "total": self.total, "messages": self.messages,
                "downloadable": bool(self.out_path) and self.status != "error"}


@dataclass
class Job:
    id: str
    kind: str
    dir: Path
    files: list[FileOutcome] = field(default_factory=list)
    state: str = "running"                 # running | done | failed
    percent: float = 0.0
    stage: str = ""
    result: dict = field(default_factory=dict)
    inputs: list[Upload] = field(default_factory=list)
    options: dict = field(default_factory=dict)
    snapshot: object = None
    created: float = field(default_factory=time.time)
    error: str = ""


OLE_HEADER = bytes.fromhex("D0CF11E0A1B11AE1")

# Метка, которую модель испортила: пробел между основой и номером («Name 1») или две метки без разделителя («Name1Name2»).
_STEMS = "|".join(sorted((k.stem for k in KINDS if k.code != "EMAIL"), key=len, reverse=True))
SPACED_TOKEN_RE = re.compile(rf"(?<![A-Za-z0-9_])({_STEMS})[ \u00a0]+(\d{{1,7}})(?![\d.,A-Za-z_])")
GLUED_TOKEN_RE = re.compile(rf"(?<![A-Za-z0-9_])(?:(?:{_STEMS})\d{{1,7}}(?:_\d{{1,6}})?){{2,}}(?![A-Za-z0-9_])")


def container_problem(name: str, data: bytes) -> str:
    """Понятная причина, по которой Office-файл нельзя открыть, — до попытки его обработать."""
    suffix = Path(name).suffix.lower()
    if suffix in (".xlsx", ".docx", ".pptx"):
        if data.startswith(OLE_HEADER):
            return ("Файл защищён паролем или сохранён в старом формате. Снимите защиту в Excel, Word или PowerPoint "
                    "и загрузите его снова.")
        if not data.startswith(b"PK"):
            return "Файл повреждён: это не документ Office."
    if suffix == ".pdf" and not data.startswith(b"%PDF"):
        return "Файл повреждён: это не PDF."
    return ""


MAX_UNPACKED_BYTES = 1024 * 1024 * 1024


def unpacked_size_problem(path: Path) -> str:
    """Архив, который при распаковке занял бы гигабайты («бомба»), не открываем: он повесил бы компьютер."""
    if path.suffix.lower() not in (".xlsx", ".docx", ".pptx"):
        return ""
    try:
        with zipfile.ZipFile(path) as z:
            total = sum(info.file_size for info in z.infolist())
    except zipfile.BadZipFile:
        return "Файл повреждён: не удаётся прочитать содержимое."
    if total > MAX_UNPACKED_BYTES:
        return "Файл слишком велик после распаковки (больше 1 ГБ) и не обрабатывается."
    return ""


def safe_name(name: str) -> str:
    # macOS отдаёт имена в разложенной форме («й» из двух знаков): ни Windows, ни поиск по тексту такого не ждут.
    base = re.split(r"[\\/]", unicodedata.normalize("NFC", name))[-1].strip().strip(".") or "file"
    base = re.sub(r'[<>:"|?*\x00-\x1f]', "_", base)
    return base[:150]


def split_ext(name: str) -> tuple[str, str]:
    path = Path(name)
    return path.stem, path.suffix


class Service:
    def __init__(self, vault: Vault, workdir: Path):
        self.vault = vault
        self.workdir = workdir
        workdir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.jobs: dict[str, Job] = {}
        self._commit_counter = 0
        self.cleanup()

    # -- рабочие каталоги -----------------------------------------------------

    def cleanup(self, older_than: float = 6 * 3600) -> None:
        limit = time.time() - older_than
        for child in self.workdir.iterdir():
            try:
                if child.stat().st_mtime < limit:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                continue
        for job_id in [j for j, job in self.jobs.items() if job.created < limit]:
            self.jobs.pop(job_id, None)

    def new_job(self, kind: str, options: dict | None = None) -> Job:
        from .util import opaque_id
        job_id = opaque_id(6).lower()
        directory = self.workdir / job_id
        (directory / "in").mkdir(parents=True, exist_ok=True)
        (directory / "out").mkdir(parents=True, exist_ok=True)
        job = Job(job_id, kind, directory, options=options or {})
        self.jobs[job_id] = job
        return job

    def forget_job(self, job_id: str) -> None:
        job = self.jobs.pop(job_id, None)
        if job:
            shutil.rmtree(job.dir, ignore_errors=True)

    # -- предпросмотр ---------------------------------------------------------

    def peek(self, upload: Upload) -> dict:
        """Что за файл: поддерживается ли, не обезличен ли он уже. Ничего не меняет."""
        name = safe_name(upload.name)
        path = self.workdir / "peek" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(upload.data)
        try:
            kind = classify(path)
            info = {"name": name, "size": len(upload.data), "format": FORMAT_LABEL.get(kind, ""),
                    "supported": kind in FORMAT_LABEL, "note": "", "tokens": 0}
            if kind not in FORMAT_LABEL:
                info["note"] = UNSUPPORTED_HELP.get(path.suffix.lower(),
                                                    "Формат не поддерживается. Поддерживаются Excel, Word, PowerPoint, PDF и текстовые файлы.")
                return info
            problem = container_problem(name, upload.data)
            if problem:
                info["supported"], info["note"] = False, problem
                return info
            try:
                text = extract_text(path)
            except Exception:
                info["note"] = "Файл не удалось прочитать: возможно, он повреждён или защищён паролем."
                info["supported"] = False
                return info
            known = 0
            for regex, email in ((TOKEN_RE, False), (EMAIL_TOKEN_RE, True)):
                for m in regex.finditer(text):
                    kind_, number, variant = parse_match(m, email=email)
                    if self.vault.lookup(kind_, number, variant).status in ("ok", "variant_missing"):
                        known += 1
            info["tokens"] = known
            return info
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    # -- обезличивание --------------------------------------------------------

    def settings_for(self, options: dict) -> Settings:
        prefs = self.vault.prefs
        hide = list(dict.fromkeys([*prefs.get("hide_terms", []), *options.get("hide_terms", [])]))
        keep = list(dict.fromkeys([*prefs.get("keep_terms", []), *options.get("keep_terms", [])]))
        numbers = options.get("numbers")
        strict = options.get("strict")
        return Settings(numbers=prefs.get("numbers", False) if numbers is None else bool(numbers),
                        strict=prefs.get("strict", False) if strict is None else bool(strict),
                        suggest=True, hide_terms=[t for t in hide if t.strip()],
                        keep_terms=[t for t in keep if t.strip()])

    def build_detector(self, settings: Settings) -> Detector:
        detector = Detector([], settings)
        detector.token_known = lambda kind, number, variant: \
            self.vault.lookup(kind, number, variant).status != "unknown"
        for base, entity in list(self.vault.entities.items()):
            if entity["kind"] in KNOWLEDGE_KINDS and entity.get("spellings"):
                detector.entities.seed(entity["kind"], entity["key"], entity["spellings"])
        return detector

    def anonymize(self, uploads: list[Upload], options: dict | None = None, overrides: dict[str, str] | None = None,
                  progress: Progress | None = None, job: Job | None = None) -> Job:
        options = dict(options or {})
        with self.lock:
            if job is None:
                job = self.new_job("anonymize", options)
            if job.snapshot is None:
                job.inputs = list(uploads)
                job.snapshot = (self.vault.snapshot(), self._commit_counter)
            job.options = options
            settings = self.settings_for(options)
            detector = self.build_detector(settings)
            ctx = TransformContext(self.vault, settings, detector,
                                   overrides={fold(k): v for k, v in (overrides or {}).items()})
            def report(percent: float, stage: str) -> None:
                job.percent, job.stage = percent, stage
                if progress:
                    progress(percent, stage)
            names_taken: dict[str, int] = {}
            paths: list[tuple[Upload, Path, str]] = []
            for index, upload in enumerate(uploads):
                name = safe_name(upload.name)
                # Два файла с одним именем (Отчёт.txt из разных папок) не должны затирать друг друга.
                path = job.dir / "in" / str(index) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(upload.data)
                paths.append((upload, path, classify(path)))
            supported = [(u, p, k) for u, p, k in paths if k in FORMAT_LABEL]
            if len(supported) > 1:
                # Первый проход только обучает детектор: полное ФИО из одного файла расшифровывает
                # инициалы в другом, и результат не зависит от порядка файлов.
                for index, (upload, path, kind) in enumerate(supported):
                    report(index / (2 * len(supported)), f"Анализ: {path.name}")
                    self._learn(detector, path, kind)
            outcomes: list[FileOutcome] = []
            suggestions: dict[str, dict] = {}
            for index, (upload, path, kind) in enumerate(paths):
                base = (len(supported) > 1) * .5
                report(base + (index / max(1, len(paths))) * (1 - base), f"Обезличивание: {path.name}")
                outcome = self._anonymize_one(job, upload, path, kind, detector, ctx, suggestions, names_taken)
                outcomes.append(outcome)
            job.files = outcomes
            self._commit_job(job, "anonymize", outcomes, ctx)
            job.result = self._anonymize_result(job, ctx, suggestions)
            job.state = "done"
            job.percent = 1.0
            report(1.0, "Готово")
            return job

    def rerun(self, job_id: str, options: dict, overrides: dict[str, str], progress: Progress | None = None,
              job: Job | None = None) -> Job:
        """Повторный проход с решениями пользователя: скрыть сомнительное, оставить найденное."""
        with self.lock:
            old = self.jobs.get(job_id)
            if old is None:
                raise KeyError("Исходные файлы этого задания уже удалены. Загрузите файл заново.")
            snapshot, counter = old.snapshot if old.snapshot else (None, -1)
            if snapshot is not None and counter == self._commit_counter - 1:
                # Между заданием и повтором ничего не менялось: токены, выданные впустую, возвращаем.
                self.vault.rollback(snapshot)
            merged = {**old.options, **options}
            merged_overrides = {**old.options.get("overrides", {}), **overrides}
            merged["overrides"] = merged_overrides
            merged["remembered"] = self._remember(overrides)
            fresh = job or self.new_job("anonymize", merged)
            fresh.inputs = old.inputs
            fresh.snapshot = (self.vault.snapshot(), self._commit_counter)
            self.forget_job(job_id)
            return self.anonymize(fresh.inputs, merged, merged_overrides, progress, job=fresh)

    def _remember(self, overrides: dict[str, str]) -> dict[str, list[str]]:
        """Решение человека о слове — это предпочтение, а не разовая правка: оно сохраняется в настройках."""
        prefs = self.vault.prefs
        hide, keep = list(prefs.get("hide_terms", [])), list(prefs.get("keep_terms", []))
        added = {"hide": [], "keep": []}
        for word, choice in overrides.items():
            target, opposite, bucket = (hide, keep, "hide") if choice == "hide" else (keep, hide, "keep")
            if choice not in ("hide", "keep"):
                continue
            opposite[:] = [t for t in opposite if fold(t) != fold(word)]
            if not any(fold(t) == fold(word) for t in target):
                target.append(word)
                added[bucket].append(word)
        if added["hide"] or added["keep"]:
            self.vault.set_prefs(hide_terms=hide, keep_terms=keep)
        return added

    def _learn(self, detector: Detector, path: Path, kind: str) -> None:
        try:
            if kind == "TEXT":
                scan_text_file(path, path.name, detector)
            elif kind == "OOXML":
                scan_ooxml(path, path.name, detector, True, False)
            elif kind == "PDF":
                scan_pdf(path, path.name, detector)
        except Exception:
            return

    def _anonymize_one(self, job: Job, upload: Upload, path: Path, kind: str, detector: Detector,
                       ctx: TransformContext, suggestions: dict, names_taken: dict) -> FileOutcome:
        outcome = FileOutcome(name=path.name, format=FORMAT_LABEL.get(kind, ""))
        if len(upload.data) > MAX_UPLOAD_BYTES:
            outcome.add("error", "Файл слишком большой (больше 300 МБ).")
            return outcome
        if kind not in FORMAT_LABEL:
            outcome.add("error", UNSUPPORTED_HELP.get(path.suffix.lower(), "Формат не поддерживается. Поддерживаются "
                                                       "Excel, Word, PowerPoint, PDF и текстовые файлы."))
            return outcome
        problem = container_problem(path.name, upload.data)
        if problem:
            outcome.add("error", problem)
            return outcome
        problem = unpacked_size_problem(path)
        if problem:
            outcome.add("error", problem)
            return outcome
        before = dict(ctx.counts)
        numeric_before = dict(ctx.numeric)
        try:
            out_name = self._anonymized_name(path.name, detector, ctx)
            key = out_name.casefold()
            names_taken[key] = names_taken.get(key, 0) + 1
            if names_taken[key] > 1:
                stem, ext = split_ext(out_name)
                out_name = f"{stem} ({names_taken[key]}){ext}"
            dst = job.dir / "out" / out_name
            transform = {"TEXT": transform_text_file, "OOXML": transform_ooxml, "PDF": transform_pdf}[kind]
            result = transform(path, dst, path.name, detector, ctx)
        except UnicodeDecodeError:
            outcome.add("error", "Не удалось определить кодировку текстового файла.")
            return outcome
        except Exception as exc:  # один плохой файл не должен ронять всё задание
            outcome.add("error", f"Не удалось обработать файл ({type(exc).__name__}). Возможно, он повреждён "
                                 "или защищён паролем.")
            return outcome
        outcome.out_name, outcome.out_path = out_name, str(dst)
        outcome.counts = {g: ctx.counts.get(g, 0) - before.get(g, 0) for g in ctx.counts
                          if g != "hidden" and ctx.counts.get(g, 0) - before.get(g, 0)}
        outcome.total = sum(outcome.counts.values())
        for warning in result.warnings:
            outcome.add("warn", warning)
        for notice in result.notices:
            outcome.add("info", notice)
        if result.status.value == "BLOCKED":
            outcome.out_path = ""
            outcome.add("error", "Файл не был изменён и не подходит для передачи наружу.")
            return outcome
        if ctx.numeric.get("replaced", 0) - numeric_before.get("replaced", 0):
            done = ctx.numeric["replaced"] - numeric_before.get("replaced", 0)
            outcome.add("info", f"Заменено чисел: {done}. Даты и годы не менялись. Результаты формул очищены: "
                                "Excel пересчитает их при открытии.")
        ok, problem = verify.check_openable(dst)
        if not ok:
            outcome.out_path = ""
            outcome.add("error", problem)
            return outcome
        if problem:
            outcome.add("warn", problem)
        numbers_done = ctx.numeric.get("replaced", 0) - numeric_before.get("replaced", 0)
        if outcome.total == 0 and not numbers_done:
            outcome.add("warn", "Данных для замены не найдено. Если в файле есть имена, названия или другая "
                                "конфиденциальная информация, добавьте нужные слова вручную и обезличьте файл заново.")
        for finding in result.findings:
            if finding.decision == Decision.AUTO or finding.category not in {"POSSIBLE_PERSON", "POSSIBLE_ENTITY"}:
                continue
            if ctx.overrides.get(fold(finding.original)) == "keep":
                continue
            if any(mark in finding.location.lower() for mark in ("slidemasters", "slidelayouts", "notesmasters",
                                                                 "handoutmasters")):
                continue
            entry = suggestions.setdefault(fold(finding.original), {
                "text": finding.original, "reason": finding.reason, "count": 0, "context": finding.context,
                "file": path.name, "strong": self._strong_suggestion(finding)})
            entry["count"] += 1
        return outcome

    @staticmethod
    def _strong_suggestion(finding) -> bool:
        """Похоже ли это на настоящую находку, которую не стоит пропускать.

        Слабые — аббревиатуры и латинские слова с заглавной буквы: в русском тексте их много, и почти все они
        обычные термины. Их список показывается, но тревогу не поднимает.
        """
        word = finding.original
        if finding.category == "POSSIBLE_PERSON":
            return finding.confidence >= .6
        # «МВтч», «кВт»: прописные буквы и строчный хвост — единица измерения или сокращение, а не название.
        if re.match(r"^[А-ЯЁA-Z]{2,}[а-яёa-z]+$", word):
            return False
        return not word.isascii() and not word.isupper()

    def _anonymized_name(self, name: str, detector: Detector, ctx: TransformContext) -> str:
        stem, ext = split_ext(name)
        # Подчёркивание — обычный разделитель слов в именах файлов, а для разбора оно часть слова.
        probe = stem.replace("_", " ")
        detector.harvest(probe, name)
        findings = ctx.apply_overrides(detector.scan(probe, name, "filename"))
        new_stem = replace_findings(stem, findings, ctx) if findings else stem
        return f"{new_stem}{SUFFIX_ANON}{ext}"

    def _commit_job(self, job: Job, kind: str, outcomes: list[FileOutcome], ctx: TransformContext) -> None:
        tokens: list[str] = []
        for group_tokens in ctx.tokens.values():
            tokens.extend(group_tokens)
        counts = {GROUP_LABELS.get(g, g): n for g, n in ctx.counts.items()}
        self.vault.log(kind, [o.out_name or o.name for o in outcomes if o.status != "error"], counts,
                       {"tokens": sorted(set(tokens))})
        self.vault.commit()
        self._commit_counter += 1

    def _anonymize_result(self, job: Job, ctx: TransformContext, suggestions: dict) -> dict:
        groups: dict[str, list[dict]] = {}
        for entry in ctx.replaced.values():
            groups.setdefault(entry["group"], []).append(
                {"original": entry["original"], "token": entry["token"], "count": entry["count"],
                 "reason": entry["reason"], "kind": entry["kind"]})
        for items in groups.values():
            items.sort(key=lambda e: (-e["count"], e["original"]))
        groups.pop("hidden", None)
        summary = [{"group": g, "label": GROUP_LABELS.get(g, g), "count": ctx.counts[g],
                    "unique": len({e["token"].split("_")[0] for e in groups.get(g, [])})}
                   for g in GROUP_LABELS if g != "hidden" and ctx.counts.get(g)]
        ranked = sorted(suggestions.values(), key=lambda e: (not e["strong"], -e["count"], e["text"]))[:60]
        return {"summary": summary, "groups": {GROUP_LABELS.get(g, g): v[:400] for g, v in groups.items()},
                "groups_order": [GROUP_LABELS.get(g, g) for g in GROUP_LABELS if g in groups],
                "suggestions": ranked, "total": sum(n for g, n in ctx.counts.items() if g != "hidden"), "numbers": dict(ctx.numeric),
                "overrides": ctx.overrides, "remembered": job.options.get("remembered", {})}

    # -- восстановление -------------------------------------------------------

    def restore(self, uploads: list[Upload], progress: Progress | None = None, job: Job | None = None) -> Job:
        with self.lock:
            job = job or self.new_job("restore")

            def report(percent: float, stage: str) -> None:
                job.percent, job.stage = percent, stage
                if progress:
                    progress(percent, stage)
            total = rs.RestoreStats()
            outcomes: list[FileOutcome] = []
            for index, upload in enumerate(uploads):
                name = safe_name(upload.name)
                path = job.dir / "in" / str(index) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(upload.data)
                report(index / max(1, len(uploads)), f"Восстановление: {name}")
                outcomes.append(self._restore_one(job, path, total))
            job.files = outcomes
            job.result = self._restore_result(job, total, outcomes)
            self.vault.log("restore", [o.out_name or o.name for o in outcomes if o.status != "error"],
                           {"Восстановлено": total.restored})
            self.vault.commit()
            job.state = "done"
            job.percent = 1.0
            report(1.0, "Готово")
            return job

    def _restore_one(self, job: Job, path: Path, total: "rs.RestoreStats") -> FileOutcome:
        kind = classify(path)
        outcome = FileOutcome(name=path.name, format=FORMAT_LABEL.get(kind, ""))
        problem = container_problem(path.name, path.read_bytes()[:16])
        if problem:
            outcome.add("error", problem)
            return outcome
        problem = unpacked_size_problem(path)
        if problem:
            outcome.add("error", problem)
            return outcome
        marker = read_vault_marker(path) if kind in ("OOXML", "PDF") else ""
        if marker and marker not in self.vault.known_ids:
            outcome.add("error", "Файл обезличен другим хранилищем: другим компьютером или до очистки хранилища. Метки в нём "
                                 "означают другие значения, поэтому возврат остановлен. Загрузите резервную копию "
                                 "хранилища, в котором файл обезличивался (Настройки), и повторите.")
            return outcome
        restorer = rs.Restorer(self.vault)
        stem, ext = split_ext(path.name)
        stem = stem.replace(SUFFIX_ANON, "").replace("_anon", "")
        out_name = self._restored_name(stem) + SUFFIX_RESTORED + ext
        dst = job.dir / "out" / out_name
        counter = 2
        while dst.exists():
            dst = job.dir / "out" / f"{Path(out_name).stem} ({counter}){ext}"
            counter += 1
        try:
            if kind == "TEXT":
                rs.restore_text_file(path, dst, restorer)
            elif kind == "OOXML":
                if not rs.restore_ooxml(path, dst, restorer):
                    outcome.add("warn", "Структура файла после восстановления отличается от полученной.")
            elif kind == "PDF":
                rs.restore_pdf(path, dst, restorer)
            elif path.suffix.lower() in (".odt", ".ods", ".odp", ".rtf"):
                outcome.add("error", UNSUPPORTED_HELP.get(path.suffix.lower(), "Формат не поддерживается."))
                return outcome
            else:
                outcome.add("error", "Формат не поддерживается. Поддерживаются Excel, Word, PowerPoint, PDF и текстовые файлы.")
                return outcome
        except Exception as exc:
            outcome.add("error", f"Не удалось обработать файл ({type(exc).__name__}). Возможно, он повреждён "
                                 "или защищён паролем.")
            return outcome
        stats = restorer.stats
        outcome.out_name, outcome.out_path = dst.name, str(dst)
        outcome.total = stats.restored
        outcome.counts = {"restored": stats.restored}
        ok, problem = verify.check_openable(dst)
        if not ok:
            outcome.out_path = ""
            outcome.add("error", problem)
            return outcome
        if problem:
            outcome.add("warn", problem)
        if stats.found == 0:
            outcome.add("warn", "В файле не найдено ни одной метки (Name1, Company1 и т.п.). Возможно, файл не был "
                                "обезличен этой программой или модель заменила метки.")
        if stats.unknown:
            listing = ", ".join(f"{t} ({n})" if n > 1 else t for t, n in sorted(stats.unknown.items())[:8])
            more = f" и ещё {len(stats.unknown) - 8}" if len(stats.unknown) > 8 else ""
            outcome.add("warn", f"Не удалось восстановить метки: {listing}{more}. Их нет в хранилище: "
                                "возможно, модель добавила их сама или файл обезличен на другом компьютере.")
        if stats.removed:
            outcome.add("warn", f"Секретные данные (пароли, ключи) удаляются безвозвратно и не восстанавливаются: "
                                f"{sum(stats.removed.values())} шт.")
        if stats.endings:
            outcome.add("warn", f"К меткам приклеены русские окончания ({stats.endings}): проверьте падежи в тексте.")
        if stats.variant_fallback:
            outcome.add("info", f"У {stats.variant_fallback} меток номер написания не найден: подставлено основное написание.")
        if stats.numbers:
            outcome.add("info", f"Возвращено чисел: {stats.numbers}. Формулы не менялись; Excel пересчитает их при открытии.")
        for fix in stats.sheet_fixes:
            outcome.add("warn", f"Имя листа исправлено под ограничения Excel: {fix}")
        # Остались ли токены в результате — самая полезная проверка: ничего не восстанавливается молча.
        try:
            leftover = set()
            for regex, email in ((TOKEN_RE, False), (EMAIL_TOKEN_RE, True)):
                for m in regex.finditer(extract_text(dst)):
                    kind, number, variant = parse_match(m, email=email)
                    # Только метки, которые хранилище знает: «company0» из исходного адреса — обычный текст.
                    canonical = format_token(kind, number, variant)
                    if m.group(0) == canonical and self.vault.lookup(kind, number, variant).status in ("ok", "variant_missing"):
                        leftover.add(m.group(0))
        except Exception:
            leftover = set()
        broken = self._broken_tokens(dst)
        if broken:
            outcome.add("warn", f"В файле остались метки с нарушенной записью: {', '.join(broken[:6])}. Исправьте их вручную "
                                "(например, Name 1 → Name1) и повторите возврат.")
        if leftover and not stats.unknown and not stats.removed:
            outcome.add("warn", f"В результате остались метки: {', '.join(sorted(leftover)[:6])}.")
        total.merge(stats)
        return outcome

    def _broken_tokens(self, path: Path) -> list[str]:
        """Похожее на метку, но записанное неверно. Пробел учитывается, только если такая метка есть в хранилище."""
        try:
            text = extract_text(path)
        except Exception:
            return []
        found = {m.group(0) for m in GLUED_TOKEN_RE.finditer(text)}
        for m in SPACED_TOKEN_RE.finditer(text):
            kind = next((k for k in KINDS if k.stem.lower() == m.group(1).lower()), None)
            if kind and self.vault.lookup(kind, int(m.group(2))).status in ("ok", "variant_missing", "removed"):
                found.add(m.group(0))
        return sorted(found)

    def _restored_name(self, stem: str) -> str:
        """Токены в имени файла: подчёркивание рядом с токеном — разделитель слов, а не часть токена."""
        namer = rs.Restorer(self.vault)
        edits = namer.matches(stem)
        taken = [(a, b) for a, b, _ in edits]
        for start, end, replacement in namer.matches(stem.replace("_", " ")):
            if not any(start < b and end > a for a, b in taken):
                edits.append((start, end, replacement))
        if not edits:
            return stem
        edits.sort()
        pieces, cursor = [], 0
        for start, end, replacement in edits:
            if start < cursor:
                continue
            pieces += [stem[cursor:start], replacement]
            cursor = end
        pieces.append(stem[cursor:])
        return "".join(pieces)

    def _restore_result(self, job: Job, stats: "rs.RestoreStats", outcomes: list[FileOutcome]) -> dict:
        sources = self._sources(stats)
        result = {"restored": stats.restored, "found": stats.found, "unknown": stats.unknown,
                  "numbers": stats.numbers, "sources": sources}
        target = next((o for o in outcomes if o.status != "error" and o.out_path), None)
        if target is None or not sources:
            return result
        main = sources[0]
        if main["missing"]:
            target.add("info", f"Из исходного файла «{main['name']}» в этом файле не встретились {main['missing']} из "
                               f"{main['total']} обезличенных значений. Если вы их не удаляли, проверьте результат работы модели.")
        strays = [x for x in sources[1:] if x["count"] <= 5]
        if strays and main["count"] >= 20 * max(x["count"] for x in strays):
            listing = ", ".join(f"«{x['name']}» ({x['count']})" for x in strays[:3])
            target.add("warn", f"Несколько меток относятся к другим ранее обезличенным файлам: {listing}. Если модель "
                               "добавила эти метки сама, они восстановлены неверно — проверьте результат.")
        elif len(sources) > 1:
            target.add("info", "Метки в файле относятся к нескольким исходным файлам: "
                               + ", ".join(f"«{x['name']}»" for x in sources[:4]) + ".")
        return result

    def _sources(self, stats: "rs.RestoreStats") -> list[dict]:
        """Из каких обезличенных файлов пришли восстановленные метки (жадное покрытие по журналу заданий)."""
        remaining = set(stats.entities)
        sources: list[dict] = []
        entries = [e for e in self.vault.history if e.get("kind") == "anonymize" and e.get("tokens")]
        while remaining:
            best, overlap = None, 0
            for entry in reversed(entries):
                count = len(remaining & set(entry["tokens"]))
                if count > overlap:
                    best, overlap = entry, count
            if best is None:
                break
            total = len(best["tokens"])
            matched = len(stats.entities & set(best["tokens"]))
            sources.append({"name": ", ".join(best.get("files", []))[:120], "total": total, "count": overlap,
                            "matched": matched, "missing": max(0, total - matched), "time": best.get("time")})
            remaining -= set(best["tokens"])
        return sources

    # -- файлы результата -----------------------------------------------------

    def output_path(self, job_id: str, index: int) -> tuple[Path, str] | None:
        job = self.jobs.get(job_id)
        if not job or index >= len(job.files):
            return None
        outcome = job.files[index]
        if not outcome.out_path or outcome.status == "error":
            return None
        return Path(outcome.out_path), outcome.out_name

    def zip_outputs(self, job_id: str) -> Path | None:
        job = self.jobs.get(job_id)
        if not job:
            return None
        archive = job.dir / ("Обезличенные файлы.zip" if job.kind == "anonymize" else "Восстановленные файлы.zip")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            for outcome in job.files:
                if outcome.out_path and outcome.status != "error":
                    z.write(outcome.out_path, outcome.out_name)
        return archive
