"""Окно программы: страница в нативном окне (pywebview), нативные диалоги файлов, один экземпляр.

Ядро и страница те же, что раньше, но браузер не открывается: страницу показывает встроенный компонент
системы (WebView2 на Windows). Открытие и сохранение файлов идут через нативные диалоги, поэтому
результат не проходит через «загрузки» браузера. Закрытие окна завершает процесс.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

from . import addin_install, crypto
from .addin import ADDIN_DIR, create_addin_server
from .vault import Vault
from .webapp import App, create_server

TITLE = "Anonymizer"
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
WEBVIEW2_MESSAGE = (
    "Для работы окна нужен компонент Microsoft Edge WebView2 Runtime, он не найден.\n\n"
    "Установите его: " + WEBVIEW2_URL + "\n"
    "или обновите Windows и запустите программу снова."
)
OPEN_TYPES = ("Документы (*.xlsx;*.docx;*.pptx;*.pdf;*.txt;*.csv;*.md;*.json;*.xml;*.html;*.log;*.yaml)", "Все файлы (*.*)")


# -- проверки среды --------------------------------------------------------------

def webview2_installed() -> bool:
    """Есть ли на Windows среда WebView2 (Evergreen). На других системах проверка не нужна."""
    if sys.platform != "win32":
        return True
    import winreg
    client = r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for hive, path in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node" + client[len("SOFTWARE"):]),
                       (winreg.HKEY_LOCAL_MACHINE, client), (winreg.HKEY_CURRENT_USER, client)):
        try:
            with winreg.OpenKey(hive, path) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
                if version and version != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def fatal(message: str) -> None:
    """Сообщение пользователю до появления окна: у собранной программы нет консоли."""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, TITLE, 0x10)
    elif sys.stderr:
        print(message, file=sys.stderr)


def system_is_dark() -> bool:
    try:
        if sys.platform == "win32":
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
        if sys.platform == "darwin":
            out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True, timeout=2)
            return out.stdout.strip() == "Dark"
    except Exception:
        pass
    return False


# -- один экземпляр --------------------------------------------------------------

class InstanceLock:
    """Блокировка файла на всё время работы. Её снимает система, даже если программа упала."""

    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def acquire(self) -> bool:
        handle = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def instance_file() -> Path:
    return crypto.app_data_dir() / "instance.json"


def notify_existing(timeout: float = 5.0) -> bool:
    """Первый экземпляр выводит своё окно вперёд. Возвращает, ответил ли он."""
    try:
        info = json.loads(instance_file().read_text("utf-8"))
        connection = http.client.HTTPConnection("127.0.0.1", int(info["port"]), timeout=timeout)
        connection.request("POST", "/api/focus", b"{}", headers={
            "Cookie": f"anon_t={info['token']}", "X-Requested-With": "anonymizer", "Content-Type": "application/json"})
        ok = connection.getresponse().status == 200
        connection.close()
        return ok
    except Exception:
        return False


# -- мост между страницей и системой ---------------------------------------------

class Bridge:
    """Методы, которые страница вызывает через pywebview. Все возвращают словарь и не бросают исключений."""

    def __init__(self, app: App, window: Callable[[], object], quit_app: Callable[[], None] | None = None):
        self._app = app
        self._window = window
        self._quit = quit_app
        self._last_dir = ""
        self._saved: set[str] = set()
        self._pending_backup: bytes | None = None

    # -- диалоги -------------------------------------------------------------

    def _dialog(self, kind: str, **kwargs):
        import webview
        dialog = getattr(webview.FileDialog, kind)
        result = self._window().create_file_dialog(dialog, directory=self._last_dir, **kwargs)
        if not result:
            return []
        paths = [result] if isinstance(result, str) else list(result)
        if paths:
            self._last_dir = str(Path(paths[0]).parent if kind != "FOLDER" else Path(paths[0]))
        return paths

    # -- открыть -------------------------------------------------------------

    def pick_files(self) -> dict:
        try:
            paths = self._dialog("OPEN", allow_multiple=True, file_types=OPEN_TYPES)
        except Exception as exc:
            return {"error": f"Не удалось открыть окно выбора файла ({type(exc).__name__}). Перетащите файл в окно."}
        files = []
        for path in paths:
            try:
                info = self._app.add_upload_path(path)
                files.append({"name": Path(path).name, "size": info.get("size", 0), "info": info})
            except (OSError, ValueError) as exc:
                files.append({"name": Path(path).name, "error": str(exc) if isinstance(exc, ValueError)
                              else "Файл не удалось прочитать. Проверьте, что он не открыт в другой программе."})
        return {"files": files}

    def quit_app(self) -> dict:
        """Полностью закрыть программу (окно по крестику только прячется, пока подключён add-in)."""
        if self._quit is None:
            return {"error": "Закрытие недоступно."}
        threading.Thread(target=self._quit, daemon=True).start()
        return {"ok": True}

    def pick_folder(self, kind: str = "anonymize") -> dict:
        """Целая папка: обход идёт здесь, на странице остаётся только её описание и номер."""
        try:
            paths = self._dialog("FOLDER")
        except Exception as exc:
            return {"error": f"Не удалось открыть окно выбора папки ({type(exc).__name__}). Повторите."}
        if not paths:
            return {"cancelled": True}
        try:
            return {"folder": self._app.add_folder(paths[0], "restore" if kind == "restore" else "anonymize")}
        except (OSError, ValueError) as exc:
            return {"error": str(exc) if isinstance(exc, ValueError) else "Папку не удалось прочитать."}

    def reveal_folder(self, job_id: str) -> dict:
        """Показать зеркальную папку, которую программа создала сама."""
        job = self._app.service.jobs.get(job_id)
        context = getattr(job, "folder", None)
        dest = getattr(context, "dest", None)
        if dest is None or not Path(dest).is_dir():
            return {"error": "Папка не найдена. Возможно, её переместили."}
        try:
            if sys.platform == "win32":
                os.startfile(str(dest))                      # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(dest)])
            else:
                subprocess.Popen(["xdg-open", str(dest)])
        except OSError:
            return {"error": "Не удалось открыть папку."}
        return {"ok": True}

    # -- сохранить -----------------------------------------------------------

    def save_result(self, job_id: str, index: int) -> dict:
        found = self._app.service.output_path(job_id, int(index))
        if found is None:
            return {"error": "Файл больше не доступен. Обработайте его заново."}
        source, name = found
        suffix = Path(name).suffix
        types = (f"Файл (*{suffix})",) if suffix else ()
        try:
            paths = self._dialog("SAVE", save_filename=name, file_types=types)
        except Exception as exc:
            return {"error": f"Не удалось открыть окно сохранения ({type(exc).__name__}). Повторите."}
        if not paths:
            return {"cancelled": True}
        return self._copy(source, Path(paths[0]))

    def save_all(self, job_id: str) -> dict:
        job = self._app.service.jobs.get(job_id)
        if job is None:
            return {"error": "Файлы больше не доступны. Обработайте их заново."}
        try:
            folders = self._dialog("FOLDER")
        except Exception as exc:
            return {"error": f"Не удалось открыть окно выбора папки ({type(exc).__name__}). Повторите."}
        if not folders:
            return {"cancelled": True}
        saved = {}
        for index in range(len(job.files)):
            found = self._app.service.output_path(job_id, index)
            if found is None:
                continue
            source, name = found
            target = self._free_name(Path(folders[0]) / name)
            result = self._copy(source, target)
            if "error" in result:
                return result
            saved[index] = result["path"]
        return {"saved": saved, "folder": folders[0]}

    def save_text(self, name: str, text: str, encoding: str = "utf-8-sig") -> dict:
        try:
            paths = self._dialog("SAVE", save_filename=str(name), file_types=())
        except Exception as exc:
            return {"error": f"Не удалось открыть окно сохранения ({type(exc).__name__}). Повторите."}
        if not paths:
            return {"cancelled": True}
        try:
            Path(paths[0]).write_text(str(text), encoding=encoding, newline="")
        except OSError:
            return {"error": "Не удалось записать файл. Выберите другую папку."}
        self._saved.add(str(Path(paths[0])))
        return {"path": paths[0]}

    @staticmethod
    def _free_name(target: Path) -> Path:
        counter = 2
        candidate = target
        while candidate.exists():
            candidate = target.with_name(f"{target.stem} ({counter}){target.suffix}")
            counter += 1
        return candidate

    def _copy(self, source: Path, target: Path) -> dict:
        try:
            shutil.copyfile(source, target)
        except OSError:
            return {"error": "Не удалось сохранить файл. Выберите другую папку или закройте файл, если он открыт."}
        self._saved.add(str(target))
        return {"path": str(target)}

    def reveal(self, path: str) -> dict:
        """Показать сохранённый файл в проводнике. Только файлы, которые записала сама программа."""
        if str(path) not in self._saved or not Path(path).exists():
            return {"error": "Файл не найден. Возможно, его переместили."}
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", f"/select,{path}"])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", str(Path(path).parent)])
        except OSError:
            return {"error": "Не удалось открыть папку."}
        return {"ok": True}

    # -- резервная копия -----------------------------------------------------

    def export_backup(self, password: str) -> dict:
        if len(password or "") < 6:
            return {"error": "Пароль должен быть не короче 6 знаков."}
        try:
            paths = self._dialog("SAVE", save_filename="anonymizer-backup.anonkeys", file_types=("Копия (*.anonkeys)",))
        except Exception as exc:
            return {"error": f"Не удалось открыть окно сохранения ({type(exc).__name__}). Повторите."}
        if not paths:
            return {"cancelled": True}
        try:
            Path(paths[0]).write_bytes(self._app.vault.export_backup(password))
        except ValueError as exc:                    # фоновая программа не смогла подготовить копию
            return {"error": str(exc)}
        except OSError:
            return {"error": "Не удалось записать файл. Выберите другую папку."}
        self._saved.add(str(Path(paths[0])))
        return {"path": paths[0]}

    def pick_backup(self) -> dict:
        try:
            paths = self._dialog("OPEN", allow_multiple=False, file_types=("Копия (*.anonkeys)", "Все файлы (*.*)"))
        except Exception as exc:
            return {"error": f"Не удалось открыть окно выбора файла ({type(exc).__name__}). Повторите."}
        if not paths:
            return {"cancelled": True}
        try:
            if Path(paths[0]).stat().st_size > 64 * 1024 * 1024:
                return {"error": "Это не резервная копия: файл слишком большой."}
            self._pending_backup = Path(paths[0]).read_bytes()
        except OSError:
            return {"error": "Файл не удалось прочитать."}
        return {"name": Path(paths[0]).name}

    def import_backup(self, password: str) -> dict:
        blob, self._pending_backup = self._pending_backup, None
        if blob is None:
            return {"error": "Сначала выберите файл копии."}
        try:
            info = self._app.vault.import_backup(blob, password or "")
        except (ValueError, crypto.KeyErrorSafe) as exc:
            return {"error": str(exc)}
        return {"info": info, "state": self._app.state()}


# -- запуск ----------------------------------------------------------------------

def bring_to_front(window) -> None:
    try:
        window.restore()
        window.show()
        window.on_top = True
        window.on_top = False
    except Exception:
        pass


def start_addin(app: App):
    """HTTPS-сервер add-in. Не получилось (порт занят, нет сертификата) — программа работает без него."""
    try:
        server, api = create_addin_server(app)
    except OSError:
        app.addin_error = "Порт 47831 занят другой программой: панель в Office не сможет подключиться. Закройте её и перезапустите Anonymizer."
        return None
    except Exception as exc:
        app.addin_error = f"Сервер add-in не запустился ({type(exc).__name__}). Переустановите программу."
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, api


def run(background: bool = False) -> int:
    if not webview2_installed():
        fatal(WEBVIEW2_MESSAGE)
        return 2
    lock = InstanceLock(crypto.app_data_dir() / "instance.lock")
    if not lock.acquire():
        notify_existing()
        return 0
    try:
        import webview
    except Exception as exc:
        fatal(f"Не удалось загрузить компонент окна ({type(exc).__name__}). Переустановите программу.")
        lock.release()
        return 3
    server, app = create_server(Vault.default())
    port = server.server_address[1]
    instance_file().write_text(json.dumps({"port": port, "token": app.token, "pid": os.getpid()}), "utf-8")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    addin = start_addin(app)
    # Окно по крестику прячется, а не закрывается, пока add-in подключён: иначе панель в Office потеряла бы связь.
    keep_alive = background or (addin is not None and addin_install.is_installed())
    state = {"quitting": False}
    holder: dict = {}

    def quit_now() -> None:
        state["quitting"] = True
        try:
            holder["window"].destroy()
        except Exception:
            pass
    bridge = Bridge(app, lambda: holder["window"], quit_now)
    webview.settings["ALLOW_DOWNLOADS"] = False
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
    window = webview.create_window(
        TITLE, f"http://127.0.0.1:{port}/?t={app.token}", js_api=bridge, width=920, height=780, min_size=(520, 520),
        background_color="#1f1e1c" if system_is_dark() else "#faf9f5", text_select=True, hidden=background)
    holder["window"] = window
    app.on_focus = lambda: bring_to_front(window)
    if addin is not None:
        def show_tab(tab: str) -> None:
            bring_to_front(window)
            try:
                window.evaluate_js(f"showTab({json.dumps(tab)})")
            except Exception:
                pass
        addin[1].tab_request = show_tab

    def on_closing():
        if keep_alive and not state["quitting"]:
            window.hide()
            return False
        return None
    window.events.closing += on_closing
    code = 0
    try:
        webview.start(gui="edgechromium" if sys.platform == "win32" else None, private_mode=True)
    except Exception as exc:
        fatal(f"Окно не удалось открыть ({type(exc).__name__}). Проверьте, что установлен WebView2 Runtime.")
        code = 4
    finally:
        server.shutdown()
        if addin is not None:
            addin[0].shutdown()
            addin[0].server_close()
        app.vault.commit()
        shutil.rmtree(app.service.workdir, ignore_errors=True)
        try:
            instance_file().unlink()
        except OSError:
            pass
        lock.release()
    return code


def selfcheck(report: Path) -> int:
    """Проверка собранной программы без окна: ресурсы, словарь, сервер, компонент окна. Итог пишется в файл, потому что у
    собранной программы нет консоли. Код возврата 0, если всё на месте."""
    info: dict = {"frozen": bool(getattr(sys, "frozen", False)), "python": sys.version.split()[0], "platform": sys.platform}
    checks: dict[str, bool] = {}

    def check(name: str, fn) -> None:
        try:
            checks[name] = bool(fn())
        except Exception as exc:
            checks[name] = False
            info[f"{name}_error"] = f"{type(exc).__name__}: {exc}"[:200]

    from . import lexicon
    from .webapp import WEB_DIR

    check("page_resource", lambda: (WEB_DIR / "index.html").stat().st_size > 1000)
    check("morphology_dictionary", lexicon.available)

    def window_component():
        import webview
        from importlib import metadata
        info["pywebview"] = metadata.version("pywebview")
        if sys.platform == "win32":
            import clr  # noqa: F401  (pythonnet: без него окно не откроется)
            return webview2_installed()
        return True
    check("window_component", window_component)

    def server_answers():
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            server, app = create_server(Vault(Path(tmp) / "vault.bin"), Path(tmp) / "work")
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
                connection.request("GET", f"/?t={app.token}", headers={"Host": f"127.0.0.1:{server.server_address[1]}"})
                redirect = connection.getresponse()
                cookie = redirect.getheader("Set-Cookie", "").split(";")[0]
                redirect.read()
                connection.request("GET", "/", headers={"Host": f"127.0.0.1:{server.server_address[1]}", "Cookie": cookie})
                page = connection.getresponse()
                return page.status == 200 and "Anonymizer".encode("ascii") in page.read()
            finally:
                server.shutdown()
                server.server_close()
    check("server_and_page", server_answers)

    def addin_page():
        import ssl
        import tempfile
        from . import certs
        with tempfile.TemporaryDirectory() as tmp:
            key = os.urandom(32)
            files = certs.ensure_certs(Path(tmp) / "addin", key)
            _, app = create_server(Vault(Path(tmp) / "vault.bin"), Path(tmp) / "work")
            server, _api = create_addin_server(app, port=0, files=files, directory=Path(tmp) / "addin", key=key)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                port = server.server_address[1]
                context = ssl.create_default_context(cafile=str(files.ca_cert))
                connection = http.client.HTTPSConnection("localhost", port, context=context, timeout=10)
                connection.request("GET", "/addin/taskpane.html", headers={"Host": f"localhost:{port}"})
                page = connection.getresponse()
                connection.close()
                return page.status == 200 and (ADDIN_DIR / "vendor" / "office.js").is_file()
            finally:
                server.shutdown()
                server.server_close()
    check("addin_https_page", addin_page)

    def helper_and_window_client():
        import tempfile
        from . import helper
        with tempfile.TemporaryDirectory() as tmp:
            server, app = create_server(Vault(Path(tmp) / "vault.bin"), Path(tmp) / "work")
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                info = {"port": server.server_address[1], "token": app.token}
                client = helper.HelperClient(info, timeout=10)
                client.json("POST", "/api/window/open", {"id": "check"})
                remote = helper.RemoteApp(client)
                try:
                    return helper.ping(info) and client.json("GET", "/api/ping")["windows"] == 1 and isinstance(remote.state(), dict)
                finally:
                    remote.cleanup()
            finally:
                server.shutdown()
                server.server_close()
    check("helper_and_window_client", helper_and_window_client)

    def full_circle():
        import tempfile
        from .service import Service, Upload
        with tempfile.TemporaryDirectory() as tmp:
            service = Service(Vault(Path(tmp) / "vault.bin"), Path(tmp) / "work")
            source = "Директор Смирнов Алексей Петрович, ООО «Ромашка», г. Новосибирск.".encode("utf-8")
            job = service.anonymize([Upload("проверка.txt", source)])
            hidden = Path(job.files[0].out_path).read_bytes()
            back = service.restore([Upload("проверка.txt", hidden)])
            return "Смирнов".encode("utf-8") not in hidden and Path(back.files[0].out_path).read_bytes() == source
    check("anonymize_and_restore", full_circle)

    info["checks"] = checks
    info["ok"] = all(checks.values())
    report.write_text(json.dumps(info, ensure_ascii=False, indent=1), "utf-8")
    return 0 if info["ok"] else 1


def main() -> None:
    # У собранной программы без консоли нет стандартных потоков: библиотека, которая пишет в них, упала бы с AttributeError.
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    if len(sys.argv) >= 3 and sys.argv[1] == "--selfcheck":
        os._exit(selfcheck(Path(sys.argv[2])))
    if any(a in sys.argv[1:] for a in ("--install-addin", "--uninstall-addin")):
        os._exit(addin_install.main_cli(sys.argv[1:]))
    try:
        if "--inprocess" in sys.argv[1:]:       # старый режим: окно и серверы в одном процессе (для разработки и проверок)
            code = run(background="--background" in sys.argv[1:])
        else:
            from . import helper
            code = helper.main_helper_mode(sys.argv[1:])
    except Exception as exc:
        fatal(f"Программа не запустилась ({type(exc).__name__}: {exc}).")
        code = 1
    # Задания идут в фоновых потоках: после закрытия окна процесс должен завершиться сразу.
    os._exit(code)
