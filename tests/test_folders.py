"""Целая папка: зеркальная структура, общие метки между файлами, возврат, плохой файл."""
import unittest
from pathlib import Path

from anonymizer import folders
from anonymizer.service import SUFFIX_ANON, SUFFIX_RESTORED

from .helpers import Env, docx_bytes, docx_text, xlsx_bytes, xlsx_cells

SECRET = "Смирнов Алексей Петрович"


class FolderTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.src = self.env.base / "Клиент Ромашка"
        (self.src / "Договоры" / "2024").mkdir(parents=True)
        (self.src / "Отчёты").mkdir()
        (self.src / "Договоры" / "Договор.docx").write_bytes(docx_bytes([f"Директор {SECRET}, ООО «Ромашка», г. Новосибирск."]))
        (self.src / "Договоры" / "2024" / "Договор.docx").write_bytes(docx_bytes([f"Подписал {SECRET}. Телефон +7 913 555-12-34."]))
        (self.src / "Отчёты" / "Реестр.xlsx").write_bytes(xlsx_bytes({"A1": "ФИО", "A2": SECRET, "B2": "ООО «Ромашка»"}))
        (self.src / "заметка.txt").write_text(f"Звонил {SECRET}", encoding="utf-8")
        (self.src / "картинка.png").write_bytes(b"\x89PNG not really")
        (self.src / ".hidden").mkdir()
        (self.src / ".hidden" / "x.txt").write_text("skip", encoding="utf-8")
        (self.src / "~$Договор.docx").write_bytes(b"lock")

    def tearDown(self):
        self.env.close()

    def run_folder(self, kind, source, options=None):
        scan = folders.scan_folder(source, kind)
        context = folders.FolderContext(scan, kind)
        job = self.env.service.new_job(kind, options or {})
        folders.run_folder(self.env.service, context, options or {}, job)
        return job, context

    def test_scan_skips_unsupported_hidden_and_lock_files(self):
        scan = folders.scan_folder(self.src, "anonymize")
        self.assertEqual(sorted(rel.as_posix() for rel, _ in scan.files),
                         sorted(["Договоры/2024/Договор.docx", "Договоры/Договор.docx", "заметка.txt", "Отчёты/Реестр.xlsx"]))
        self.assertEqual([s["path"] for s in scan.skipped], ["картинка.png"])

    def test_mirror_hides_all_data_including_folder_names(self):
        job, context = self.run_folder("anonymize", self.src)
        self.assertEqual(job.state, "done")
        dest = context.dest
        self.assertTrue(dest.name.endswith(SUFFIX_ANON))
        self.assertNotIn("Ромашка", str(dest))
        self.assertTrue(self.src.is_dir() and (self.src / "Договоры" / "Договор.docx").exists(), "исходная папка не тронута")
        everything = [p for p in dest.rglob("*") if p.is_file()]
        self.assertEqual(len(everything), 4)
        for path in everything:
            self.assertNotIn("Смирнов", str(path))
            if path.suffix == ".docx":
                self.assertNotIn("Смирнов", docx_text(path))
            elif path.suffix == ".txt":
                self.assertNotIn("Смирнов", path.read_text("utf-8"))
        xlsx = next(p for p in everything if p.suffix == ".xlsx")
        self.assertNotIn(SECRET, " ".join(str(v) for v in xlsx_cells(xlsx).get("Лист1", {}).values()))

    def test_same_person_gets_same_label_across_files(self):
        job, context = self.run_folder("anonymize", self.src)
        texts = [docx_text(p) for p in context.dest.rglob("*.docx")]
        import re
        labels = {tuple(re.findall(r"Name\d+", t)) for t in texts}
        flat = {x for group in labels for x in group}
        self.assertEqual(len(flat), 1, texts)

    def test_same_named_files_keep_clean_names_in_their_own_folders(self):
        job, context = self.run_folder("anonymize", self.src)
        names = sorted(p.name for p in context.dest.rglob("*.docx"))
        self.assertEqual(len(set(names)), 1, names)          # без « (2)» в имени

    def test_restore_whole_folder_returns_data_and_names(self):
        _, anon = self.run_folder("anonymize", self.src)
        job, restored = self.run_folder("restore", anon.dest)
        self.assertEqual(job.state, "done")
        self.assertTrue(restored.dest.name.endswith(SUFFIX_RESTORED))
        self.assertTrue(restored.dest.name.startswith("Клиент Ромашка"), restored.dest.name)
        rel = {p.relative_to(restored.dest).as_posix() for p in restored.dest.rglob("*") if p.is_file()}
        self.assertEqual(rel, {"Договоры/2024/Договор (восстановлено).docx", "Договоры/Договор (восстановлено).docx",
                               "заметка (восстановлено).txt", "Отчёты/Реестр (восстановлено).xlsx"})
        text = docx_text(restored.dest / "Договоры" / "Договор (восстановлено).docx")
        self.assertIn(SECRET, text)
        self.assertEqual((restored.dest / "заметка (восстановлено).txt").read_text("utf-8"), f"Звонил {SECRET}")

    def test_bad_file_does_not_stop_the_job(self):
        (self.src / "Отчёты" / "Сломан.docx").write_bytes(b"PK not a zip")
        job, context = self.run_folder("anonymize", self.src)
        self.assertEqual(job.state, "done")
        summary = job.result["folder"]
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["written"], 4)
        bad = next(f for f in job.files if f.name.endswith("Сломан.docx"))
        self.assertEqual(bad.status, "error")

    def test_empty_folder_is_refused_with_a_reason(self):
        empty = self.env.base / "пусто"
        empty.mkdir()
        with self.assertRaises(ValueError):
            self.run_folder("anonymize", empty)

    def test_previous_output_inside_folder_is_not_processed_again(self):
        _, first = self.run_folder("anonymize", self.src)
        nested = self.src / first.dest.name
        first.dest.rename(nested)
        scan = folders.scan_folder(self.src, "anonymize")
        self.assertFalse(any(first.dest.name in rel.as_posix() for rel, _ in scan.files))

    def test_second_run_does_not_overwrite_first_result(self):
        _, first = self.run_folder("anonymize", self.src)
        _, second = self.run_folder("anonymize", self.src)
        self.assertNotEqual(first.dest, second.dest)
        self.assertTrue(first.dest.is_dir() and second.dest.is_dir())

    def test_app_runs_folder_jobs_and_rerun_rewrites_the_mirror(self):
        import time

        from anonymizer.webapp import App
        app = App(self.env.vault, self.env.base / "appwork", "t")
        info = app.add_folder(str(self.src), "anonymize")
        self.assertEqual(info["files"], 4)

        def wait(job_id):
            app.runners[job_id].join(timeout=120)
            for _ in range(50):
                job = app.job_public(job_id)
                if job["state"] != "running":
                    return job
                time.sleep(0.1)
            self.fail("задание не завершилось")
        first = wait(app.start_folder_job("anonymize", info["id"], {}))
        self.assertEqual(first["state"], "done", first)
        out = Path(first["result"]["folder"]["out"])
        self.assertTrue(out.is_dir())
        again = wait(app.start_job("anonymize", [], {}, {"Новосибирск": "hide"}, rerun_of=first["id"]))
        self.assertEqual(again["state"], "done", again)
        new_out = Path(again["result"]["folder"]["out"])
        self.assertEqual(new_out, out, "результат прошлого прохода заменён, а не лежит рядом вторым")
        self.assertEqual(len([p for p in out.parent.iterdir() if p.name.endswith(SUFFIX_ANON) or "(обезличено)" in p.name]), 1)
        with self.assertRaises(KeyError):
            app.start_folder_job("anonymize", "нет", {})
        with self.assertRaises(ValueError):
            app.add_folder(str(self.src / "нет такой"), "anonymize")

    def test_rerun_replaces_mirror(self):
        job, context = self.run_folder("anonymize", self.src)
        first = context.dest
        folders.remove_previous(context)
        self.assertFalse(first.exists())


if __name__ == "__main__":
    unittest.main()
