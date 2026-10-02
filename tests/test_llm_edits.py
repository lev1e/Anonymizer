"""Что делает модель с обезличенным файлом: переставляет, переименовывает, дописывает. Возврат должен выдержать.

Документы собраны так, как их отдают Word, Excel и PowerPoint: с колонтитулами, свойствами, именованными диапазонами.
Правки делаются теми же библиотеками, которыми модель правит файлы."""
import io
import re
import unittest
import zipfile

from docx import Document
from openpyxl import load_workbook
from openpyxl.workbook.defined_name import DefinedName
from pptx import Presentation

from .helpers import Env, docx_bytes, pptx_bytes, xlsx_bytes, xlsx_cells

BOSS = "Смирнов Алексей Петрович"
ASSISTANT = "Иванова Мария Сергеевна"
PHONE = "+7 913 555-12-34"
MAIL = "smirnov@romashka.ru"
CLIENT = "ООО «Ромашка»"


def raw_text(data: bytes) -> str:
    """Весь текст файла со всех частей, в том числе свойства: так ищет утечку тот, кому файл попал в руки."""
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if name.endswith((".xml", ".rels")):
                out.append(re.sub(r"<[^>]+>", "", z.read(name).decode("utf-8", "ignore")))
    return "\n".join(out)


def word_file() -> bytes:
    d = Document()
    d.core_properties.author = BOSS
    d.core_properties.title = f"Договор поставки {CLIENT}"
    d.sections[0].header.paragraphs[0].text = f"{CLIENT} — конфиденциально"
    d.sections[0].footer.paragraphs[0].text = f"Директор {BOSS}, тел. {PHONE}"
    d.add_heading("Договор поставки", 1)
    d.add_paragraph(f"Директор {BOSS} ({CLIENT}, г. Новосибирск) подписал договор. Почта: {MAIL}.")
    d.add_paragraph(f"Помощник: {ASSISTANT}. Телефон {PHONE}.")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "ФИО", "Телефон"
    t.cell(1, 0).text, t.cell(1, 1).text = BOSS, PHONE
    d.add_paragraph("Последний абзац без данных, обычный текст про поставки.")
    out = io.BytesIO()
    d.save(out)
    return out.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def hide(self, name, data):
        job = self.env.anonymize((name, data))
        self.assertEqual(job.files[0].status in ("ok", "attention"), True, job.files[0].messages)
        return job, self.env.output_bytes(job)

    def back(self, name, data):
        job = self.env.restore((name, data))
        self.assertTrue(job.files[0].out_path, job.files[0].messages)
        return job, self.env.output_bytes(job)


class WordEdits(Base):
    def test_no_sensitive_value_survives_in_any_part(self):
        _, hidden = self.hide("a.docx", word_file())
        text = raw_text(hidden)
        for secret in ("Смирнов", "Алексей", "Иванова", "Ромашка", "913", MAIL, "Новосибирск"):
            self.assertNotIn(secret, text, secret)

    def test_title_with_contract_word_and_company_label_does_not_trigger_residual_warning(self):
        d = Document()
        d.add_paragraph(f"Договор поставки {CLIENT}")
        d.add_paragraph(f"Директор {BOSS}, г. Новосибирск.")
        out = io.BytesIO()
        d.save(out)
        job, _ = self.hide("t.docx", out.getvalue())
        self.assertEqual(job.files[0].status, "ok", job.files[0].messages)

    def test_model_reorders_edits_and_changes_case(self):
        _, hidden = self.hide("a.docx", word_file())
        d = Document(io.BytesIO(hidden))
        paras = d.paragraphs
        body = paras[0]._p.getparent()
        # перенос: последний абзац в начало; регистр метки; дописанный текст с меткой и с неизвестной меткой
        body.insert(0, paras[-1]._p)
        for p in d.paragraphs:
            if p.text.startswith("Помощник"):
                for r in p.runs:
                    r.text = re.sub(r"Name(\d+)", lambda m: f"name{m.group(1)}", r.text)
        d.add_paragraph("Итог: Name1 согласует, Name99 не знаком, Company1 в Москве.")
        out = io.BytesIO()
        d.save(out)
        job, restored = self.back("a.docx", out.getvalue())
        text = "\n".join(p.text for p in Document(io.BytesIO(restored)).paragraphs)
        self.assertIn(ASSISTANT, text)                       # регистр метки не помешал
        self.assertIn(f"Итог: {BOSS} согласует", text)
        self.assertIn("Name99", text)                        # чужая метка остаётся как есть
        self.assertTrue(any("Name99" in m["text"] for m in job.files[0].messages), job.files[0].messages)
        self.assertTrue(text.splitlines()[0].startswith("Последний"), "перенос абзаца сохранился")

    def test_label_split_across_runs_keeps_the_space_after_it(self):
        """Word режет «Company1 ждёт» на «Company» и «1 ждёт»; без xml:space="preserve» он отбрасывал пробел: «Ромашкаждёт»."""
        _, hidden = self.hide("a.docx", word_file())
        d = Document(io.BytesIO(hidden))
        p = d.add_paragraph()
        p.add_run("Итог: ")
        p.add_run("Company")
        p.add_run("1 ждёт.")
        out = io.BytesIO()
        d.save(out)
        _, restored = self.back("a.docx", out.getvalue())
        with zipfile.ZipFile(io.BytesIO(restored)) as z:
            xml = z.read("word/document.xml").decode("utf-8")
        run = re.search(r"<w:t[^>]*>[^<]* ждёт\.</w:t>", xml)
        self.assertIsNotNone(run, xml[-600:])
        self.assertIn('xml:space="preserve"', run.group(0))

    def test_model_deletes_the_paragraph_with_a_value_and_rewrites_table(self):
        _, hidden = self.hide("a.docx", word_file())
        d = Document(io.BytesIO(hidden))
        for p in list(d.paragraphs):
            if "Помощник" in p.text:
                p._p.getparent().remove(p._p)
        d.tables[0].add_row().cells[0].text = "Name2"
        out = io.BytesIO()
        d.save(out)
        job, restored = self.back("a.docx", out.getvalue())
        doc = Document(io.BytesIO(restored))
        self.assertNotIn(ASSISTANT, "\n".join(p.text for p in doc.paragraphs))
        self.assertEqual(doc.tables[0].rows[1].cells[0].text, BOSS)
        self.assertEqual(doc.tables[0].rows[2].cells[0].text, ASSISTANT)

    def test_headers_footers_and_properties_come_back(self):
        _, hidden = self.hide("a.docx", word_file())
        _, restored = self.back("a.docx", hidden)
        text = raw_text(restored)
        for expected in (BOSS, CLIENT.split("«")[1].rstrip("»"), PHONE, f"Договор поставки {CLIENT}"):
            self.assertIn(expected, text, expected)
        self.assertNotRegex(text, r"\b(?:Name|Company|Phone|Meta)\d+\b")

    def test_russian_endings_added_by_model_are_flagged_not_guessed(self):
        _, hidden = self.hide("a.docx", word_file())
        d = Document(io.BytesIO(hidden))
        d.add_paragraph("Передайте Name1у и Name2е.")
        out = io.BytesIO()
        d.save(out)
        job, restored = self.back("a.docx", out.getvalue())
        self.assertTrue(any("окончан" in m["text"] for m in job.files[0].messages), job.files[0].messages)


class ExcelEdits(Base):
    def book(self) -> bytes:
        data = xlsx_bytes({"A1": "ФИО", "B1": "Компания", "C1": "Сумма", "A2": BOSS, "B2": CLIENT, "C2": 1200,
                           "A3": ASSISTANT, "B3": CLIENT, "C3": 800}, title="Реестр",
                          extra_sheets={"Итоги": {"A1": "Всего", "B1": "=SUM(Реестр!C2:C3)"}})
        wb = load_workbook(io.BytesIO(data))
        wb.defined_names["Клиент_Ромашка"] = DefinedName("Клиент_Ромашка", attr_text="Реестр!$B$2")
        wb.properties.creator = BOSS
        out = io.BytesIO()
        wb.save(out)
        return out.getvalue()

    def test_no_sensitive_value_survives_including_range_names(self):
        _, hidden = self.hide("r.xlsx", self.book())
        text = raw_text(hidden)
        for secret in ("Смирнов", "Алексей", "Иванова", "Ромашка"):
            self.assertNotIn(secret, text, secret)

    def test_model_sorts_rows_renames_sheet_and_adds_formulas(self):
        _, hidden = self.hide("r.xlsx", self.book())
        wb = load_workbook(io.BytesIO(hidden))
        ws = wb["Реестр"]
        row2, row3 = [c.value for c in ws[2]], [c.value for c in ws[3]]
        for i, v in enumerate(row3):                       # сортировка: строки меняются местами
            ws.cell(2, i + 1).value = v
        for i, v in enumerate(row2):
            ws.cell(3, i + 1).value = v
        ws["D1"] = "С НДС"
        ws["D2"] = "=C2*1.2"
        ws["D3"] = "=C3*1.2"
        ws.title = "Данные"                                # лист переименован: формулы обновляет сама библиотека
        wb["Итоги"]["B1"] = "=SUM(Данные!C2:C3)"
        wb["Итоги"]["A2"] = '="Итого для "&Данные!A2'
        wb.create_sheet("Заметки")["A1"] = "Name1 звонил, Company1 ждёт"
        out = io.BytesIO()
        wb.save(out)
        job, restored = self.back("r.xlsx", out.getvalue())
        cells = xlsx_cells(restored)
        self.assertEqual(cells["Данные"]["A2"], ASSISTANT)
        self.assertEqual(cells["Данные"]["A3"], BOSS)
        self.assertEqual(cells["Данные"]["B3"], CLIENT)
        # Метка, которую дописала модель, возвращается в первое сохранённое написание организации (с формой или без неё).
        self.assertRegex(cells["Заметки"]["A1"], rf"^{BOSS} звонил, (?:ООО «)?Ромашка»? ждёт$")
        self.assertEqual(cells["Итоги"]["B1"], "=SUM(Данные!C2:C3)")
        self.assertNotRegex(raw_text(restored), r"\b(?:Name|Company|Meta)\d+\b")

    def test_sheet_name_with_label_is_restored(self):
        data = xlsx_bytes({"A1": BOSS}, title=BOSS[:25])
        _, hidden = self.hide("s.xlsx", data)
        names = load_workbook(io.BytesIO(hidden)).sheetnames
        self.assertNotIn("Смирнов", " ".join(names))
        _, restored = self.back("s.xlsx", hidden)
        self.assertEqual(load_workbook(io.BytesIO(restored)).sheetnames, [BOSS[:25]])

    def test_range_name_round_trip(self):
        _, hidden = self.hide("r.xlsx", self.book())
        _, restored = self.back("r.xlsx", hidden)
        self.assertIn("Клиент_Ромашка", load_workbook(io.BytesIO(restored)).defined_names)


class PowerPointEdits(Base):
    def deck(self) -> bytes:
        prs = Presentation()
        for title, body in ((f"Проект для {CLIENT}", f"Руководитель: {BOSS}\nГород: Новосибирск"),
                            ("Итоги", f"Контакт: {ASSISTANT}, {PHONE}")):
            s = prs.slides.add_slide(prs.slide_layouts[1])
            s.shapes.title.text = title
            s.placeholders[1].text = body
        s.notes_slide.notes_text_frame.text = f"Не забыть позвонить {BOSS}"
        prs.core_properties.author = BOSS
        out = io.BytesIO()
        prs.save(out)
        return out.getvalue()

    def test_no_sensitive_value_survives_including_notes(self):
        _, hidden = self.hide("d.pptx", self.deck())
        text = raw_text(hidden)
        for secret in ("Смирнов", "Алексей", "Иванова", "Ромашка", "913"):
            self.assertNotIn(secret, text, secret)

    def test_model_reorders_slides_and_adds_a_slide(self):
        _, hidden = self.hide("d.pptx", self.deck())
        prs = Presentation(io.BytesIO(hidden))
        ids = prs.slides._sldIdLst
        first = list(ids)[0]
        ids.remove(first)
        ids.append(first)                                     # слайды поменяны местами
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = "Новый слайд"
        s.placeholders[1].text = "Согласовать с Name1 и Company1"
        out = io.BytesIO()
        prs.save(out)
        _, restored = self.back("d.pptx", out.getvalue())
        slides = Presentation(io.BytesIO(restored)).slides
        texts = ["\n".join(sh.text_frame.text for sh in sl.shapes if sh.has_text_frame) for sl in slides]
        self.assertIn(ASSISTANT, texts[0])
        self.assertIn(BOSS, texts[1])
        self.assertRegex(texts[2], rf"Согласовать с {BOSS} и (?:ООО «)?Ромашка»?")
        self.assertIn(f"Не забыть позвонить {BOSS}", slides[0].notes_slide.notes_text_frame.text)


if __name__ == "__main__":
    unittest.main()
