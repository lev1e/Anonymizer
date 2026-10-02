import unittest

from anonymizer.detectors import Detector
from anonymizer.directory import person_from_name
from anonymizer.models import Decision, Settings


def scan(people, text, settings=None):
    detector = Detector(people, settings or Settings.persons_only())
    detector.harvest(text)
    return detector, detector.scan(text, "x", "body")


def originals(findings, category="PERSON", decision=Decision.AUTO):
    return [f.original for f in findings if f.category == category and f.decision == decision]


class NameTests(unittest.TestCase):
    def setUp(self):
        self.ivanov = person_from_name("Иванов Иван Иванович", department="Логистика")
        self.igor = person_from_name("Иванов Игорь Петрович", department="ИТ")
        self.maria = person_from_name("Петрова Мария Сергеевна", department="Закупки")

    def test_oblique_cases_with_initials(self):
        """The dative/genitive/instrumental plus initials is the default form in Russian
        paperwork; missing it was the single largest leak in the previous engine."""
        cases = {
            "Иванову И.И. направлено письмо": "Иванову И.И.",
            "От Иванова И.И. получено согласие": "Иванова И.И.",
            "Согласовано с Ивановым И.И.": "Ивановым И.И.",
            "Иванов И.И. подписал акт": "Иванов И.И.",
            "И.И. Иванов подписал акт": "И.И. Иванов",
            "И. И. Иванову передано": "И. И. Иванову",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                _, findings = scan([self.ivanov], text)
                self.assertIn(expected, originals(findings))

    def test_declension_covers_possessive_surnames(self):
        for text in ("Ивановым И.И.", "Иванову", "Иванове И.И."):
            with self.subTest(text=text):
                _, findings = scan([self.ivanov], text)
                self.assertTrue(originals(findings), f"не найдено в {text!r}")

    def test_fleeting_vowel_given_names(self):
        person = person_from_name("Петров Пётр Павлович")
        _, findings = scan([person], "Согласовано с Петром Петровым")
        self.assertTrue(originals(findings))

    def test_shared_initials_stay_ambiguous(self):
        _, findings = scan([self.ivanov, self.igor], "Иванов И. подписал")
        review = [f for f in findings if f.decision == Decision.REVIEW]
        self.assertEqual(len(review), 1)
        self.assertEqual(len(review[0].candidates), 2)

    def test_distinct_initials_disambiguate(self):
        _, findings = scan([self.ivanov, self.igor], "Иванов И.П. и Иванов И.И.")
        auto = [f for f in findings if f.decision == Decision.AUTO]
        self.assertEqual(len(auto), 2)
        self.assertNotEqual(auto[0].person_id, auto[1].person_id)

    def test_whitespace_and_homoglyphs_do_not_hide_a_name(self):
        for text in ("Иванов  Иван   Иванович", "Иванов Иван Иванович",
                     "Иванов\nИван Иванович", "Ивaнов Иван Иванович"):
            with self.subTest(text=text):
                _, findings = scan([self.ivanov], text)
                self.assertTrue(originals(findings), f"не найдено в {text!r}")

    def test_double_surname_declines_on_both_halves(self):
        person = person_from_name("Иванов-Петров Иван Иванович")
        _, findings = scan([person], "Иванову-Петрову И.И. поручено")
        self.assertIn("Иванову-Петрову И.И.", originals(findings))

    def test_transliterated_spellings(self):
        person = person_from_name("Мария Иванова")
        for text in ("Maria Ivanova", "Ivanova Maria", "M. Ivanova"):
            with self.subTest(text=text):
                _, findings = scan([person], text)
                self.assertTrue(originals(findings), f"не найдено в {text!r}")

    def test_learns_full_name_from_the_document(self):
        """No directory at all: the document's own full name resolves its abbreviations."""
        text = "Назначен Иванов Иван Иванович. Далее Иванову И.И. поручено, согласовано с Ивановым И.И."
        detector, findings = scan([], text)
        self.assertEqual(len(detector.discovered), 1)
        found = originals(findings)
        self.assertIn("Иванову И.И.", found)
        self.assertIn("Ивановым И.И.", found)

    def test_learning_can_be_switched_off(self):
        """The flag stops building a person registry; it never stops replacing a name.
        Recognition by shape is a safety floor, not a convenience feature."""
        text = "Иванов Иван Иванович. Далее Иванову И.И."
        detector, findings = scan([], text, Settings(learn_names=False))
        self.assertEqual(detector.discovered, {})
        self.assertIsNone(next(f for f in findings if f.original == "Иванову И.И.").person_id)

    def test_bare_initials_are_replaced_without_any_directory(self):
        """A file that only ever abbreviates names must not ship as clean."""
        detector = Detector([], Settings())
        findings = detector.scan("Согласовано с Ивановым И.И. и Петровой М.С.", "x", "body")
        self.assertEqual(originals(findings), ["Ивановым И.И.", "Петровой М.С."])

    def test_two_people_with_one_surname_keep_separate_identities(self):
        text = "Иванов Иван Иванович и Иванов Игорь Петрович. Далее Иванову И.И. и Иванову И.П."
        detector, findings = scan([], text)
        self.assertEqual(len(detector.discovered), 2)
        by_text = {f.original: f.person_id for f in findings if f.decision == Decision.AUTO}
        self.assertEqual(by_text["Иванову И.И."], by_text["Иванов Иван Иванович"])
        self.assertEqual(by_text["Иванову И.П."], by_text["Иванов Игорь Петрович"])
        self.assertNotEqual(by_text["Иванову И.И."], by_text["Иванову И.П."])

    def test_learns_patronymic_for_a_directory_person(self):
        person = person_from_name("Мария Иванова")
        text = "Иванова Мария Сергеевна утвердила. Далее Ивановой М.С. поручено."
        _, findings = scan([person], text)
        self.assertIn("Ивановой М.С.", originals(findings))

    def test_business_prose_produces_no_findings(self):
        text = ("Договор Поставки заключён между Обществом Ограниченной Ответственности и Акционерным "
                "Обществом. Российская Федерация, Московская Область. Приложение Номер Два. "
                "Генеральный Директор утвердил Положение. Настоящим Стороны подтверждают.")
        _, findings = scan([self.ivanov], text)
        self.assertEqual(findings, [])

    def test_full_name_outside_the_directory_is_learned_and_replaced(self):
        """A surname, a given name and a patronymic together are unambiguous enough to act on:
        leaving them for manual review is what used to bury every file in the review pile."""
        detector, findings = scan([self.ivanov], "Сидоров Пётр Алексеевич не в справочнике")
        self.assertIn("Сидоров Пётр Алексеевич", originals(findings))
        self.assertEqual(len(detector.discovered), 1)

    def test_two_surname_shaped_words_are_not_a_finding(self):
        """«Снова Смирнов» и «Причина Иванова» неотличимы от двух фамилий подряд.

        Такая пара без имени или отчества рядом опознанию не поддаётся, и раньше она
        засоряла ручную проверку. Настоящее ФИО в этой форме встречается редко и всё равно
        распознаётся, как только рядом появляется имя, отчество или инициал.
        """
        detector = Detector([self.ivanov], Settings())
        self.assertEqual(detector.scan("Снова Смирнов подписали", "x", "body"), [])
        self.assertEqual(detector.scan("Причина Кузнецова указана", "x", "body"), [])

    def test_name_shape_without_learning_stays_for_review(self):
        detector = Detector([self.ivanov], Settings())
        findings = detector.scan("Сидоров Пётр Алексеевич", "x", "path:0")
        self.assertEqual([f.category for f in findings], ["POSSIBLE_PERSON"])
        self.assertEqual(findings[0].decision, Decision.REVIEW)

    def test_typo_is_review_with_candidate(self):
        _, findings = scan([person_from_name("Мария Иванова")], "Мария Иваонва")
        review = [f for f in findings if f.decision == Decision.REVIEW]
        self.assertTrue(review)
        self.assertEqual(review[0].reason, "Возможная опечатка в фамилии")
        self.assertTrue(review[0].candidates)

    def test_surname_after_a_street_marker_is_not_a_person(self):
        _, findings = scan([self.maria], "офис на ул. Петровой")
        self.assertEqual(originals(findings), [])

    def test_full_namesake_is_ambiguous(self):
        a, b = person_from_name("Иван Петров"), person_from_name("Иван Петров")
        hit = next(f for f in scan([a, b], "Иван Петров")[1] if f.category == "PERSON")
        self.assertEqual(hit.decision, Decision.REVIEW)


class WithoutAnyDirectoryTests(unittest.TestCase):
    """Что происходит с человеком, которого нет ни в каком справочнике."""

    def _scan(self, text):
        detector = Detector([], Settings())
        detector.harvest(text, "f")
        return [f for f in detector.scan(text, "f", "body") if f.decision == Decision.AUTO]

    def test_shapes_that_are_recognised(self):
        cases = {
            "Ответственный Сидоров Пётр Алексеевич подписал": "Сидоров Пётр Алексеевич",
            "Ответственный Кузнецов Ыгыа Ньургунович подписал": "Кузнецов Ыгыа Ньургунович",
            "Ответственный Пётр Сидоров подписал": "Пётр Сидоров",
            "Ответственный Сидоров П.А. подписал": "Сидоров П.А.",
            "Ответственный П.А. Сидоров подписал": "П.А. Сидоров",
            "Ответственный Сидоров ПА подписал": "Сидоров ПА",
            "Согласовано с Сидоровым П.А.": "Сидоровым П.А.",
            "Ответственный Шпац О.В. подписал": "Шпац О.В.",
            "Ответственный Иванов-Петров И.И. подписал": "Иванов-Петров И.И.",
            "Уважаемый Пётр Алексеевич, направляем акт": "Пётр Алексеевич",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertIn(expected, [f.original for f in self._scan(text)])

    def test_salutation_is_not_taken_for_a_surname(self):
        """«Уважаемый Пётр Алексеевич» — обращение: заменить надо имя с отчеством, но не его."""
        found = [f.original for f in self._scan("Уважаемый Пётр Алексеевич, направляем акт")]
        self.assertEqual(found, ["Пётр Алексеевич"])

    def test_adjectival_surnames_still_work(self):
        """Толстая и Грозный — настоящие фамилии, а не прилагательные."""
        for text in ("Толстая Мария Ивановна подписала", "Грозный Иван Васильевич утвердил",
                     "Толстая О.В. подписала"):
            with self.subTest(text=text):
                self.assertTrue(self._scan(text))

    def test_shapes_that_are_documented_as_missed(self):
        """Известные границы: без опоры рядом слово неотличимо от обычного."""
        for text in ("Ответственный Пётр подписал акт",
                     "Ответственный Ли Вэй подписал акт"):
            with self.subTest(text=text):
                self.assertEqual(self._scan(text), [])


class DirectoryParsingTests(unittest.TestCase):
    def test_surname_ending_in_ich_is_not_taken_for_a_patronymic(self):
        for name in ("Рабинович Иван Петрович", "Абрамович Роман Аркадьевич", "Шостакович Дмитрий Дмитриевич"):
            with self.subTest(name=name):
                person = person_from_name(name)
                self.assertEqual(person.surname, name.split()[0])
                self.assertEqual(person.patronymic, name.split()[2])

    def test_given_name_first_layout(self):
        person = person_from_name("Иван Иванович Иванов")
        self.assertEqual((person.surname, person.given_name, person.patronymic), ("Иванов", "Иван", "Иванович"))


class StructuredDataTests(unittest.TestCase):
    def test_valid_identifiers_are_detected(self):
        text = ("Почта user@example.org, телефон +7 (999) 123-45-67, СНИЛС 112-233-445 95, "
                "ИНН 500100732259, карта 4012 8888 8888 1881, @telegram_user, 192.168.10.4")
        categories = {f.category for f in Detector([], Settings()).scan(text, "x", "body")}
        self.assertTrue({"EMAIL", "PHONE", "SNILS", "INN_PERSON", "CARD", "USERNAME", "IP_ADDRESS"}.issubset(categories),
                        categories)

    def test_plain_numbers_are_not_personal_data(self):
        findings = Detector([], Settings()).scan("Заказ 1234567890, позиция 123456789012, строка 40702", "x", "body")
        self.assertEqual(findings, [])

    def test_address_survives_a_name_inside_it(self):
        person = person_from_name("Иванов Иван Иванович")
        text = "Адрес: г. Москва, ул. Ленина, д.5, кв.7, Иванов Иван Иванович"
        findings = Detector([person], Settings()).scan(text, "x", "body")
        self.assertEqual({f.category for f in findings}, {"ADDRESS", "PERSON"})
        address = next(f for f in findings if f.category == "ADDRESS")
        self.assertIn("Ленина", address.original)

    def test_lone_old_date_is_a_possible_birth_date_in_a_birth_context(self):
        detector = Detector([], Settings())
        detector.harvest("ФИО | Дата рождения | Отдел", "x")
        findings = detector.scan("12.05.1985", "x", "cell")
        self.assertEqual([f.category for f in findings], ["BIRTH_DATE"])
        self.assertEqual(findings[0].decision, Decision.REVIEW)

    def test_lone_old_date_without_birth_context_is_left_alone(self):
        """A register of old contract dates must not turn every row into a review item."""
        detector = Detector([], Settings())
        detector.harvest("Реестр договоров | Дата | Сумма", "x")
        self.assertEqual(detector.scan("12.05.2005", "x", "cell"), [])

    def test_recent_lone_date_is_left_alone(self):
        detector = Detector([], Settings())
        detector.harvest("Дата рождения", "x")
        self.assertEqual(detector.scan("12.05.2024", "x", "cell"), [])

    def test_secret_is_permanent_by_default(self):
        hits = Detector([], Settings()).scan("api_key=abcdefghijklmnopqrstuvwxyz123456", "x", "body")
        secret = next(f for f in hits if f.category == "SECRET")
        self.assertTrue(secret.permanently_removed)

    def test_business_mode_is_opt_in(self):
        text = "Договор № A-42, расчётный счёт 40702123456789012345, маржа 18%"
        self.assertFalse(any(f.category == "BUSINESS" for f in Detector([], Settings()).scan(text, "x", "body")))
        settings = Settings(business_confidential=True)
        self.assertTrue(any(f.category == "BUSINESS" for f in Detector([], settings).scan(text, "x", "body")))


if __name__ == "__main__":
    unittest.main()
