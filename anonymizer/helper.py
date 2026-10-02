"""Два процесса вместо одного: фоновая программа (helper) и окно.

Фоновая программа без окна владеет хранилищем и обоими серверами (страница программы и add-in). Окно (pywebview) подключается к
ней как клиент по локальному адресу. Поэтому:
- add-in работает, даже когда окно закрыто, и фон не держит WebView2 в памяти;
- сбой окна не останавливает панель в Office;
- на macOS окно в фоне больше не удерживает GIL и не замораживает серверы (без событий цикл Cocoa не отдаёт интерпретатор).

Окно выбирает файлы и папки в системных диалогах, а читает их фоновая программа (`/api/native/*`): страница путей не получает.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from types import SimpleNamespace

from . import addin_install, crypto, desktop
from .addin import create_addin_server
from .vault import Vault
from .webapp import App, create_server

SERVE_FLAG = "--serve"
BACKGROUND_FLAG = "--background"
WATCHDOG_SECONDS = 15
STARTUP_GRACE_SECONDS = 45


# -- запуск процессов ---------------------------------------------------------------------

def launch_args(*extra: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, *extra]
    return [sys.executable, str(Path(__file__).resolve().parent.parent / "main.py"), *extra]


def spawn_detached(args: list[str]) -> None:
    """Запускает процесс, который переживёт тот, что его запустил (у собранной программы нет консоли)."""
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000     # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, **kwargs)


# -- клиент -------------------------------------------------------------------------------

class HelperError(ValueError):
    """Фоновая программа ответила ошибкой; текст уже понятен человеку."""


class HelperClient:
    def __init__(self, info: dict, timeout: float = 120.0):
        self.port = int(info["port"])
        self.token = info["token"]
        self.timeout = timeout

    def _headers(self, extra: dict | None = None) -> dict:
        return {"Cookie": f"anon_t={self.token}", "X-Requested-With": "anonymizer", **(extra or {})}

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None,
                timeout: float | None = None) -> tuple[int, dict, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout or self.timeout)
        try:
            connection.request(method, path, body=body, headers=self._headers(headers))
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def json(self, method: str, path: str, payload: dict | None = None, timeout: float | None = None) -> dict:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        status, _, data = self.request(method, path, body, {"Content-Type": "application/json"} if body is not None else None, timeout)
        try:
            parsed = json.loads(data.decode("utf-8")) if data else {}
        except ValueError:
            parsed = {}
        if status >= 400:
            raise HelperError(parsed.get("error") or f"Ошибка {status}. Повторите действие.")
        return parsed


def read_instance() -> dict | None:
    try:
        info = json.loads(desktop.instance_file().read_text("utf-8"))
        return info if info.get("port") and info.get("token") else None
    except (OSError, ValueError):
        return None


def ping(info: dict | None, timeout: float = 3.0) -> bool:
    if not info:
        return False
    try:
        return bool(HelperClient(info).json("GET", "/api/ping", timeout=timeout).get("ok"))
    except (OSError, ValueError, http.client.HTTPException):    # HelperError и негодный токен — тоже ValueError
        return False


def ensure_helper(timeout: float = 30.0) -> dict:
    """Фоновая программа, которая отвечает; при необходимости запускается."""
    info = read_instance()
    if ping(info):
        return info
    spawn_detached(launch_args(SERVE_FLAG))
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.25)
        info = read_instance()
        if ping(info):
            return info
    raise RuntimeError("Фоновая программа не запустилась. Перезапустите Anonymizer.")


# -- удалённый «App»: то, что нужно мосту окна, но через HTTP -------------------------------

class _RemoteJobs:
    def __init__(self, client: HelperClient):
        self._client = client

    def get(self, job_id: str):
        try:
            job = self._client.json("GET", f"/api/jobs/{urllib.parse.quote(str(job_id))}")
        except HelperError:
            return None
        folder = (job.get("result") or {}).get("folder")
        return SimpleNamespace(files=job.get("files", []), kind=job.get("kind"),
                               folder=SimpleNamespace(dest=folder["out"]) if folder else None)


class _RemoteService:
    def __init__(self, client: HelperClient, scratch: Path):
        self._client = client
        self._scratch = scratch
        self.jobs = _RemoteJobs(client)

    def output_path(self, job_id: str, index: int):
        """Готовый файл копируется во временную папку окна: сохраняет его уже окно, в место, выбранное человеком."""
        status, headers, data = self._client.request("GET", f"/api/download/{urllib.parse.quote(str(job_id))}/{int(index)}")
        if status != 200:
            return None
        disposition = next((v for k, v in headers.items() if k.lower() == "content-disposition"), "")
        name = urllib.parse.unquote(disposition.split("filename*=UTF-8''", 1)[1]) if "filename*=UTF-8''" in disposition else "result"
        folder = Path(tempfile.mkdtemp(dir=self._scratch))
        path = folder / name
        path.write_bytes(data)
        return path, name


class _RemoteVault:
    def __init__(self, client: HelperClient):
        self._client = client

    def export_backup(self, password: str) -> bytes:
        status, _, data = self._client.request("POST", "/api/backup/export", json.dumps({"password": password}).encode("utf-8"),
                                               {"Content-Type": "application/json"})
        if status != 200:
            raise HelperError(json.loads(data.decode("utf-8")).get("error", "Не удалось подготовить копию."))
        return data

    def import_backup(self, blob: bytes, password: str) -> dict:
        status, _, data = self._client.request("POST", "/api/backup/import", blob, {
            "X-Password": urllib.parse.quote(password), "Content-Type": "application/octet-stream"})
        parsed = json.loads(data.decode("utf-8")) if data else {}
        if status != 200:
            raise HelperError(parsed.get("error", "Не удалось загрузить копию."))
        return {k: v for k, v in parsed.items() if k not in ("version", "notice", "prefs", "stats", "history", "groups", "platform")}


class RemoteApp:
    """Те же методы, что у `App`, которыми пользуется мост окна, но через фоновую программу."""

    def __init__(self, client: HelperClient):
        self.client = client
        self.scratch = Path(tempfile.mkdtemp(prefix="anonymizer-window-"))
        self.service = _RemoteService(client, self.scratch)
        self.vault = _RemoteVault(client)

    def add_upload_path(self, path: str) -> dict:
        return self.client.json("POST", "/api/native/upload-path", {"path": path})

    def add_folder(self, path: str, kind: str) -> dict:
        return self.client.json("POST", "/api/native/folder", {"path": path, "kind": kind})["folder"]

    def state(self) -> dict:
        return self.client.json("GET", "/api/state")

    def cleanup(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)


# -- фоновая программа ----------------------------------------------------------------------

def should_exit(app: App, now: float, started: float) -> bool:
    """Пора ли завершаться: окон нет, фон не нужен add-in, и прошло время на запуск окна."""
    if app.quit_event.is_set():
        return True
    if app.keep_alive:
        return False
    if app.live_windows():
        return False
    return now - started > STARTUP_GRACE_SECONDS and (not app.windows or now - max(app.windows.values()) > app.WINDOW_ALIVE_SECONDS)


def run_helper(background: bool = False) -> int:
    lock = desktop.InstanceLock(crypto.app_data_dir() / "instance.lock")
    if not lock.acquire():
        return 0                                        # фоновая программа уже работает
    server, app = create_server(Vault.default())
    port = server.server_address[1]
    app.pinned_alive = background
    app.keep_alive = background or addin_install.is_installed()
    desktop.instance_file().write_text(json.dumps({"port": port, "token": app.token, "pid": os.getpid()}), "utf-8")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    addin = desktop.start_addin(app)
    if addin is not None:
        addin[1].tab_request = app.request_window
    app.spawn_window = lambda tab=None: spawn_detached(launch_args(*(["--tab", tab] if tab else [])))
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_: app.quit_event.set())
        except (ValueError, OSError):
            pass
    started = time.time()
    try:
        # Главный поток спит на событии: без окна и без опроса программа не тратит процессор.
        while not should_exit(app, time.time(), started):
            app.quit_event.wait(WATCHDOG_SECONDS)
    finally:
        server.shutdown()
        if addin is not None:
            addin[0].shutdown()
            addin[0].server_close()
        app.vault.commit()
        shutil.rmtree(app.service.workdir, ignore_errors=True)
        try:
            desktop.instance_file().unlink()
        except OSError:
            pass
        lock.release()
    return 0


# -- окно ---------------------------------------------------------------------------------------

def run_window(tab: str | None = None) -> int:
    if not desktop.webview2_installed():
        desktop.fatal(desktop.WEBVIEW2_MESSAGE)
        return 2
    try:
        import webview
    except Exception as exc:
        desktop.fatal(f"Не удалось загрузить компонент окна ({type(exc).__name__}). Переустановите программу.")
        return 3
    try:
        info = ensure_helper()
    except RuntimeError as exc:
        desktop.fatal(str(exc))
        return 5
    client = HelperClient(info)
    remote = RemoteApp(client)
    window_id = uuid.uuid4().hex
    holder: dict = {}
    state = {"closing": False}

    def quit_all() -> None:
        try:
            client.json("POST", "/api/quit", {}, timeout=5)
        except Exception:
            pass
        state["closing"] = True
        try:
            holder["window"].destroy()
        except Exception:
            pass
    bridge = desktop.Bridge(remote, lambda: holder["window"], quit_all)
    url = f"http://127.0.0.1:{info['port']}/?t={info['token']}"
    window = webview.create_window(
        desktop.TITLE, url, js_api=bridge, width=920, height=780, min_size=(520, 520),
        background_color="#1f1e1c" if desktop.system_is_dark() else "#faf9f5", text_select=True)
    holder["window"] = window
    webview.settings["ALLOW_DOWNLOADS"] = False
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False

    def listen() -> None:
        """Фоновая программа будит окно событием («показать», «открыть вкладку»); в тишине запрос просто ждёт."""
        try:
            client.json("POST", "/api/window/open", {"id": window_id})
        except Exception:
            return
        while not state["closing"]:
            try:
                event = client.json("GET", f"/api/window/wait?id={window_id}", timeout=40)
            except Exception:
                time.sleep(3)
                if not ping(read_instance()):
                    return                      # фоновая программа завершилась: окну тоже пора
                continue
            if event.get("focus"):
                desktop.bring_to_front(window)
                if event.get("tab") in ("anon", "rest"):
                    try:
                        window.evaluate_js(f"showTab({json.dumps(event['tab'])})")
                    except Exception:
                        pass
    threading.Thread(target=listen, daemon=True).start()
    if tab in ("anon", "rest"):
        window.events.loaded += lambda: window.evaluate_js(f"showTab({json.dumps(tab)})")
    code = 0
    try:
        webview.start(gui="edgechromium" if sys.platform == "win32" else None, private_mode=True)
    except Exception as exc:
        desktop.fatal(f"Окно не удалось открыть ({type(exc).__name__}). Проверьте, что установлен WebView2 Runtime.")
        code = 4
    finally:
        state["closing"] = True
        try:
            client.json("POST", "/api/window/closed", {"id": window_id}, timeout=5)
        except Exception:
            pass
        remote.cleanup()
    return code


def main_helper_mode(argv: list[str]) -> int | None:
    """Разбор запуска: None, если это не режим фоновой программы или окна."""
    if SERVE_FLAG in argv or BACKGROUND_FLAG in argv:
        return run_helper(background=BACKGROUND_FLAG in argv)
    tab = argv[argv.index("--tab") + 1] if "--tab" in argv and argv.index("--tab") + 1 < len(argv) else None
    return run_window(tab)
