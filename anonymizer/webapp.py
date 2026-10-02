"""Локальный сервер страницы программы: 127.0.0.1, ключ сеанса, одна страница.

Программа не выходит в сеть. Сервер слушает только петлевой адрес, каждый запрос проверяется по
случайному токену сеанса и заголовку Host, поэтому ни браузер, ни другой компьютер в сети до него
не дотянутся. Страницу показывает окно программы (`desktop.py`).
"""

from __future__ import annotations

import http.server
import json
import os
import queue
import mimetypes
import secrets
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from . import __version__, crypto, folders
from .service import MAX_UPLOAD_BYTES, Service, Upload, safe_name
from .tokens import GROUP_LABELS
from .vault import Vault

WEB_DIR = Path(__file__).parent / "web"


class App:
    """Состояние сервера: хранилище, сервис, загруженные файлы и запущенные задания."""

    def __init__(self, vault: Vault, workdir: Path, token: str):
        self.vault = vault
        self.service = Service(vault, workdir)
        self.token = token
        self.uploads: dict[str, tuple[Upload, dict, float]] = {}
        self.folders: dict[str, tuple[folders.FolderScan, float]] = {}
        self.runners: dict[str, threading.Thread] = {}
        self.stop = threading.Event()
        self.on_focus = None        # окно подставляет сюда функцию «вывести вперёд»
        self.addin_error = ""       # почему не запустился сервер add-in (порт занят и т.п.)
        self.keep_alive = False     # фоновая программа не завершается, когда закрыто последнее окно
        self.pinned_alive = False   # запущена как фоновая (автозапуск): остаётся всегда
        self.quit_event = threading.Event()
        self.windows: dict[str, float] = {}                 # окна-клиенты: когда каждое в последний раз отзывалось
        self.window_events: dict[str, "queue.Queue[dict]"] = {}
        self.spawn_window = None    # фоновая программа подставляет сюда запуск окна

    # -- окна ------------------------------------------------------------------

    WINDOW_ALIVE_SECONDS = 60

    def register_window(self, window_id: str) -> None:
        self.windows[window_id] = time.time()
        self.window_events.setdefault(window_id, queue.Queue())

    def close_window(self, window_id: str) -> None:
        self.windows.pop(window_id, None)
        self.window_events.pop(window_id, None)

    def live_windows(self) -> list[str]:
        limit = time.time() - self.WINDOW_ALIVE_SECONDS
        return [w for w, seen in self.windows.items() if seen >= limit]

    def wait_window_event(self, window_id: str, timeout: float = 25.0) -> dict:
        """Окно висит на этом запросе и просыпается, только когда ему есть что сказать: пока тихо, процессор не тратится."""
        if window_id not in self.windows:
            self.register_window(window_id)
        self.windows[window_id] = time.time()
        try:
            event = self.window_events[window_id].get(timeout=timeout)
        except queue.Empty:
            event = {}
        if window_id in self.windows:
            self.windows[window_id] = time.time()
        return event

    def request_window(self, tab: str | None = None) -> None:
        """Показать окно программы: уже открытому окну уходит событие, иначе запускается новое."""
        live = self.live_windows()
        if live:
            for window_id in live:
                self.window_events[window_id].put({"focus": True, "tab": tab})
        elif self.spawn_window:
            self.spawn_window(tab)

    # -- загрузка -------------------------------------------------------------

    def add_upload(self, name: str, data: bytes) -> dict:
        from .util import opaque_id
        upload_id = opaque_id(6).lower()
        upload = Upload(safe_name(name), data)
        info = self.service.peek(upload)
        self.uploads[upload_id] = (upload, info, time.time())
        self._trim_uploads()
        return {"id": upload_id, **info}

    def add_upload_path(self, path: str) -> dict:
        """Файл, выбранный в системном диалоге: читается напрямую с диска, без передачи через страницу."""
        source = Path(path)
        if source.stat().st_size > MAX_UPLOAD_BYTES:
            raise ValueError("Файл слишком большой (больше 300 МБ). Выберите файл поменьше.")
        return self.add_upload(source.name, source.read_bytes())

    def add_folder(self, path: str, kind: str) -> dict:
        """Папка, выбранная в системном диалоге: обходится сразу, читаются только размеры."""
        from .util import opaque_id
        root = Path(path)
        if not root.is_dir():
            raise ValueError("Папка недоступна. Выберите её заново.")
        scan = folders.scan_folder(root, kind)
        folder_id = opaque_id(6).lower()
        self.folders[folder_id] = (scan, time.time())
        return {"id": folder_id, **scan.public()}

    def _trim_uploads(self) -> None:
        limit = time.time() - 3 * 3600
        for key in [k for k, (_, _, created) in self.uploads.items() if created < limit]:
            self.uploads.pop(key, None)

    def take_uploads(self, ids: list[str]) -> list[Upload]:
        missing = [i for i in ids if i not in self.uploads]
        if missing:
            raise KeyError("Файл больше не доступен на сервере. Выберите его заново.")
        return [self.uploads[i][0] for i in ids]

    # -- задания --------------------------------------------------------------

    def start_job(self, kind: str, ids: list[str], options: dict, overrides: dict | None = None,
                  rerun_of: str | None = None) -> str:
        if rerun_of:
            job = self.service.new_job("anonymize", options)
            uploads: list[Upload] = []
        else:
            uploads = self.take_uploads(ids)
            job = self.service.new_job(kind, options)

        old = self.service.jobs.get(rerun_of) if rerun_of else None
        old_folder = getattr(old, "folder", None)

        def work() -> None:
            try:
                if rerun_of:
                    self.service.rerun(rerun_of, options, overrides or {}, job=job)
                    if old_folder is not None:
                        folders.remove_previous(old_folder)
                        job.folder = old_folder
                        folders.finish_folder(self.service, job, old_folder)
                elif kind == "anonymize":
                    self.service.anonymize(uploads, options, overrides, job=job)
                else:
                    self.service.restore(uploads, job=job)
            except Exception as exc:  # задание не должно уронить сервер
                job.state = "failed"
                job.error = str(exc) if isinstance(exc, (KeyError, ValueError)) else \
                    f"Не удалось выполнить операцию ({type(exc).__name__}). Файл не изменён."
        return self._run(job, work)

    def _run(self, job, work) -> str:
        thread = threading.Thread(target=work, daemon=True)
        self.runners[job.id] = thread
        thread.start()
        return job.id

    def start_folder_job(self, kind: str, folder_id: str, options: dict) -> str:
        entry = self.folders.get(folder_id)
        if entry is None:
            raise KeyError("Папка больше не доступна на сервере. Выберите её заново.")
        context = folders.FolderContext(entry[0], kind)
        job = self.service.new_job(kind, options)

        def work() -> None:
            try:
                folders.run_folder(self.service, context, options, job)
            except Exception as exc:
                job.state = "failed"
                job.error = str(exc) if isinstance(exc, (KeyError, ValueError)) else \
                    f"Не удалось обработать папку ({type(exc).__name__}). Исходные файлы не изменены."
        return self._run(job, work)

    def service_busy(self) -> bool:
        return any(thread.is_alive() for thread in self.runners.values())

    def job_public(self, job_id: str) -> dict | None:
        job = self.service.jobs.get(job_id)
        if job is None:
            return None
        return {"id": job.id, "kind": job.kind, "state": job.state, "percent": job.percent, "stage": job.stage,
                "files": [f.public() for f in job.files], "result": job.result if job.state == "done" else {},
                "error": job.error}

    # -- состояние ------------------------------------------------------------

    def state(self) -> dict:
        prefs = self.vault.prefs
        history = [{"time": h["time"], "kind": h["kind"], "files": h.get("files", [])[:3], "counts": h.get("counts", {})}
                   for h in reversed(self.vault.history[-8:])]
        return {"version": __version__, "notice": self.vault.notice, "prefs": prefs, "stats": self.vault.stats(), "history": history,
                "groups": GROUP_LABELS, "platform": sys.platform}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "Anonymizer"
    protocol_version = "HTTP/1.1"

    app: App
    port: int
    _cached_body: bytes | None = None

    # -- служебное ------------------------------------------------------------

    def log_message(self, *args) -> None:  # никаких журналов с именами файлов
        return

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def _cookie_token(self) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "anon_t":
                return value
        return ""

    def _authorized(self) -> bool:
        return self._host_ok() and secrets.compare_digest(self._cookie_token(), self.app.token)

    def _send(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8",
              headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
                         "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict | list, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _body(self) -> bytes:
        # Тело читается один раз и целиком. Не прочитанное тело остаётся в соединении и портит следующий запрос
        # (страница держит соединение открытым): «Очистить хранилище» ломало всё, что нажималось после него.
        if self._cached_body is not None:
            return self._cached_body
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD_BYTES + 1024 * 1024:
            self.close_connection = True
            raise ValueError("Файл слишком большой (больше 300 МБ).")
        self._cached_body = self.rfile.read(length) if length else b""
        return self._cached_body

    def _json_body(self) -> dict:
        raw = self._body()
        return json.loads(raw.decode("utf-8")) if raw else {}

    # -- маршруты -------------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._host_ok():
            return self._error(403, "Недопустимый адрес")
        if path == "/":
            query = urllib.parse.parse_qs(parsed.query)
            supplied = (query.get("t") or [""])[0]
            if supplied and secrets.compare_digest(supplied, self.app.token):
                return self._send(302, b"", "text/plain", {
                    "Location": "/", "Set-Cookie": f"anon_t={self.app.token}; Path=/; HttpOnly; SameSite=Strict"})
            if not self._authorized():
                return self._send(403, "Откройте программу заново: адрес с ключом сеанса устарел.".encode("utf-8"),
                                  "text/plain; charset=utf-8")
            return self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        if not self._authorized():
            return self._error(403, "Нет доступа")
        if path == "/api/ping":
            return self._json({"ok": True, "pid": os.getpid(), "version": __version__, "keep_alive": self.app.keep_alive,
                                    "windows": len(self.app.live_windows())})
        if path == "/api/window/wait":
            window_id = (urllib.parse.parse_qs(parsed.query).get("id") or [""])[0]
            return self._json(self.app.wait_window_event(window_id) if window_id else {})
        if path == "/api/state":
            return self._json(self.app.state())
        if path == "/api/addin/status":
            from . import addin_install
            return self._json({**addin_install.status(), "server_error": self.app.addin_error})
        if path.startswith("/api/jobs/"):
            job = self.app.job_public(path.rsplit("/", 1)[-1])
            return self._json(job) if job else self._error(404, "Задание не найдено")
        if path.startswith("/api/download/"):
            return self._download(path)
        return self._error(404, "Не найдено")

    def _download(self, path: str) -> None:
        parts = path.strip("/").split("/")
        if len(parts) != 4:
            return self._error(404, "Не найдено")
        _, _, job_id, which = parts
        service = self.app.service
        if which == "all":
            archive = service.zip_outputs(job_id)
            if archive is None:
                return self._error(404, "Файлы не найдены")
            name = archive.name
            data = archive.read_bytes()
            mime = "application/zip"
        else:
            found = service.output_path(job_id, int(which)) if which.isdigit() else None
            if found is None:
                return self._error(404, "Файл не найден")
            file_path, name = found
            data = file_path.read_bytes()
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        quoted = urllib.parse.quote(name)
        self._send(200, data, mime, {"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"})

    def do_POST(self) -> None:
        self._cached_body = None
        if not self._host_ok() or not self._authorized():
            self.close_connection = True          # тело не читаем: чужому запросу соединение не оставляем
            return self._error(403, "Нет доступа")
        path = urllib.parse.urlparse(self.path).path
        try:
            self._body()
        except ValueError as exc:
            return self._error(400, str(exc))
        if self.headers.get("X-Requested-With") != "anonymizer":
            return self._error(403, "Нет доступа")
        try:
            if path == "/api/upload":
                name = urllib.parse.unquote(self.headers.get("X-Filename") or "file")
                data = self._body()
                if not data:
                    return self._error(400, "Файл пустой.")
                return self._json(self.app.add_upload(name, data))
            if path == "/api/anonymize":
                body = self._json_body()
                job_id = self.app.start_job("anonymize", body.get("files", []), body.get("options", {}))
                return self._json({"job": job_id})
            if path == "/api/restore":
                body = self._json_body()
                job_id = self.app.start_job("restore", body.get("files", []), {})
                return self._json({"job": job_id})
            if path == "/api/folder/anonymize":
                body = self._json_body()
                return self._json({"job": self.app.start_folder_job("anonymize", body.get("folder", ""), body.get("options", {}))})
            if path == "/api/folder/restore":
                body = self._json_body()
                return self._json({"job": self.app.start_folder_job("restore", body.get("folder", ""), {})})
            if path == "/api/rerun":
                body = self._json_body()
                job_id = self.app.start_job("anonymize", [], body.get("options", {}), body.get("overrides", {}),
                                            rerun_of=body.get("job"))
                return self._json({"job": job_id})
            if path == "/api/prefs":
                body = self._json_body()
                allowed = {k: body[k] for k in ("retention_days", "hide_terms", "keep_terms", "numbers", "strict", "countries", "neutral_names") if k in body}
                self.app.vault.set_prefs(**allowed)
                return self._json(self.app.state())
            if path == "/api/vault/clear":
                self.app.vault.clear()
                return self._json(self.app.state())
            if path == "/api/backup/export":
                password = self._json_body().get("password", "")
                if len(password) < 6:
                    return self._error(400, "Пароль должен быть не короче 6 знаков.")
                blob = self.app.vault.export_backup(password)
                return self._send(200, blob, "application/octet-stream", {
                    "Content-Disposition": "attachment; filename*=UTF-8''anonymizer-backup.anonkeys"})
            if path == "/api/backup/import":
                password = urllib.parse.unquote(self.headers.get("X-Password") or "")
                info = self.app.vault.import_backup(self._body(), password)
                return self._json({**info, **self.app.state()})
            if path == "/api/forget":
                body = self._json_body()
                self.app.service.forget_job(body.get("job", ""))
                return self._json({"ok": True})
            if path in ("/api/addin/install", "/api/addin/uninstall"):
                from . import addin_install
                steps = addin_install.install() if path.endswith("install") and not path.endswith("uninstall") else addin_install.uninstall()
                self.app.keep_alive = self.app.pinned_alive or addin_install.is_installed()
                return self._json({"steps": steps})
            if path == "/api/native/upload-path":
                # Путь выбрал человек в системном окне; читает файл фоновая программа, а не страница.
                try:
                    return self._json(self.app.add_upload_path(str(self._json_body().get("path", ""))))
                except OSError:
                    return self._error(400, "Файл не удалось прочитать. Проверьте, что он не открыт в другой программе.")
            if path == "/api/native/folder":
                body = self._json_body()
                try:
                    return self._json({"folder": self.app.add_folder(str(body.get("path", "")), body.get("kind", "anonymize"))})
                except OSError:
                    return self._error(400, "Папку не удалось прочитать.")
            if path == "/api/window/open":
                self.app.register_window(str(self._json_body().get("id", "")))
                return self._json({"ok": True})
            if path == "/api/window/closed":
                self.app.close_window(str(self._json_body().get("id", "")))
                return self._json({"ok": True})
            if path == "/api/quit":
                self.app.quit_event.set()
                return self._json({"ok": True})
            if path == "/api/focus":
                if self.app.on_focus:
                    self.app.on_focus()
                else:
                    self.app.request_window()
                return self._json({"ok": True})
        except KeyError as exc:
            return self._error(404, str(exc.args[0]) if exc.args else "Не найдено")
        except (ValueError, crypto.KeyErrorSafe) as exc:
            return self._error(400, str(exc))
        except Exception as exc:
            return self._error(500, f"Внутренняя ошибка ({type(exc).__name__}).")
        return self._error(404, "Не найдено")


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def handle_error(self, request, client_address) -> None:
        # Страница обрывает соединение при перезагрузке; это не ошибка сервера.
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)

    def server_bind(self) -> None:
        # HTTPServer.server_bind вызывает getfqdn(): на корпоративных компьютерах с медленным DNS запуск
        # зависал на минуты. Программе имя узла не нужно.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def create_server(vault: Vault | None = None, workdir: Path | None = None, port: int = 0) -> tuple[Server, App]:
    token = secrets.token_urlsafe(24)
    vault = vault or Vault.default()
    app = App(vault, workdir or (crypto.app_data_dir() / "work"), token)
    port = port or _free_port()
    handler = type("BoundHandler", (Handler,), {"app": app, "port": port})
    server = Server(("127.0.0.1", port), handler)
    return server, app
