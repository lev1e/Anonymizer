"""Четыре течи детектора: ИНН без контекста, адрес до конца строки, уменьшительные имена
и латинские имена. Каждый класс проверяет и саму находку, и то, чем за неё платим —
ложные срабатывания на обычном деловом тексте."""

import unittest

from anonymizer.detectors import Detector
from anonymizer.models import Decision, Settings


def scan(text, settings=None):
    detector = Detector([], settings or Settings.persons_only())
    detector.harvest(text, "x")
    return detector.scan(text, "x", "body")


def categories(findings):
    return {f.category for f in findings}


def one(findings, category):
    return next(f for f in findings if f.category == category)


class InnContextTests(unittest.TestCase):
    """Контрольную сумму ИНН проходит около одного из ста двенадцатизначных чисел, поэтому
    без слова «ИНН» рядом номер заказа уходил в автозамену наравне с настоящим ИНН."""

    def test_bare_twelve_digits_are_not_an_inn(self):
        findings = scan("Заказ №123456789047 передан в доставку.")
        self.assertEqual(findings, [])

    def test_inn_with_keyword_is_still_detected(self):
        findings = scan("ИНН 500100732259 указан в договоре.")
        self.assertEqual([f.category for f in findings], ["INN_PERSON"])
        self.assertEqual(findings[0].decision, Decision.AUTO)
        self.assertEqual(findings[0].original, "500100732259")

    def test_inn_survives_a_short_gap_after_the_keyword(self):
        findings = scan("ИНН плательщика: 500100732259.")
        self.assertIn("INN_PERSON", categories(findings))

    def test_invoice_numbers_stay_untouched(self):
        text = "Счёт 123456789047 от 12.03.2024, накладная 210987654321, позиция 123456789012."
        self.assertEqual(scan(text), [])


class AddressBoundaryTests(unittest.TestCase):
    """`[^\\n;]{8,180}` тянулся до конца строки и уносил остаток предложения."""

    def test_address_stops_at_the_flat_number(self):
        text = ("Адрес: г. Москва, ул. Ленина, д. 5, кв. 12, а также сообщаем, "
                "что собрание состоится завтра в офисе компании.")
        address = one(scan(text), "ADDRESS")
        self.assertEqual(address.original, "г. Москва, ул. Ленина, д. 5, кв. 12")   # подпись «Адрес:» остаётся в тексте

    def test_multipart_address_survives_whole(self):
        text = "Адрес: 190000, г. Санкт-Петербург, ул. Мира, д. 10, корп. 2, оф. 305."
        address = one(scan(text), "ADDRESS")
        self.assertIn("корп. 2", address.original)
        self.assertIn("оф. 305", address.original)

    def test_registration_address_is_still_caught(self):
        text = "Зарегистрирован: г. Казань, ул. Баумана, дом 7, кв. 3, паспорт выдан в 2015 году."
        address = one(scan(text), "ADDRESS")
        self.assertTrue(address.original.endswith("кв. 3"), address.original)

    def test_house_number_without_the_abbreviation(self):
        """«ул. Тверская, 7» — номер дома без «д.», и замыкание обязано его узнавать."""
        text = "Адрес: 125009, г. Москва, ул. Тверская, 7"
        self.assertEqual(one(scan(text), "ADDRESS").original, text.split(": ", 1)[1])

    def test_sentence_after_a_colon_without_an_address_is_not_one(self):
        for text in ("Адрес: уточняется, ответ направим в течение трёх рабочих дней.",
                     "Адрес: уточняется, ответ будет дан в течение 3 рабочих дней.",
                     "Адрес доставки согласован, отгрузка 5 марта."):
            with self.subTest(text=text):
                self.assertNotIn("ADDRESS", categories(scan(text)), text)


class DiminutiveNameTests(unittest.TestCase):
    """Уменьшительное имя — единственное упоминание человека в переписке, и раньше оно не
    находилось вовсе."""

    def test_diminutive_in_a_sentence(self):
        findings = scan("Сегодня к нам заходил Саша и принёс документы.")
        person = one(findings, "POSSIBLE_PERSON")
        self.assertEqual(person.original, "Саша")
        self.assertEqual(person.decision, Decision.REVIEW)

    def test_diminutive_at_the_start_of_a_sentence(self):
        findings = scan("Вова обещал перезвонить завтра.")
        self.assertEqual([f.original for f in findings], ["Вова"])

    def test_oblique_case_is_detected(self):
        findings = scan("Передайте Кате отчёт до пятницы.")
        self.assertEqual([f.original for f in findings], ["Кате"])

    def test_diminutive_never_goes_to_auto_replace(self):
        for text in ("Заходил Саша.", "Вова обещал перезвонить.", "Позвони Мише вечером."):
            with self.subTest(text=text):
                findings = scan(text)
                self.assertTrue(findings, text)
                self.assertTrue(all(f.decision == Decision.REVIEW for f in findings), findings)

    def test_common_nouns_that_look_like_diminutives_are_left_alone(self):
        text = "Слава компании держится на качестве, а поля таблицы заполнены полностью."
        self.assertEqual(scan(text), [])

    def test_ordinary_business_prose_stays_clean(self):
        text = ("Заявка на поставку канцелярских товаров согласована отделом снабжения. "
                "Договор аренды нежилого помещения подписан обеими сторонами. "
                "Оплата производится в течение десяти рабочих дней с даты выставления счёта.")
        self.assertEqual(scan(text), [])


class LatinNameTests(unittest.TestCase):
    """Кириллический разбор латиницу не видит совсем: «John Smith» не давал находки."""

    def test_latin_pair_with_a_known_given_name_is_replaced(self):
        findings = scan("Контракт подписал John Smith от лица компании.")
        person = one(findings, "PERSON")
        self.assertEqual(person.original, "John Smith")
        self.assertEqual(person.decision, Decision.AUTO)

    def test_latin_pair_without_a_known_given_name_goes_to_review(self):
        findings = scan("Со стороны заказчика присутствовал Zorblat Quimby.")
        self.assertEqual([f.original for f in findings], ["Zorblat Quimby"])
        self.assertEqual(findings[0].decision, Decision.REVIEW)

    def test_surname_of_a_latin_person_is_found_alone_later(self):
        findings = scan("Peter Brown подписал договор. Позднее Brown подтвердил оплату.")
        self.assertEqual([f.original for f in findings], ["Peter Brown", "Brown"])
        self.assertTrue(all(f.decision == Decision.AUTO for f in findings))

    def test_sentence_boundary_stops_a_latin_pair(self):
        findings = scan("Договор подписал John Smith. Later Agreement was archived.")
        self.assertEqual([f.original for f in findings], ["John Smith"])

    def test_product_names_are_not_people(self):
        for text in ("Мы используем Microsoft Office для подготовки отчётов.",
                     "На сервере установлен Windows Server последней версии.",
                     "Файлы открываются через Adobe Acrobat Reader.",
                     "Оборудование поставила компания IBM.",
                     "Филиал открыт в New York в прошлом году."):
            with self.subTest(text=text):
                self.assertEqual(scan(text), [], text)

    def test_all_caps_abbreviations_are_not_people(self):
        self.assertEqual(scan("Отчёты в форматах PDF DOCX принимаются оба."), [])

    def test_three_capitalised_words_in_a_row_are_a_product(self):
        self.assertEqual(scan("Установлен пакет Corel Draw Suite на рабочем месте."), [])

    def test_english_document_is_not_flooded(self):
        text = "The Purchase Order was signed and delivered by the Supplier."
        self.assertEqual(scan(text), [])


class LoneSurnameTests(unittest.TestCase):
    """Фамилия человека, которого в файле ни разу не назвали полностью.

    «Сегодня Воронцов представил отчёт» не подходило ни под одно контекстное правило: слева
    наречие, справа обычный глагол. Находки не возникало вовсе, и фамилия молча уезжала в
    выгрузку. Опорой здесь служит словарь: слово разобрано как фамилия, взято из словаря и
    обычного чтения не имеет. Автозамена по такому доводу недопустима — только ручная проверка.
    """

    def check(self, text, word):
        findings = scan(text)
        self.assertEqual([f.original for f in findings], [word], text)
        self.assertEqual(findings[0].category, "POSSIBLE_PERSON")
        self.assertEqual(findings[0].decision, Decision.REVIEW)
        self.assertLess(findings[0].confidence, .7)
        return findings[0]

    def test_surname_after_an_adverb(self):
        self.check("Сегодня Воронцов представил отчёт совету директоров.", "Воронцов")

    def test_surname_after_an_ordinary_verb(self):
        self.check("Отчёт подготовил Мельников.", "Мельников")

    def test_surname_after_the_preposition_s(self):
        """«с» — это и предлог, и сокращение «село». Без точки это предлог, и за ним человек."""
        self.check("Согласовано с Ковалёвой.", "Ковалёвой")

    def test_surname_in_an_oblique_case(self):
        self.check("Обязанности возложены на Хабарова.", "Хабарова")

    def test_surname_opening_the_sentence(self):
        self.check("Гуськова сообщила о переносе совещания.", "Гуськова")

    def test_finding_is_never_automatic(self):
        for text in ("Заявку рассмотрел Терентьев вчера.",
                     "Ключи от кабинета у Мещерякова.",
                     "Комментарии Пивоварова приложены к письму."):
            for finding in scan(text):
                self.assertEqual(finding.decision, Decision.REVIEW, text)

    def test_village_abbreviation_still_suppresses_the_word(self):
        """С точкой «с.» остаётся селом, и название посёлка человеком не становится."""
        self.assertEqual(scan("Отгружено в с. Ковалёво в срок."), [])

    def test_full_name_keeps_its_automatic_decision(self):
        findings = scan("Иванов Иван Иванович подписал акт приёмки.")
        self.assertEqual([f.original for f in findings], ["Иванов Иван Иванович"])
        self.assertEqual(findings[0].decision, Decision.AUTO)
        self.assertGreater(findings[0].confidence, .9)

    def test_known_person_still_resolves_by_bare_surname(self):
        text = "Петров Пётр Петрович утвердил смету. Позже Петров уточнил сумму."
        detector = Detector([], Settings())
        detector.harvest(text, "x")
        findings = detector.scan(text, "x", "body")
        bare = [f for f in findings if f.original == "Петров"]
        self.assertEqual(len(bare), 1)
        self.assertEqual(bare[0].category, "PERSON")
        self.assertTrue(bare[0].person_id)


class LoneSurnameFalsePositiveTests(unittest.TestCase):
    """Цена правила. Заглавное слово фамильного вида есть в каждом втором деловом абзаце,
    и список ручной проверки, забитый городами и заголовками, никто разбирать не станет."""

    def assert_clean(self, *texts):
        for text in texts:
            self.assertEqual(scan(text), [], text)

    def test_cities_and_countries(self):
        self.assert_clean("Оборудование отгружено со склада в Кирове и доставлено в Тамбов.",
                          "Ростов и Воронеж включены в маршрут доставки.",
                          "Пушкин включён в перечень городов присутствия компании.",
                          "Королёв и Одинцово вошли в зону обслуживания центра.",
                          "Россия и Казахстан входят в таможенный союз.")

    def test_regions_spelled_as_adjectives(self):
        self.assert_clean("Российская Федерация, Московская Область указаны в реквизитах.",
                          "Ленинградская Область включена в отчёт за квартал.")

    def test_company_and_brand_names(self):
        self.assert_clean("Сбербанк и Газпром указаны в реестре контрагентов.",
                          "Аэрофлот подтвердил бронирование билетов.",
                          "Ростелеком направил счёт за услуги связи.")

    def test_months_and_weekdays(self):
        self.assert_clean("Сентябрь закрыт по всем подразделениям.",
                          "Понедельник объявлен рабочим днём.",
                          "Декабрь традиционно даёт пик отгрузок.")

    def test_sentence_initial_business_words(self):
        self.assert_clean("Договор вступает в силу с момента подписания.",
                          "Согласовано с юридическим отделом.",
                          "Приложение является неотъемлемой частью соглашения.",
                          "Спецификация содержит перечень оборудования.",
                          "Распоряжение доведено до всех подразделений.")

    def test_genitive_plurals_at_the_start_of_a_sentence(self):
        """«Договоров», «Актов», «Работников» — родительный падеж множественного числа, и по
        окончанию он неотличим от фамилии на -ов."""
        self.assert_clean("Договоров на текущий год заключено больше обычного.",
                          "Актов приёмки за сентябрь не поступало.",
                          "Документов, подтверждающих расходы, представлено не было.",
                          "Работников, привлечённых к сверхурочной работе, было четверо.",
                          "Продавцов на рынке стало заметно больше.")

    def test_job_titles_and_abbreviations(self):
        self.assert_clean("Генеральный Директор утвердил Положение.",
                          "Заместитель Начальника Отдела согласовал заявку.",
                          "Отчёты в форматах PDF XLSX принимаются оба.")

    def test_a_paragraph_of_business_prose_stays_clean(self):
        text = ("Настоящим уведомляем о смене банковских реквизитов организации. "
                "Расчёты производятся в безналичной форме в течение десяти банковских дней. "
                "Сверка взаиморасчётов проведена по состоянию на первое октября. "
                "Списание безнадёжной задолженности согласовано с финансовой службой. "
                "Мониторинг исполнения поручений осуществляется еженедельно.")
        self.assertEqual(scan(text), [])


class ParticleSurnameTests(unittest.TestCase):
    """Фамилия с приставкой-частицей: «д'Артаньян». Частица пишется со строчной буквы, и
    проверка «имя собственное начинается с заглавной» отбрасывала слово целиком. Имя и
    отчество рядом при этом находились, а фамилия уходила в выгрузку открытым текстом —
    то есть находка была, выглядела правдоподобно, и потери никто не замечал."""

    def test_lowercase_particle_keeps_the_surname(self):
        findings = scan("д'Артаньян Шарль Огюстович подписал акт приёмки.")
        self.assertEqual([f.original for f in findings], ["д'Артаньян Шарль Огюстович"])

    def test_typographic_apostrophe_works_the_same(self):
        findings = scan("д’Артаньян Шарль Огюстович подписал акт приёмки.")
        self.assertEqual([f.original for f in findings], ["д’Артаньян Шарль Огюстович"])

    def test_capitalised_particle_still_works(self):
        """Без harvest, как и «д'Артаньян»: с harvest «О'Коннор» теряется по другой причине —
        окончания «-ор» нет среди фамильных, и в справочник фамилия не попадает вовсе. Это
        отдельная течь, к частице отношения не имеющая: «Коннор» без апострофа теряется так же.
        """
        detector = Detector([], Settings())
        findings = detector.scan("О'Коннор Джон Иванович", "x", "body")
        self.assertEqual([f.original for f in findings], ["О'Коннор Джон Иванович"])

    def test_hyphenated_surname_still_works(self):
        self.assertEqual([f.original for f in scan("Петров-Водкин Кузьма Сергеевич")],
                         ["Петров-Водкин Кузьма Сергеевич"])

    def test_particle_surname_resolves_on_its_own_after_the_full_name(self):
        text = ("Договор подписал д'Артаньян Шарль Огюстович. "
                "Позже д'Артаньян уточнил сумму.")
        detector = Detector([], Settings())
        detector.harvest(text, "x")
        findings = detector.scan(text, "x", "body")
        bare = [f for f in findings if f.original == "д'Артаньян"]
        self.assertEqual(len(bare), 1)
        self.assertEqual(bare[0].category, "PERSON")
        self.assertTrue(bare[0].person_id)

    def test_english_contractions_are_not_names(self):
        """«don't» и «can't» устроены так же — строчная буква, апостроф, буква, — и правило
        обязано их пропускать. Спасает требование заглавной буквы после апострофа."""
        self.assertEqual(scan("Клиент сказал don't и положил трубку."), [])
        self.assertEqual(scan("We can't confirm the Purchase Order today."), [])

    def test_ordinary_business_prose_stays_clean(self):
        text = ("Стороны договорились об условиях поставки товара. "
                "Договор вступает в силу с момента подписания. "
                "Приложение является неотъемлемой частью соглашения.")
        self.assertEqual(scan(text), [])


class SurnameListTests(unittest.TestCase):
    """Список фамилий через запятую: строка подписей, столбец согласующих, выпадающий список
    в ячейке Excel. Заглушка «два слова фамильного вида подряд неразличимы» построена на
    соседстве через пробел и раньше съедала такой список целиком, оставляя одну последнюю
    фамилию, — а файл при этом объявлялся чистым."""

    def surnames(self, text):
        return [f.original for f in scan(text)]

    def test_comma_separated_list_with_spaces(self):
        self.assertEqual(self.surnames("Петров, Сидоров и Мельников"), ["Сидоров", "Мельников"])

    def test_comma_separated_list_without_spaces(self):
        """Так выглядит список проверки данных в ячейке Excel."""
        self.assertEqual(self.surnames("Мельников,Сидоров,Терентьев"),
                         ["Мельников", "Сидоров", "Терентьев"])

    def test_one_surname_per_line(self):
        self.assertEqual(self.surnames("Мельников\nСидоров\nТерентьев"),
                         ["Мельников", "Сидоров", "Терентьев"])

    def test_semicolon_separated_list(self):
        self.assertEqual(self.surnames("Сидоров; Мельников; Терентьев"),
                         ["Сидоров", "Мельников", "Терентьев"])

    def test_every_entry_of_a_list_goes_to_review(self):
        for finding in scan("Сидоров, Мельников, Терентьев"):
            self.assertEqual(finding.decision, Decision.REVIEW)

    def test_pair_separated_by_a_space_is_still_suppressed(self):
        """Запятая — разделитель списка, пробел — нет. «Снова Смирнов» и «Причина Кузнецова»
        по-прежнему неразличимы, и находки давать не должны."""
        self.assertEqual(scan("Снова Смирнов подписали"), [])
        self.assertEqual(scan("Причина Кузнецова указана"), [])


class LatinPatronymicTests(unittest.TestCase):
    """Отчество в латинской записи. Суффиксы отчества были выписаны только кириллицей, поэтому
    из «Ivanov Ivan Ivanovich» находка забирала два слова из трёх: файл уходил помеченным как
    чистый, а на странице оставалось «Ivanovich»."""

    def test_full_latin_name_is_taken_whole(self):
        for text, want in (("Ivanov Ivan Ivanovich", "Ivanov Ivan Ivanovich"),
                           ("Petrov Petr Petrovich podpisal", "Petrov Petr Petrovich"),
                           ("Sidorov Aleksey Nikolaevich", "Sidorov Aleksey Nikolaevich"),
                           ("Ivanova Anna Ivanovna", "Ivanova Anna Ivanovna")):
            self.assertEqual([f.original for f in scan(text)], [want], text)

    def test_latin_name_inside_a_sentence(self):
        findings = scan("Dogovor podpisal Ivanov Ivan Ivanovich")
        self.assertEqual([f.original for f in findings], ["Ivanov Ivan Ivanovich"])
        self.assertEqual(findings[0].decision, Decision.AUTO)

    def test_nothing_of_the_patronymic_is_left_behind(self):
        for text in ("Ivanov Ivan Ivanovich", "Ivanova Anna Ivanovna"):
            leftovers = [f.original for f in scan(text) if f.original.startswith("Ivanov")]
            self.assertEqual(len(leftovers), 1, text)

    def test_arbitrary_latin_triples_are_not_names(self):
        """Правило опирается на окончание отчества, а не на «три слова с заглавной подряд»."""
        self.assertEqual(scan("Smith John Michael"), [])
        self.assertEqual(scan("Corel Draw Suite installed"), [])

    def test_english_words_ending_in_ich_are_not_patronymics(self):
        """Короткого «-ich» в латинском списке нет намеренно: «Munich», «sandwich», «Norwich»
        встречаются в обычной деловой переписке постоянно."""
        self.assertEqual(scan("Our Munich office confirmed the sandwich catering invoice."), [])
        self.assertEqual(scan("Rich Text Format and Portable Document Format are supported."), [])
        self.assertEqual(scan("Delivery to Greenwich was rescheduled to Monday."), [])

    def test_cyrillic_patronymic_is_unchanged(self):
        findings = scan("Договор подписал Иванов Иван Иванович")
        self.assertEqual([f.original for f in findings], ["Иванов Иван Иванович"])
        self.assertEqual(findings[0].decision, Decision.AUTO)


class SignatureLineTests(unittest.TestCase):
    """«Подписал Иванов.» не давало ни одной находки, и приказ уезжал в READY_TO_UPLOAD
    с фамилией в открытом виде — самый обычный вид документа из тех, ради которых
    программа и написана.

    Причина: словарь считает «Иванов», «Попов», «Балашов» ещё и обиходными словами
    (иванов день, попов сын), а такое слово детектор отбрасывал молча. Теперь оно
    проходит при двух доводах сразу: перед ним стоит глагол-деятель, и словарь возводит
    слово к нему же самому.
    """

    SIGNATURES = ("Подписал Иванов.", "Согласовал Петров.", "Приказ подписал Иванов",
                  "Исполнитель: Балашов.", "Получено Кимом.")

    def test_a_surname_after_an_actor_verb_is_found(self):
        for text in self.SIGNATURES:
            with self.subTest(text=text):
                self.assertTrue(scan(text), f"подпись не опознана: {text}")

    def test_the_same_word_in_ordinary_prose_is_left_alone(self):
        """Довод именно в связке. Без глагола-деятеля слово остаётся словом."""
        for text in ("Иванов день отмечают летом.", "Иванов чай собирают в июле.",
                     "Попов день прошёл тихо."):
            with self.subTest(text=text):
                self.assertEqual(scan(text), [])

    def test_a_word_whose_lemma_is_not_a_surname_stays_out(self):
        """Второй довод отсекает прилагательные-топонимы: «Нижегородская» сводится к
        «нижегородский», а это не фамилия. «Московская» сюда не попадает намеренно —
        «Московский» фамилия настоящая, и слово уходит человеку на проверку."""
        for text in ("Согласовал Нижегородская в рабочем порядке.",
                     "Согласовал Ленинградская в рабочем порядке.",
                     "Согласовал Тверская в рабочем порядке."):
            with self.subTest(text=text):
                self.assertEqual(scan(text), [])

    def test_a_surname_that_is_also_a_town_still_counts(self):
        """Городов, названных в честь людей, в России много, поэтому фамилия-город —
        частая фамилия. Молчать о ней в строке подписи значит терять целый пласт."""
        for word in ("Чехов", "Пушкин", "Королёв", "Гагарин", "Калинин"):
            with self.subTest(word=word):
                self.assertTrue(scan(f"Согласовал {word} в рабочем порядке."))

    def test_the_decision_is_always_review(self):
        """Довод слабее полного ФИО, поэтому решает человек, а не автозамена."""
        for finding in scan("Подписал Иванов."):
            self.assertNotEqual(finding.decision, Decision.AUTO)


class ShortSurnameTests(unittest.TestCase):
    """«Ким» и «Цой» — фамилии целого региона, а порог в четыре буквы отбрасывал их всегда:
    «Договор подписал Ким» не давал ни одной находки.
    """

    def test_a_three_letter_surname_in_context_is_found(self):
        for text in ("Договор подписал Ким.", "Ответственный: Ким", "Согласовал Цой."):
            with self.subTest(text=text):
                self.assertTrue(scan(text), f"короткая фамилия пропущена: {text}")

    def test_a_three_letter_word_without_context_is_left_alone(self):
        """Три буквы — довод слабый, и один он не тянет."""
        self.assertEqual(scan("Ким рассмотрен комиссией."), [])

    def test_two_letter_words_stay_out(self):
        """«Ли» словарь знает и как частицу, и слишком часто это она и есть."""
        for text in ("Подписал Ли.", "Ли рассмотрен комиссией."):
            with self.subTest(text=text):
                self.assertEqual(scan(text), [])

    def test_a_given_name_alone_is_not_a_surname(self):
        """Признак «бывает именем» из проверки убран — держит её словарь фамилий."""
        for text in ("Подписал Иван.", "Подписала Мария."):
            with self.subTest(text=text):
                self.assertEqual(scan(text), [])

    def test_a_short_surname_is_matched_again_further_down(self):
        """Прежде «Ким Иван Иванович» в шапке обезличивался, а «Ким» в подписи ниже — нет."""
        found = [f.original for f in scan("Ким Иван Иванович подписал акт. Ниже расписался Ким.")]
        self.assertIn("Ким", found)


class ApostropheSurnameTests(unittest.TestCase):
    """Фамилию с частицей словарь не знает целиком, и предсказатель разбирал её наугад:
    «О'Коннор» он читал как родительный падеж выдуманного «о'коннора». Человек заносился
    в список под этой формой — чужой падеж и потерянная заглавная внутри слова, — а
    настоящее написание в указатель не попадало, и второе упоминание в том же документе
    оставалось необезличенным.
    """

    def test_the_spelling_is_kept_as_written(self):
        self.assertEqual(Detector._to_nominative("О'Коннор", "Джон", "Иванович"),
                         ("О'Коннор", "Джон", "Иванович"))

    def test_a_repeat_mention_is_found(self):
        for name in ("О'Коннор", "д'Артаньян"):
            with self.subTest(name=name):
                text = f"{name} Джон Иванович подписал акт. Позже {name} уточнил смету."
                self.assertEqual(len([f for f in scan(text)]), 2, text)

    def test_ordinary_surnames_are_still_brought_to_the_nominative(self):
        """Оговорка касается только слов с апострофом: обычную фамилию по-прежнему
        приводим к именительному, иначе человек рассыпется на три записи."""
        self.assertEqual(Detector._to_nominative("Смирновой", "Анне", "Петровне")[0], "Смирнова")
        self.assertEqual(Detector._to_nominative("Иванову", "Ивану", "Ивановичу")[0], "Иванов")


class DashedSurnameTests(unittest.TestCase):
    """Двойная фамилия через дефис работает. Через дефис с пробелами — нет, и это
    сознательный выбор: «Исполнитель - Иванов Иван Иванович» встречается в документах
    несравнимо чаще, а склеивание дало бы фамилию «Исполнитель-Иванов».
    """

    def test_a_double_surname_is_found_whole(self):
        found = [f.original for f in scan("Римский-Корсаков Николай Андреевич подписал акт.")]
        self.assertIn("Римский-Корсаков Николай Андреевич", found)

    def test_a_role_line_with_a_dash_keeps_the_role_out_of_the_name(self):
        for text, name in (("Исполнитель - Иванов Иван Иванович", "Иванов Иван Иванович"),
                           ("Директор — Кузнецов Иван Иванович", "Кузнецов Иван Иванович")):
            with self.subTest(text=text):
                self.assertEqual([f.original for f in scan(text)], [name])


class ActorVerbTests(unittest.TestCase):
    """Список глаголов был выписан вручную, и в нём не хватало ровно того, чего не подумали
    написать: «Утверждаю: Голубев» и «Прошу Иванова» не давали ни одной находки. Формы
    русского глагола по одной не перечислить, поэтому решает разбор.
    """

    OUTSIDE_THE_OLD_LIST = ("Утверждаю", "Визирую", "Прошу", "Поручил", "Рассмотрел",
                            "Подтвердил", "Отправил", "Вернул", "Заверил", "Уведомил")

    def test_verbs_that_were_never_on_the_list_now_count(self):
        for verb in self.OUTSIDE_THE_OLD_LIST:
            with self.subTest(verb=verb):
                self.assertTrue(scan(f"{verb} Иванов."), f"после «{verb}» фамилия не найдена")

    def test_short_participles_count_too(self):
        """«получено», «подписано», «принят» стоят в тех же строках, что и глагол."""
        for text in ("Получено Иванов", "Подписано Иванов", "Принят Иванов"):
            with self.subTest(text=text):
                self.assertTrue(scan(text))

    def test_the_words_that_are_not_verbs_are_still_covered(self):
        """«исполнитель», «ответственный», «от» морфология глаголами не считает — они
        остались списком."""
        for text in ("Исполнитель: Балашов.", "Ответственный Иванов", "Получено от Иванов"):
            with self.subTest(text=text):
                self.assertTrue(scan(text))

    def test_an_ordinary_noun_before_the_word_is_not_a_context(self):
        """Расширение касается глаголов, а не любого соседа слева."""
        for text in ("Иванов день отмечают летом.", "Попов день прошёл тихо."):
            with self.subTest(text=text):
                self.assertEqual(scan(text), [])


class FeminineSurnameTests(unittest.TestCase):
    """Женская фамилия сводится к мужской — «Иванова» к «иванов», — и по строгому
    равенству слова и начальной формы каждая женская подпись оставалась неопознанной.
    """

    def test_a_feminine_surname_in_a_signature_is_found(self):
        for surname in ("Иванова", "Попова", "Антонова", "Ильина", "Смирнова",
                        "Кузнецова", "Петрова", "Козловская"):
            with self.subTest(surname=surname):
                self.assertTrue(scan(f"Подписала {surname}."), surname)

    def test_a_place_named_by_its_adjective_is_not_a_person(self):
        """«Московская область» — место, и узнаётся оно по слову справа, а не слева."""
        for text in ("Согласовал Московская область в рабочем порядке.",
                     "Отчёт направлен в Ленинградская область.",
                     "Утвердил Краснодарский край."):
            with self.subTest(text=text):
                self.assertEqual([f for f in scan(text) if "PERSON" in f.category], [])


class PatronymicShapedSurnameTests(unittest.TestCase):
    """«Станкевич», «Макаревич», «Богданович» — фамилии, а не отчества, но по виду слова
    их не отличить, и в строке подписи они молчали все до одной.
    """

    def test_a_patronymic_shaped_surname_in_a_signature_is_found(self):
        for surname in ("Станкевич", "Макаревич", "Тарасевич"):
            with self.subTest(surname=surname):
                self.assertTrue(scan(f"Подписал {surname}."), surname)

    def test_the_dictionary_is_still_the_limit(self):
        """Предел не в правиле, а в словаре: «Богданович», «Карпович», «Климович»
        OpenCorpora фамилиями не считает вовсе, и одиночными они по-прежнему молчат.
        В составе ФИО они находятся, и тест это фиксирует, чтобы предел был виден."""
        self.assertEqual(scan("Подписал Богданович."), [])
        self.assertTrue(scan("Богданович Иван Иванович подписал акт."))

    def test_a_woman_carries_the_same_form(self):
        """У женщин такая фамилия не склоняется — «Подписала Станкевич», — поэтому
        отдельного правила ей не нужно, но проверить это обязательно: строка подписи
        женщины встречается не реже мужской."""
        for surname in ("Станкевич", "Макаревич", "Тарасевич"):
            with self.subTest(surname=surname):
                self.assertTrue(scan(f"Подписала {surname}."), surname)
                self.assertTrue(scan(f"Получено от {surname}."), surname)

    def test_a_real_patronymic_alone_is_still_not_a_surname(self):
        """Послабление держит словарь: «Ивановна» и «Иванович» фамилиями он не считает,
        и одиночными они по-прежнему не находятся."""
        for text in ("Подписала Ивановна.", "Подписал Иванович.", "Согласовала Петровна."):
            with self.subTest(text=text):
                self.assertEqual(scan(text), [])

    def test_a_full_name_with_a_feminine_patronymic_is_read_whole(self):
        found = [f.original for f in scan("Иванова Мария Ивановна подписала акт.")]
        self.assertEqual(found, ["Иванова Мария Ивановна"])

    def test_without_a_verb_in_front_it_stays_out(self):
        """Довод здесь только контекстный: без него вид слова говорит против находки."""
        self.assertEqual(scan("Станкевич отмечают летом."), [])

    def test_a_full_name_is_still_read_as_one(self):
        """Ослабление не должно разрывать ФИО на части."""
        found = [f.original for f in scan("Подписал Иванов Иван Иванович.")]
        self.assertEqual(found, ["Иванов Иван Иванович"])


if __name__ == "__main__":
    unittest.main()
