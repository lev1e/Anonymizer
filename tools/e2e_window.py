"""Сквозная проверка в настоящем окне программы (pywebview).

Окно открывается так же, как в `desktop.run`, страницу нажимают через JavaScript, файлы выбираются и
сохраняются через тот же мост, что использует пользователь. Отличие одно: системные окна «Открыть» и
«Сохранить как» подменяются заранее заданными путями, потому что автоматика не может нажимать в них.
Сами системные окна этой проверкой не покрываются.

Запуск: .venv/bin/python tools/e2e_window.py [--shots ПАПКА] [--theme light|dark]
Каждый файл из case/ проходит путь: выбрать → Обезличить → Сохранить как → выбрать сохранённый →
Восстановить → Сохранить как. Результат сверяется с исходником по ячейкам, абзацам и фигурам.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import webview  # noqa: E402

from anonymizer import desktop  # noqa: E402
from anonymizer.vault import Vault  # noqa: E402
from anonymizer.webapp import create_server  # noqa: E402
from tests.helpers import content_units  # noqa: E402

CASE = ROOT / "case"


class Driver:
    def __init__(self, shots: Path | None, dark: bool):
        self.shots = shots
        self.dark = dark
        self.home = Path(tempfile.mkdtemp(prefix="anon-e2e-"))
        os.environ["ANONYMIZER_HOME"] = str(self.home)
        self.out = self.home / "saved"
        self.out.mkdir()
        self.queue: list[list[str]] = []
        self.dialog_log: list[tuple] = []
        self.results: list[tuple[str, bool, str]] = []
        self.server, self.app = create_server(Vault(self.home / "vault.bin"), self.home / "work")
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.holder: dict = {}
        self.bridge = desktop.Bridge(self.app, lambda: self.holder["window"])
        self.bridge._dialog = self._fake_dialog        # подмена только системных окон

    def _fake_dialog(self, kind, **kwargs):
        self.dialog_log.append((kind, kwargs.get("save_filename", ""), kwargs.get("file_types", ())))
        if not self.queue:
            return []
        answer = self.queue.pop(0)
        return answer

    # -- помощники -----------------------------------------------------------

    def js(self, code: str):
        return self.holder["window"].evaluate_js(code)

    def wait_js(self, condition: str, timeout: float = 90):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self.js(f"Boolean({condition})"):
                    return True
            except Exception:
                pass
            time.sleep(.3)
        return False

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, bool(ok), detail))
        print(("OK   " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""), flush=True)

    def shot(self, name: str) -> None:
        """Снимок содержимого окна средствами WKWebView (macOS): разрешение на запись экрана не нужно."""
        if not self.shots:
            return
        self.shots.mkdir(parents=True, exist_ok=True)
        try:
            import AppKit
            from PyObjCTools import AppHelper
            from webview.platforms.cocoa import BrowserView
            view = BrowserView.instances[self.holder["window"].uid].webview
            done = threading.Event()

            def handler(image, error):
                try:
                    if image is not None:
                        rep = AppKit.NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
                        data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, None)
                        data.writeToFile_atomically_(str(self.shots / f"{name}.png"), True)
                finally:
                    done.set()

            AppHelper.callAfter(lambda: view.takeSnapshotWithConfiguration_completionHandler_(None, handler))
            done.wait(10)
        except Exception as exc:
            print("снимок не сделан:", repr(exc))

    def click(self, selector: str) -> None:
        self.js(f"document.querySelector({json.dumps(selector)}).click()")

    def text(self, selector: str) -> str:
        return self.js(f"(document.querySelector({json.dumps(selector)})||{{innerText:''}}).innerText") or ""

    # -- сценарий ------------------------------------------------------------

    def run(self) -> None:
        window = self.holder["window"]
        if not self.wait_js("document.readyState==='complete' && window.pywebview && window.pywebview.api && window.pywebview.api.pick_files"):
            self.check("окно загрузило страницу и мост", False)
            return self.finish()
        self.check("окно загрузило страницу и мост", True)
        self.check("заголовок окна", self.js("document.title") == "Anonymizer")
        self.shot("01-start")
        files = sorted(p for ext in ("xlsx", "docx", "pptx") for p in CASE.glob(f"*.{ext}"))
        for source in files:
            self.one_file(source)
        self.multi(files[:2])
        self.extras()
        self.drop_scenario()
        self.finish()

    def one_file(self, source: Path) -> None:
        label = source.name[:40]
        self.js("modes.anon.files.length = 0; modes.rest.files.length = 0; renderFiles('anon'); renderFiles('rest'); showTab('anon'); document.querySelector('#result-anon').innerHTML='';")
        self.queue = [[str(source)]]
        self.click("#pick-anon")
        ok = self.wait_js("modes.anon.files.length===1 && modes.anon.files[0].state==='ready'", 30)
        self.check(f"[{label}] выбор файла через мост", ok)
        if not ok:
            return
        self.click("#btn-anon")
        ok = self.wait_js("document.querySelector('#result-anon .status')", 240)
        status = self.text("#result-anon .status")
        self.check(f"[{label}] обезличивание завершено", ok and status.startswith("Готово") or status.startswith("Файл готов"), status)
        self.shot("02-result-" + re.sub(r"\W", "_", source.stem)[:20])
        target = self.out / f"anon_{source.name}"
        self.queue = [[str(target)]]
        self.click("[data-save='0']")
        ok = self.wait_js("document.querySelector('#saved-anon-0') && !document.querySelector('#saved-anon-0').hidden", 30)
        self.check(f"[{label}] сохранение через мост", ok and target.exists(), self.text("#saved-anon-0"))
        if not target.exists():
            return
        self.check(f"[{label}] после сохранения есть Показать в папке", "Показать в папке" in self.text("#saved-anon-0"))
        # Возврат: тот же файл, как будто его вернул ИИ.
        self.js("showTab('rest')")
        self.queue = [[str(target)]]
        self.click("#pick-rest")
        ok = self.wait_js("modes.rest.files.length===1 && modes.rest.files[0].state==='ready'", 30)
        self.check(f"[{label}] выбор обезличенного файла для возврата", ok and self.js("modes.rest.files[0].info.tokens") > 0)
        self.click("#btn-rest")
        ok = self.wait_js("document.querySelector('#result-rest .status')", 240)
        self.check(f"[{label}] возврат завершён", ok, self.text("#result-rest .status"))
        back = self.out / f"back_{source.name}"
        self.queue = [[str(back)]]
        self.click("#result-rest [data-save='0']")
        ok = self.wait_js("document.querySelector('#saved-rest-0') && !document.querySelector('#saved-rest-0').hidden", 30)
        self.check(f"[{label}] сохранение восстановленного файла", ok and back.exists())
        if back.exists():
            original, restored = content_units(source), content_units(back)
            diff = [k for k in original if original[k] != restored.get(k)]
            self.check(f"[{label}] после круга все {len(original)} единиц совпали с исходником", not diff and len(original) == len(restored), f"{len(diff)} отличий: {diff[:3]}")

    def multi(self, files: list[Path]) -> None:
        self.js("modes.anon.files.length = 0; renderFiles('anon'); showTab('anon'); document.querySelector('#result-anon').innerHTML='';")
        self.queue = [[str(p) for p in files]]
        self.click("#pick-anon")
        ok = self.wait_js("modes.anon.files.length===2 && modes.anon.files.every(f=>f.state==='ready')", 30)
        self.check("[несколько файлов] выбор двух файлов", ok)
        self.click("#btn-anon")
        ok = self.wait_js("document.querySelector('#result-anon .status')", 240)
        self.check("[несколько файлов] задание завершено", ok)
        folder = self.out / "many"
        folder.mkdir()
        self.queue = [[str(folder)]]
        self.click("#save-all")
        ok = self.wait_js("document.querySelector('#saved-anon-0') && !document.querySelector('#saved-anon-0').hidden", 30)
        self.check("[несколько файлов] сохранение в папку", ok and len(list(folder.iterdir())) == 2, str(list(folder.iterdir())))
        # Отмена диалога не должна ломать страницу.
        self.queue = [[]]
        self.click("[data-save='1']")
        time.sleep(1)
        self.check("[отмена диалога] страница жива", "Сохранить как" in self.text("#result-anon"))
        self.shot("03-multi")

    # -- дополнительные сценарии ---------------------------------------------

    def pick(self, mode: str, path: Path, expect_ready: bool = True) -> bool:
        self.js(f"modes.{mode}.files.length = 0; renderFiles('{mode}'); showTab('{mode}'); document.querySelector('#result-{mode}').innerHTML='';")
        self.queue = [[str(path)]]
        self.click(f"#pick-{mode}")
        return self.wait_js(f"modes.{mode}.files.length===1 && modes.{mode}.files[0].state==='ready'", 30)

    def run_and_save(self, mode: str, target: Path) -> str:
        self.click(f"#btn-{mode}")
        self.wait_js(f"document.querySelector('#result-{mode} .status')", 240)
        status = self.text(f"#result-{mode} .status")
        if target and self.js(f"Boolean(document.querySelector('#result-{mode} [data-save=\"0\"]'))"):
            self.queue = [[str(target)]]
            self.click(f"#result-{mode} [data-save='0']")
            self.wait_js(f"document.querySelector('#saved-{'anon' if mode == 'anon' else 'rest'}-0') && !document.querySelector('#saved-{'anon' if mode == 'anon' else 'rest'}-0').hidden", 30)
        return status

    def ask(self, password: str | None) -> None:
        """Ответ в собственном диалоге страницы (пароль или подтверждение)."""
        self.wait_js("document.querySelector('#ask').open", 10)
        if password is not None:
            self.js(f"document.querySelector('#ask-input').value = {json.dumps(password)}")
        self.click("#ask-ok")
        self.wait_js("!document.querySelector('#ask').open", 10)

    def drop_scenario(self) -> None:
        """Перетаскивание: файл, брошенный на окно (не только на рамку), добавляется, страница остаётся на месте."""
        self.js("modes.anon.files.length = 0; renderFiles('anon'); showTab('anon'); document.querySelector('#result-anon').innerHTML='';")
        before = self.js("location.href")
        self.js("""(() => {
          const dt = new DataTransfer();
          dt.items.add(new File(["Директор Смирнов Алексей Петрович подписал акт."], "перетащено.txt", {type: "text/plain"}));
          const ev = new DragEvent("drop", {dataTransfer: dt, bubbles: true, cancelable: true});
          window.__dropPrevented = !document.body.dispatchEvent(ev);
        })()""")
        ok = self.wait_js("modes.anon.files.length===1 && modes.anon.files[0].state==='ready'", 15)
        self.check("[перетаскивание] файл, брошенный на окно, добавлен в список", ok)
        self.check("[перетаскивание] страница не ушла на документ", self.js("location.href") == before and self.js("window.__dropPrevented") is True)
        self.click("#btn-anon")
        self.wait_js("document.querySelector('#result-anon .status')", 60)
        self.check("[перетаскивание] перетащенный файл обезличивается", self.text("#result-anon .status").startswith("Готово"), self.text("#result-anon .status"))

    def extras(self) -> None:
        from openpyxl import load_workbook
        source = CASE / "Данные Клиента.xlsx"
        # A. Числа Excel: включить, скачать, вернуть.
        self.js("document.querySelector('#opt-numbers').checked = true")
        self.pick("anon", source)
        self.js("document.querySelector('#opt-numbers').checked = true")
        anon_numbers = self.out / "numbers_anon.xlsx"
        self.run_and_save("anon", anon_numbers)
        cells = load_workbook(anon_numbers).worksheets[0]
        self.check("[числа Excel] числа заменены в обезличенном файле", cells["G14"].value != 1200 and isinstance(cells["G14"].value, (int, float)), str(cells["G14"].value))
        self.js("document.querySelector('#opt-numbers').checked = false")
        self.pick("rest", anon_numbers)
        back_numbers = self.out / "numbers_back.xlsx"
        self.run_and_save("rest", back_numbers)
        original = load_workbook(source).worksheets[0]
        restored = load_workbook(back_numbers).worksheets[0]
        same = all(original[c].value == restored[c].value for c in ("G14", "G15", "G16", "G17", "G18", "H18"))
        self.check("[числа Excel] числа вернулись как в исходнике", same, f"{[restored[c].value for c in ('G14','G15','G16')]}")

        # B. Уже обезличенный файл в режиме «Обезличить».
        plain = self.out / f"anon_{source.name}"
        self.pick("anon", plain)
        note = self.text("#files-anon")
        self.check("[уже обезличенный файл] страница предлагает восстановить", "Восстановить его" in note and "уже есть метки" in note, note[:120])

        # C. Чужой файл: обезличен другим хранилищем.
        from tests.helpers import Env
        other = Env()
        foreign_job = other.anonymize(source)
        foreign = self.out / "foreign.xlsx"
        shutil.copy(foreign_job.files[0].out_path, foreign)
        self.pick("rest", foreign)
        status = self.run_and_save("rest", None)
        messages = self.text("#result-rest .msgs")
        self.check("[чужой файл] возврат остановлен с понятной причиной", status.startswith("Файл не восстановлен") and "другим хранилищем" in messages, f"{status} | {messages[:120]}")
        other.close()

        # D. Резервная копия, очистка, загрузка копии.
        self.js("showTab('anon')")
        self.click("#open-settings")
        self.wait_js("document.querySelector('#settings').open", 10)
        backup = self.out / "backup.anonkeys"
        self.click("#btn-backup")
        self.wait_js("document.querySelector('#ask').open", 10)
        self.js("document.querySelector('#ask-input').value = 'abc'")   # слишком короткий: диалог сообщает и остаётся открытым
        self.click("#ask-ok")
        self.wait_js("!document.querySelector('#ask-error').hidden", 10)
        short_error = self.text("#ask-error")
        self.check("[резервная копия] короткий пароль отклонён строкой с причиной", "не короче 6" in short_error, short_error)
        self.queue = [[str(backup)]]
        self.ask("пароль-12345")
        self.wait_js("document.querySelector('.toast')", 15)
        self.check("[резервная копия] копия сохранена", backup.exists() and backup.stat().st_size > 100, self.text(".toast"))
        entities_before = self.app.vault.stats()["entities"]
        self.click("#btn-clear")
        self.ask(None)
        self.wait_js("document.querySelector('#vault-stats').innerText.includes('Сохранено значений: 0')", 10)
        self.check("[резервная копия] хранилище очищено", self.app.vault.stats()["entities"] == 0)
        self.js("document.querySelector('#settings').close()")
        self.pick("rest", plain)
        status = self.run_and_save("rest", None)
        self.check("[резервная копия] после очистки файл не восстанавливается", status.startswith("Файл не восстановлен") or status.startswith("Файл восстановлен не полностью"), self.text("#result-rest")[:200])
        self.click("#open-settings")
        self.wait_js("document.querySelector('#settings').open", 10)
        self.queue = [[str(backup)]]
        self.click("#btn-import")
        self.ask("пароль-12345")
        self.wait_js(f"document.querySelector('#vault-stats').innerText.includes('Сохранено значений: {entities_before}')", 15)
        self.check("[резервная копия] копия загружена, значения на месте", self.app.vault.stats()["entities"] == entities_before, str(self.app.vault.stats()))
        self.js("document.querySelector('#settings').close()")
        self.pick("rest", plain)
        back_after = self.out / "after_import.xlsx"
        status = self.run_and_save("rest", back_after)
        same = back_after.exists() and load_workbook(back_after).worksheets[0]["C6"].value == load_workbook(source).worksheets[0]["C6"].value
        self.check("[резервная копия] файл снова восстанавливается", same, status)
        self.shot("04-after-backup")

    def restart_check(self) -> None:
        """Перезапуск: новое хранилище читает тот же файл на диске и возвращает данные из файла, обезличенного до перезапуска."""
        from openpyxl import load_workbook
        from anonymizer.service import Service, Upload
        self.app.vault.commit()
        vault = Vault(self.home / "vault.bin")
        service = Service(vault, self.home / "work2")
        plain = self.out / "anon_Данные Клиента.xlsx"
        job = service.restore([Upload(plain.name, plain.read_bytes())])
        ok = job.files[0].status == "ok" and load_workbook(job.files[0].out_path).worksheets[0]["C6"].value == "ООО «Орбита-Гидропроект»"
        self.results.append(("[перезапуск] файл восстанавливается новым процессом из хранилища на диске", ok, str(job.files[0].messages)))
        print(("OK   " if ok else "FAIL ") + "[перезапуск] файл восстанавливается новым процессом из хранилища на диске", flush=True)
        if not ok:
            self.exit_code = 1

    def finish(self) -> None:
        failed = [r for r in self.results if not r[1]]
        print(f"\nИтого: {len(self.results) - len(failed)} из {len(self.results)} проверок пройдено", flush=True)
        self.exit_code = 1 if failed else 0
        self.holder["window"].destroy()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shots", type=Path)
    parser.add_argument("--theme", choices=["light", "dark"], default="light")
    args = parser.parse_args()
    driver = Driver(args.shots, args.theme == "dark")
    driver.exit_code = 2
    window = webview.create_window(desktop.TITLE, f"http://127.0.0.1:{driver.port}/?t={driver.app.token}", js_api=driver.bridge,
                                   width=920, height=780, min_size=(520, 520), text_select=True)
    driver.holder["window"] = window
    webview.settings["ALLOW_DOWNLOADS"] = False
    webview.start(driver.run, private_mode=True)
    driver.server.shutdown()
    driver.restart_check()
    shutil.rmtree(driver.home, ignore_errors=True)
    return driver.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
