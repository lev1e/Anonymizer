"""Компании, проекты, города, домены и имена файлов: что должно находиться и что трогать нельзя."""
import unittest

from anonymizer.detectors import Detector
from anonymizer.models import Decision, Settings


def scan(text, **settings):
    detector = Detector([], Settings(**settings))
    detector.harvest(text, "t")
    return detector.scan(text, "t", "x")


def found(text, category=None, **settings):
    return [f.original for f in scan(text, **settings)
            if f.decision == Decision.AUTO and (category is None or f.category == category)]


class OrganizationTests(unittest.TestCase):
    def test_legal_form_with_quotes_hides_only_the_name(self):
        self.assertEqual(found("Клиент: ООО «Аврора-Гидропроект» (dummy).", "ORG"), ["Аврора-Гидропроект"])
        self.assertEqual(found('Контракт с АО "Ромашка Плюс" подписан.', "ORG"), ["Ромашка Плюс"])
        self.assertEqual(found("Поставщик — ПАО «Газпром нефть».", "ORG"), ["Газпром нефть"])

    def test_legal_form_without_quotes(self):
        self.assertEqual(found("Склады ООО Сумитек интернейшнл находятся в трёх городах.", "ORG"),
                         ["Сумитек интернейшнл"])
        self.assertEqual(found("Заказчик АО Вектор принял работы.", "ORG"), ["Вектор"])

    def test_latin_company_names(self):
        self.assertEqual(found("Contract with Acme Trading Ltd was signed.", "ORG"), ["Acme Trading"])
        self.assertEqual(found("Sumitec International LLC delivers parts.", "ORG"), ["Sumitec International"])

    def test_once_named_the_company_is_hidden_everywhere_in_the_document(self):
        text = "Заказчик — ООО «Вектор». Оплата поступила от Вектор. Договор с «Вектор» продлён."
        self.assertEqual(found(text, "ORG"), ["Вектор", "Вектор", "Вектор"])

    def test_other_case_of_a_known_name_is_the_same_company(self):
        text = "ООО «Аврора» подписало акт. Претензий к Авроры нет. Передано Авроре."
        self.assertEqual(found(text, "ORG"), ["Аврора", "Авроры", "Авроре"])

    def test_multiword_name_in_another_case(self):
        text = "Компания «Технологии Доверия» выросла. Офисы Технологий Доверия открыты в шести городах."
        self.assertEqual(found(text, "ORG"), ["Технологии Доверия", "Технологий Доверия"])

    def test_abbreviation_belongs_to_the_full_name(self):
        text = "Технологии Доверия (ТеДо) — правопреемник. ТеДо предлагает три этапа."
        result = scan(text)
        orgs = [f for f in result if f.category == "ORG"]
        self.assertEqual([f.original for f in orgs], ["Технологии Доверия", "ТеДо", "ТеДо"])
        self.assertEqual({f.key for f in orgs}, {"технологии доверия"})

    def test_company_named_in_quotes_after_a_hint_word(self):
        self.assertEqual(found("Компания «Напитки Вместе» — ведущий производитель.", "ORG"), ["Напитки Вместе"])
        self.assertEqual(found("Основные конкуренты: «Каскад-Энерго», «ГидроВолга».", "ORG"),
                         ["Каскад-Энерго", "ГидроВолга"])

    def test_well_known_companies_are_found_without_context(self):
        self.assertIn("Сбербанк", found("Расчёты идут через Сбербанк и Газпромбанк.", "ORG"))
        self.assertIn("PwC", found("Первый офис PwC в России (ex-PwC)."))
        self.assertEqual(found("Сравнение с Carlsberg и Heineken.", "ORG"), ["Carlsberg", "Heineken"])

    def test_ordinary_quoted_text_is_left_alone(self):
        for text in ("Выявление «скрытых» особенностей рынка.",
                     "В колонке «Что уточнить» указаны вопросы.",
                     "Статус «Выполняется» присвоен процессу.",
                     "Система «WMS» и «1С» используются на складе.",
                     "Раздел «Общие положения» утверждён.",
                     "Ответ «да» или «нет»."):
            with self.subTest(text=text):
                self.assertEqual(found(text, "ORG"), [])
                self.assertEqual(found(text, "PROJECT"), [])

    def test_software_and_public_terms_are_not_companies(self):
        self.assertEqual(found("Файл открывается в Excel и Word, данные из ERP и CRM.", "ORG"), [])

    def test_organization_detection_can_be_switched_off(self):
        self.assertEqual(found("ООО «Аврора»", "ORG", organizations=False), [])


class ProjectTests(unittest.TestCase):
    def test_object_named_after_a_kind_of_facility(self):
        self.assertEqual(found("Проект: строительство и ввод ГЭС «Аврора-3».", "PROJECT"), ["Аврора-3"])
        self.assertEqual(found("Разработка месторождения «Северное сияние» начата.", "PROJECT"), ["Северное сияние"])
        self.assertEqual(found("В рамках проекта «Феникс» запущена платформа.", "PROJECT"), ["Феникс"])

    def test_project_name_is_hidden_everywhere_after_the_first_mention(self):
        text = "Проект «Феникс» стартует в мае. Бюджет Феникс согласован."
        self.assertEqual(found(text, "PROJECT"), ["Феникс", "Феникс"])


class GeographyTests(unittest.TestCase):
    def test_cities_in_any_case(self):
        self.assertEqual(found("Склады в Москве, Красноярске и Нижнем Новгороде.", "CITY"),
                         ["Москве", "Красноярске", "Нижнем Новгороде"])

    def test_small_places_are_found_by_context(self):
        text = "Сервис в г. Ковдор, участок на п/ст Тальжино и ОП Вологда."
        self.assertEqual(found(text, "CITY"), ["Ковдор", "Тальжино", "Вологда"])

    def test_known_small_place_is_hidden_elsewhere_in_the_document(self):
        text = "Филиал в г. Ковдор открыт. Отдел сервиса Ковдор закрыт."
        self.assertEqual(found(text, "CITY"), ["Ковдор", "Ковдор"])

    def test_regions_and_branch_names(self):
        self.assertEqual(found("Кемеровской области и Красноярского края.", "REGION"), ["Кемеровской", "Красноярского"])
        self.assertEqual(found("Дальневосточный филиал и Кузбасский офис.", "REGION"),
                         ["Дальневосточный", "Кузбасский"])

    def test_address_is_one_piece_with_its_city(self):
        result = [f for f in scan("Подразделение в г. Иркутск ул. Дорожная, д. 1 работает.") if f.decision == Decision.AUTO]
        self.assertEqual([(f.category, f.original) for f in result], [("ADDRESS", "г. Иркутск ул. Дорожная, д. 1")])

    def test_countries_are_not_hidden(self):
        self.assertEqual(found("Экспорт в Китай, Вьетнам и Бразилию из России.", "CITY"), [])
        self.assertEqual(found("Экспорт в Китай, Вьетнам и Бразилию из России.", "REGION"), [])

    def test_surname_that_looks_like_a_city_is_not_a_city(self):
        for text in ("Отчёт подготовил Иванов А.", "Договор подписан Ивановой Анной Сергеевной.",
                     "Согласовано с Курганов Дмитрий Леонидович."):
            with self.subTest(text=text):
                self.assertEqual(found(text, "CITY"), [])

    def test_given_name_is_not_a_city(self):
        self.assertEqual(found("Владимир подписал приказ.", "CITY"), [])

    def test_abbreviation_of_a_sentence_is_not_a_place(self):
        self.assertEqual(found("Требуется тара, упаковка и т.п. Как правило, этого хватает.", "CITY"), [])

    def test_geography_can_be_switched_off(self):
        self.assertEqual(found("Офис в Москве", "CITY", geo=False), [])


class DomainAndFileTests(unittest.TestCase):
    def test_corporate_domain_is_hidden_but_public_mail_is_not(self):
        text = "Пишите на ivan@sumitec.ru или на ivan@gmail.com; сайт sumitec.ru и https://www.sumitec.ru/about."
        domains = found(text, "DOMAIN")
        # путь после домена входит в замену: в нём тоже бывает имя клиента
        self.assertEqual(domains, ["sumitec.ru", "sumitec.ru/about"])
        self.assertIn("ivan@gmail.com", found(text, "EMAIL"))

    def test_file_names_lose_the_stem_and_keep_the_extension_outside(self):
        result = [f for f in scan("Данные в файле Данные Клиента.xlsx и Маркетинг.pptx.") if f.category == "FILE"]
        self.assertEqual([f.original for f in result], ["Данные Клиента", "Маркетинг"])

    def test_format_names_are_not_file_names(self):
        self.assertEqual(found("Форматы XLSX и PDF поддерживаются, а также .docx.", "FILE"), [])

    def test_infrastructure_addresses_are_never_touched(self):
        self.assertEqual(found("http://schemas.openxmlformats.org/officeDocument/2006/relationships", "DOMAIN"), [])


class CustomTermsTests(unittest.TestCase):
    def test_always_hide_terms(self):
        text = "Проект Орион и внутренний код ЗУП-Х используются."
        self.assertEqual(found(text, "TERM", hide_terms=["Орион", "ЗУП-Х"]), ["Орион", "ЗУП-Х"])

    def test_hide_term_in_another_case(self):
        self.assertEqual(found("Спросите у Орионе.", "TERM", hide_terms=["Орион"]), ["Орионе"])

    def test_never_hide_terms_win_over_detection(self):
        self.assertEqual(found("ООО «Аврора» подписало.", keep_terms=["Аврора"]), [])
        self.assertEqual(found("Сбербанк и Газпром.", keep_terms=["Сбербанк"]), ["Газпром"])


class SuggestionTests(unittest.TestCase):
    def test_unknown_proper_names_are_offered_not_replaced(self):
        text = "Основные игроки рынка: Балтика, Трехсосенский и Maltynation."
        review = [f.original for f in scan(text, suggest=True) if f.category in {"POSSIBLE_ENTITY", "POSSIBLE_PERSON"}]
        self.assertIn("Трехсосенский", review)
        self.assertIn("Maltynation", review)
        self.assertEqual(found(text, "POSSIBLE_ENTITY", suggest=True), [])

    def test_ordinary_words_are_not_offered(self):
        text = "Аудит проводится в июне. Клиента информируют заранее, Договор подписан."
        self.assertEqual([f.original for f in scan(text, suggest=True) if f.category == "POSSIBLE_ENTITY"], [])


class BusinessProseTests(unittest.TestCase):
    PROSE = ("Настоящим уведомляем о смене банковских реквизитов организации. Расчёты производятся в безналичной "
             "форме в течение десяти банковских дней. Сверка взаиморасчётов проведена по состоянию на первое "
             "октября. Российская Федерация, Договор Поставки № 44-А. Генеральный директор "
             "утвердил положение. Отчёт за 2026 год, EBITDA и CAPEX выросли на 15%, WACC — 14%. См. Приложение 2.")

    def test_no_entities_are_invented_in_plain_business_text(self):
        entities = {"ORG", "PROJECT", "CITY", "REGION", "DOMAIN", "FILE", "TERM"}
        self.assertEqual([f.original for f in scan(self.PROSE) if f.category in entities], [])


if __name__ == "__main__":
    unittest.main()
