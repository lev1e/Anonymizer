"""Настоящие файлы из папки case: полный круг и проверка, что чувствительное не осталось."""
import io
import json
import re
import unittest
import zipfile
from pathlib import Path

from openpyxl import load_workbook

from anonymizer.service import Upload

from .helpers import CASE_DIR, Env, content_units, zip_text

# Служебные файлы блокировки Office («~$…»), пока файл открыт в программе, не документы.
CASE_FILES = sorted(p for ext in ("xlsx", "docx", "pptx") for p in CASE_DIR.glob(f"*.{ext}") if not p.name.startswith("~$")) if CASE_DIR.is_dir() else []

# Значения, которые в обезличенных файлах встречаться не должны ни в каком виде, и имена файлов лежат только в case/
# (папка не публикуется): case/expectations.json и case/must_disappear.json.
MUST_DISAPPEAR: dict[str, list[str]] = {}
# Настоящие названия и значения, по которым проверяются файлы, лежат только в case/ (папка не публикуется).
_EXPECT_FILE = CASE_DIR / "expectations.json"
EXPECT = json.loads(_EXPECT_FILE.read_text(encoding="utf-8")) if _EXPECT_FILE.is_file() else {}
FILES = EXPECT.get("files", {})
_PRIVATE_TERMS = CASE_DIR / "must_disappear.json"
if _PRIVATE_TERMS.is_file():
    for _name, _terms in json.loads(_PRIVATE_TERMS.read_text(encoding="utf-8")).items():
        MUST_DISAPPEAR[_name] = list(dict.fromkeys([*MUST_DISAPPEAR.get(_name, []), *_terms]))


@unittest.skipUnless(CASE_FILES and FILES, "папка case отсутствует")
class CaseFilesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Env()
        cls.job = cls.env.anonymize(*CASE_FILES)
        cls.restored = cls.env.restore(*[cls.env.as_upload(cls.job, i) for i in range(len(CASE_FILES))
                                         if cls.job.files[i].out_path])

    @classmethod
    def tearDownClass(cls):
        cls.env.close()

    def index(self, name):
        return next(i for i, f in enumerate(self.job.files) if f.name == name)

    def test_every_file_is_produced_without_errors(self):
        for outcome in self.job.files:
            self.assertNotEqual(outcome.status, "error", (outcome.name, outcome.messages))
            self.assertTrue(outcome.out_path, outcome.name)

    def test_warnings_are_only_about_images(self):
        for outcome in self.job.files:
            for message in outcome.messages:
                # Необезличенные встроенные объекты (.xlsb, OLE) — настоящий риск, о нём предупреждать обязательно.
                if message["level"] == "warn" and not message["text"].startswith("Вложенные объекты не обезличены"):
                    self.fail(f"{outcome.name}: {message['text']}")

    def test_sensitive_values_are_gone_from_every_file(self):
        for name, words in MUST_DISAPPEAR.items():
            xml = zip_text(self.env.output_bytes(self.job, self.index(name)))
            for word in words:
                with self.subTest(file=name, word=word):
                    if word in xml:
                        at = xml.index(word)
                        self.fail(f"{word!r} остался: ...{xml[max(0, at - 50):at + 60]!r}")

    def test_every_employee_name_is_hidden(self):
        name = FILES["employees"]
        source = load_workbook(CASE_DIR / name, read_only=True)
        sheet = source[EXPECT["employees_sheet"]]
        people = [row[2] for row in sheet.iter_rows(min_row=4, values_only=True) if row[2]]
        out = zip_text(self.env.output_bytes(self.job, self.index(name)))
        words = set(re.findall(r"[^\W\d_]+", out))
        leaked = [p for p in people if p in out or p.split()[0] in words]
        self.assertEqual(leaked[:5], [], f"утекло {len(leaked)} из {len(people)}")

    def test_every_email_is_hidden(self):
        name = FILES["employees"]
        out = zip_text(self.env.output_bytes(self.job, self.index(name)))
        emails = re.findall(r"[\w.+-]+@(?!example\.com)[\w-]+\.[\w.-]+", out)
        self.assertEqual(emails[:5], [])

    def test_structure_is_unchanged(self):
        for i, outcome in enumerate(self.job.files):
            source = CASE_DIR / outcome.name
            # Эскиз первой страницы удаляется намеренно: на картинке читается исходное содержимое. Так же намеренно удаляются
            # вложения, которые программа не обезличивает (книга .xlsb диаграммы, объект OLE .bin).
            before = [n for n in zipfile.ZipFile(source).namelist() if not n.startswith("docProps/thumbnail")
                      and not ("/embeddings/" in n and n.lower().endswith((".bin", ".xlsb", ".xls")))]
            after = zipfile.ZipFile(outcome.out_path).namelist()
            self.assertEqual(sorted(before), sorted(after), outcome.name)

    def test_round_trip_returns_every_cell_exactly(self):
        for i, outcome in enumerate(self.restored.files):
            with self.subTest(file=outcome.name):
                self.assertNotEqual(outcome.status, "error", outcome.messages)
                # Возврат называет файл исходным именем, даже если обезличенный получил нейтральное «Файл1».
                original_name = re.sub(r" \(восстановлено\)", "", outcome.out_name)
                original = content_units(CASE_DIR / original_name)
                restored = content_units(Path(outcome.out_path))
                differing = [k for k in original if original[k] != restored.get(k)]
                self.assertEqual(differing[:3], [], f"{len(differing)} отличий")
                self.assertEqual(len(original), len(restored))

    def test_nothing_is_left_unrestored(self):
        self.assertEqual(self.restored.result["unknown"], {})

    def test_formulas_are_not_touched(self):
        name = FILES["stock"]
        before = load_workbook(CASE_DIR / name)
        after = load_workbook(self.env.output(self.job, self.index(name)))
        formulas_before = {(ws.title, c.coordinate): c.value for ws in before.worksheets for row in ws.iter_rows()
                           for c in row if isinstance(c.value, str) and c.value.startswith("=")}
        formulas_after = {(ws.title, c.coordinate): c.value for ws in after.worksheets for row in ws.iter_rows()
                          for c in row if isinstance(c.value, str) and c.value.startswith("=")}
        self.assertEqual(len(formulas_before), len(formulas_after))
        self.assertGreater(len(formulas_before), 0)

    def test_workbook_restructured_by_a_model_is_still_restored(self):
        name = FILES["client_data"]
        anon = load_workbook(self.env.output(self.job, self.index(name)))
        source = anon.worksheets[0]
        model = anon.create_sheet("Assumptions", 0)
        moved = 0
        for row in source.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and not cell.value.startswith("="):
                    model.cell(2 + moved, 5, cell.value)
                    moved += 1
        buffer = io.BytesIO()
        anon.save(buffer)
        restored = self.env.restore(Upload("model.xlsx", buffer.getvalue()))
        moved_values = {c.value for row in load_workbook(self.env.output(restored)).worksheets[0].iter_rows()
                        for c in row if c.value}
        if not EXPECT.get("restored_values"):
            self.skipTest("нет case/expectations.json")
        for expected in EXPECT["restored_values"]:
            self.assertTrue(any(expected in str(v) for v in moved_values), expected)

    def test_rows_reversed_and_text_extended_by_a_model_still_restore(self):
        """Модель переставила строки, дописала текст в ячейки и разложила данные по новому листу."""
        name = FILES["employees"]
        anon = load_workbook(self.env.output(self.job, self.index(name)))
        source = anon[EXPECT["employees_sheet"]]
        rows = [[c.value for c in row] for row in source.iter_rows()]
        rebuilt = anon.create_sheet("Итог", 0)
        for r, row in enumerate(reversed(rows), 1):
            for c, value in enumerate(row, 1):
                if isinstance(value, str) and not value.startswith("="):
                    value = "LLM: " + value
                rebuilt.cell(r, c, value)
        buffer = io.BytesIO()
        anon.save(buffer)
        restored = self.env.restore(Upload("model.xlsx", buffer.getvalue()))
        self.assertEqual(restored.result["unknown"], {})
        original = load_workbook(CASE_DIR / name)[EXPECT["employees_sheet"]]
        expected = [[c.value for c in row] for row in original.iter_rows()]
        expected = [[("LLM: " + v) if isinstance(v, str) and not v.startswith("=") else v for v in row]
                    for row in reversed(expected)]
        got_sheet = load_workbook(self.env.output(restored))["Итог"]
        got = [[c.value for c in row] for row in got_sheet.iter_rows()]
        self.assertEqual(len(got), len(expected))
        differing = [(i, j) for i, (a, b) in enumerate(zip(got, expected)) for j, (x, y) in enumerate(zip(a, b)) if x != y]
        self.assertEqual(differing[:3], [], f"{len(differing)} ячеек не совпали")

    def test_documents_extended_by_a_model_still_restore(self):
        from docx import Document
        name = FILES["demo_doc"]
        doc = Document(str(self.env.output(self.job, self.index(name))))
        for paragraph in doc.paragraphs:
            if paragraph.text.strip():
                paragraph.add_run(" [проверено]")
        buffer = io.BytesIO()
        doc.save(buffer)
        restored = self.env.restore(Upload("ответ.docx", buffer.getvalue()))
        original = content_units(CASE_DIR / name)
        got = content_units(self.env.output(restored))
        for key, value in original.items():
            if key.startswith("p") and value.strip():
                self.assertEqual(got[key], value + " [проверено]", key)

    def test_cross_file_consistency_one_token_per_company(self):
        """Одна компания — одна метка во всех файлах. Метку определяем по хранилищу, а не по порядку номеров в файле."""
        if not EXPECT.get("org"):
            self.skipTest("нет case/expectations.json")
        bases = [base for base, entity in self.env.vault.entities.items()
                 if entity["kind"] == "ORG" and any(EXPECT["org"] in spelling for spelling in entity["spellings"])]
        self.assertEqual(len(bases), 1, bases)
        for name in (FILES["client_data"], FILES["terms"], FILES["marketing"]):
            xml = zip_text(self.env.output_bytes(self.job, self.index(name)))
            self.assertIn(bases[0], set(re.findall(r"Company\d+", xml)), name)


if __name__ == "__main__":
    unittest.main()
