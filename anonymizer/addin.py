"""Add-in для Word, Excel и PowerPoint: локальный HTTPS-сервер и API панели.

Панель (страница внутри Office) читает открытый файл целиком, отправляет его сюда, получает обезличенный или
восстановленный файл и вставляет его в документ. Всё распознавание остаётся в `Service`; здесь только транспорт,
зашифрованная копия оригинала до замены и проверка результата после замены.
"""

from __future__ import annotations

import base64
import copy
import hmac
import io
import json
import mimetypes
import os
import re
import secrets
import ssl
import sys
import tempfile
import time
import urllib.parse
import zipfile
from collections import Counter
from pathlib import Path

from lxml import etree

from . import __version__, certs, crypto
from .formats import classify, extract_text
from .service import MAX_UPLOAD_BYTES, Upload, container_problem, safe_name, unpacked_size_problem
from .util import atomic_write, opaque_id
from .webapp import App, Handler, Server

ADDIN_PORT = 47831
ADDIN_DIR = Path(__file__).parent / "addin_web"
OFFICE_KINDS = {".docx": "Word", ".xlsx": "Excel", ".pptx": "PowerPoint"}
KEEP_BACKUPS = 20
KEEP_BACKUP_SECONDS = 7 * 24 * 3600


# -- копия оригинала -------------------------------------------------------------------

class Originals:
    """Зашифрованные копии файлов до замены. Позволяют вернуть документ ровно таким, каким он был."""

    def __init__(self, directory: Path, key: bytes | None = None):
        self.dir = directory
        self.key = key
        self.dir.mkdir(parents=True, exist_ok=True)

    def _seal(self, data: bytes) -> bytes:
        return crypto.encrypt_blob(data, self.key)

    def _open(self, blob: bytes) -> bytes:
        return crypto.decrypt_blob(blob, self.key)

    def save(self, kind: str, name: str, data: bytes) -> str:
        self.purge()
        backup_id = opaque_id(8).lower()
        meta = {"id": backup_id, "kind": kind, "name": name, "size": len(data), "time": time.time()}
        atomic_write(self.dir / f"{backup_id}.bin", self._seal(data))
        atomic_write(self.dir / f"{backup_id}.meta", self._seal(json.dumps(meta, ensure_ascii=False).encode("utf-8")))
        return backup_id

    def meta(self, backup_id: str) -> dict | None:
        if not re.fullmatch(r"[a-z0-9]{8,16}", backup_id or ""):
            return None
        try:
            return json.loads(self._open((self.dir / f"{backup_id}.meta").read_bytes()))
        except (OSError, ValueError, crypto.KeyErrorSafe):
            return None

    def read(self, backup_id: str) -> bytes | None:
        if self.meta(backup_id) is None:
            return None
        try:
            return self._open((self.dir / f"{backup_id}.bin").read_bytes())
        except (OSError, crypto.KeyErrorSafe):
            return None

    def listing(self) -> list[dict]:
        items = [m for m in (self.meta(p.stem) for p in self.dir.glob("*.meta")) if m]
        return sorted(items, key=lambda m: m["time"], reverse=True)

    def discard(self, backup_id: str) -> None:
        for suffix in (".bin", ".meta"):
            try:
                (self.dir / f"{backup_id}{suffix}").unlink()
            except OSError:
                pass

    def purge(self) -> None:
        items = self.listing()
        limit = time.time() - KEEP_BACKUP_SECONDS
        for index, meta in enumerate(items):
            if index >= KEEP_BACKUPS or meta["time"] < limit:
                self.discard(meta["id"])
        known = {m["id"] for m in items}
        for path in self.dir.iterdir():
            if path.stem not in known and path.suffix in (".bin", ".meta", ".tmp"):
                try:
                    if path.stat().st_mtime < limit:
                        path.unlink()
                except OSError:
                    pass


class TrackedChanges(ValueError):
    """В документе Word есть исправления: они хранят прежний текст, и заменой его не убрать."""

    def __init__(self, count: int):
        super().__init__(f"В документе есть исправления ({count}). Они хранят прежний текст. Примите или отклоните их и повторите.")
        self.count = count


_REVISION_TAGS = re.compile(rb"<w:(?:ins|del|moveFrom|moveTo)\b")


def tracked_changes(data: bytes) -> int:
    """Число исправлений Word (вставки, удаления, перемещения) во всех частях документа. Для не-Word файлов ноль."""
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in archive.namelist():
                if name.startswith("word/") and name.endswith(".xml") and ("document" in name or "header" in name
                                                                            or "footer" in name or "comments" in name
                                                                            or "footnotes" in name or "endnotes" in name):
                    total += len(_REVISION_TAGS.findall(archive.read(name)))
    except (zipfile.BadZipFile, KeyError):
        return 0
    return total


# -- структура и проверка --------------------------------------------------------------

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
       "p": "http://schemas.openxmlformats.org/presentationml/2006/main"}


def structure_of(source, suffix: str | None = None) -> dict:
    """Описание файла, нужное панели для сборки документа и проверки: листы Excel, имена, число слайдов.

    `source` — путь к файлу или его байты (тогда нужно расширение)."""
    info: dict = {"sheets": [], "names": [], "slides": 0, "headers": 0, "comments": 0, "props": {}, "custom": []}
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    elif suffix is None:
        suffix = Path(source).suffix
    suffix = (suffix or "").lower()
    if suffix not in OFFICE_KINDS:
        return info
    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        if suffix == ".xlsx":
            root = etree.fromstring(archive.read("xl/workbook.xml"))
            for sheet in root.iterfind(".//m:sheets/m:sheet", _NS):
                info["sheets"].append({"name": sheet.get("name"), "state": sheet.get("state") or "visible"})
            for node in root.iterfind(".//m:definedNames/m:definedName", _NS):
                if node.get("localSheetId") is None and not (node.get("name") or "").startswith("_xlnm."):
                    info["names"].append({"name": node.get("name"), "ref": node.text or "", "hidden": node.get("hidden") == "1"})
        elif suffix == ".pptx":
            info["slides"] = sum(1 for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
        else:
            info["headers"] = sum(1 for n in names if re.fullmatch(r"word/(header|footer)\d*\.xml", n))
            info["parts"] = header_footer_parts(archive)
        info["props"], info["custom"] = _properties(archive, names)
        info["comments"] = sum(1 for n in names if "comments" in n.lower() and n.endswith(".xml") and "Extended" not in n and "Ids" not in n)
    return info


_CORE_FIELDS = {"creator": "author", "title": "title", "subject": "subject", "keywords": "keywords", "description": "comments",
                "category": "category"}
_APP_FIELDS = {"Company": "company", "Manager": "manager"}


def _properties(archive: zipfile.ZipFile, names: list[str]) -> tuple[dict, list[dict]]:
    """Свойства файла (автор, название, компания…) и пользовательские свойства: Office при вставке файла их не переносит."""
    props: dict[str, str] = {}
    custom: list[dict] = []
    for part, fields in (("docProps/core.xml", _CORE_FIELDS), ("docProps/app.xml", _APP_FIELDS)):
        if part in names:
            try:
                for node in etree.fromstring(archive.read(part)):
                    field = fields.get(etree.QName(node).localname)
                    if field:
                        props[field] = node.text or ""
            except etree.XMLSyntaxError:
                pass
    if "docProps/custom.xml" in names:
        try:
            for node in etree.fromstring(archive.read("docProps/custom.xml")):
                value = next(iter(node), None)
                if value is not None and node.get("name"):
                    custom.append({"name": node.get("name"), "value": value.text or ""})
        except etree.XMLSyntaxError:
            pass
    return props, custom


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_HF_TYPES = {"default": "Primary", "first": "FirstPage", "even": "EvenPages"}


def _plain(root) -> str:
    paragraphs = []
    for para in root.iter(f"{{{_W}}}p"):
        paragraphs.append("".join(t.text or "" for t in para.iter(f"{{{_W}}}t")))
    return "\n".join(paragraphs)


def header_footer_parts(archive: zipfile.ZipFile) -> list[dict]:
    """Колонтитулы по разделам. Вставка файла в Office заменяет тело, но не колонтитулы: панель переписывает их сама.

    У каждого колонтитула: раздел, вид (header/footer), тип (Primary/FirstPage/EvenPages), текст и, если колонтитул не
    ссылается на картинки и связи, готовый маленький документ для вставки с сохранением оформления."""
    names = set(archive.namelist())
    if "word/document.xml" not in names or "word/_rels/document.xml.rels" not in names:
        return []
    rels = {r.get("Id"): r.get("Target") for r in etree.fromstring(archive.read("word/_rels/document.xml.rels"))}
    document = etree.fromstring(archive.read("word/document.xml"))
    out: list[dict] = []
    for index, sect in enumerate(document.iter(f"{{{_W}}}sectPr")):
        for ref in sect:
            tag = etree.QName(ref).localname
            if tag not in ("headerReference", "footerReference"):
                continue
            target = rels.get(ref.get(f"{{{_R}}}id"))
            part = f"word/{target}" if target and not target.startswith("/") else (target or "").lstrip("/")
            if part not in names:
                continue
            root = etree.fromstring(archive.read(part))
            item = {"section": index, "kind": "header" if tag == "headerReference" else "footer",
                    "type": _HF_TYPES.get(ref.get(f"{{{_W}}}type") or "default", "Primary"), "text": _plain(root), "docx": None}
            if not any(attr.startswith(f"{{{_R}}}") for node in root.iter() for attr in node.attrib):
                item["docx"] = base64.b64encode(_mini_docx(archive, document, root)).decode("ascii")
            out.append(item)
    return out


def _mini_docx(archive: zipfile.ZipFile, document, part_root) -> bytes:
    """Копия документа, где всё тело заменено содержимым колонтитула: так оформление колонтитула сохраняется при вставке."""
    doc = copy.deepcopy(document)
    body = doc.find(f"{{{_W}}}body")
    for child in list(body):
        body.remove(child)
    for child in part_root:
        body.append(copy.deepcopy(child))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for info in archive.infolist():
            data = etree.tostring(doc, xml_declaration=True, encoding="UTF-8", standalone=True) \
                if info.filename == "word/document.xml" else archive.read(info.filename)
            z.writestr(info.filename, data)
    return out.getvalue()


def _words(text: str) -> Counter:
    return Counter(re.findall(r"[^\W_]+", text.casefold()))


def _fold(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _body_text(path: Path) -> str:
    """Текст документа без свойств файла: свойства Office пишет сам (автор, дата, список листов), и они не совпадают по построению."""
    if path.suffix.lower() not in OFFICE_KINDS:
        return extract_text(path)
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / path.name
        with zipfile.ZipFile(path) as source, zipfile.ZipFile(copy, "w", zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                if not info.filename.lower().startswith("docprops/"):
                    target.writestr(info, source.read(info.filename))
        return extract_text(copy)


def _leaks(values: list[str], text: str) -> list[str]:
    folded = _fold(text)
    found = []
    for value in dict.fromkeys(values):
        needle = _fold(value)
        if len(needle) >= 3 and re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", folded):
            found.append(value)
    return found


def verify_document(expected: Path, actual: Path, replaced: list[str] | None = None) -> dict:
    """Сверяет документ, заново прочитанный из Office, с тем, что должно было получиться.

    `problems` — то, что точно не так (потерян текст, остался заменённый текст, изменилось число листов или слайдов);
    `notes` — отличия, которые могут быть нормальными (Office пересохраняет файл по-своему).
    """
    problems: list[dict] = []
    warnings: list[dict] = []
    notes: list[str] = []
    expected_text, actual_text = _body_text(expected), _body_text(actual)
    # Метки метаданных (Meta11: имя темы, автор) Office переписывает сам: на содержимое документа они не влияют.
    exp_words, act_words = (Counter({w: n for w, n in _words(t).items() if not re.fullmatch(r"meta\d+", w)})
                            for t in (expected_text, actual_text))
    missing, extra = exp_words - act_words, act_words - exp_words
    if missing:
        sample = ", ".join(w for w, _ in missing.most_common(8))
        problems.append({"code": "lost_text", "text": f"После замены в документе нет части текста ({sum(missing.values())} слов): {sample}."})
    if extra:
        sample = ", ".join(w for w, _ in extra.most_common(8))
        notes.append(f"В документе появились слова, которых нет в результате ({sum(extra.values())}): {sample}.")
    if replaced:
        leaks = _leaks(replaced, actual_text)
        if leaks:
            problems.append({"code": "leak", "text": "Заменённые значения всё ещё есть в документе: " + ", ".join(f"«{v}»" for v in leaks[:8]) +
                             ". Возможно, они остались в служебных частях файла (образцы слайдов, скрытые листы)."})
        in_properties = [v for v in _leaks(replaced, extract_text(actual)) if v not in leaks]
        if in_properties:
            warnings.append({"code": "properties", "text": "В свойствах файла остались: " + ", ".join(f"«{v}»" for v in in_properties[:6]) +
                             ". Office записывает имя автора сам при каждом сохранении. Чтобы файл не нёс его, включите в Office «Удалять личные "
                             "сведения при сохранении» или отправляйте файл через программу Anonymizer."})
    want, have = structure_of(expected), structure_of(actual)
    if want["sheets"] and [s["name"] for s in want["sheets"]] != [s["name"] for s in have["sheets"]]:
        problems.append({"code": "sheets", "text": "Имена или порядок листов отличаются от ожидаемых: " +
                         ", ".join(s["name"] for s in have["sheets"][:8]) + "."})
    if want["slides"] and want["slides"] != have["slides"]:
        problems.append({"code": "slides", "text": f"Число слайдов {have['slides']}, ожидалось {want['slides']}."})
    if want["headers"] and have["headers"] < want["headers"]:
        problems.append({"code": "headers", "text": "Колонтитулы документа отличаются от ожидаемых: часть колонтитулов не перенеслась."})
    if want["comments"] and have["comments"] < want["comments"]:
        problems.append({"code": "comments", "text": "Примечания документа отличаются от ожидаемых: часть примечаний не перенеслась."})
    return {"ok": not problems, "problems": problems, "warnings": warnings, "notes": notes}


# -- сервис ------------------------------------------------------------------------------

class AddinApi:
    def __init__(self, app: App, originals: Originals, token: str):
        self.app = app
        self.originals = originals
        self.token = token
        self.meta: dict[str, dict] = {}
        self.tab_request = None          # окно подставляет сюда функцию «показать вкладку»

    def info(self) -> dict:
        prefs = self.app.vault.prefs
        return {"ok": True, "version": __version__, "platform": sys.platform,
                "prefs": {k: prefs.get(k) for k in ("numbers", "strict", "countries", "neutral_names")},
                "stats": self.app.vault.stats(), "notice": self.app.vault.notice}

    def start(self, kind: str, name: str, data: bytes, options: dict) -> dict:
        if kind not in ("anonymize", "restore"):
            raise ValueError("Неизвестная операция.")
        name = safe_name(name)
        if Path(name).suffix.lower() not in OFFICE_KINDS:
            raise ValueError("Add-in работает с файлами Word, Excel и PowerPoint (.docx, .xlsx, .pptx).")
        if not data:
            raise ValueError("Документ пустой: из него не удалось прочитать данные.")
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("Документ слишком большой (больше 300 МБ).")
        problem = container_problem(name, data[:16])
        if problem:
            raise ValueError(problem)
        if kind == "anonymize" and name.lower().endswith(".docx"):
            count = tracked_changes(data)
            if count:
                raise TrackedChanges(count)
        backup_id = self.originals.save(kind, name, data)
        upload_id = opaque_id(6).lower()
        self.app.uploads[upload_id] = (Upload(name, data), {}, time.time())
        job_id = self.app.start_job(kind, [upload_id], options if kind == "anonymize" else {})
        self.meta[job_id] = {"backup": backup_id, "name": name, "kind": kind}
        return {"job": job_id, "backup": backup_id}

    def rerun(self, job_id: str, options: dict, overrides: dict) -> dict:
        if job_id not in self.meta:
            raise KeyError("Задание уже удалено. Начните заново.")
        new_id = self.app.start_job("anonymize", [], options, overrides, rerun_of=job_id)
        self.meta[new_id] = dict(self.meta.pop(job_id))
        return {"job": new_id, "backup": self.meta[new_id]["backup"]}

    def job(self, job_id: str) -> dict | None:
        public = self.app.job_public(job_id)
        if public is None or job_id not in self.meta:
            return None
        meta = self.meta[job_id]
        public["backup"] = meta["backup"]
        public["name"] = meta["name"]
        if public["state"] == "done" and public["files"] and public["files"][0]["downloadable"]:
            found = self.app.service.output_path(job_id, 0)
            if found:
                try:
                    public["structure"] = structure_of(found[0])
                except Exception:
                    public["structure"] = {}
        public["result"] = _slim_result(public.get("result") or {})
        return public

    def result_bytes(self, job_id: str) -> bytes | None:
        found = self.app.service.output_path(job_id, 0) if job_id in self.meta else None
        return found[0].read_bytes() if found else None

    def verify(self, job_id: str, data: bytes) -> dict:
        job = self.app.service.jobs.get(job_id)
        found = self.app.service.output_path(job_id, 0) if job else None
        if not found or job_id not in self.meta:
            raise KeyError("Результат этого задания уже удалён. Проверка невозможна.")
        name = self.meta[job_id]["name"]
        with tempfile.TemporaryDirectory(dir=self.app.service.workdir) as tmp:
            actual = Path(tmp) / name
            actual.write_bytes(data)
            problem = container_problem(name, data[:16]) or unpacked_size_problem(actual)
            if problem:
                return {"ok": False, "problems": [{"code": "unreadable", "text": problem}], "notes": []}
            replaced = job.replaced if job.kind == "anonymize" else None
            return verify_document(found[0], actual, replaced)

    def unchanged(self, job_id: str, data: bytes) -> dict:
        """Не изменился ли документ, пока шла обработка. Замена затёрла бы то, что человек успел набрать."""
        meta = self.meta.get(job_id)
        original = self.originals.read(meta["backup"]) if meta else None
        if original is None:
            raise KeyError("Исходная копия задания уже удалена.")
        with tempfile.TemporaryDirectory(dir=self.app.service.workdir) as tmp:
            before, after = Path(tmp) / ("a_" + meta["name"]), Path(tmp) / ("b_" + meta["name"])
            before.write_bytes(original)
            after.write_bytes(data)
            words = lambda path: Counter(w for w in _words(_body_text(path)).elements() if not w.isdigit())   # noqa: E731
            changed = (words(before) - words(after)) + (words(after) - words(before))
        return {"same": not changed, "changed": sum(changed.values()), "sample": [w for w, _ in changed.most_common(5)]}

    def backup_bytes(self, backup_id: str) -> tuple[dict, bytes] | None:
        meta = self.originals.meta(backup_id)
        data = self.originals.read(backup_id) if meta else None
        return (meta, data) if meta and data is not None else None

    def forget(self, job_id: str) -> None:
        self.meta.pop(job_id, None)
        self.app.service.forget_job(job_id)


def _slim_result(result: dict) -> dict:
    """Панели хватает сводки: полная таблица замен остаётся в программе."""
    keep = ("summary", "total", "suggestions", "strong_total", "weak_total", "restored", "found", "unknown", "numbers")
    slim = {k: result[k] for k in keep if k in result}
    slim["suggestions"] = [x for x in slim.get("suggestions", []) if x.get("strong")][:30]
    return slim


# -- сервер --------------------------------------------------------------------------------

MIME = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".html": "text/html; charset=utf-8",
        ".png": "image/png", ".svg": "image/svg+xml", ".json": "application/json", ".xml": "application/xml"}


class AddinHandler(Handler):
    """Маршруты страницы add-in. Отдельный класс: другая модель доступа, чем у окна программы."""

    api: AddinApi

    def _origins(self) -> set[str]:
        return {f"https://localhost:{self.port}", f"https://127.0.0.1:{self.port}"}

    def _api_authorized(self) -> bool:
        if not self._host_ok():
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in self._origins():
            return False
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            return False
        if self.headers.get("X-Requested-With") != "anonymizer":
            return False
        return hmac.compare_digest(self.headers.get("X-Addin-Token") or "", self.api.token)

    # -- GET --

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._host_ok():
            return self._error(403, "Недопустимый адрес")
        if path.startswith("/addin/"):
            return self._static(path[len("/addin/"):])
        if not self._api_authorized():
            return self._error(403, "Нет доступа")
        if path == "/api/addin/info":
            return self._json(self.api.info())
        if path == "/api/addin/backups":
            return self._json({"items": self.api.originals.listing()[:10]})
        match = re.fullmatch(r"/api/addin/jobs/([a-z0-9]+)", path)
        if match:
            job = self.api.job(match.group(1))
            return self._json(job) if job else self._error(404, "Задание не найдено")
        match = re.fullmatch(r"/api/addin/result/([a-z0-9]+)", path)
        if match:
            data = self.api.result_bytes(match.group(1))
            return self._send(200, data, "application/octet-stream") if data is not None else self._error(404, "Результат не найден")
        match = re.fullmatch(r"/api/addin/backup/([a-z0-9]+)", path)
        if match:
            found = self.api.backup_bytes(match.group(1))
            return self._send(200, found[1], "application/octet-stream") if found else self._error(404, "Копия не найдена")
        match = re.fullmatch(r"/api/addin/backup-info/([a-z0-9]+)", path)
        if match:
            # Структура (листы, колонтитулы, свойства) может быть большой: она идёт телом ответа, а не заголовком.
            found = self.api.backup_bytes(match.group(1))
            if not found:
                return self._error(404, "Копия не найдена")
            return self._json({"meta": found[0], "structure": structure_of(found[1], Path(found[0]["name"]).suffix)})
        return self._error(404, "Не найдено")

    def _static(self, rel: str) -> None:
        rel = urllib.parse.unquote(rel) or "taskpane.html"
        target = (ADDIN_DIR / rel).resolve()
        try:
            target.relative_to(ADDIN_DIR.resolve())
        except ValueError:
            return self._error(404, "Не найдено")
        if not target.is_file() or target.name.endswith(".tmpl"):
            return self._error(404, "Не найдено")
        data = target.read_bytes()
        if target.name == "taskpane.html":
            data = data.replace(b"__ADDIN_TOKEN__", self.api.token.encode("ascii"))
        self._send(200, data, MIME.get(target.suffix, mimetypes.guess_type(target.name)[0] or "application/octet-stream"))

    # -- POST --

    def do_POST(self) -> None:
        self._cached_body = None
        if not self._api_authorized():
            self.close_connection = True
            return self._error(403, "Нет доступа")
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            self._body()
        except ValueError as exc:
            return self._error(400, str(exc))
        try:
            if path in ("/api/addin/anonymize", "/api/addin/restore"):
                name = urllib.parse.unquote(self.headers.get("X-Filename") or "document")
                options = json.loads(urllib.parse.unquote(self.headers.get("X-Options") or "{}") or "{}")
                kind = "anonymize" if path.endswith("anonymize") else "restore"
                return self._json(self.api.start(kind, name, self._body(), options if isinstance(options, dict) else {}))
            if path == "/api/addin/rerun":
                body = self._json_body()
                return self._json(self.api.rerun(body.get("job", ""), body.get("options", {}), body.get("overrides", {})))
            match = re.fullmatch(r"/api/addin/verify/([a-z0-9]+)", path)
            if match:
                return self._json(self.api.verify(match.group(1), self._body()))
            if path == "/api/addin/debug":
                # Журнал шагов панели для отладки в Office, где консоли нет. Включается переменной ANONYMIZER_ADDIN_DEBUG.
                if os.environ.get("ANONYMIZER_ADDIN_DEBUG"):
                    with open(certs.addin_dir() / "addin-debug.log", "a", encoding="utf-8") as log:
                        log.write(f"{time.strftime('%H:%M:%S')} {self._body().decode('utf-8', 'replace')[:500]}\n")
                return self._json({"ok": True})
            match = re.fullmatch(r"/api/addin/unchanged/([a-z0-9]+)", path)
            if match:
                return self._json(self.api.unchanged(match.group(1), self._body()))
            match = re.fullmatch(r"/api/addin/forget/([a-z0-9]+)", path)
            if match:
                self.api.forget(match.group(1))
                return self._json({"ok": True})
            if path == "/api/addin/open-app":
                tab = (self._json_body().get("tab") or "anon")
                if self.api.tab_request:
                    self.api.tab_request("rest" if tab == "rest" else "anon")
                elif self.api.app.on_focus:
                    self.api.app.on_focus()
                return self._json({"ok": True})
        except KeyError as exc:
            return self._error(404, str(exc.args[0]) if exc.args else "Не найдено")
        except TrackedChanges as exc:
            return self._json({"error": str(exc), "code": "tracked_changes", "count": exc.count}, 400)
        except (ValueError, crypto.KeyErrorSafe) as exc:
            return self._error(400, str(exc))
        except Exception as exc:
            return self._error(500, f"Внутренняя ошибка ({type(exc).__name__}).")
        return self._error(404, "Не найдено")


class AddinServer(Server):
    allow_reuse_address = sys.platform != "win32"      # на Windows этот флаг позволил бы второму процессу занять тот же порт

    def handle_error(self, request, client_address) -> None:
        if isinstance(sys.exc_info()[1], (ssl.SSLError, ConnectionError, TimeoutError)):
            return                                      # Office и система часто бросают рукопожатие: это не ошибка сервера
        super().handle_error(request, client_address)


def load_token(directory: Path) -> str:
    """Токен установки: один и тот же между запусками, чтобы открытая панель не теряла связь после перезапуска программы."""
    path = directory / "addin.token"
    try:
        value = path.read_text("ascii").strip()
        if len(value) >= 32:
            return value
    except OSError:
        pass
    value = secrets.token_urlsafe(32)
    atomic_write(path, value.encode("ascii"))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return value


def create_addin_server(app: App, port: int = ADDIN_PORT, files: certs.CertFiles | None = None,
                        directory: Path | None = None, key: bytes | None = None) -> tuple[AddinServer, AddinApi]:
    directory = directory or certs.addin_dir()
    files = files or certs.ensure_certs(directory, key)
    api = AddinApi(app, Originals(directory / "originals", key), load_token(directory))
    handler = type("BoundAddinHandler", (AddinHandler,), {"app": app, "api": api, "port": port})
    server = AddinServer(("127.0.0.1", port), handler, bind_and_activate=False)
    try:
        server.server_bind()
        server.server_activate()
    except OSError:
        server.server_close()
        raise
    handler.port = server.server_address[1]        # порт 0 (тесты): настоящий порт известен только после привязки
    context = certs.ssl_context(files)
    # Рукопожатие выполняется в потоке обработчика, а не в потоке приёма соединений: медленный клиент не должен
    # задерживать остальных.
    server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    return server, api
