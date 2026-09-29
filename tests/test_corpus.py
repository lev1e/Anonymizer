"""Замер полноты распознавания на массиве фамилий, которых нет во встроенных словарях.

Полнота считается по покрытию: фрагмент засчитан, только если в нём не осталось ни одной
непокрытой буквы. Отдельно считается число ложных срабатываний на деловом тексте, в котором
нет ни одного ФИО, — расти оно не должно никогда.
"""
import random
import unittest

from anonymizer.detectors import Detector
from anonymizer.models import Decision, Settings
from anonymizer.names_data import FEMALE_GIVEN_NAMES, MALE_GIVEN_NAMES

from .surnames_sample import FREQUENT_SURNAMES

NBSP = " "
BUSINESS_PROSE = """Договор Поставки № 44-А заключён между Обществом с Ограниченной Ответственностью и
Акционерным Обществом. Российская Федерация, Московская Область, Ленинский Район.
Приложение Номер Два к Дополнительному Соглашению. Генеральный Директор утвердил Положение
Об Оплате Труда. Настоящим Стороны подтверждают Обязательства. Техническое Задание,
Служебная Записка, Пояснительная Записка, Товарная Накладная, Счёт Фактура.
Ответственный Исполнитель: Отдел Логистики. Согласующий: Юридическая Служба.
Учет Расчетов, Ведение Плана Счетов, Формирование Уведомлений, Сверка Расчетов.
Заказ 1234567890 от 15.03.2024, позиция 123456789012, строка 40702, ГОСТ Р 51141-98.
Транспортный Отдел, Отдел Сервиса, Головной Офис, Обособленное Подразделение ДВФ.
Оборудование ГСМ, Система ЕНС, Регламент ISO 9001, Стандарт Качества."""


def _patronymic(base: str, female: bool) -> str:
    low = base.lower()
    if low.endswith(("й", "ь")):
        return base[:-1] + ("евна" if female else "евич")
    if low.endswith("а"):
        return base[:-1] + ("ична" if female else "ич")
    return base + ("овна" if female else "ович")


def build_people(count: int, seed: int = 20260825) -> list[dict]:
    rnd = random.Random(seed)
    people, used = [], set()
    while len(people) < count:
        surname = rnd.choice(FREQUENT_SURNAMES)
        if surname in used:            # однофамильцы разбираются отдельным тестом
            continue
        used.add(surname)
        female = surname.lower().endswith(("ова", "ева", "ина", "ына", "ская", "цкая"))
        given = rnd.choice(FEMALE_GIVEN_NAMES if female else MALE_GIVEN_NAMES)
        people.append({"surname": surname, "given": given,
                       "patronymic": _patronymic(rnd.choice(MALE_GIVEN_NAMES), female)})
    return people


def spellings(p: dict) -> dict[str, str]:
    s, n, o = p["surname"], p["given"], p["patronymic"]
    i, j = n[0], o[0]
    return {
        "Фамилия Имя Отчество": f"{s} {n} {o}",
        "Имя Отчество Фамилия": f"{n} {o} {s}",
        "Имя Фамилия": f"{n} {s}",
        "Фамилия Имя": f"{s} {n}",
        "Фамилия И.О.": f"{s} {i}.{j}.",
        "Фамилия И. О.": f"{s} {i}. {j}.",
        "И.О. Фамилия": f"{i}.{j}. {s}",
        "И. О. Фамилия": f"{i}. {j}. {s}",
        "Фамилия И.": f"{s} {i}.",
        "Фамилия ИО слитно": f"{s} {i}{j}",
        "Фамилия И.О без точки": f"{s} {i}.{j}",
        "ВЕРХНИЙ РЕГИСТР": f"{s} {n} {o}".upper(),
        "нижний регистр": f"{s} {n} {o}".lower(),
        "неразрывный пробел": f"{s}{NBSP}{i}.{j}.",
        "двойной пробел": f"{s}  {n}  {o}",
        "перенос строки": f"{s}\n{n} {o}",
    }


def fully_covered(findings, fragment: str, line: str) -> bool:
    start, end = line.index(fragment), line.index(fragment) + len(fragment)
    position = start
    for a, b in sorted((f.start, f.end) for f in findings if f.decision == Decision.AUTO):
        if b <= position or a >= end:
            continue
        if a > position and any(ch.isalpha() for ch in line[position:a]):
            return False
        position = max(position, b)
    return not any(ch.isalpha() for ch in line[position:end])


class CorpusRecallTests(unittest.TestCase):
    PEOPLE = 200
    MINIMUM_RECALL = .99

    @classmethod
    def setUpClass(cls):
        cls.people = build_people(cls.PEOPLE)
        cls.detector = Detector([], Settings())
        cls.detector.harvest(". ".join(f"{p['surname']} {p['given']} {p['patronymic']}"
                                       for p in cls.people), "f")

    def test_every_person_is_registered(self):
        self.assertEqual(len(self.detector.discovered), self.PEOPLE)

    def test_recall_per_spelling_format(self):
        weak = []
        for label in spellings(self.people[0]):
            hits = 0
            for p in self.people:
                fragment = spellings(p)[label]
                line = f"Согласовано, {fragment}, отдел логистики."
                if fully_covered(self.detector.scan(line, "f", "l"), fragment, line):
                    hits += 1
            rate = hits / len(self.people)
            if rate < self.MINIMUM_RECALL:
                weak.append(f"{label}: {rate:.1%}")
        self.assertEqual(weak, [])

    def test_business_prose_has_no_findings(self):
        findings = Detector([], Settings.persons_only()).scan(BUSINESS_PROSE, "f", "l")
        # Номер договора («№ 44-А») скрывается намеренно: он позволяет опознать сделку.
        self.assertEqual([(f.category, f.original) for f in findings if f.category != "CONTRACT"], [])

    def test_namesakes_are_flagged_not_guessed(self):
        """Два человека с одной фамилией и одинаковыми инициалами — это review, не догадка."""
        detector = Detector([], Settings())
        detector.harvest("Кулешова Анна Андреевна. Кулешова Алиса Артёмовна.", "f")
        findings = detector.scan("Кулешова А.А. подписала", "f", "l")
        self.assertEqual([f.decision for f in findings], [Decision.REVIEW])
        self.assertEqual(len(findings[0].candidates), 2)


if __name__ == "__main__":
    unittest.main()
