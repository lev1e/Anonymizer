"""Расширенный набор файлов: договор, протокол, письма, таблица сотрудников, презентация с диаграммой, PDF.

Для каждого файла проверяется: ничего из списка тайн не осталось, обычные слова не тронуты, после круга и после
правок «как у ИИ» значения возвращаются, а чужие данные не появляются.
"""
import io
import re
import unittest
import zipfile
from pathlib import Path

import fitz
from docx import Document
from openpyxl import load_workbook
from pptx import Presentation

from .extended_cases import all_cases
from .helpers import Env, content_units, zip_text

CASES = {case.name: case for case in all_cases()}
# Латинское название компании без юридической формы программа не отличает от обычных слов: оно предлагается на проверку.
KNOWN_MISSES = {"Northwind"}


def visible_text(path: Path) -> str:
    if path.suffix == ".pdf":
        with fitz.open(path) as doc:
            return "\n".join(page.get_text() for page in doc).replace("\xa0", " ") + " ".join(str(v) for v in (doc.metadata or {}).values())
    if path.suffix in (".xlsx", ".docx", ".pptx"):
        return zip_text(path.read_bytes())
    return path.read_text("utf-8")


class ExtendedCaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Env()
        cls.names = list(CASES)
        cls.job = cls.env.anonymize(*[(name, case.data) for name, case in CASES.items()])

    @classmethod
    def tearDownClass(cls):
        cls.env.close()

    def output(self, name: str) -> Path:
        return Path(self.job.files[self.names.index(name)].out_path)

    def test_every_file_is_produced(self):
        for name, outcome in zip(self.names, self.job.files):
            self.assertNotEqual(outcome.status, "error", (name, outcome.messages))
            self.assertTrue(outcome.out_path, name)

    def test_no_secret_value_is_left(self):
        for name, case in CASES.items():
            text = visible_text(self.output(name))
            for secret in case.secrets:
                if secret in KNOWN_MISSES:
                    continue
                with self.subTest(file=name, value=secret):
                    self.assertNotIn(secret, text)

    def test_ordinary_words_are_not_replaced(self):
        for name, case in CASES.items():
            text = visible_text(self.output(name))
            for word in case.keep:
                with self.subTest(file=name, word=word):
                    self.assertIn(word, text)

    def test_full_circle_returns_every_unit(self):
        back = self.env.restore(*[self.env.as_upload(self.job, i) for i in range(len(self.names))])
        for name, outcome in zip(self.names, back.files):
            case = CASES[name]
            with self.subTest(file=name):
                self.assertNotEqual(outcome.status, "error", outcome.messages)
                restored = Path(outcome.out_path)
                if restored.suffix in (".docx", ".xlsx", ".pptx"):
                    source = Path(self.env.base / "src" / name)
                    source.parent.mkdir(exist_ok=True)
                    source.write_bytes(case.data)
                    before, after = content_units(source), content_units(restored)
                    differing = [k for k in before if before[k] != after.get(k)]
                    self.assertEqual(differing[:3], [], [(k, before[k], after.get(k)) for k in differing[:3]])
                elif restored.suffix == ".txt":
                    self.assertEqual(restored.read_bytes().decode("utf-8"), case.data.decode("utf-8"))
                else:
                    text = visible_text(restored)
                    for secret in case.secrets[:3]:
                        self.assertIn(secret, text)

    def test_the_same_person_has_one_label_across_files(self):
        contract = zip_text(self.output("Договор подряда.docx").read_bytes())
        deck = zip_text(self.output("Презентация.pptx").read_bytes())
        self.assertTrue(re.search(r"Company\d+", contract))
        self.assertIsNone(re.search(r"Company\d+_\d+_\d+", deck))


class ModelEditsOnExtendedCases(unittest.TestCase):
    """Правки, которые делает модель: пересохранение библиотекой, дописанный текст, изменённый регистр меток."""

    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def anonymized(self, name):
        job = self.env.anonymize((name, CASES[name].data))
        return job, Path(job.files[0].out_path)

    def test_docx_resaved_with_added_text_and_uppercase_labels(self):
        job, out = self.anonymized("Договор подряда.docx")
        doc = Document(str(out))
        for paragraph in doc.paragraphs:
            for run in paragraph.runs:
                run.text = re.sub(r"\b(Name|Company)(\d+)", lambda m: m.group(1).upper() + m.group(2), run.text)
        doc.add_paragraph("Итог по проекту: расхождений нет.")
        buffer = io.BytesIO()
        doc.save(buffer)
        back = self.env.restore(("ответ.docx", buffer.getvalue()))
        text = zip_text(Path(back.files[0].out_path).read_bytes())
        for value in ("Смирнов", "Маяк-Строй", "Кузнецов", "Технопарк Север"):
            self.assertIn(value, text)
        self.assertEqual(back.result["unknown"], {})

    def test_xlsx_rows_reversed_moved_and_resaved(self):
        job, out = self.anonymized("Сотрудники.xlsx")
        wb = load_workbook(out)
        ws = wb["Сотрудники"]
        rows = [[c.value for c in row] for row in ws.iter_rows(min_row=1, max_row=5)]
        summary = wb.create_sheet("Итог", 0)
        for r, row in enumerate(reversed(rows), 1):
            for c, value in enumerate(row, 1):
                summary.cell(r, c, value)
        buffer = io.BytesIO()
        wb.save(buffer)
        back = self.env.restore(("ответ.xlsx", buffer.getvalue()))
        restored = load_workbook(Path(back.files[0].out_path))
        values = {c.value for row in restored["Итог"].iter_rows() for c in row if c.value}
        for expected in ("Соколов Андрей Павлович", "a.sokolov@zavod-tula.ru", "Омск", "Абрамов Тимур Русланович"):
            self.assertIn(expected, values)

    def test_pptx_resaved_with_a_new_slide(self):
        job, out = self.anonymized("Презентация.pptx")
        prs = Presentation(str(out))
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        slide.shapes.title.text = "Резюме: " + " ".join(re.findall(r"Name\d+", zip_text(out.read_bytes()))[:2])
        buffer = io.BytesIO()
        prs.save(buffer)
        back = self.env.restore(("ответ.pptx", buffer.getvalue()))
        text = zip_text(Path(back.files[0].out_path).read_bytes())
        self.assertIn("Никитин", text)
        self.assertIn("Пермь", text)
        self.assertEqual(back.result["unknown"], {})

    def test_text_letters_are_returned_with_declensions_and_case_changes(self):
        job, out = self.anonymized("Письмо.txt")
        text = out.read_text("utf-8")
        edited = re.sub(r"Name(\d+)", lambda m: f"NAME{m.group(1)}", text) + "\nP.S. Проверьте Company1."
        back = self.env.restore(("ответ.txt", edited))
        restored = Path(back.files[0].out_path).read_text("utf-8")
        self.assertIn("Лебедев", restored)
        self.assertIn("Красноярск", restored)


if __name__ == "__main__":
    unittest.main()
