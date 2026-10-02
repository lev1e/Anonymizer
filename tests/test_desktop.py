"""Окно программы: мост к системным диалогам, один экземпляр, соответствие страницы требованиям к дизайну."""
import json
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path

from anonymizer import crypto, desktop
from anonymizer.vault import Vault
from anonymizer.webapp import WEB_DIR, create_server

from .helpers import Env, xlsx_bytes, xlsx_cells


class FakeWindow:
    """Подмена системных окон: отдаёт заранее заданные ответы и запоминает, о чём спросили."""

    def __init__(self):
        self.answers = []
        self.asked = []

    def create_file_dialog(self, dialog_type=10, directory="", allow_multiple=False, save_filename="", file_types=()):
        self.asked.append({"type": dialog_type, "directory": directory, "multiple": allow_multiple,
                           "name": save_filename, "types": tuple(file_types)})
        return self.answers.pop(0) if self.answers else None


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.server, self.app = create_server(self.env.vault, self.env.base / "work")
        self.window = FakeWindow()
        self.bridge = desktop.Bridge(self.app, lambda: self.window)
        self.folder = self.env.base / "user"
        self.folder.mkdir()

    def tearDown(self):
        self.server.server_close()
        self.env.close()

    def write(self, name, data):
        path = self.folder / name
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return str(path)

    def anonymized(self):
        path = self.write("Данные.xlsx", xlsx_bytes({"A1": "ООО «Орбита-Гидропроект»", "B1": "Пётр Сидоров"}))
        self.window.answers = [(path,)]
        picked = self.bridge.pick_files()
        job_id = self.app.start_job("anonymize", [picked["files"][0]["info"]["id"]], {})
        self.app.runners[job_id].join(60)
        return job_id

    def test_pick_reads_files_from_disk_and_reports_each_one(self):
        good = self.write("a.xlsx", xlsx_bytes({"A1": "Иван Петров"}))
        old = self.write("старый.xls", b"x")
        self.window.answers = [(good, old, str(self.folder / "нет.txt"))]
        result = self.bridge.pick_files()
        first, second, third = result["files"]
        self.assertTrue(first["info"]["supported"])
        self.assertFalse(second["info"]["supported"])
        self.assertIn("xlsx", second["info"]["note"])
        self.assertIn("error", third)
        self.assertTrue(self.window.asked[0]["multiple"])
        self.assertTrue(any("xlsx" in t for t in self.window.asked[0]["types"]))

    def test_pick_cancelled_gives_an_empty_list(self):
        self.window.answers = [None]
        self.assertEqual(self.bridge.pick_files(), {"files": []})

    def test_a_dialog_failure_is_reported_as_a_line_not_an_exception(self):
        def broken(*args, **kwargs):
            raise RuntimeError("нет окна")
        self.window.create_file_dialog = broken
        self.assertIn("error", self.bridge.pick_files())
        self.assertIn("error", self.bridge.save_result("x", 0))

    def test_save_result_copies_the_file_and_remembers_the_folder(self):
        job_id = self.anonymized()
        target = self.folder / "результат.xlsx"
        self.window.answers = [(str(target),)]
        result = self.bridge.save_result(job_id, 0)
        self.assertEqual(result["path"], str(target))
        self.assertNotIn("Сидоров".encode(), target.read_bytes())
        ask = self.window.asked[-1]
        self.assertIn("(обезличено)", ask["name"])
        self.assertTrue(ask["name"].endswith(".xlsx"))
        self.window.answers = [(str(self.folder / "второй.xlsx"),)]
        self.bridge.save_result(job_id, 0)
        self.assertEqual(self.window.asked[-1]["directory"], str(self.folder))

    def test_save_result_accepts_a_plain_string_and_survives_cancel(self):
        job_id = self.anonymized()
        self.window.answers = [None]
        self.assertEqual(self.bridge.save_result(job_id, 0), {"cancelled": True})
        target = self.folder / "s.xlsx"
        self.window.answers = [str(target)]
        self.assertEqual(self.bridge.save_result(job_id, 0)["path"], str(target))

    def test_save_result_to_an_unwritable_place_is_one_line_with_an_action(self):
        job_id = self.anonymized()
        self.window.answers = [(str(self.folder / "нет" / "папки" / "f.xlsx"),)]
        result = self.bridge.save_result(job_id, 0)
        self.assertIn("error", result)
        self.assertIn("папк", result["error"])

    def test_unknown_job_is_a_line(self):
        self.assertIn("error", self.bridge.save_result("nope", 0))
        self.assertIn("error", self.bridge.save_all("nope"))

    def test_save_all_writes_into_a_folder_and_never_overwrites(self):
        one = self.write("один.txt", "Пётр Сидоров подписал договор с ООО «Орбита-Гидропроект»")
        two = self.write("два.txt", "Ольга Иванова")
        self.window.answers = [(one, two)]
        ids = [f["info"]["id"] for f in self.bridge.pick_files()["files"]]
        job_id = self.app.start_job("anonymize", ids, {"neutral_names": False})
        self.app.runners[job_id].join(60)
        target = self.env.base / "out"
        target.mkdir()
        (target / "один (обезличено).txt").write_text("занято", "utf-8")
        self.window.answers = [(str(target),)]
        result = self.bridge.save_all(job_id)
        self.assertEqual(sorted(result["saved"]), [0, 1])
        self.assertEqual((target / "один (обезличено).txt").read_text("utf-8"), "занято")
        self.assertTrue((target / "один (обезличено) (2).txt").exists())
        self.assertEqual(len(list(target.iterdir())), 3)

    def test_reveal_only_opens_files_the_program_saved(self):
        stranger = self.write("чужой.txt", "x")
        self.assertIn("error", self.bridge.reveal(stranger))
        self.assertIn("error", self.bridge.reveal("/etc/hosts"))

    def test_csv_is_saved_with_a_bom_for_excel(self):
        target = self.folder / "соответствия.csv"
        self.window.answers = [(str(target),)]
        result = self.bridge.save_text("соответствия.csv", "Группа;Было\r\nИмена;Пётр")
        self.assertEqual(result["path"], str(target))
        self.assertTrue(target.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertIn("Пётр", target.read_text("utf-8-sig"))

    def test_backup_round_trip_through_the_bridge(self):
        self.anonymized()
        self.assertIn("error", self.bridge.export_backup("abc"))
        target = self.folder / "копия.anonkeys"
        self.window.answers = [(str(target),)]
        self.assertEqual(self.bridge.export_backup("надёжный-пароль")["path"], str(target))
        other = Env()
        try:
            _, other_app = create_server(other.vault, other.base / "work")
            other_bridge = desktop.Bridge(other_app, lambda: self.window)
            self.assertIn("error", other_bridge.import_backup("надёжный-пароль"))      # файл ещё не выбран
            self.window.answers = [(str(target),)]
            self.assertEqual(other_bridge.pick_backup(), {"name": "копия.anonkeys"})
            self.assertIn("error", other_bridge.import_backup("неверный пароль"))
            self.window.answers = [(str(target),)]
            other_bridge.pick_backup()
            result = other_bridge.import_backup("надёжный-пароль")
            self.assertNotIn("error", result)
            self.assertGreater(other.vault.stats()["entities"], 0)
        finally:
            other.close()


class SingleInstanceTests(unittest.TestCase):
    def test_second_lock_is_refused_until_the_first_is_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "instance.lock"
            first, second = desktop.InstanceLock(path), desktop.InstanceLock(path)
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()

    def test_a_second_launch_asks_the_first_window_to_come_forward(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get("ANONYMIZER_HOME")
            os.environ["ANONYMIZER_HOME"] = tmp
            server = None
            try:
                self.assertFalse(desktop.notify_existing(timeout=1))       # запущенной копии нет
                env = Env()
                server, app = create_server(env.vault, env.base / "work")
                called = []
                app.on_focus = lambda: called.append(True)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                desktop.instance_file().write_text(json.dumps({"port": server.server_address[1], "token": app.token}), "utf-8")
                self.assertTrue(desktop.notify_existing())
                self.assertEqual(called, [True])
                desktop.instance_file().write_text(json.dumps({"port": server.server_address[1], "token": "чужой"}), "utf-8")
                self.assertFalse(desktop.notify_existing(timeout=1))
                env.close()
            finally:
                if server:
                    server.shutdown()
                    server.server_close()
                if previous is None:
                    os.environ.pop("ANONYMIZER_HOME", None)
                else:
                    os.environ["ANONYMIZER_HOME"] = previous

    def test_webview2_check_is_skipped_outside_windows(self):
        import sys
        if sys.platform != "win32":
            self.assertTrue(desktop.webview2_installed())


class SelfCheckTests(unittest.TestCase):
    def test_selfcheck_reports_every_component_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get("ANONYMIZER_HOME")
            os.environ["ANONYMIZER_HOME"] = tmp
            try:
                report = Path(tmp) / "report.json"
                self.assertEqual(desktop.selfcheck(report), 0)
                info = json.loads(report.read_text("utf-8"))
            finally:
                if previous is None:
                    os.environ.pop("ANONYMIZER_HOME", None)
                else:
                    os.environ["ANONYMIZER_HOME"] = previous
        self.assertTrue(info["ok"], info)
        self.assertEqual(set(info["checks"]), {"page_resource", "morphology_dictionary", "window_component", "server_and_page", "addin_https_page", "helper_and_window_client",
                                               "anonymize_and_restore"})
        self.assertTrue(info["pywebview"])


class PageContractTests(unittest.TestCase):
    """Требования к внешнему виду, которые легко нарушить правкой стилей."""

    @classmethod
    def setUpClass(cls):
        cls.html = (WEB_DIR / "index.html").read_text("utf-8")

    def test_no_gradients_glows_shadows_or_animation(self):
        for word in ("gradient", "box-shadow", "@keyframes", "animation", "transition", "text-shadow", "filter:"):
            self.assertNotIn(word, self.html, word)

    def test_no_emoji_icons_or_images(self):
        self.assertIsNone(re.search("[\U0001F300-\U0001FAFF☀-➿⬀-⯿]", self.html))
        for tag in ("<svg", "<img", "<canvas"):
            self.assertNotIn(tag, self.html)

    def test_forbidden_words_are_absent(self):
        visible = re.sub(r"<style.*?</style>|<script.*?</script>", "", self.html, flags=re.S).lower()
        for word in ("умн", "магия", "магическ", "ии-помощник"):
            self.assertNotIn(word, visible)
        self.assertNotIn("!", re.sub(r"<style.*?</style>|<script.*?</script>", "", self.html, flags=re.S).replace("<!doctype", ""))

    def test_page_loads_nothing_from_the_network(self):
        self.assertIsNone(re.search(r"(src|href)=[\"']https?:", self.html))
        self.assertNotIn("@import", self.html)
        self.assertNotIn("url(http", self.html)

    def test_one_accent_colour_and_dark_theme_are_defined(self):
        self.assertIn("prefers-color-scheme: dark", self.html)
        self.assertIn("--accent:", self.html)
        # Акцент допустим только у главной кнопки и фокуса.
        uses = [m.start() for m in re.finditer(r"var\(--accent[a-z-]*\)|var\(--focus\)", self.html)]
        self.assertLessEqual(len(uses), 8)

    def test_ids_are_unique(self):
        ids = re.findall(r'\bid="([^"$]+)"', self.html)
        self.assertEqual(len(ids), len(set(ids)), sorted({i for i in ids if ids.count(i) > 1}))

    def test_every_control_has_a_visible_focus_style(self):
        self.assertIn(":focus-visible", self.html)
        self.assertNotIn("outline: none", self.html)
        self.assertNotIn("outline:0", self.html)


if __name__ == "__main__":
    unittest.main()
