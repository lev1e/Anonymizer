"""Подключение add-in к Office: сертификат, manifest, регистрация и автозапуск. Без прав администратора.

Windows: manifest регистрируется в `HKCU\\Software\\Microsoft\\Office\\16.0\\WEF\\Developer`, Office подхватывает его при запуске.
macOS: manifest копируется в папку `wef` каждого приложения Office (Word, Excel, PowerPoint).
"""

from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path

from . import __version__, certs
from .addin import ADDIN_DIR, ADDIN_PORT

ADDIN_ID = "6f1c2f3a-8d0e-4b51-9c2e-1a7b5d3e4f60"
HOSTS = {"Word": "Document", "Excel": "Workbook", "Powerpoint": "Presentation"}
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WEF_KEY = r"Software\Microsoft\Office\16.0\WEF\Developer"
AGENT_LABEL = "com.anonymizer.helper"


def base_url(port: int = ADDIN_PORT) -> str:
    return f"https://localhost:{port}"


SHARED_RUNTIME = """  <Requirements>
    <Sets DefaultMinVersion="1.1">
      <Set Name="SharedRuntime" MinVersion="1.1"/>
    </Sets>
  </Requirements>
"""


def render_manifest(port: int = ADDIN_PORT, shared: bool = True) -> str:
    block = (ADDIN_DIR / "host_block.xml.tmpl").read_text("utf-8")
    blocks = "\n".join(block.replace("{HOST_TYPE}", host) for host in HOSTS.values())
    text = (ADDIN_DIR / "manifest.xml.tmpl").read_text("utf-8")
    for key, value in (("{HOST_BLOCKS}", blocks), ("{REQUIREMENTS}", SHARED_RUNTIME if shared else ""), ("{BASE}", base_url(port)), ("{ADDIN_ID}", ADDIN_ID), ("{VERSION}", __version__)):
        text = text.replace(key, value)
    return text


def manifest_path() -> Path:
    return certs.addin_dir() / "Anonymizer.manifest.xml"


def mac_manifest_targets() -> list[Path]:
    root = Path.home() / "Library" / "Containers"
    return [root / f"com.microsoft.{app}" / "Data" / "Documents" / "wef" / f"{ADDIN_ID}.manifest.xml" for app in HOSTS]


def launch_command(dev: bool = False) -> list[str] | None:
    """Команда запуска программы в фоне. Из исходников автозапуск только по явной просьбе (`--dev-autostart`)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--background"]
    if dev:
        return [sys.executable, str(Path(__file__).resolve().parent.parent / "main.py"), "--serve", "--background"]
    return None


def is_installed() -> bool:
    try:
        if sys.platform == "win32":
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WEF_KEY) as key:
                winreg.QueryValueEx(key, ADDIN_ID)
                return manifest_path().exists()
        if sys.platform == "darwin":
            return any(p.exists() for p in mac_manifest_targets())
    except OSError:
        return False
    return False


def status() -> dict:
    files = certs.ensure_certs()
    return {"installed": is_installed(), "trusted": certs.is_trusted(files), "port": ADDIN_PORT,
            "autostart": autostart_enabled(), "supported": sys.platform in ("win32", "darwin")}


# -- автозапуск ----------------------------------------------------------------------------

def _agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"


def autostart_enabled() -> bool:
    try:
        if sys.platform == "win32":
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.QueryValueEx(key, "Anonymizer")
                return True
        if sys.platform == "darwin":
            return _agent_path().exists()
    except OSError:
        return False
    return False


def set_autostart(enabled: bool, dev: bool = False) -> tuple[bool, str]:
    command = launch_command(dev)
    try:
        if sys.platform == "win32":
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                if enabled and command:
                    winreg.SetValueEx(key, "Anonymizer", 0, winreg.REG_SZ, " ".join(f'"{c}"' for c in command))
                else:
                    try:
                        winreg.DeleteValue(key, "Anonymizer")
                    except OSError:
                        pass
        elif sys.platform == "darwin":
            path = _agent_path()
            if enabled and command:
                path.parent.mkdir(parents=True, exist_ok=True)
                # KeepAlive только при аварийном выходе: «Выйти из программы» завершает её с кодом 0 и не перезапускается.
                path.write_bytes(plistlib.dumps({"Label": AGENT_LABEL, "ProgramArguments": command, "RunAtLoad": True,
                                                 "KeepAlive": {"SuccessfulExit": False}, "ProcessType": "Background"}))
            elif path.exists():
                path.unlink()
        else:
            return False, "Автозапуск на этой системе не настраивается."
    except OSError as exc:
        return False, f"Автозапуск не настроен ({type(exc).__name__})."
    if enabled and not command:
        return False, "Автозапуск доступен у установленной программы; из исходников её нужно запускать вручную."
    return True, "Автозапуск включён." if enabled else "Автозапуск выключен."


# -- установка ----------------------------------------------------------------------------

def install(trust: bool = True, autostart: bool = True, files: certs.CertFiles | None = None, dev_autostart: bool = False) -> list[dict]:
    """Шаги подключения. Каждый шаг сообщает результат; сбой одного не отменяет остальные."""
    steps: list[dict] = []

    def step(name: str, ok: bool, message: str) -> None:
        steps.append({"name": name, "ok": ok, "message": message})

    try:
        files = files or certs.ensure_certs()
        step("certificate", True, "Сертификат для локальной страницы готов.")
    except Exception as exc:
        step("certificate", False, f"Не удалось создать сертификат ({type(exc).__name__}).")
        return steps
    if trust:
        if certs.is_trusted(files):
            step("trust", True, "Сертификат уже в доверенных.")
        else:
            ok, message = certs.trust_ca(files)
            step("trust", ok, message)
    try:
        manifest_path().write_text(render_manifest(), "utf-8")
        if sys.platform == "win32":
            import winreg
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, WEF_KEY) as key:
                winreg.SetValueEx(key, ADDIN_ID, 0, winreg.REG_SZ, str(manifest_path()))
            step("register", True, "Add-in зарегистрирован для Word, Excel и PowerPoint. Перезапустите Office.")
        elif sys.platform == "darwin":
            for target in mac_manifest_targets():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(render_manifest(), "utf-8")
            step("register", True, "Add-in добавлен для Word, Excel и PowerPoint. Перезапустите Office.")
        else:
            step("register", False, "Эта система не поддерживается: add-in работает в Windows и macOS.")
    except OSError as exc:
        step("register", False, f"Не удалось зарегистрировать add-in ({type(exc).__name__}).")
    if autostart:
        ok, message = set_autostart(True, dev_autostart)
        step("autostart", ok, message)
    return steps


def uninstall(untrust: bool = True) -> list[dict]:
    steps: list[dict] = []
    try:
        if sys.platform == "win32":
            import winreg
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WEF_KEY, 0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, ADDIN_ID)
            except OSError:
                pass
        elif sys.platform == "darwin":
            for target in mac_manifest_targets():
                if target.exists():
                    target.unlink()
        steps.append({"name": "register", "ok": True, "message": "Add-in удалён из Office."})
    except OSError as exc:
        steps.append({"name": "register", "ok": False, "message": f"Не удалось удалить add-in ({type(exc).__name__})."})
    ok, message = set_autostart(False)
    steps.append({"name": "autostart", "ok": ok, "message": message})
    if untrust:
        try:
            certs.untrust_ca(certs.ensure_certs())
            steps.append({"name": "trust", "ok": True, "message": "Сертификат убран из доверенных."})
        except Exception:
            steps.append({"name": "trust", "ok": False, "message": "Сертификат не удалось убрать из доверенных."})
    try:
        manifest_path().unlink()
    except OSError:
        pass
    return steps


def describe(steps: list[dict]) -> str:
    return "\n".join(("[ok] " if s["ok"] else "[!!] ") + s["message"] for s in steps)


def main_cli(argv: list[str]) -> int:
    if "--uninstall-addin" in argv:
        steps = uninstall()
    else:
        steps = install(trust="--no-trust" not in argv, autostart="--no-autostart" not in argv, dev_autostart="--dev-autostart" in argv)
    out = describe(steps)
    if sys.stdout:
        print(out, flush=True)
    return 0 if all(s["ok"] or s["name"] == "autostart" for s in steps) else 1
