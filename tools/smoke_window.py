"""Запуск настоящего `desktop.run()`: окно открывается, вторая копия выводит его вперёд, закрытие завершает работу.

Окно закрывается программно через несколько секунд. Проверяется то, что не покрывает `e2e_window.py`: блокировка одного
экземпляра, файл `instance.json`, вывод окна вперёд, очистка при закрытии.
"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
home = Path(tempfile.mkdtemp(prefix="anon-smoke-"))
os.environ["ANONYMIZER_HOME"] = str(home)

import webview  # noqa: E402

from anonymizer import crypto, desktop  # noqa: E402

results: list[tuple[str, bool]] = []
original_start = webview.start
fronted = threading.Event()
original_front = desktop.bring_to_front
desktop.bring_to_front = lambda window: (fronted.set(), original_front(window))[1]


def scenario():
    time.sleep(4)
    lock = desktop.InstanceLock(home / "instance.lock")
    results.append(("вторая копия не получает блокировку", not lock.acquire()))
    results.append(("файл instance.json записан", (home / "instance.json").exists()))
    results.append(("вторая копия выводит окно вперёд", desktop.notify_existing() and fronted.wait(5)))
    window = webview.windows[0]
    results.append(("страница загружена в окно", bool(window.get_current_url())))
    window.destroy()


def start(func=None, *args, **kwargs):
    return original_start(scenario, *args, **kwargs)


webview.start = start
code = desktop.run()
results.append(("run() вернул 0 после закрытия окна", code == 0))
results.append(("instance.json удалён", not (home / "instance.json").exists()))
lock = desktop.InstanceLock(home / "instance.lock")
results.append(("блокировка снята", lock.acquire()))
lock.release()
results.append(("рабочая папка удалена", not (home / "work").exists()))
for name, ok in results:
    print(("OK   " if ok else "FAIL ") + name)
raise SystemExit(0 if all(ok for _, ok in results) else 1)
