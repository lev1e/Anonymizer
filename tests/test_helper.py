"""Фоновая программа и окно-клиент: окно работает с хранилищем только через неё."""
import json
import threading
import time
import unittest
from pathlib import Path

from anonymizer import helper
from anonymizer.webapp import create_server

from .helpers import Env, docx_bytes, docx_text, xlsx_bytes
from .test_desktop import FakeWindow
from anonymizer import desktop


class Base(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.server, self.app = create_server(self.env.vault, self.env.base / "work")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.info = {"port": self.server.server_address[1], "token": self.app.token}
        self.client = helper.HelperClient(self.info, timeout=30)
        self.remote = helper.RemoteApp(self.client)
        self.window = FakeWindow()
        self.bridge = desktop.Bridge(self.remote, lambda: self.window)
        self.folder = self.env.base / "user"
        self.folder.mkdir()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.remote.cleanup()
        self.env.close()

    def write(self, name, data):
        path = self.folder / name
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return str(path)

    def wait(self, job_id):
        for _ in range(600):
            job = self.client.json("GET", f"/api/jobs/{job_id}")
            if job["state"] != "running":
                return job
            time.sleep(0.1)
        self.fail("задание не завершилось")


class ClientTests(Base):
    def test_ping_needs_the_token(self):
        self.assertTrue(helper.ping(self.info))
        self.assertFalse(helper.ping({"port": self.info["port"], "token": "чужой"}))
        self.assertFalse(helper.ping(None))

    def test_window_picks_files_and_the_helper_reads_them(self):
        path = self.write("Договор.docx", docx_bytes(["Директор Смирнов Алексей Петрович."]))
        self.window.answers = [(path,)]
        picked = self.bridge.pick_files()
        info = picked["files"][0]["info"]
        self.assertTrue(info["supported"], picked)
        job = self.client.json("POST", "/api/anonymize", {"files": [info["id"]], "options": {}})
        done = self.wait(job["job"])
        self.assertEqual(done["state"], "done", done)

    def test_save_result_downloads_from_the_helper_and_writes_where_the_person_chose(self):
        path = self.write("Данные.xlsx", xlsx_bytes({"A1": "Пётр Сидоров"}))
        self.window.answers = [(path,)]
        info = self.bridge.pick_files()["files"][0]["info"]
        job = self.client.json("POST", "/api/anonymize", {"files": [info["id"]], "options": {}})["job"]
        self.wait(job)
        target = self.folder / "результат.xlsx"
        self.window.answers = [(str(target),)]
        result = self.bridge.save_result(job, 0)
        self.assertEqual(result["path"], str(target))
        self.assertNotIn("Сидоров".encode(), target.read_bytes())
        self.assertIn("(обезличено)", self.window.asked[-1]["name"])

    def test_save_all_and_unknown_job_are_lines_not_exceptions(self):
        self.assertIn("error", self.bridge.save_result("нет такого", 0))
        self.assertIn("error", self.bridge.save_all("нет такого"))

    def test_unreadable_path_and_folder_report_one_line(self):
        self.window.answers = [(str(self.folder / "нет.docx"),)]
        picked = self.bridge.pick_files()
        self.assertIn("error", picked["files"][0])
        self.window.answers = [(str(self.folder / "нет такой папки"),)]
        self.assertIn("error", self.bridge.pick_folder("anonymize"))

    def test_folder_job_through_the_window_bridge(self):
        (self.folder / "Отчёты").mkdir()
        (self.folder / "Отчёты" / "а.docx").write_bytes(docx_bytes(["Директор Смирнов Алексей Петрович."]))
        self.window.answers = [(str(self.folder / "Отчёты"),)]
        picked = self.bridge.pick_folder("anonymize")
        self.assertEqual(picked["folder"]["files"], 1, picked)
        job = self.client.json("POST", "/api/folder/anonymize", {"folder": picked["folder"]["id"], "options": {}})["job"]
        done = self.wait(job)
        self.assertEqual(done["state"], "done", done)
        revealed = []
        import subprocess
        original = subprocess.Popen
        subprocess.Popen = lambda *a, **k: revealed.append(a)       # не открываем Finder в тесте
        try:
            self.assertEqual(self.bridge.reveal_folder(job), {"ok": True})
        finally:
            subprocess.Popen = original

    def test_backup_round_trip_goes_through_the_helper(self):
        target = self.folder / "копия.anonkeys"
        self.window.answers = [(str(target),)]
        self.assertEqual(self.bridge.export_backup("пароль-123")["path"], str(target))
        self.window.answers = [(str(target),)]
        self.assertEqual(self.bridge.pick_backup()["name"], "копия.anonkeys")
        result = self.bridge.import_backup("пароль-123")
        self.assertIn("state", result, result)
        self.window.answers = [(str(target),)]
        self.bridge.pick_backup()
        self.assertIn("error", self.bridge.import_backup("неверный"))


class WindowEventsTests(Base):
    def test_a_waiting_window_wakes_only_when_there_is_something_to_say(self):
        self.client.json("POST", "/api/window/open", {"id": "w1"})
        started = time.time()
        got = {}
        thread = threading.Thread(target=lambda: got.update(self.client.json("GET", "/api/window/wait?id=w1", timeout=30)))
        thread.start()
        time.sleep(0.5)
        self.assertTrue(thread.is_alive(), "пока тихо, запрос ждёт")
        self.client.json("POST", "/api/focus", {})
        thread.join(5)
        self.assertEqual(got.get("focus"), True)
        self.assertLess(time.time() - started, 5)

    def test_no_window_means_the_helper_launches_one(self):
        launched = []
        self.app.spawn_window = lambda tab=None: launched.append(tab)
        self.client.json("POST", "/api/focus", {})
        self.assertEqual(launched, [None])
        self.app.request_window("rest")
        self.assertEqual(launched, [None, "rest"])

    def test_closed_window_is_forgotten(self):
        self.client.json("POST", "/api/window/open", {"id": "w2"})
        self.assertEqual(self.app.live_windows(), ["w2"])
        self.client.json("POST", "/api/window/closed", {"id": "w2"})
        self.assertEqual(self.app.live_windows(), [])


class LifetimeTests(Base):
    def test_helper_without_windows_exits_after_the_grace_period_unless_pinned(self):
        started = time.time() - helper.STARTUP_GRACE_SECONDS - 1
        self.assertTrue(helper.should_exit(self.app, time.time(), started))
        self.app.keep_alive = True
        self.assertFalse(helper.should_exit(self.app, time.time(), started))

    def test_helper_waits_while_a_window_is_alive_and_after_quit_it_stops(self):
        self.app.register_window("w")
        started = time.time() - 1000
        self.assertFalse(helper.should_exit(self.app, time.time(), started))
        self.app.windows["w"] -= 1000                      # окно давно не отзывалось
        self.assertTrue(helper.should_exit(self.app, time.time(), started))
        self.app.keep_alive = True
        self.client.json("POST", "/api/quit", {})
        self.assertTrue(helper.should_exit(self.app, time.time(), time.time()))

    def test_fresh_helper_gives_the_window_time_to_appear(self):
        self.assertFalse(helper.should_exit(self.app, time.time(), time.time()))


class StartupTests(unittest.TestCase):
    def test_autostart_from_sources_only_when_asked(self):
        from anonymizer import addin_install
        self.assertIsNone(addin_install.launch_command())
        command = addin_install.launch_command(dev=True)
        self.assertEqual(command[-2:], ["--serve", "--background"])
        self.assertTrue(Path(command[1]).name == "main.py")

    def test_launch_args_point_at_main_when_not_frozen(self):
        args = helper.launch_args(helper.SERVE_FLAG)
        self.assertEqual(args[-1], "--serve")
        self.assertTrue(Path(args[1]).name == "main.py" and Path(args[1]).exists())

    def test_modes_are_chosen_by_flags(self):
        self.assertEqual(helper.SERVE_FLAG, "--serve")
        self.assertEqual(helper.BACKGROUND_FLAG, "--background")


if __name__ == "__main__":
    unittest.main()
