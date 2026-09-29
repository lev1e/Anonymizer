"""Настройки «Скрывать страны» и «Заменять названия файлов нейтральными». Все названия и имена выдуманы."""
import io
import re
import unittest
import zipfile
from pathlib import Path

import fitz

from anonymizer.service import safe_name

from .helpers import Env, content_units, docx_bytes, pdf_bytes, pptx_bytes, xlsx_bytes

WEB = Path(__file__).resolve().parent.parent / "anonymizer" / "web" / "index.html"

COUNTRY_TEXT = ("Поставки из Армении и Омана в ОАЭ; армянский коньяк продаётся хорошо.\n"
                "Китай, в Китае, китайские партнёры, КНР и China.\n"
                "Россия, РФ, в России, российский рынок, Russia.\n"
                "Ю. Корея и Южной Корее, USA и США.\n")
KEPT_TEXT = ("Страны СНГ и другие страны. Офис на Китайгородском проезде, район Китай-город. "
             "Курс RUB и USD. Поставщик ООО «Россия Инвест» подписал договор.\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def text_of(self, job, index=0) -> str:
        return self.env.output(job, index).read_text(encoding="utf-8")


class CountryTests(Base):
    def test_off_by_default_countries_stay(self):
        job = self.env.anonymize(("c.txt", COUNTRY_TEXT))
        out = self.text_of(job)
        for word in ("Армении", "Китае", "ОАЭ", "Россия", "армянский"):
            self.assertIn(word, out)
        self.assertNotIn("Country", out)

    def test_every_form_is_hidden_and_restored_exactly(self):
        job = self.env.anonymize(("c.txt", COUNTRY_TEXT), options={"countries": True})
        out = self.text_of(job)
        for word in ("Армени", "армянск", "Оман", "ОАЭ", "Китай", "Китае", "китайск", "КНР", "China", "Росси",
                     "РФ", "российск", "Russia", "Корея", "Корее", "USA", "США"):
            self.assertNotIn(word, out)
        restored = self.env.restore(self.env.as_upload(job))
        self.assertEqual(self.env.output(restored).read_text(encoding="utf-8"), COUNTRY_TEXT)

    def test_forms_of_one_country_share_one_base_token(self):
        job = self.env.anonymize(("c.txt", COUNTRY_TEXT), options={"countries": True})
        replaced = {e["original"]: e["token"] for items in job.result["groups"].values() for e in items}
        china = {replaced[w].split("_")[0] for w in ("Китай", "Китае", "китайские", "КНР", "China")}
        russia = {replaced[w].split("_")[0] for w in ("Россия", "РФ", "России", "российский", "Russia")}
        korea = {replaced[w].split("_")[0] for w in ("Ю. Корея", "Южной Корее")}
        for group in (china, russia, korea):
            self.assertEqual(len(group), 1, group)
            self.assertTrue(next(iter(group)).startswith("Country"))
        self.assertEqual(len(china | russia | korea), 3)
        self.assertEqual(replaced["Армении"].split("_")[0], replaced["армянский"].split("_")[0])

    def test_common_nouns_parts_of_words_currencies_and_company_names_stay(self):
        job = self.env.anonymize(("k.txt", KEPT_TEXT), options={"countries": True})
        out = self.text_of(job)
        for word in ("Страны СНГ", "другие страны", "Китайгородском", "Китай-город", "RUB", "USD"):
            self.assertIn(word, out)
        # Компания целиком заменяется своей меткой, страна внутри названия отдельно не выделяется.
        self.assertRegex(out, r"ООО «Company\d+»")
        self.assertNotIn("Country", out)
        restored = self.env.restore(self.env.as_upload(job))
        self.assertEqual(self.env.output(restored).read_text(encoding="utf-8"), KEPT_TEXT)

    def test_office_file_round_trip(self):
        data = docx_bytes(["Экспорт в Китай и Армению вырос.", "Китайские и армянские партнёры довольны."])
        job = self.env.anonymize(("c.docx", data), options={"countries": True})
        body = zipfile.ZipFile(self.env.output(job)).read("word/document.xml").decode()
        self.assertNotIn("Китай", body)
        self.assertNotIn("армянск", body)
        restored = self.env.restore(self.env.as_upload(job))
        source = self.env.base / "c.docx"
        source.write_bytes(data)
        self.assertEqual(content_units(source), content_units(self.env.output(restored)))

    def test_preference_is_used_when_option_is_absent(self):
        env = Env(countries=True)
        try:
            job = env.anonymize(("c.txt", "Склад в Армении."))
            self.assertNotIn("Армении", env.output(job).read_text(encoding="utf-8"))
        finally:
            env.close()


def zip_blob(path: Path) -> bytes:
    with zipfile.ZipFile(path) as z:
        return z.comment + "\n".join(z.namelist()).encode() + b"".join(z.read(n) for n in z.namelist())


class NeutralNameTests(Base):
    def test_default_name_is_neutral_and_restore_returns_the_original(self):
        job = self.env.anonymize(("Реестр Кедровый Лог.txt", "Заметка о поставке."))
        self.assertEqual(job.files[0].out_name, "Файл1 (обезличено).txt")
        restored = self.env.restore(self.env.as_upload(job))
        self.assertEqual(restored.files[0].out_name, "Реестр Кедровый Лог (восстановлено).txt")

    def test_off_keeps_the_previous_naming(self):
        job = self.env.anonymize(("Заметки.txt", "Заметка о поставке."), options={"neutral_names": False})
        self.assertEqual(job.files[0].out_name, "Заметки (обезличено).txt")
        restored = self.env.restore(self.env.as_upload(job))
        self.assertEqual(restored.files[0].out_name, "Заметки (восстановлено).txt")

    def test_same_stem_different_extensions_and_duplicates_get_unique_names(self):
        job = self.env.anonymize(("Реестр Кедровый.txt", "а"), ("Реестр Кедровый.csv", "б"), ("Реестр Кедровый.txt", "в"))
        names = [f.out_name for f in job.files]
        self.assertEqual(len(set(names)), 3, names)
        self.assertEqual(names[0], "Файл1 (обезличено).txt")
        self.assertEqual(names[1], "Файл2 (обезличено).csv")
        restored = self.env.restore(*[self.env.as_upload(job, i) for i in range(3)])
        self.assertEqual([f.out_name for f in restored.files][:2],
                         ["Реестр Кедровый (восстановлено).txt", "Реестр Кедровый (восстановлено).csv"])
        self.assertTrue(restored.files[2].out_name.startswith("Реестр Кедровый"))

    def test_the_vault_keeps_the_number_for_a_known_name(self):
        first = self.env.anonymize(("План Кедровый.txt", "а"))
        other = self.env.anonymize(("Другой.txt", "б"))
        again = self.env.anonymize(("План Кедровый.txt", "в"))
        self.assertEqual(first.files[0].out_name, again.files[0].out_name)
        self.assertEqual(other.files[0].out_name, "Файл2 (обезличено).txt")
        rerun = self.env.service.rerun(again.id, {}, {})
        self.assertEqual(rerun.files[0].out_name, first.files[0].out_name)
        reopened = self.env.reopen()
        job = reopened.anonymize(("План Кедровый.txt", "г"))
        self.assertEqual(job.files[0].out_name, first.files[0].out_name)

    def test_odd_and_long_names_come_back(self):
        for name in ("Договор №5 — «Кедровый Лог» (финал) v2.final.txt", "Ёжик_в_тумане ✓.txt",
                     "План " + "очень длинное название " * 8 + ".txt", ".скрытый.txt"):
            with self.subTest(name=name):
                env = Env()
                try:
                    job = env.anonymize((name, "Текст без имён."))
                    self.assertRegex(job.files[0].out_name, r"^Файл\d+ \(обезличено\)\.txt$")
                    restored = env.restore(env.as_upload(job))
                    stem = safe_name(name).rsplit(".", 1)[0]
                    self.assertEqual(restored.files[0].out_name, f"{stem} (восстановлено).txt")
                finally:
                    env.close()

    def test_restore_keeps_what_the_model_appended_to_the_name(self):
        job = self.env.anonymize(("Отчёт Кедровый.txt", "а"))
        upload = self.env.as_upload(job, name="Файл1 (обезличено)_v2.txt")
        restored = self.env.restore(upload)
        self.assertEqual(restored.files[0].out_name, "Отчёт Кедровый_v2 (восстановлено).txt")

    def test_original_name_never_reaches_the_output(self):
        secret = "Кедровый_Лог_тайный_план"
        files = {
            f"{secret}.txt": "Заметка о поставке.".encode(),
            f"{secret}.docx": docx_bytes(["Заметка о поставке."]),
            f"{secret}.xlsx": xlsx_bytes({"A1": "Заметка о поставке."}),
            f"{secret}.pptx": pptx_bytes(["Заметка о поставке."]),
            f"{secret}.pdf": pdf_bytes("Note about delivery"),
        }
        job = self.env.anonymize(*files.items())
        for index, outcome in enumerate(job.files):
            with self.subTest(file=outcome.out_name):
                self.assertNotIn("Кедровый", outcome.out_name)
                path = self.env.output(job, index)
                raw = path.read_bytes()
                blob = zip_blob(path) if zipfile.is_zipfile(path) else raw
                if path.suffix == ".pdf":
                    doc = fitz.open(path)
                    blob += repr(doc.metadata).encode() + "".join(p.get_text() for p in doc).encode()
                    doc.close()
                for fragment in ("Кедровый", "тайный"):
                    for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
                        self.assertNotIn(fragment.encode(encoding), blob)
        restored = self.env.restore(*[self.env.as_upload(job, i) for i in range(len(files))])
        self.assertEqual(sorted(f.out_name for f in restored.files),
                         sorted(f"{secret} (восстановлено){Path(n).suffix}" for n in files))


class PageTests(unittest.TestCase):
    def test_both_options_are_wired_like_numbers(self):
        html = WEB.read_text(encoding="utf-8")
        for control, option, pref in (("opt-countries", "countries", "pref-countries"),
                                      ("opt-names", "neutral_names", "pref-names")):
            self.assertIn(f'id="{control}"', html)
            self.assertIn(f'{option}: $("#{control}").checked', html)
            self.assertIn(f'$("#{control}").checked = !!STATE.prefs.{option}', html)
            self.assertIn(f'{option}: $("#{pref}").checked', html)
            self.assertLess(html.index('id="opt-numbers"'), html.index(f'id="{control}"'))
            self.assertLess(html.index(f'id="{control}"'), html.index('id="btn-anon"'))
        self.assertIn("Скрывать страны", html)
        self.assertIn("Заменять названия файлов нейтральными", html)


if __name__ == "__main__":
    unittest.main()
