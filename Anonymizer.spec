# -*- mode: python ; coding: utf-8 -*-
import sys

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

hiddenimports = (collect_submodules("fitz") + collect_submodules("cryptography")
                 + collect_submodules("pymorphy3") + ["dawg2_python"])

# Словарь OpenCorpora — это файлы данных внутри пакета, а не модули. Без явного сбора
# PyInstaller их не положит, и в собранном .exe разбор молча откатится на суффиксные
# правила: программа запустится, но косвенные падежи перестанут распознаваться.
datas = collect_data_files("pymorphy3_dicts_ru") + [("anonymizer/web", "anonymizer/web")]
binaries = []

# Окно программы: pywebview показывает страницу через WebView2. Его библиотеки (Microsoft.Web.WebView2.*.dll,
# WebBrowserInterop) и скрипты лежат внутри пакета; на Windows нужны ещё pythonnet и clr_loader (мост к .NET).
for package in ("webview",) + (("pythonnet", "clr_loader") if sys.platform == "win32" else ()):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden
if sys.platform == "win32":
    hiddenimports += ["clr"]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Другие оболочки окна pywebview не нужны: на Windows работает только WebView2, а лишние Qt и GTK добавили бы сотни мегабайт.
    excludes=["pytest", "numpy", "pandas", "tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy", "gi", "cefpython3"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Anonymizer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    manifest="packaging/app.manifest",
    version="packaging/version_info.txt",
)
