"""Раунд 3, поток B: люди, списки, латиница и почта (находки B1–B9). Все имена и названия выдуманы."""
import re
import unittest
from pathlib import Path

from anonymizer.detectors import Detector
from anonymizer.models import Decision, Settings

from .helpers import Env, content_units, xlsx_bytes

NAME = re.compile(r"Name\d+(?:_\d+)?")


def cells(texts: list[str], env: Env | None = None) -> list[str]:
    """Каждый текст — отдельная ячейка одного листа; возвращает обезличенные ячейки по порядку."""
    env = env or Env()
    data = xlsx_bytes({f"A{i + 1}": t for i, t in enumerate(texts)})
    job = env.anonymize(("Лист.xlsx", data))
    out = content_units(Path(job.files[0].out_path))
    return [out.get(f"Лист1!A{i + 1}") for i in range(len(texts))]


def base(token: str) -> str:
    return token.split("_")[0]


def findings(texts: list[str], index: int):
    detector = Detector([], Settings())
    detector.harvest("\n".join(texts), "f")
    return detector.scan(texts[index], "f", "x")


class B1AdjectiveAfterRole(unittest.TestCase):
    def test_unit_adjective_after_a_position_is_not_a_person(self):
        texts = ["Начальник Транспортного отдела", "Директор Финансового департамента",
                 "Руководитель Правового и Кадрового управлений", "Главный бухгалтер Казанского офиса",
                 "Директор Волжского филиала"]
        for source, out in zip(texts, cells(texts)):
            with self.subTest(source=source):
                self.assertNotRegex(out, NAME)

    def test_unit_adjective_is_not_learned_as_a_surname(self):
        out = cells(["Начальник Транспортного отдела", "Отчёт Транспортного отдела за май"])
        self.assertEqual(out, ["Начальник Транспортного отдела", "Отчёт Транспортного отдела за май"])

    def test_surname_after_a_position_is_still_hidden(self):
        out = cells(["Директор Громов", "Директор Громов П.С.", "Начальник Абдурахманов подписал"])
        self.assertRegex(out[0], r"^Директор Name\d+$")
        self.assertRegex(out[1], r"^Директор Name\d+(_\d+)?$")
        self.assertRegex(out[2], r"^Начальник Name\d+ подписал$")


class B2SettlementAfterCapitals(unittest.TestCase):
    def test_settlement_after_unit_abbreviation_is_not_a_person(self):
        for text in ("ОП Полево", "Администрация ОП Липкино"):
            with self.subTest(text=text):
                out = cells([text])[0]
                self.assertNotRegex(out, NAME)
                self.assertIn("ОП", out)

    def test_capital_pair_before_three_different_words_is_an_abbreviation(self):
        self.assertRegex(cells(["ГС Рапшин"])[0], r"^Name\d+$")        # одна пара — ещё инициалы
        out = cells(["ГС Рапшин", "ГС Дубровка", "ГС Бусыгино", "ГС Смородинка"])
        self.assertTrue(all(o.startswith("ГС ") and not NAME.search(o) for o in out), out)

    def test_glued_initials_after_a_surname_still_work(self):
        self.assertRegex(cells(["Громов АВ"])[0], r"^Name\d+$")


class B3LabelAfterInitial(unittest.TestCase):
    def test_label_after_a_dotted_initial_is_not_swallowed(self):
        out = cells(["Жаров А.. ЮГ - Полякова Е."])[0]
        self.assertRegex(out, r"^Name\d+\. ЮГ - Name\d+$")

    def test_initials_do_not_cross_a_list_separator(self):
        out = cells(["Громов И.И.; Метла Д.В.; Жаров А.А."])[0]
        self.assertRegex(out, r"^Name\d+; Name\d+; Name\d+(_\d+)?$")


class B4NoBindingAcrossStrangers(unittest.TestCase):
    def test_given_name_next_to_a_foreign_surname_is_not_the_named_person(self):
        out = cells(["Лапшин Игорь Петрович", "Верн Игорь", "Верн Игорь; Лапшин Игорь Петрович"])
        self.assertEqual(out[1], "Верн Игорь")
        self.assertRegex(out[2], r"^Верн Игорь; Name\d+(_\d+)?$")

    def test_lone_given_name_still_binds_to_the_only_person(self):
        out = cells(["Лапшин Игорь Петрович", "Игорь, добрый день"])
        self.assertEqual(base(out[1].split(",")[0]), base(out[0]))


class B5ListCells(unittest.TestCase):
    def test_common_word_surname_with_dotted_initials(self):
        self.assertRegex(cells(["Калина Д.В."])[0], r"^Name\d+$")

    def test_heading_with_initials_keeps_its_behaviour_without_a_list(self):
        self.assertEqual(cells(["Логист А.П.", "Поставка К.Т."]), ["Логист А.П.", "Поставка К.Т."])

    def test_lone_surnames_in_a_list_of_people(self):
        out = cells(["Участники: Громов Пётр Ильич, Ракитин, Сомов, Жарова Анна Сергеевна, Поляков"])[0]
        self.assertRegex(out, r"^Участники: Name\d+, Name\d+, Name\d+, Name\d+, Name\d+$")

    def test_ordinary_words_in_a_list_of_people_stay(self):
        out = cells(["Итоги: Громов Пётр Ильич, Жарова Анна Сергеевна, Склад, Логистика", "Громов Пётр Ильич, Машина"])
        self.assertRegex(out[0], r"^Итоги: Name\d+, Name\d+, Склад, Логистика$")
        self.assertRegex(out[1], r"^Name\d+, Машина$")


class B6UnusualPairs(unittest.TestCase):
    def test_rare_surname_next_to_a_given_name_goes_to_review(self):
        found = findings(["Верн Игорь"], 0)
        self.assertEqual([(f.original, f.category, f.decision) for f in found],
                         [("Верн Игорь", "POSSIBLE_PERSON", Decision.REVIEW)])

    def test_surname_read_as_a_given_name_next_to_a_given_name(self):
        found = findings(["Лаури Геннадий"], 0)
        self.assertEqual([(f.category, f.decision) for f in found], [("POSSIBLE_PERSON", Decision.REVIEW)])
        out = cells(["Крылов Пётр Ильич, Жукова Анна Сергеевна, Лаури Геннадий, Баранов Олег Иванович"])[0]
        self.assertRegex(out, r"^Name\d+, Name\d+, Name\d+, Name\d+$")

    def test_departments_and_places_in_a_list_of_people_stay(self):
        out = cells(["Отдел продаж; Сервис Запад; Крылов Пётр Ильич; Жукова Анна Сергеевна",
                     "Крылов Пётр Ильич, Жукова Анна Сергеевна, Отдел Павел, Вера Надежда"])
        self.assertRegex(out[0], r"^Отдел продаж; Сервис Запад; Name\d+; Name\d+$")
        self.assertRegex(out[1], r"^Name\d+, Name\d+, Отдел Павел, Вера Надежда$")

    def test_ordinary_word_next_to_a_given_name_is_not_flagged(self):
        for text in ("Отдел Павел", "Приказ Игорь"):
            with self.subTest(text=text):
                self.assertFalse([f for f in findings([text], 0) if f.category == "POSSIBLE_PERSON"])


class B7LatinSpelling(unittest.TestCase):
    PAIRS = [("Журавлёв Александр Петрович", "Zhuravlev Aleksander"), ("Журавлёв Олег Ильич", "Zhuravlev Oleg"),
             ("Полякова Наталия Игоревна", "Polyakova Natalya"), ("Губарев Евгений Павлович", "Gubarev Eugeniy"),
             ("Кожемякин Леонид Васильевич", "Leonid V. Kozhemyakin"), ("Лифшиц Елена Олеговна", "Lifshits Elena")]

    def test_latin_row_gets_the_token_of_the_cyrillic_row(self):
        texts = [t for pair in self.PAIRS for t in pair]
        out = cells(texts)
        for k, (cyrillic, latin) in enumerate(self.PAIRS):
            with self.subTest(latin=latin):
                self.assertRegex(out[2 * k + 1], r"^Name\d+_\d+$")
                self.assertEqual(base(out[2 * k + 1]), base(out[2 * k]))

    def test_no_first_name_left_next_to_a_hidden_latin_surname(self):
        out = cells(["Губарев Евгений Павлович", "Gubarev Xavier", "Смирнов Олег Петрович", "Smirnov Report"])
        self.assertRegex(out[1], r"^Name\d+(_\d+)?$")
        self.assertRegex(out[3], r"^Name\d+(_\d+)? Report$")


class B8EmailWithStrayPunctuation(unittest.TestCase):
    def test_stray_semicolon_before_at(self):
        out = cells(["Писать: petrushkin;@yandex.ru", "пишите в телеграм @ivan_gromov", "сайт @yandex.ru"])
        self.assertEqual(out[0], "Писать: email1@example.com")
        self.assertRegex(out[1], r"^пишите в телеграм Handle\d+$")
        self.assertEqual(out[2], "сайт @yandex.ru")


class RoundTrip(unittest.TestCase):
    def test_every_new_behaviour_restores_exactly(self):
        texts = ["Директор Громов П.С.", "Начальник Транспортного отдела", "ОП Полево", "Жаров А.. ЮГ - Полякова Е.",
                 "Громов И.И.; Метла Д.В.; Жаров А.А.", "Участники: Громов Пётр Ильич, Ракитин, Сомов, Поляков",
                 "Журавлёв Александр Петрович", "Zhuravlev Aleksander", "Leonid V. Kozhemyakin",
                 "Кожемякин Леонид Васильевич", "Gubarev Xavier", "Губарев Евгений Павлович", "Писать: petrushkin;@yandex.ru"]
        env = Env()
        data = xlsx_bytes({f"A{i + 1}": t for i, t in enumerate(texts)})
        job = env.anonymize(("Лист.xlsx", data))
        back = env.restore(env.as_upload(job, 0, "Лист.xlsx"))
        restored = content_units(Path(back.files[0].out_path))
        self.assertEqual([restored.get(f"Лист1!A{i + 1}") for i in range(len(texts))], texts)


if __name__ == "__main__":
    unittest.main()
