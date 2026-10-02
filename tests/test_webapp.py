"""Локальный сервер: доступ только по ключу сеанса, полный круг через HTTP, безопасность запросов."""
import http.client
import json
import threading
import time
import unittest
import urllib.parse

from anonymizer.webapp import create_server

from .helpers import Env, xlsx_bytes


class WebAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Env()
        cls.server, cls.app = create_server(cls.env.vault, cls.env.base / "work")
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.cookie = f"anon_t={cls.app.token}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.env.close()

    def request(self, method, path, body=None, headers=None, host=None, cookie=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        head = {"Host": host or f"127.0.0.1:{self.port}"}
        if cookie:
            head["Cookie"] = self.cookie
        head.update(headers or {})
        connection.request(method, path, body=body, headers=head)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, response.getheaders(), data

    def post_json(self, path, payload):
        return self.request("POST", path, json.dumps(payload).encode(), {
            "Content-Type": "application/json", "X-Requested-With": "anonymizer"})

    def upload(self, name, data):
        status, _, body = self.request("POST", "/api/upload", data, {
            "X-Filename": urllib.parse.quote(name), "X-Requested-With": "anonymizer"})
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def wait(self, job_id):
        for _ in range(200):
            status, _, body = self.request("GET", f"/api/jobs/{job_id}")
            info = json.loads(body)
            if info["state"] != "running":
                return info
            time.sleep(.1)
        self.fail("задание не завершилось")

    # -- безопасность --------------------------------------------------------

    def test_page_needs_the_session_key(self):
        status, _, _ = self.request("GET", "/", cookie=False)
        self.assertEqual(status, 403)
        status, headers, _ = self.request("GET", f"/?t={self.app.token}", cookie=False)
        self.assertEqual(status, 302)
        self.assertIn("HttpOnly", dict(headers)["Set-Cookie"])
        self.assertIn("SameSite=Strict", dict(headers)["Set-Cookie"])
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("Anonymizer", body.decode())

    def test_wrong_key_is_refused(self):
        status, _, _ = self.request("GET", "/?t=wrong", cookie=False)
        self.assertEqual(status, 403)
        status, _, _ = self.request("GET", "/api/state", headers={"Cookie": "anon_t=wrong"}, cookie=False)
        self.assertEqual(status, 403)

    def test_foreign_host_header_is_refused(self):
        status, _, _ = self.request("GET", "/api/state", host="evil.example:80")
        self.assertEqual(status, 403)

    def test_post_without_marker_header_is_refused(self):
        status, _, _ = self.request("POST", "/api/prefs", b"{}", {"Content-Type": "application/json"})
        self.assertEqual(status, 403)

    def test_responses_are_not_cached_and_carry_security_headers(self):
        _, headers, _ = self.request("GET", "/api/state")
        h = {k.lower(): v for k, v in headers}
        self.assertEqual(h["cache-control"], "no-store")
        self.assertIn("frame-ancestors 'none'", h["content-security-policy"])

    # -- сценарий ------------------------------------------------------------

    def test_full_cycle_over_http(self):
        data = xlsx_bytes({"A1": "Клиент", "B1": "ООО «Орбита-Гидропроект»", "B2": "Пётр Сидоров"})
        info = self.upload("Данные Клиента.xlsx", data)
        self.assertTrue(info["supported"])
        self.assertEqual(info["tokens"], 0)
        status, _, body = self.post_json("/api/anonymize", {"files": [info["id"]], "options": {}})
        self.assertEqual(status, 200)
        job = self.wait(json.loads(body)["job"])
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(job["files"][0]["status"], "ok")
        status, headers, blob = self.request("GET", f"/api/download/{job['id']}/0")
        self.assertEqual(status, 200)
        self.assertIn("attachment", dict(headers)["Content-Disposition"])
        self.assertNotIn("Сидоров".encode(), blob)
        again = self.upload("ответ.xlsx", blob)
        self.assertGreaterEqual(again["tokens"], 2)
        status, _, body = self.post_json("/api/restore", {"files": [again["id"]]})
        restored = self.wait(json.loads(body)["job"])
        self.assertEqual(restored["files"][0]["status"], "ok", restored["files"][0]["messages"])
        status, _, back = self.request("GET", f"/api/download/{restored['id']}/0")
        from .helpers import xlsx_cells
        self.assertEqual(xlsx_cells(back)["Лист1"]["B2"], "Пётр Сидоров")

    def test_rerun_endpoint_applies_the_users_decision(self):
        info = self.upload("a.txt", "Основные игроки: Трехдубский и Carlsberg.".encode())
        _, _, body = self.post_json("/api/anonymize", {"files": [info["id"]], "options": {}})
        first = self.wait(json.loads(body)["job"])
        self.assertTrue(any(s["text"] == "Трехдубский" for s in first["result"]["suggestions"]))
        _, _, body = self.post_json("/api/rerun", {"job": first["id"], "overrides": {"Трехдубский": "hide"}, "options": {}})
        second = self.wait(json.loads(body)["job"])
        _, _, blob = self.request("GET", f"/api/download/{second['id']}/0")
        self.assertNotIn("Трехдубский".encode(), blob)

    def test_unsupported_file_is_explained_before_running(self):
        info = self.upload("старый.xls", b"x")
        self.assertFalse(info["supported"])
        self.assertIn("xlsx", info["note"])

    def test_upload_of_nothing_is_an_error(self):
        status, _, _ = self.request("POST", "/api/upload", b"", {"X-Filename": "a.txt", "X-Requested-With": "anonymizer"})
        self.assertEqual(status, 400)

    def test_preferences_round_trip(self):
        status, _, body = self.post_json("/api/prefs", {"hide_terms": ["Орион"], "numbers": True, "retention_days": 90})
        self.assertEqual(status, 200)
        prefs = json.loads(body)["prefs"]
        self.assertEqual(prefs["hide_terms"], ["Орион"])
        self.assertTrue(prefs["numbers"])
        info = self.upload("b.txt", "Проект Орион идёт".encode())
        _, _, body = self.post_json("/api/anonymize", {"files": [info["id"]], "options": {"numbers": None}})
        job = self.wait(json.loads(body)["job"])
        _, _, blob = self.request("GET", f"/api/download/{job['id']}/0")
        self.assertNotIn("Орион".encode(), blob)
        self.post_json("/api/prefs", {"hide_terms": [], "numbers": False})

    def test_backup_endpoint_requires_a_password(self):
        status, _, _ = self.post_json("/api/backup/export", {"password": "abc"})
        self.assertEqual(status, 400)
        status, _, blob = self.post_json("/api/backup/export", {"password": "long-enough"})
        self.assertEqual(status, 200)
        self.assertTrue(blob.startswith(b"ANONBACKUP1"))

    def test_focus_request_reaches_the_window_and_needs_the_session_key(self):
        calls = []
        self.app.on_focus = lambda: calls.append(1)
        try:
            status, _, _ = self.post_json("/api/focus", {})
            self.assertEqual(status, 200)
            self.assertEqual(calls, [1])
            status, _, _ = self.request("POST", "/api/focus", b"{}", {"X-Requested-With": "anonymizer"}, cookie=False)
            self.assertEqual(status, 403)
            self.assertEqual(calls, [1])
        finally:
            self.app.on_focus = None

    def test_native_window_bridge_needs_eval_in_the_page_policy(self):
        """pywebview создаёт методы моста через eval. Без 'unsafe-eval' кнопки окна молча не работают (найдено в окне)."""
        _, headers, _ = self.request("GET", "/api/state")
        policy = {k.lower(): v for k, v in headers}["content-security-policy"]
        self.assertIn("'unsafe-eval'", policy)
        self.assertIn("default-src 'self'", policy)

    def test_upload_from_a_path_reads_the_file_without_the_page(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "заметка.txt"
            source.write_text("Проект Орион ведёт Пётр Сидоров", "utf-8")
            info = self.app.add_upload_path(str(source))
        self.assertTrue(info["supported"])
        self.assertEqual(info["name"], "заметка.txt")
        with self.assertRaises(OSError):
            self.app.add_upload_path("/нет/такого/файла.txt")

    def test_a_body_that_a_route_does_not_read_does_not_break_the_next_request(self):
        """Соединение остаётся открытым, и непрочитанное тело `{}` читалось следующим запросом как его строка: ответ 501."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        head = {"Host": f"127.0.0.1:{self.port}", "Cookie": self.cookie, "X-Requested-With": "anonymizer",
                "Content-Type": "application/json"}
        for path in ("/api/vault/clear", "/api/focus", "/api/vault/clear"):
            connection.request("POST", path, body=b"{}", headers=head)
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 200, path)
            connection.request("GET", "/api/state", headers={"Host": head["Host"], "Cookie": self.cookie})
            follow = connection.getresponse()
            follow.read()
            self.assertEqual(follow.status, 200, f"после {path}")
        connection.close()

    def test_unauthorised_post_with_a_body_closes_the_connection(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        connection.request("POST", "/api/prefs", body=b'{"numbers": true}', headers={"Host": f"127.0.0.1:{self.port}"})
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 403)
        self.assertEqual(response.getheader("Connection"), "close")
        connection.close()

    def test_unknown_job_is_404(self):
        status, _, _ = self.request("GET", "/api/jobs/nope")
        self.assertEqual(status, 404)
        status, _, _ = self.request("GET", "/api/download/nope/0")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
