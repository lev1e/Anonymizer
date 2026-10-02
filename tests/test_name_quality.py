"""Порог точности и полноты распознавания ФИО на размеченном корпусе.

Отличается от `test_corpus`: там полнота меряется на сгенерированных ФИО правильной формы,
здесь — на выписанных вручную случаях, каждый из которых когда-то ломался. Половина корпуса
отрицательная: деловой текст, в котором людей нет и любая находка засчитывается как ошибка.

Пороги стоят чуть ниже достигнутого, чтобы тест ловил регресс, а не дрожание на границе.
"""
import unittest

from anonymizer.detectors import Detector
from anonymizer.models import Settings

from .name_corpus import NEGATIVE, POSITIVE

PERSONISH = {"PERSON", "POSSIBLE_PERSON"}

MIN_PRECISION = 0.97
MIN_RECALL = 0.93


def measure() -> tuple[float, float, list[str], list[str]]:
    detector = Detector([], Settings())
    for text, _ in POSITIVE:
        detector.harvest(text, "corpus.txt")
    for text in NEGATIVE:
        detector.harvest(text, "corpus.txt")

    hits = misses = 0
    missed: list[str] = []
    for text, expected in POSITIVE:
        spans = [f.original for f in detector.scan(text, "corpus.txt", "body")
                 if f.category in PERSONISH]
        for name in expected:
            if any(name in span or span in name for span in spans):
                hits += 1
            else:
                misses += 1
                missed.append(f"{name!r} в {text!r}, найдено {spans}")

    false_positives: list[str] = []
    for text in NEGATIVE:
        for finding in detector.scan(text, "corpus.txt", "body"):
            if finding.category in PERSONISH:
                false_positives.append(f"{finding.original!r} в {text!r} — {finding.reason}")

    precision = hits / (hits + len(false_positives)) if hits or false_positives else 1.0
    recall = hits / (hits + misses) if hits or misses else 1.0
    return precision, recall, missed, false_positives


class NameQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.precision, cls.recall, cls.missed, cls.false_positives = measure()

    def test_precision(self):
        self.assertGreaterEqual(
            self.precision, MIN_PRECISION,
            f"точность {self.precision:.3f}; ложные срабатывания:\n  " + "\n  ".join(self.false_positives))

    def test_recall(self):
        self.assertGreaterEqual(
            self.recall, MIN_RECALL,
            f"полнота {self.recall:.3f}; пропущено:\n  " + "\n  ".join(self.missed))

    def test_learned_names_are_normalised(self):
        """Выученное ФИО должно осесть в списке в именительном падеже и с верным родом.

        Иначе один человек, упомянутый в трёх падежах, распадается на три записи, и каждое
        его упоминание уходит в ручную проверку как неоднозначное.
        """
        documents = [
            "Приказываю выдать Иванову Ивану Ивановичу материальную помощь",
            "Доверенность выдана Смирновой Анне Петровне",
            "в отношении Кузнецовой Ольги Дмитриевны",
            "от Гаврилюка Сергея Владимировича",
        ]
        detector = Detector([], Settings())
        for text in documents:
            detector.harvest(text, "orders.docx")
        learned = sorted(p.full_name for p in detector.discovered.values())
        self.assertEqual(learned, [
            "Гаврилюк Сергей Владимирович",
            "Иванов Иван Иванович",
            "Кузнецова Ольга Дмитриевна",
            "Смирнова Анна Петровна",
        ])

    def test_case_forms_and_initials_share_one_identity(self):
        """«Иванову Ивану Ивановичу» и «Ивановым И.И.» — один человек, а не два."""
        detector = Detector([], Settings())
        for text in ("Приказываю выдать Иванову Ивану Ивановичу", "Согласовано Ивановым И.И."):
            detector.harvest(text, "orders.docx")
        full = detector.scan("Приказываю выдать Иванову Ивану Ивановичу", "orders.docx", "body")
        short = detector.scan("Согласовано Ивановым И.И.", "orders.docx", "body")
        self.assertEqual(len(full), 1)
        self.assertEqual(len(short), 1)
        self.assertEqual(full[0].person_id, short[0].person_id)

    def test_software_name_beside_initials_is_not_a_person(self):
        """Регресс-тест на самую дорогую ошибку разбора.

        «Excel A.B.» заводил человека по фамилии Excel, после чего каждое упоминание Excel во
        всех файлах проекта вырезалось как ФИО — и восстановить его было уже нечем.
        """
        detector = Detector([], Settings())
        documents = ["Выгрузка в Excel A.B. подтверждена", "Файл Excel обновлён",
                     "Логист А.П. отгрузил товар", "Логист принял груз",
                     "Поставка К.Т. задержана", "Поставка выполнена"]
        for text in documents:
            detector.harvest(text, "sheet.xlsx")
        for text in documents:
            found = [f.original for f in detector.scan(text, "sheet.xlsx", "A1")
                     if f.category in PERSONISH]
            self.assertEqual(found, [], f"{text!r} дал находку {found}")


if __name__ == "__main__":
    unittest.main()
