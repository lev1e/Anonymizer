"""Дефекты, найденные независимым тестировщиком (206 сценариев). Номер D-nn — номер в его отчёте."""
import io
import re
import unittest
import zipfile
from pathlib import Path

import fitz
from docx import Document
from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.table import Table
from pptx import Presentation

from .helpers import Env, pdf_bytes, xlsx_bytes, xlsx_cells, zip_text


def scan_text(text: str, env: Env | None = None) -> str:
    env = env or Env()
    job = env.anonymize(("t.txt", text))
    return Path(job.files[0].out_path).read_text("utf-8")


def raw_parts(path, *names) -> str:
    with zipfile.ZipFile(path) as z:
        return "\n".join(z.read(n).decode("utf-8", "ignore") for n in (names or z.namelist()) if n in z.namelist())


class D01SameFileNames(unittest.TestCase):
    def test_two_files_with_one_name_stay_two_results(self):
        env = Env()
        job = env.anonymize(("Отчёт.txt", "Директор Гладышев Аркадий Львович"), ("Отчёт.txt", "Директор Воронцова Полина Андреевна"))
        first, second = (Path(f.out_path).read_text("utf-8") for f in job.files)
        self.assertNotEqual(first, second)
        self.assertEqual(len(env.vault.entities), 2)
        self.assertNotEqual(job.files[0].out_path, job.files[1].out_path)

    def test_restore_of_two_files_with_one_name(self):
        env = Env()
        job = env.anonymize(("Отчёт.txt", "Директор Гладышев Аркадий Львович"), ("Отчёт.txt", "Директор Воронцова Полина Андреевна"))
        back = env.restore(env.as_upload(job, 0, "Отчёт.txt"), env.as_upload(job, 1, "Отчёт.txt"))
        texts = [Path(f.out_path).read_text("utf-8") for f in back.files]
        self.assertEqual(texts, ["Директор Гладышев Аркадий Львович", "Директор Воронцова Полина Андреевна"])


class D02YearAndClause(unittest.TestCase):
    def test_g_after_a_year_and_p_before_a_clause_are_not_places(self):
        for text in ("Итоги за 2025 г. Выручка выросла на 12 %.", "В 2025 г. Компания выросла.", "См. п. Условия оплаты договора.",
                     "по состоянию на 31 декабря 2025 г. Обязательства"):
            with self.subTest(text=text):
                self.assertNotIn("City", scan_text(text))

    def test_real_places_after_the_same_abbreviations_are_still_hidden(self):
        out = scan_text("Офис в г. Ковдор, п. Тальжино и г. Москва.")
        self.assertEqual(len(re.findall(r"City\d+", out)), 3, out)


class D03NumberFormats(unittest.TestCase):
    def test_format_strings_in_formulas_are_not_companies(self):
        for formula in ('=TEXT(A1,"0.00")', '=TEXT(A1,"0.0%")', '=TEXT(A1,"ДД.ММ.ГГГГ")', '=TEXT(A1,"dd.mm.yyyy")', '=IF(A1>0,"0.00","x")',
                        '=TEXT(A1,"#,##0")'):
            with self.subTest(formula=formula):
                env = Env()
                job = env.anonymize(("f.xlsx", xlsx_bytes({"A1": 5, "B1": formula})))
                self.assertEqual(xlsx_cells(env.output_bytes(job))["Лист1"]["B1"], formula)

    def test_short_capital_names_are_still_companies(self):
        out = scan_text("Клиент подписал договор с ООО «СМС» и ООО «ДМС».")
        self.assertNotIn("СМС", out)
        self.assertNotIn("ДМС", out)


class D04Metadata(unittest.TestCase):
    def test_company_and_manager_of_app_properties_and_title(self):
        env = Env()
        doc = Document()
        doc.add_paragraph("Текст")
        doc.core_properties.title = "Договор ООО «Вектор-Строй»"
        buffer = io.BytesIO()
        doc.save(buffer)
        src = zipfile.ZipFile(io.BytesIO(buffer.getvalue()))
        patched = io.BytesIO()
        with zipfile.ZipFile(patched, "w") as z:
            for item in src.infolist():
                data = src.read(item.filename)
                if item.filename == "docProps/app.xml":
                    data = data.decode().replace("</Properties>", "<Company>ООО «Вектор-Строй»</Company></Properties>").encode()
                z.writestr(item, data)
        job = env.anonymize(("a.docx", patched.getvalue()))
        text = raw_parts(job.files[0].out_path, "docProps/app.xml", "docProps/core.xml")
        self.assertNotIn("Вектор", text)


class D05LocalPaths(unittest.TestCase):
    def _patched(self, part, fn):
        wb = Workbook()
        wb.active["A1"] = "текст"
        buffer = io.BytesIO()
        wb.save(buffer)
        src = zipfile.ZipFile(io.BytesIO(buffer.getvalue()))
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            for item in src.infolist():
                data = src.read(item.filename)
                if item.filename == part:
                    data = fn(data.decode()).encode()
                z.writestr(item, data)
        return out.getvalue()

    def test_absolute_path_of_the_author_is_hidden_and_returns(self):
        path = "C:\\Users\\agerasimov009\\Desktop\\СУМИТЕК\\Клиент\\"
        data = self._patched("xl/workbook.xml", lambda x: x.replace("<workbookPr", f'<x15ac:absPath xmlns:x15ac="http://schemas.microsoft.com/office/spreadsheetml/2010/11/ac" url="{path}"/><workbookPr', 1))
        env = Env()
        job = env.anonymize(("a.xlsx", data))
        self.assertNotIn("agerasimov009", raw_parts(job.files[0].out_path))
        self.assertNotIn("СУМИТЕК", raw_parts(job.files[0].out_path, "xl/workbook.xml"))
        back = env.restore(env.as_upload(job))
        self.assertIn(path.replace("\\", "\\"), raw_parts(back.files[0].out_path, "xl/workbook.xml"))

    def test_file_url_of_a_template_in_docx_relationships(self):
        doc = Document()
        doc.add_paragraph("Текст")
        buffer = io.BytesIO()
        doc.save(buffer)
        src = zipfile.ZipFile(io.BytesIO(buffer.getvalue()))
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            for item in src.infolist():
                data = src.read(item.filename)
                if item.filename == "word/settings.xml":
                    pass
                if item.filename == "word/_rels/settings.xml.rels" or item.filename == "word/_rels/document.xml.rels":
                    data = data.decode().replace("</Relationships>", '<Relationship Id="rIdT" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate" Target="file:///C:/Users/Гладышев/Documents/Шаблон.dotm" TargetMode="External"/></Relationships>').encode()
                z.writestr(item, data)
        env = Env()
        job = env.anonymize(("a.docx", out.getvalue()))
        self.assertNotIn("Гладышев", raw_parts(job.files[0].out_path))


class D06LocalPhones(unittest.TestCase):
    def test_russian_numbers_without_a_country_code(self):
        for phone in ("(495) 123-45-72", "495 123-45-73", "7(495)1234571", "916-123-45-81", "79161234567", "+375 29 123-45-78",
                      "+380 44 123 45 79"):
            with self.subTest(phone=phone):
                out = scan_text(f"Связь: {phone}")
                self.assertRegex(out, r"Phone\d+")
                self.assertNotRegex(out, r"\d{3}")

    def test_numbers_that_are_not_phones_survive(self):
        for text in ("Арт. 123-45-6789", "Заказ 12-34-56", "Код 2026-03-15", "Сумма 1 234 567 ₽", "Версия 3.4.5.6"):
            with self.subTest(text=text):
                self.assertNotIn("Phone", scan_text(text))

    def test_phone_inside_a_technical_specification_number(self):
        self.assertNotIn("Phone", scan_text("ТУ 1234-567-89012345-2018"))


class D07Requisites(unittest.TestCase):
    def test_inn_kpp_and_iban_in_ordinary_wording(self):
        out = scan_text("ИНН/КПП 7707123458/770701001; ИНН организации 7707123458; КПП 770701001; IBAN DE89 3704 0044 0532 0130 00")
        for digits in ("7707123458", "770701001", "3704 0044"):
            self.assertNotIn(digits, out)

    def test_invalid_iban_is_not_replaced(self):
        self.assertNotIn("Account", scan_text("Код DE00 1234 5678 9012 3456 78 остаётся"))

    def test_phone_card_and_snils_written_as_numbers_in_a_workbook(self):
        env = Env()
        wb = Workbook()
        ws = wb.active
        ws["A1"], ws["A2"], ws["A3"], ws["A4"] = 79165551234, 4276550012345677, 11223344595, 1200
        buffer = io.BytesIO()
        wb.save(buffer)
        job = env.anonymize(("n.xlsx", buffer.getvalue()))
        self.assertEqual(job.files[0].status, "ok", job.files[0].messages)
        cells = xlsx_cells(env.output_bytes(job))["Sheet"]
        self.assertRegex(str(cells["A1"]), r"Phone\d+")
        self.assertRegex(str(cells["A2"]), r"Card\d+")
        self.assertRegex(str(cells["A3"]), r"Snils\d+")
        self.assertEqual(cells["A4"], 1200)
        back = env.restore(env.as_upload(job))
        restored = xlsx_cells(env.output_bytes(back))["Sheet"]
        self.assertEqual([str(restored[c]) for c in ("A1", "A2", "A3")], ["79165551234", "4276550012345677", "11223344595"])
        self.assertEqual(restored["A4"], 1200)


class D08LatinNames(unittest.TestCase):
    def test_latin_spellings_of_russian_names(self):
        rows = [("Андреев Андрей Александрович", "Andreev Andrej"), ("Кузнецов Александр Сергеевич", "Kuznetsov Aleksandr"),
                ("Михайлов Евгений Петрович", "Mikhailov Evgeniy"), ("Ильин Сергей Викторович", "Ilin Sergey"),
                ("Зайцев Роман Юрьевич", "Zaytsev Roman"), ("Орёл Наталья Ивановна", "Orel Natalya")]
        env = Env()
        wb = Workbook()
        ws = wb.active
        ws.append(["ФИО", "Latin"])
        for row in rows:
            ws.append(list(row))
        buffer = io.BytesIO()
        wb.save(buffer)
        job = env.anonymize(("l.xlsx", buffer.getvalue()))
        text = raw_parts(job.files[0].out_path, "xl/worksheets/sheet1.xml")
        for _, latin in rows:
            for word in latin.split():
                self.assertNotIn(word, text, latin)

    def test_namesakes_get_their_own_label_instead_of_staying_visible(self):
        env = Env()
        job = env.anonymize(("a.txt", "Ильин Сергей Викторович. Ильин Сергей Николаевич. Ilin Sergey подписал акт."))
        out = Path(job.files[0].out_path).read_text("utf-8")
        self.assertNotIn("Ilin", out)
        self.assertNotIn("Sergey", out)
        back = env.restore(env.as_upload(job))
        self.assertIn("Ilin Sergey подписал", Path(back.files[0].out_path).read_text("utf-8"))


class D09Pdf(unittest.TestCase):
    def _pdf(self):
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Report text", fontname="helv", fontsize=12)
        doc.set_toc([[1, "Глава Гладышев Аркадий Львович", 1]])
        doc.set_metadata({"title": "Отчёт ООО «Вектор-Строй»", "author": "Гладышев Аркадий Львович", "subject": "Клиент Ромашка"})
        doc.set_xml_metadata('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                             '<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator>Гладышев</dc:creator></rdf:Description></rdf:RDF></x:xmpmeta>')
        annot = page.add_text_annot((100, 100), "Позвонить Воронцовой Полине Андреевне")
        annot.set_info(title="Гладышев Аркадий")
        annot.update()
        data = doc.tobytes()
        doc.close()
        return data

    def test_bookmarks_xmp_annotations_and_properties_are_cleaned(self):
        env = Env()
        job = env.anonymize(("r.pdf", self._pdf()))
        with fitz.open(job.files[0].out_path) as doc:
            blob = doc.tobytes().decode("latin-1", "ignore")
            metadata = " ".join(str(v) for v in (doc.metadata or {}).values())
            toc = " ".join(str(e[1]) for e in doc.get_toc())
            notes = " ".join(str(a.info) for page in doc for a in (page.annots() or []))
            xmp = doc.get_xml_metadata()
        for word in ("Гладышев", "Вектор", "Ромашка", "Воронцов"):
            self.assertNotIn(word, metadata + toc + notes + xmp, word)
        self.assertEqual(job.files[0].status, "ok", job.files[0].messages)

    def test_pdf_round_trip_keeps_pages(self):
        env = Env()
        job = env.anonymize(("r.pdf", self._pdf()))
        back = env.restore(env.as_upload(job))
        self.assertNotEqual(back.files[0].status, "error", back.files[0].messages)


class D09MoreAfterRerun(unittest.TestCase):
    def test_pdf_form_field_value_is_hidden(self):
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Anketa", fontname="helv", fontsize=12)
        widget = fitz.Widget()
        widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        widget.field_name = "fio"
        widget.rect = fitz.Rect(72, 120, 320, 140)
        widget.field_value = "Директор Гладышев Аркадий Львович"
        page.add_widget(widget)
        data = doc.tobytes()
        doc.close()
        env = Env()
        job = env.anonymize(("f.pdf", data))
        with fitz.open(job.files[0].out_path) as out:
            values = [w.field_value for page in out for w in (page.widgets() or [])]
        self.assertTrue(values and all("Гладышев" not in str(v) for v in values), values)

    def test_titles_of_parts_in_app_properties_are_scanned(self):
        doc = Document()
        doc.add_paragraph("Директор Гладышев Аркадий Львович подписал.")
        buffer = io.BytesIO()
        doc.save(buffer)
        src = zipfile.ZipFile(io.BytesIO(buffer.getvalue()))
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            for item in src.infolist():
                data = src.read(item.filename)
                if item.filename == "docProps/app.xml":
                    data = data.decode().replace("</Properties>", '<TitlesOfParts><vt:vector xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes" size="1" baseType="lpstr"><vt:lpstr>Директор Гладышев Аркадий Львович</vt:lpstr></vt:vector></TitlesOfParts></Properties>').encode()
                z.writestr(item, data)
        env = Env()
        job = env.anonymize(("a.docx", out.getvalue()))
        self.assertNotIn("Гладышев", raw_parts(job.files[0].out_path, "docProps/app.xml"))

    def test_path_of_a_link_to_an_already_known_domain_is_hidden(self):
        out = scan_text("Сайт vector-co.ru, ссылка https://vector-co.ru/crm/gladyshev.")
        self.assertNotIn("gladyshev", out)
        self.assertNotIn("vector-co", out)

    def test_village_named_like_a_neuter_word_is_still_a_place(self):
        out = scan_text("Завод в п. Кольцово Новосибирской области. См. п. Условия оплаты.")
        self.assertNotIn("Кольцово", out)
        self.assertIn("п. Условия оплаты", out)


class D10HeaderFooter(unittest.TestCase):
    def test_codes_do_not_glue_to_the_text(self):
        wb = Workbook()
        ws = wb.active
        ws["A1"] = "текст"
        ws.oddHeader.left.text = "Лист"
        ws.oddHeader.right.text = "gladyshev@severvet.ru"
        ws.oddFooter.left.text = "+7 916 555-12-34"
        ws.oddFooter.center.text = "ООО «Северный Ветер»"
        buffer = io.BytesIO()
        wb.save(buffer)
        env = Env()
        job = env.anonymize(("h.xlsx", buffer.getvalue()))
        sheet = raw_parts(job.files[0].out_path, "xl/worksheets/sheet1.xml")
        for word in ("gladyshev", "916 555", "Северный"):
            self.assertNotIn(word, sheet)
        self.assertIn("&amp;Remail", sheet.replace("\u200b", ""))
        back = env.restore(env.as_upload(job))
        restored = raw_parts(back.files[0].out_path, "xl/worksheets/sheet1.xml")
        self.assertIn("&amp;Rgladyshev@severvet.ru", restored)
        self.assertIn("&amp;C" + "ООО «Северный Ветер»", restored)


class D11Attributes(unittest.TestCase):
    def test_hyperlink_tooltip_and_validation_prompt(self):
        wb = Workbook()
        ws = wb.active
        ws["A1"] = "ссылка"
        ws["A1"].hyperlink = "https://example.org/x"
        ws["A1"].hyperlink.tooltip = "Письмо Гладышеву Аркадию Львовичу"
        dv = DataValidation(type="list", formula1='"a,b"', showInputMessage=True)
        dv.promptTitle, dv.prompt = "Гладышев", "Введите gladyshev@severvet.ru"
        ws.add_data_validation(dv)
        dv.add("B1")
        buffer = io.BytesIO()
        wb.save(buffer)
        env = Env()
        job = env.anonymize(("a.xlsx", buffer.getvalue()))
        sheet = raw_parts(job.files[0].out_path, "xl/worksheets/sheet1.xml")
        for word in ("Гладыше", "gladyshev"):
            self.assertNotIn(word, sheet)

    def test_powerpoint_comment_authors_and_sdt_alias(self):
        doc = Document()
        doc.add_paragraph("Текст")
        buffer = io.BytesIO()
        doc.save(buffer)
        src = zipfile.ZipFile(io.BytesIO(buffer.getvalue()))
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            for item in src.infolist():
                data = src.read(item.filename)
                if item.filename == "word/document.xml":
                    data = data.decode().replace(
                        "<w:body>", '<w:body><w:sdt><w:sdtPr><w:alias w:val="Гладышев Аркадий"/><w:tag w:val="Клиент ООО «Северный Ветер»"/></w:sdtPr>'
                                     '<w:sdtContent><w:p><w:r><w:t>поле</w:t></w:r></w:p></w:sdtContent></w:sdt>', 1).encode()
                z.writestr(item, data)
        env = Env()
        job = env.anonymize(("a.docx", out.getvalue()))
        text = raw_parts(job.files[0].out_path, "word/document.xml")
        self.assertNotIn("Гладышев", text)
        self.assertNotIn("Северный Ветер", text)


class D12RoleSurnames(unittest.TestCase):
    def test_surname_after_a_role_is_replaced(self):
        for text, word in (("Директор Сидоров подписал.", "Сидоров"), ("Ответственный: Абдуллаев", "Абдуллаев"),
                           ("Менеджер Ковалёв сообщил.", "Ковалёв")):
            with self.subTest(text=text):
                self.assertNotIn(word, scan_text(text))

    def test_single_surnames_under_a_full_name_header(self):
        env = Env()
        wb = Workbook()
        ws = wb.active
        ws.append(["ФИО", "Оклад"])
        for name in ("Иванов", "Сидорова", "Гладышев"):
            ws.append([name, 100])
        buffer = io.BytesIO()
        wb.save(buffer)
        job = env.anonymize(("f.xlsx", buffer.getvalue()))
        text = zip_text(env.output_bytes(job))
        for word in ("Иванов", "Сидорова", "Гладышев"):
            self.assertNotIn(word, text)

    def test_ordinary_words_after_roles_survive(self):
        out = scan_text("Директор завода принял решение. Менеджер проекта доволен. Автор статьи неизвестен.")
        self.assertNotIn("Name", out)


class D13MetadataWhole(unittest.TestCase):
    def test_latin_author_is_replaced_entirely(self):
        wb = Workbook()
        wb.active["A1"] = "текст"
        wb.properties.lastModifiedBy = "Mikhail V. Zakharov"
        buffer = io.BytesIO()
        wb.save(buffer)
        env = Env()
        job = env.anonymize(("a.xlsx", buffer.getvalue()))
        core = raw_parts(job.files[0].out_path, "docProps/core.xml")
        self.assertNotIn("Mikhail", core)
        self.assertNotIn("Zakharov", core)


class D14Quotes(unittest.TestCase):
    def test_nested_and_single_quotes(self):
        for text, words in (('Заказчик ООО «Ромашка "Люкс"» подписал.', ("Ромашка", "Люкс")), ("ООО 'Зелёный Луг'", ("Зелёный", "Луг")),
                            ("ЗАО ‘Балтийская Звезда’", ("Балтийская", "Звезда")), ('Проект «Гранд "Альфа"» идёт.', ("Гранд", "Альфа"))):
            with self.subTest(text=text):
                out = scan_text(text)
                for word in words:
                    self.assertNotIn(word, out)

    def test_sole_proprietor_is_one_person_with_the_form_kept(self):
        out = scan_text("ИП Абдуллаев Рустам Камилович")
        self.assertRegex(out, r"^ИП Name\d+$")

    def test_initials_after_a_surname_are_still_initials(self):
        self.assertNotIn("Иванов", scan_text("Подписал Иванов ИП."))


class D15Companies(unittest.TestCase):
    def test_form_after_the_name(self):
        out = scan_text("Ромашка ООО выставила счёт.")
        self.assertNotIn("Ромашка", out)

    def test_names_after_cue_words_are_offered_for_review(self):
        env = Env()
        job = env.anonymize(("a.txt", "Клиент: Вектор-Ко. Работаем с корпорацией Тихая Гавань. Идёт проект Кедр."))
        offered = {s["text"] for s in job.result["suggestions"]}
        self.assertTrue({"Вектор-Ко", "Тихая Гавань", "Кедр"} <= offered, offered)

    def test_cue_words_without_a_name_offer_nothing(self):
        env = Env()
        job = env.anonymize(("a.txt", "Клиент доволен. Проект завершён. Компания растёт."))
        self.assertEqual(job.result["suggestions"], [])


class D16UrlPaths(unittest.TestCase):
    def test_path_and_query_of_a_corporate_link_are_hidden(self):
        out = scan_text("Профиль https://crm.firma-x.ru/users/zakhar-ryabinin?user=ryabinin. Сайт firma-x.ru.")
        self.assertNotIn("ryabinin", out)
        self.assertNotIn("firma-x", out)

    def test_profile_links(self):
        out = scan_text("канал t.me/vector_co и https://github.com/ivanivanov/repo")
        self.assertNotIn("vector_co", out)
        self.assertNotIn("ivanivanov", out)


class D17Addresses(unittest.TestCase):
    def test_avenue_with_an_adjective_and_postal_index(self):
        out = scan_text("119991, Москва, Ленинский пр-т, 32а")
        self.assertNotIn("119991", out)
        self.assertNotIn("Ленинский", out)

    def test_birth_date_in_words(self):
        self.assertNotIn("1979", scan_text("Дата рождения: 5 июля 1979 года"))


class D20TableNames(unittest.TestCase):
    def test_table_name_gets_no_invisible_character_and_returns(self):
        wb = Workbook()
        ws = wb.active
        ws.append(["Код", "Значение"])
        ws.append([1, 2])
        table = Table(displayName="Таблица_Гладышев", ref="A1:B2")
        ws.add_table(table)
        ws["D1"] = "Гладышев Аркадий Львович"
        buffer = io.BytesIO()
        wb.save(buffer)
        env = Env()
        job = env.anonymize(("t.xlsx", buffer.getvalue()))
        name = re.search(r'displayName="([^"]+)"', raw_parts(job.files[0].out_path, "xl/tables/table1.xml")).group(1)
        self.assertNotIn("\u200b", name)
        self.assertNotIn("Гладышев", name)
        back = env.restore(env.as_upload(job))
        self.assertIn('displayName="Таблица_Гладышев"', raw_parts(back.files[0].out_path, "xl/tables/table1.xml"))


class D21SheetRename(unittest.TestCase):
    def test_formulas_follow_a_sheet_renamed_by_the_limit(self):
        env = Env()
        data = xlsx_bytes({"A1": "Директор Гладышев Аркадий Львович", "B1": "Директор Воронцова Полина Андреевна"})
        job = env.anonymize(("a.xlsx", data))
        tokens = re.findall(r"Name\d+", zip_text(env.output_bytes(job)))
        wb = load_workbook(env.output(job))
        sheet = wb.create_sheet(f"{tokens[0]} и {tokens[1]} итоги")
        wb.active["D1"] = f"='{sheet.title}'!A1"
        buffer = io.BytesIO()
        wb.save(buffer)
        back = env.restore(("m.xlsx", buffer.getvalue()))
        restored = load_workbook(env.output(back))
        names = restored.sheetnames
        self.assertTrue(all(len(n) <= 31 for n in names), names)
        formula = restored.worksheets[0]["D1"].value
        target = re.match(r"='(.*)'!A1", formula).group(1)
        self.assertIn(target, names, f"{formula} {names}")

    def test_slash_in_a_restored_sheet_name(self):
        env = Env()
        job = env.anonymize(("a.xlsx", xlsx_bytes({"A1": "ООО «Ромашка/Ландыш»"})))
        token = re.search(r"Company\d+", zip_text(env.output_bytes(job))).group(0)
        wb = load_workbook(env.output(job))
        wb.create_sheet(token)
        wb.active["D1"] = f"={token}!A1"
        buffer = io.BytesIO()
        wb.save(buffer)
        back = env.restore(("m.xlsx", buffer.getvalue()))
        restored = load_workbook(env.output(back))
        self.assertNotIn("/", "".join(restored.sheetnames))
        formula = restored.worksheets[0]["D1"].value
        self.assertRegex(formula, r"^=('?)[^/]+\1!A1$")


class D22ForeignVault(unittest.TestCase):
    def test_file_from_another_computer_is_refused_for_office_and_pdf(self):
        a, b = Env(), Env()
        data = xlsx_bytes({"A1": "Директор Гладышев Аркадий Львович"})
        job = a.anonymize(("a.xlsx", data))
        b.anonymize(("z.txt", "Директор Петренко Виктор Сергеевич"))       # у B метка Name1 означает другого человека
        back = b.restore(a.as_upload(job))
        self.assertEqual(back.files[0].status, "error")
        self.assertIn("другим хранилищем", back.files[0].messages[0]["text"])
        pdf_job = a.anonymize(("a.pdf", pdf_bytes("Director Gladyshev Arkadiy")))
        self.assertEqual(b.restore(a.as_upload(pdf_job)).files[0].status, "error")

    def test_own_file_and_file_after_a_backup_import_are_accepted(self):
        a, b = Env(), Env()
        job = a.anonymize(("a.xlsx", xlsx_bytes({"A1": "Директор Гладышев Аркадий Львович"})))
        self.assertNotEqual(a.restore(a.as_upload(job)).files[0].status, "error")
        b.vault.import_backup(a.vault.export_backup("надёжный-пароль"), "надёжный-пароль")
        back = b.restore(a.as_upload(job))
        self.assertEqual(back.files[0].status, "ok", back.files[0].messages)

    def test_clearing_the_vault_invalidates_earlier_files(self):
        env = Env()
        job = env.anonymize(("a.xlsx", xlsx_bytes({"A1": "Директор Гладышев Аркадий Львович"})))
        env.vault.clear()
        self.assertEqual(env.restore(env.as_upload(job)).files[0].status, "error")

    def test_file_that_lost_the_mark_is_restored_as_before(self):
        env = Env()
        job = env.anonymize(("a.xlsx", xlsx_bytes({"A1": "Директор Гладышев Аркадий Львович"})))
        wb = load_workbook(env.output(job))
        wb.properties.identifier = None          # пересохранение другой программой снимает пометку
        buffer = io.BytesIO()
        wb.save(buffer)
        back = env.restore(("m.xlsx", buffer.getvalue()))
        self.assertEqual(xlsx_cells(env.output_bytes(back))["Sheet"]["A1"] if "Sheet" in xlsx_cells(env.output_bytes(back)) else
                         xlsx_cells(env.output_bytes(back))["Лист1"]["A1"], "Директор Гладышев Аркадий Львович")

    def test_the_mark_is_removed_from_the_restored_file(self):
        env = Env()
        job = env.anonymize(("a.xlsx", xlsx_bytes({"A1": "Директор Гладышев Аркадий Львович"})))
        self.assertIn("anonymizer:", raw_parts(job.files[0].out_path, "docProps/core.xml"))
        back = env.restore(env.as_upload(job))
        self.assertNotIn("anonymizer:", raw_parts(back.files[0].out_path, "docProps/core.xml"))


class D24AlreadyAnonymized(unittest.TestCase):
    def test_a_second_pass_keeps_existing_labels(self):
        env = Env()
        first = env.anonymize(("a.txt", "Заказчик ООО «Северный Ветер», директор Гладышев Аркадий Львович, gladyshev@severvet.ru, г. Тверь."))
        once = Path(first.files[0].out_path).read_text("utf-8")
        second = env.anonymize(("b.txt", once))
        twice = Path(second.files[0].out_path).read_text("utf-8")
        self.assertEqual(once, twice)
        back = env.restore(env.as_upload(second))
        self.assertIn("Северный Ветер", Path(back.files[0].out_path).read_text("utf-8"))


class D25BrokenLabels(unittest.TestCase):
    def test_spaced_and_glued_labels_are_reported(self):
        env = Env()
        env.anonymize(("a.txt", "Директор Гладышев Аркадий Львович и Воронцова Полина Андреевна"))
        for text in ("Name1Name2 подписали", "Name 1 подписал", "Приложение Name1 и Name 2"):
            with self.subTest(text=text):
                back = env.restore(("m.txt", text))
                self.assertTrue(any("нарушенной записью" in m["text"] for m in back.files[0].messages), back.files[0].messages)

    def test_ordinary_words_that_look_like_labels_are_not_reported(self):
        env = Env()
        env.anonymize(("a.txt", "Директор Гладышев Аркадий Львович"))
        back = env.restore(("m.txt", "Project 7 и Company 9 идут по плану, Name1 подписал"))
        self.assertFalse(any("нарушенной" in m["text"] for m in back.files[0].messages))


class D26Specification(unittest.TestCase):
    def test_hyphenated_specification_number_is_not_a_phone(self):
        out = scan_text("ТУ 1234-567-89012345-2018 и ГОСТ 12345-2013")
        self.assertNotIn("Phone", out)


if __name__ == "__main__":
    unittest.main()
