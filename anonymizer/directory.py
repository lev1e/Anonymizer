"""Человек по написанию имени. Справочника сотрудников в продукте нет: людей программа узнаёт по тексту.

Функция осталась для проверок распознавания, которые заводят известного человека вручную.
"""

from __future__ import annotations

import re

from .models import Person
from .morphology import split_full_name
from .util import opaque_id


def person_from_name(full_name: str, department: str = "", title: str = "", email: str = "",
                     employee_number: str = "", aliases: list[str] | None = None) -> Person:
    full = re.sub(r"\s+", " ", full_name.strip())
    surname, given, patronymic = split_full_name(full.split())
    return Person(opaque_id(12), opaque_id(7), full, surname, given, patronymic, department, title, email,
                  employee_number, aliases or [])
