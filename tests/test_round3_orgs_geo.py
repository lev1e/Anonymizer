"""Раунд 3, поток A: организации, бренды и география (находки A1–A10). Все названия выдуманы или общеизвестны."""
import re
import unittest
from pathlib import Path

from anonymizer.detectors import Detector
from anonymizer.entities import EntityRecognizer, transliterations
from anonymizer.models import Decision, Settings

from .helpers import Env, docx_bytes, docx_text

TOKEN = re.compile(r"(?:City|Region|Company|Project|Domain)\d+(?:_\d+)?")


def detector_for(texts: list[str], **settings) -> Detector:
    detector = Detector([], Settings(**settings))
    detector.harvest("\n".join(texts), "f")
    return detector


def found(texts: list[str], index: int, category: str | None = None, **settings) -> list[str]:
    detector = detector_for(texts, **settings)
    return [f.original for f in detector.scan(texts[index], "f", "x")
            if f.decision == Decision.AUTO and (category is None or f.category == category)]


def keys(texts: list[str], category: str) -> list[str]:
    """Ключ объекта (он определяет базовую метку) для каждой находки нужного вида по всем текстам."""
    detector = detector_for(texts)
    out = []
    for text in texts:
        out += [f.key for f in detector.scan(text, "f", "x") if f.decision == Decision.AUTO and f.category == category]
    return out


def paragraphs(texts: list[str]) -> list[str]:
    """Каждый текст — отдельный абзац документа; возвращает обезличенные абзацы по порядку."""
    env = Env()
    job = env.anonymize(("Документ.docx", docx_bytes(texts)))
    out = docx_text(Path(job.files[0].out_path)).split("\n")
    env.close()
    return out[:len(texts)]


def base(token: str) -> str:
    return token.split("_")[0]


class ConsistentPlaceTokens(unittest.TestCase):
    """A10: один город или регион — одна базовая метка во всех падежах и в виде прилагательного."""

    def test_case_forms_of_a_city_share_one_key(self):
        self.assertEqual(set(keys(["Офис в г. Томске открыт.", "Склад г. Томск", "Томск"], "CITY")), {"томск"})

    def test_region_adjective_forms_share_one_key(self):
        self.assertEqual(set(keys(["Поставки по Омской области", "Омская область"], "REGION")), {"омский"})

    def test_region_noun_and_its_adjective_share_one_key(self):
        self.assertEqual(set(keys(["Кузбасс и Кузбасский филиал"], "REGION")), {"кузбасс"})
        self.assertEqual(set(keys(["Сибирь; Сибирский филиал", "Сибирского филиала"], "REGION")), {"сибирь"})

    def test_tokens_in_the_output_are_one_base_with_variants(self):
        out = paragraphs(["Офис в г. Томске открыт.", "Томск", "Урал и Уральский филиал"])
        city = [TOKEN.search(line).group(0) for line in out[:2]]
        self.assertEqual({base(t) for t in city}, {base(city[0])})
        region = TOKEN.findall(out[2])
        self.assertEqual(len({base(t) for t in region}), 1, out[2])

    def test_region_and_city_of_the_same_name_stay_different(self):
        # «Омская область» — не город Омск: разные объекты, разные метки.
        self.assertNotEqual(set(keys(["в Омской области"], "REGION")), set(keys(["в Омске"], "CITY")))


class Settlements(unittest.TestCase):
    """A8: сокращения населённых пунктов, капс, дефисные названия, связь форм, площадки в кавычках."""

    def test_settlement_abbreviations(self):
        self.assertEqual(found(["Работаем в пгт. Верхнеозёрный и в с. Малиновка"], 0, "CITY"), ["Верхнеозёрный", "Малиновка"])
        self.assertEqual(found(["Доставка в д. Сосновка"], 0, "CITY"), ["Сосновка"])
        self.assertEqual(found(["Склад в ст. Кавказская"], 0, "CITY"), ["Кавказская"])

    def test_abbreviations_with_other_meanings_stay(self):
        self.assertEqual(found(["и т. д. Далее по списку"], 0), [])
        self.assertEqual(found(["см. с. 5 и ст. 15 ТК"], 0), [])
        self.assertEqual(found(["д. Отчёт"], 0), [])
        self.assertEqual(found(["в районе Нового года"], 0), [])
        self.assertEqual(found(["ст. Научный сотрудник"], 0), [])

    def test_all_caps_and_hyphenated_settlement(self):
        self.assertEqual(found(["ОП САЛЫ-КУЛ"], 0, "CITY"), ["САЛЫ-КУЛ"])
        self.assertEqual(found(["ОП АХО"], 0), [])

    def test_learned_settlement_in_other_cases(self):
        texts = ["в селе Малиновке", "Малиновка", "в районе Малиновки"]
        self.assertEqual(set(keys(texts, "CITY")), {"малиновке"})

    def test_short_name_of_a_hyphenated_city(self):
        texts = ["Комсомольск-на-Амуре и Комсомольск", "в Комсомольске работаем"]
        self.assertEqual(set(keys(texts, "CITY")), {"комсомольск-на-амуре"})

    def test_district_adjective_before_and_after_the_head(self):
        self.assertEqual(set(keys(["Участок в Гурьевском районе, р-н Гурьевский"], "REGION")), {"гурьевский"})
        self.assertEqual(set(keys(["Нерюнгринский улус; в Нерюнгринском районе"], "REGION")), {"нерюнгринский"})

    def test_quoted_site_after_a_unit_word_is_not_a_company(self):
        detector = detector_for(["подразделение «Каменка»", "склад «Южный»"])
        first = [f.category for f in detector.scan("подразделение «Каменка»", "f", "x") if f.decision == Decision.AUTO]
        second = [f.category for f in detector.scan("склад «Южный»", "f", "x") if f.decision == Decision.AUTO]
        self.assertEqual(first, ["CITY"])
        self.assertEqual(second, ["PROJECT"])
        self.assertEqual(found(["компания «Гранит»"], 0, "ORG"), ["Гранит"])

    def test_small_town_from_the_gazetteer(self):
        self.assertEqual(found(["Поставки в Сосновоборск и Шарыпово"], 0, "CITY"), ["Сосновоборск", "Шарыпово"])


class UrlPriority(unittest.TestCase):
    """A4: адрес сайта целиком выше известного названия внутри пути."""

    def test_known_brand_inside_a_url_does_not_split_it(self):
        text = "Источник: https://www.beer-news-site.com/press/heineken-forecast-2025"
        self.assertEqual(found([text], 0), ["beer-news-site.com/press/heineken-forecast-2025"])

    def test_brand_outside_a_url_is_still_a_company(self):
        self.assertEqual(found(["Отчёт Heineken за год"], 0, "ORG"), ["Heineken"])


class BranchAbbreviations(unittest.TestCase):
    """A9: сокращения филиалов и округов — написания того же места."""

    TEXTS = ["Дальневосточный Филиал", "Северо-Западный филиал", "Сибирский филиал", "ДВФ", "План по ДВФ и СЗФ",
             "ДВ", "СИБ - отчёт сдан", "В ДВ филиале есть отчёт", "Реестр СФ за март", "СФ; ДВ"]

    def test_three_letter_initialisms_are_the_same_region(self):
        detector = detector_for(self.TEXTS)
        far_east = {f.key for f in detector.scan("Дальневосточный Филиал", "f", "x") if f.category == "REGION"}
        self.assertEqual({f.key for f in detector.scan("ДВФ", "f", "x") if f.decision == Decision.AUTO}, far_east)
        self.assertEqual(found(self.TEXTS, 4, "REGION"), ["ДВФ", "СЗФ"])

    def test_two_letter_forms_only_as_labels(self):
        self.assertEqual(found(self.TEXTS, 5, "REGION"), ["ДВ"])
        self.assertEqual(found(self.TEXTS, 6, "REGION"), ["СИБ"])
        self.assertEqual(found(self.TEXTS, 7, "REGION"), ["ДВ"])

    def test_ambiguous_or_ordinary_abbreviation_stays(self):
        # «СФ» подходит и Сибирскому, и Северо-Западному филиалу, а ещё это счёт-фактура: не трогаем.
        self.assertEqual(found(self.TEXTS, 8), [])
        self.assertEqual(found(self.TEXTS, 9, "REGION"), ["ДВ"])
        self.assertEqual(found(["двф и прочее"], 0), [])

    def test_business_abbreviation_in_a_row_of_places_stays(self):
        # «ТК», «ИТ» — деловые сокращения из стоп-списка; «АБВ» не стоит после слова из ряда мест.
        texts = ["Сервис Томск", "Сервис Омск", "Сервис ТК", "Сервис ИТ", "Отчёт АБВ"]
        self.assertEqual(found(texts, 2), [])
        self.assertEqual(found(texts, 3), [])
        self.assertEqual(found(texts, 4), [])

    def test_place_code_registered_from_the_row(self):
        texts = ["Сервис Томск", "Сервис Омск", "Сервис КМ", "Продажи КМ"]
        self.assertEqual(found(texts, 2, "REGION"), ["КМ"])
        self.assertEqual(found(texts, 3, "REGION"), ["КМ"])

    def test_federal_district(self):
        texts = ["Северо-Западный федеральный округ", "Северо-Западного федерального округа", "Работа в СЗФО"]
        self.assertEqual(len(set(keys(texts, "REGION"))), 1)
        self.assertEqual(found(texts, 2, "REGION"), ["СЗФО"])


class OrganizationsByContext(unittest.TestCase):
    """A1, A3: соседи в перечне, скобка после компании, слово-подсказка, родовое слово в конце названия."""

    def test_siblings_of_a_company_in_a_list(self):
        self.assertEqual(found(["Конкуренты: ООО «Ромашка», Insignia и DFTK"], 0, "ORG"), ["Ромашка", "Insignia", "DFTK"])

    def test_list_without_a_company_is_left_alone(self):
        self.assertEqual(found(["Отделы: Продажи, Закупки, Логистика"], 0), [])
        self.assertEqual(found(["Балтика, Москва и Отчёт"], 0, "ORG"), ["Балтика"])

    def test_person_next_to_a_company_is_not_a_company(self):
        self.assertNotIn("Anna Kessler", found(["ООО «Ромашка», Anna Kessler"], 0, "ORG"))

    def test_former_name_in_brackets_is_the_same_company(self):
        texts = ["ООО «Маяк» (ex-Стройтех)", "позже Стройтех"]
        detector = detector_for(texts)
        first = {f.key for f in detector.scan(texts[0], "f", "x") if f.category == "ORG"}
        second = {f.key for f in detector.scan(texts[1], "f", "x") if f.category == "ORG"}
        self.assertEqual(len(first), 1)
        self.assertEqual(first, second)

    def test_cue_word_before_an_unknown_name(self):
        self.assertEqual(found(["заявка на портал ELMO"], 0, "PROJECT"), ["ELMO"])
        self.assertEqual(found(["сервис ЭДО и сервис Доставка"], 0), [])

    def test_trailing_generic_noun(self):
        self.assertEqual(found(["Отчёт Сорвитек Групп за год"], 0, "ORG"), ["Сорвитек Групп"])
        texts = ["Маякс Холдинг подписал", "Позже Маякс прислал"]
        self.assertEqual(found(texts, 1, "ORG"), ["Маякс"])
        self.assertEqual(found(["Первая Компания и Новая Корпорация"], 0, "ORG"), [])

    def test_well_known_brand(self):
        self.assertEqual(found(["Координатор по продаже запчастей КОМАЦУ"], 0, "ORG"), ["КОМАЦУ"])


class Suggestions(unittest.TestCase):
    """A2: подсказки для проверки."""

    FILLER = " ".join(f"слово{i}" for i in range(60))

    def suggest(self, texts: list[str], index: int) -> list[tuple[str, float]]:
        recognizer = EntityRecognizer()
        recognizer.learn("\n".join(texts))
        text = texts[index]
        return [(text[h.start:h.end], h.confidence) for h in recognizer.suggest_hits(text)]

    def test_isolated_latin_word_is_a_strong_suggestion(self):
        self.assertEqual(self.suggest(["Проверили вместе с Kovrix отгрузку"], 0), [("Kovrix", .65)])

    def test_run_of_latin_words_is_one_weak_suggestion(self):
        self.assertEqual(self.suggest(["Работали с Pernel Ricaud Rousse в прошлом году"], 0),
                         [("Pernel Ricaud Rousse", .45)])

    def test_one_word_cell_is_offered(self):
        self.assertEqual([w for w, _ in self.suggest(["МПКР"], 0)], ["МПКР"])

    def test_capitalised_verb_is_not_offered(self):
        self.assertEqual(self.suggest(["Инвентаризируем"], 0), [])

    def test_compound_of_ordinary_words_is_not_offered(self):
        self.assertEqual(self.suggest(["Инженер-механик"], 0), [])
        self.assertEqual(self.suggest(["Список: Инженер-технолог"], 0), [])

    def test_repeated_name_is_strong(self):
        texts = ["Решили: ОПХР и МПКР; далее ОПХР сообщает"]
        self.assertEqual(self.suggest(texts, 0), [("ОПХР", .65), ("МПКР", .45), ("ОПХР", .65)])

    def test_capitalised_dictionary_word_mid_sentence(self):
        texts = [self.FILLER, "выполнить силами (нашими ИТ или Топлог), создается заявка", "Отчёт По Продажам и Закупкам"]
        self.assertEqual([w for w, _ in self.suggest(texts, 1)], ["Топлог"])
        self.assertEqual(self.suggest(texts, 2), [])

    def test_strict_mode_hides_repeated_names(self):
        texts = ["Решили: ОПХР и МПКР; далее ОПХР сообщает"]
        self.assertEqual(found(texts, 0, "ORG", strict=True), ["ОПХР", "ОПХР"])
        self.assertEqual(found(texts, 0, "ORG"), [])


class LatinSpellings(unittest.TestCase):
    """A5: латинское написание русского названия."""

    def test_transliterations(self):
        self.assertEqual(transliterations("ТеКо"), ["TeKo"])
        self.assertEqual(transliterations("Хабаровск"), ["Khabarovsk", "Habarovsk"])

    def test_latin_spelling_in_a_file_name_like_text(self):
        texts = ["Компания «ТеКо» подготовила отчёт", "5_A4_TeKo basic template_blue", "Orbita report"]
        self.assertEqual(found(texts, 1, "ORG"), ["TeKo"])
        self.assertEqual(found(texts, 2), [])

    def test_ordinary_word_names_are_not_transliterated(self):
        texts = ["ООО «Мост» подписало", "Most of the work is done"]
        self.assertEqual(found(texts, 1), [])


class RegionAdjectives(unittest.TestCase):
    """A6: прилагательные макрорегионов и федеральных округов."""

    def test_federal_districts_and_hyphenated_adjectives(self):
        self.assertEqual(found(["Уральский федеральный округ"], 0, "REGION"), ["Уральский"])
        self.assertEqual(found(["Северо-Западный федеральный округ"], 0, "REGION"), ["Северо-Западный"])

    def test_department_adjectives_stay(self):
        self.assertEqual(found(["Коммерческий отдел, Технический департамент"], 0), [])
        self.assertEqual(found(["Новым районом занимается"], 0), [])


class WorldCities(unittest.TestCase):
    """A7: крупные города мира и латинское название после «г.»."""

    def test_chinese_cities(self):
        self.assertEqual(found(["Офис в Wuhan и склад в Урумчи"], 0, "CITY"), ["Wuhan", "Урумчи"])

    def test_latin_city_after_intro(self):
        self.assertEqual(found(["Завод в г. Qianmen"], 0, "CITY"), ["Qianmen"])
        self.assertEqual(found(["в городе Excel"], 0), [])


class RoundTrip(unittest.TestCase):
    def test_every_new_behaviour_restores_exactly(self):
        texts = ["Офис в г. Томске открыт.", "Томск", "Кузбасс и Кузбасский филиал", "Дальневосточный Филиал", "ДВФ",
                 "ДВ", "в с. Малиновка", "в селе Малиновке", "Комсомольск-на-Амуре и Комсомольск", "ОП САЛЫ-КУЛ",
                 "Конкуренты: ООО «Ромашка», Insignia и DFTK", "ООО «Маяк» (ex-Стройтех)", "Компания «ТеКо»",
                 "5_A4_TeKo basic template_blue", "Сервис Томск", "Сервис Омск", "Сервис КМ",
                 "https://www.beer-news-site.com/press/heineken-forecast-2025", "Маякс Холдинг и Маякс"]
        env = Env()
        job = env.anonymize(("Документ.docx", docx_bytes(texts)))
        anonymized = docx_text(Path(job.files[0].out_path))
        self.assertNotIn("Томск", anonymized)
        self.assertNotIn("ДВФ", anonymized)
        back = env.restore(env.as_upload(job, 0, "Документ.docx"))
        self.assertEqual(docx_text(Path(back.files[0].out_path)).split("\n")[:len(texts)], texts)
        env.close()


class PlaceLabelsFollowUp(unittest.TestCase):
    """Доработка A8/A6: прилагательное-место как метка, место с номером, регион после подписи «Регион»."""

    def test_bare_adjective_label_is_the_learned_district(self):
        texts = ["Отгрузка в Гурьевском районе", "20 сервис Гурьевский", "Гурьевский"]
        self.assertEqual(found(texts, 1, "REGION"), ["Гурьевский"])
        self.assertEqual(found(texts, 2, "REGION"), ["Гурьевский"])
        self.assertEqual(set(keys(texts, "REGION")), {"гурьевский"})

    def test_adjective_label_of_a_gazetteer_city(self):
        self.assertEqual(found(["сервис Томский"], 0, "REGION"), ["Томский"])

    def test_generic_adjective_after_a_unit_word_stays(self):
        texts = ["склад Технический", "20 сервис Технический", "сервис Коммерческий", "Центральный", "сервис Советский"]
        for index in range(len(texts)):
            self.assertEqual(found(texts, index), [], texts[index])

    def test_place_with_a_number_is_the_learned_place(self):
        texts = ["в р-не Сунгуда", "20 Поле Сунгуда2", "Поле Сунгуда12 и Маяк2"]
        self.assertEqual(found(texts, 1, "CITY"), ["Сунгуда2"])
        self.assertEqual(found(texts, 2), ["Сунгуда12"])
        self.assertEqual(set(keys(texts, "CITY")), {"сунгуда"})

    def test_region_adjective_as_value_of_a_label(self):
        self.assertEqual(found(["Регион: Северо-Западный"], 0, "REGION"), ["Северо-Западный"])
        self.assertEqual(found(["Федеральный округ — Уральский"], 0, "REGION"), ["Уральский"])
        self.assertEqual(found(["Регион: Технический"], 0), [])
        self.assertEqual(found(["Отдел Северо-Западный"], 0), [])

    def test_follow_up_restores_exactly(self):
        texts = ["Отгрузка в Гурьевском районе", "20 сервис Гурьевский", "в р-не Сунгуда", "20 Поле Сунгуда2",
                 "Регион: Северо-Западный"]
        env = Env()
        job = env.anonymize(("Документ.docx", docx_bytes(texts)))
        anonymized = docx_text(Path(job.files[0].out_path))
        self.assertNotIn("Гурьевский", anonymized)
        self.assertNotIn("Сунгуда", anonymized)
        back = env.restore(env.as_upload(job, 0, "Документ.docx"))
        self.assertEqual(docx_text(Path(back.files[0].out_path)).split("\n")[:len(texts)], texts)
        env.close()


if __name__ == "__main__":
    unittest.main()


class ServiceCueBoundaryTests(unittest.TestCase):
    """Слово-подсказка («сервис», «портал») не действует через границу строки и на «Фамилия Имя»."""

    def test_cue_does_not_cross_newline(self):
        texts = ["Торговый представитель по продаже и сервису\nМасленин Марина"]
        self.assertEqual(found(texts, 0, "PROJECT"), [])

    def test_cue_does_not_take_a_person(self):
        texts = ["по сервису Масленин Марина Владимировна"]
        self.assertEqual(found(texts, 0, "PROJECT"), [])

    def test_cue_still_finds_a_service_name(self):
        texts = ["Заявка на портал ELMA"]
        self.assertIn("ELMA", found(texts, 0, "PROJECT"))
