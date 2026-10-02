from __future__ import annotations

import re
from functools import lru_cache

from . import lexicon, names_data
from .normalize import fold


MALE, FEMALE, UNKNOWN = "m", "f", "?"

HUSHING = "жчшщц"
VELAR = "гкхжчшщ"

PATRONYMIC_MALE = ("ович", "евич", "ьич", "ич")
PATRONYMIC_FEMALE = ("овна", "евна", "инична", "ична")

# Те же отчества в латинской записи: выгрузки из ERP, загранпаспорта, отсканированные
# переводы. Короткого «-ich» здесь намеренно нет — под него попадают «Munich», «sandwich»,
# «rich», и правило начало бы находить людей в обычном английском тексте. Длинные окончания
# такой двусмысленности не дают: английского слова на «-ovich» или «-ovna» не существует.
PATRONYMIC_LATIN = ("ovich", "evich", "ovych", "evych", "ievich", "yevich",
                    "ovna", "evna", "ichna", "inichna", "ovni", "evni")

SURNAME_ENDINGS = (
    "ов", "ев", "ин", "ын", "ский", "цкий", "ской", "цкой", "ова", "ева", "ина", "ына",
    "ская", "цкая", "их", "ых", "енко", "ко", "ук", "юк", "чук", "ян", "янц", "швили",
    "дзе", "адзе", "оглы", "уллин", "иев", "аев", "еев", "яев", "оев",
)

# Male given names that end in -а/-я and would otherwise be read as female.
MALE_A_NAMES = frozenset(fold(n) for n in names_data.MALE_A_ENDING)

# Names whose stem loses a vowel when declined; a suffix table alone gets these wrong.
FLEETING_STEMS = {
    "пётр": "Петр", "петр": "Петр", "павел": "Павл", "лев": "Льв", "яков": "Яков",
    "михаил": "Михаил", "даниил": "Даниил", "гавриил": "Гавриил", "самуил": "Самуил",
    "игнат": "Игнат", "тимофей": "Тимофе", "матвей": "Матве",
}

MALE_NAMES = frozenset(fold(n) for n in names_data.MALE_GIVEN_NAMES)
FEMALE_NAMES = frozenset(fold(n) for n in names_data.FEMALE_GIVEN_NAMES)

# Уменьшительные имена. Раньше они жили только в MALE_A_ENDING, то есть работали на род и
# склонение и ни на что больше: «Саша» и «Вова» не считались именами вовсе.
DIMINUTIVE_NAMES = frozenset(fold(n) for n in names_data.DIMINUTIVE_GIVEN_NAMES)

ENGLISH_NAMES = frozenset(fold(n) for n in names_data.ENGLISH_GIVEN_NAMES.split())

GIVEN_NAMES = MALE_NAMES | FEMALE_NAMES | DIMINUTIVE_NAMES

# Имена-омонимы обычных слов: именем считаются только рядом с фамилией или отчеством.
AMBIGUOUS_GIVEN = frozenset(fold(n) for n in names_data.AMBIGUOUS_GIVEN_NAMES.split())


def is_ambiguous_given(word: str) -> bool:
    return fold(word) in AMBIGUOUS_GIVEN

# Capitalised words that pair up constantly in Russian paperwork and are never a person.
# Without this list every "Российская Федерация" becomes a review item and buries the real ones.
STOP_WORDS = frozenset(fold(w) for w in (
    "Общество Обществом Обществу Общества Ограниченной Ответственностью Ответственности "
    "Акционерное Акционерным Акционерного Публичное Публичным Закрытое Открытое Товарищество "
    "Индивидуальный Предприниматель Предпринимателя Компания Компании Корпорация Холдинг Группа "
    "Российская Российской Федерация Федерации Республика Республики Область Области Край Края "
    "Округ Округа Район Района Город Города Москва Москве Москвы Санкт Петербург Петербурге "
    "Новосибирск Екатеринбург Казань Нижний Новгород Челябинск Самара Омск Ростов Уфа Красноярск "
    "Пермь Воронеж Волгоград Краснодар Саратов Тюмень Тольятти Ижевск Барнаул Ульяновск Иркутск "
    "Хабаровск Ярославль Владивосток Махачкала Томск Оренбург Кемерово Рязань Астрахань Пенза "
    "Договор Договора Договору Договором Контракт Контракта Соглашение Соглашения Приложение "
    "Приложения Дополнительное Спецификация Протокол Протокола Акт Акта Счёт Счет Счета Фактура "
    "Накладная Заявка Заявки Заказ Заказа Номер Номера Пункт Пункта Раздел Раздела Статья Статьи "
    "Настоящий Настоящим Настоящего Настоящее Стороны Сторона Сторонами Заказчик Заказчика "
    "Исполнитель Исполнителя Поставщик Поставщика Покупатель Покупателя Продавец Продавца "
    "Подрядчик Подрядчика Арендатор Арендодатель Генеральный Генерального Директор Директора "
    "Заместитель Заместителя Начальник Начальника Руководитель Руководителя Главный Главного "
    "Старший Ведущий Управление Управления Департамент Департамента Отдел Отдела Служба Службы "
    "Комитет Министерство Министерства Правительство Президент Совет Совета Собрание Комиссия "
    "Положение Положения Регламент Регламента Инструкция Инструкции Порядок Порядка Правила "
    "Требования Условия Оплата Оплаты Стоимость Стоимости Цена Цены Сумма Суммы Итого Всего "
    "Январь Января Февраль Февраля Март Марта Апрель Апреля Май Мая Июнь Июня Июль Июля Август "
    "Августа Сентябрь Сентября Октябрь Октября Ноябрь Ноября Декабрь Декабря Понедельник Вторник "
    "Среда Четверг Пятница Суббота Воскресенье Приказ Приказа Распоряжение Служебная Записка "
    "Пояснительная Техническое Задание Отчёт Отчет Отчёта Отчета Справка Справки Уведомление "
    "Согласование Согласовано Утверждено Утверждаю Подпись Подписи Дата Даты Место Экземпляр "
    "Приемки Приёмки Выполненных Оказанных Работ Услуг Товара Товаров Материалов Основании "
    "Соответствии Течение Целях Рамках Части Данные Данных Информация Информации Система Системы "
    "Проект Проекта Программа Программы Этап Этапа Версия Версии Список Перечень Реестр Таблица "
    "Рисунок График Диаграмма Форма Формы Бланк Образец Приложении Приложением "
    "Учет Учёт Учета Учёта Расчет Расчёт Расчеты Расчёты Расчетов Расчётов Ведение Ведения "
    "Обработка Обработки Разработка Разработки Подготовка Подготовки Формирование Формирования "
    "Компенсация Компенсации Сверка Сверки Начисление Начисления Списание Списания Отражение "
    "Контроль Контроля Анализ Анализа Планирование Мониторинг Сопровождение Обеспечение "
    "Взаимодействие Исполнение Утверждение Оформление Хранение Архивирование Актуализация "
    "Бухгалтер Бухгалтера Бухгалтерия Кассир Экономист Юрист Менеджер Специалист Инженер "
    "Филиал Филиала Филиале Головной Обособленное Подразделение Подразделения "
    "Ответственный Ответственная Ответственные Ответственного Ответственной Владелец Владельца "
    "Согласующий Утверждающий Проверяющий Разработчик Разработчика Автор Автора Контролёр "
    "Куратор Куратора Наименование Описание Комментарий Примечание Статус Тип Категория "
    "Уважаемый Уважаемая Уважаемые Уважаемого Уважаемой Дорогой Дорогая Дорогие "
    "Многоуважаемый Многоуважаемая Глубокоуважаемый Глубокоуважаемая Господин Госпожа "
    "Гражданин Гражданка Товарищ Коллеги Присутствовали Слушали Постановили"
).split())


@lru_cache(maxsize=4096)
def looks_like_patronymic(word: str) -> bool:
    """Отчество в любом падеже. Суффиксы знают только именительный, словарь — все формы."""
    if lexicon.shape(word).patronymic:
        return True
    low = fold(word)
    if len(low) < 5:
        return False
    if low.isascii():
        return low.endswith(PATRONYMIC_LATIN)
    return low.endswith(PATRONYMIC_FEMALE) or low.endswith(PATRONYMIC_MALE)


@lru_cache(maxsize=4096)
def maybe_patronymic(word: str) -> bool:
    """Мягкий признак — для слова, которое уже стоит третьим после распознанной пары.

    Там контекст сам по себе сильный, поэтому короткие отчества вроде «Фомич» и «Лукич»
    можно принять, не рискуя спутать их с обычным словом.
    """
    if lexicon.shape(word).patronymic:
        return True
    low = fold(word)
    if low.isascii():
        # Латиница обходится без послаблений: рядом стоящее ФИО опознаётся и по длинным
        # окончаниям, а короткие в английском тексте значат совсем другое.
        return len(low) >= 5 and low.endswith(PATRONYMIC_LATIN)
    return len(low) >= 4 and low.endswith(("ич", "вна", "чна", "шна"))


# The same surname shapes, but in any case form. `looks_like_surname` only recognises the
# nominative, which is right when learning a name and wrong when spotting "Ивановым И.И.".
SURNAME_ANY_CASE = re.compile(
    r"(?i)(?:"
    r"(?:ов|ев|ёв|ин|ын)(?:а|у|ым|е|ы|ых|ыми|ой|ою)?"
    r"|(?:ск|цк)(?:ий|ая|ое|ого|ому|им|ом|ой|ою|ую|ие|их|ими)"
    r"|(?:енко|ко|ук|юк|чук|ян|янц|швили|дзе|адзе|оглы|их|ых)"
    r")$")


@lru_cache(maxsize=4096)
def looks_like_surname(word: str) -> bool:
    """Фамилия в именительном падеже — форма, от которой строится склонение.

    Разбор в косвенном падеже здесь не годится: «Иванову» как фамилию словарь опознаёт, но
    записать человека под этой формой нельзя — склонение от неё пойдёт неверно.
    """
    info = lexicon.shape(word)
    if info.surname and info.nominative:
        return True
    low = fold(word)
    return len(low) >= 4 and low.endswith(SURNAME_ENDINGS)


@lru_cache(maxsize=4096)
def looks_like_surname_inflected(word: str) -> bool:
    if lexicon.shape(word).surname:
        return True
    if looks_like_latin_surname(word):
        return True
    low = fold(word)
    return len(low) >= 5 and bool(SURNAME_ANY_CASE.search(low))


# Латинские написания тех же имён. Русское ФИО попадает в латиницу постоянно — выгрузки из
# ERP, загранпаспорта, адреса почты, — и без этого набора «Ivanov Ivan» не распознаётся ничем.
# Произвольные иностранные имена сюда не входят намеренно: «John Smith» от «Data Sheet» без
# английского словаря не отличить, и правило под них съело бы точность целиком.
_TRANSLITERATED: frozenset[str] | None = None


def transliterated_given_names() -> frozenset[str]:
    """Строится один раз при первом обращении: полторы тысячи имён по четыре формы каждое —
    работа заметная, а нужна она только если в документах вообще встретилась латиница."""
    global _TRANSLITERATED
    if _TRANSLITERATED is None:
        _TRANSLITERATED = _build_transliterated_names()
    return _TRANSLITERATED


# Латинские окончания русских фамилий: -ov/-ev/-in/-sky/-enko и их варианты.
LATIN_SURNAME_ENDINGS = ("ov", "ev", "iev", "yev", "in", "yn", "ova", "eva", "ina", "yna",
                         "sky", "skiy", "skii", "ski", "skaya", "enko", "chuk", "yan", "shvili", "dze")


def _build_transliterated_names() -> frozenset[str]:
    forms: set[str] = set()
    for name in GIVEN_NAMES:
        # Имя уже свёрнуто через fold; транслитерация принимает любой регистр.
        for variant in transliterate(name):
            if len(variant) >= 3:
                forms.add(fold(variant))
    return frozenset(forms)


@lru_cache(maxsize=4096)
def looks_like_latin_surname(word: str) -> bool:
    low = fold(word)
    return len(low) >= 4 and low.isascii() and low.endswith(LATIN_SURNAME_ENDINGS)


@lru_cache(maxsize=4096)
def is_given_name(word: str) -> bool:
    """Личное имя в любом падеже: «Ивану», «Ольги», «Анне» — тоже имена."""
    if fold(word) in GIVEN_NAMES:
        return True
    if fold(word) in transliterated_given_names() or fold(word) in ENGLISH_NAMES:
        return True
    info = lexicon.shape(word)
    # Словарный разбор принимается, только если у слова НЕТ параллельного чтения ни как
    # обычной лексемы, ни как фамилии. Первое отсекает «Роман», «Вера», «Слава», «Лада»,
    # «Веста» — словарь честно помечает их Name, но в договоре это роман, вера, слава и марка
    # автомобиля. Второе отсекает «Сидоров» и «Ким»: у них есть редкое чтение как имени, и
    # без проверки пара «Пётр Сидоров» переставала разбираться — оба слова оказывались
    # именами, и ни одно не годилось на роль фамилии.
    return info.given and not info.lexical and not info.surname


_DIMINUTIVE_FORMS: frozenset[str] | None = None


def diminutive_forms() -> frozenset[str]:
    """Уменьшительное имя в любом падеже: «Саше», «Вову», «Тани»."""
    global _DIMINUTIVE_FORMS
    if _DIMINUTIVE_FORMS is None:
        forms: set[str] = set()
        for name in names_data.DIMINUTIVE_GIVEN_NAMES:
            for variant in decline_given(name):
                forms.add(fold(variant))
        _DIMINUTIVE_FORMS = frozenset(forms)
    return _DIMINUTIVE_FORMS


@lru_cache(maxsize=4096)
def is_diminutive_name(word: str) -> bool:
    """Только уменьшительное имя, без полных форм: признак человека, но слабый.

    Отдельно от `is_given_name`, потому что одного «Саши» мало для автозамены, а «Александра»
    в паре с фамилией — достаточно. Разделение позволяет детектору выбрать решение по силе
    довода, а не по факту попадания в словарь.
    """
    return fold(word) in diminutive_forms()


# Слова, которые одновременно и обиходные, и распространённые фамилии.
#
# Это единственный класс фамилий, который не берётся ни правилами, ни словарём: у них нет
# фамильного окончания, а OpenCorpora знает их как обычные существительные и на вопрос
# «фамилия ли это» отвечает «нет». Без списка «Дмитрий Тен» и «Цой В.Р.» остаются в
# документе открытыми — а это корейские, украинские и кавказские фамилии, которых в России
# миллионы носителей. Список намеренно короткий: сюда попадает слово, которое действительно
# часто встречается как фамилия, иначе он начнёт вырезать деловую лексику.
SURNAME_HOMONYMS = frozenset(fold(w) for w in (
    # корейские и китайские
    "Ким Пак Цой Ли Тен Тян Хан Юн Шин Сон Кан Нам Ан Мун Чан Хван Цхай Огай Дё Цзю "
    # украинские и белорусские
    "Шпак Заяц Бут Коваль Кравец Швец Ткач Мельник Бондарь Гончар Чабан Кушнир Дуда Дудь "
    "Соловей Журавель Лебедь Голуб Гуль Мороз Гроза Гладь Слюсарь Стельмах Тесля Гриб "
    # русские
    "Белый Чёрный Черный Король Королева Книга Рак Ван Голова Малыш Медведь Волк Орёл Орел "
    "Сокол Гусь Комар Муха Жук Зима Весна Лето Осень Крот Рысь Сом Карась Щука Окунь Ворон "
    "Грач Дрозд Скворец Чиж Стриж Ястреб Кузнец Пастух Столяр Пекарь Рыбак Мясник Череп "
    "Борода Хвост Кулак Шило Молот Топор Колос Сноп Мех Шуба Каша Борщ Пирог Сахар Соль"
).split())


@lru_cache(maxsize=4096)
def is_surname_homonym(word: str) -> bool:
    """Обиходное слово, которое при этом частая фамилия. Проверяется и в косвенном падеже."""
    if fold(word) in SURNAME_HOMONYMS:
        return True
    info = lexicon.shape(word)
    return bool(info.lemma) and fold(info.lemma) in SURNAME_HOMONYMS

COMMON_WORDS = frozenset(fold(w) for w in names_data.COMMON_WORDS) - SURNAME_HOMONYMS


@lru_cache(maxsize=8192)
def is_common_word(word: str) -> bool:
    """Слово из повседневной речи. Фамилией такое слово в документе не бывает.

    Частотный список закрывает разговорную лексику, но не деловую: «Логист», «Поставка»,
    «Выгрузка», «Реестр» в нём отсутствуют и раньше беспрепятственно становились фамилиями.
    Словарь OpenCorpora закрывает и то и другое — при условии, что разбор именно словарный
    и у слова нет параллельного чтения как фамилии или имени («Мороз», «Ким», «Заяц»).
    """
    if is_surname_homonym(word):
        return False
    info = lexicon.shape(word)
    if info.lexical and not (info.surname or info.given or info.patronymic):
        return True
    return fold(word) in COMMON_WORDS


@lru_cache(maxsize=8192)
def is_actor_verb(word: str) -> bool:
    """Глагол или краткое причастие: «подписал», «утверждаю», «получено», «принят».

    Список таких слов раньше был выписан вручную, и в нём отсутствовало ровно то, чего в нём
    не подумали написать: «Утверждаю: Голубев» и «Прошу Иванова» не давали ни одной находки,
    хотя это подпись под приказом и обычная строка служебной записки. Дописывать формы по
    одной бессмысленно — их у русского глагола десятки, и следующий пропуск найдётся так же.

    Краткое причастие входит намеренно: «получено», «подписано», «принят» стоят в тех же
    строках, что и глагол, и роль у них та же.
    """
    morph = lexicon.analyzer()
    if morph is None or not word:
        return False
    try:
        parses = morph.parse(word)
    except Exception:
        return False
    return any(parse.tag.POS in ("VERB", "PRTS") for parse in parses)


@lru_cache(maxsize=4096)
def is_toponym(word: str) -> bool:
    """Название места. По форме от фамилии не отличается вообще ничем.

    «Ростов», «Киров», «Пушкин», «Королёв», «Александров» — это и города, и фамилии, причём
    падежные окончания у них общие: «в Кирове» и «у Кирова» разбираются одинаково. Список
    городов здесь не поможет — их тысячи, и STOP_WORDS закрывает только крупнейшие. У
    OpenCorpora для топонимов есть отдельная пометка Geox; она и служит признаком.

    Гасить по ней всё подряд нельзя: настоящая фамилия «Пушкин» в документе тоже встречается.
    Признак нужен там, где других доводов за человека нет совсем, — тогда лучше промолчать.
    """
    morph = lexicon.analyzer()
    if morph is None:
        return False
    try:
        return any("Geox" in parse.tag for parse in morph.parse(word))
    except Exception:
        return False


@lru_cache(maxsize=4096)
def is_stop_word(word: str) -> bool:
    return fold(word) in STOP_WORDS


def gender_of(surname: str = "", given: str = "", patronymic: str = "") -> str:
    low_p = fold(patronymic)
    if low_p.endswith(PATRONYMIC_FEMALE):
        return FEMALE
    if low_p.endswith(PATRONYMIC_MALE) and len(low_p) >= 5:
        return MALE
    low_s = fold(surname)
    if low_s.endswith(("ова", "ева", "ина", "ына", "ская", "цкая", "ая")):
        return FEMALE
    if low_s.endswith(("ов", "ев", "ин", "ын", "ский", "цкий")):
        return MALE
    low_g = fold(given)
    if low_g in FEMALE_NAMES:
        return FEMALE
    if low_g in MALE_NAMES:
        return MALE
    if low_g.endswith(("а", "я")) and low_g not in MALE_A_NAMES:
        return FEMALE
    return UNKNOWN


def to_gender(surname: str, gender: str) -> str:
    """Согласовать фамилию с родом: Смирнов/Смирнова, Белинский/Белинская.

    Словарь приводит любую фамилию к мужской лемме, поэтому после разбора «Смирновой»
    получается «Смирнов». Род известен из отчества — им и правим, иначе в списке людей
    появится мужчина, которого в документе не было.
    """
    if not surname or gender == UNKNOWN:
        return surname
    low = fold(surname)
    if gender == FEMALE:
        if low.endswith(("ов", "ев", "ин", "ын")):
            return surname + "а"
        if low.endswith(("ский", "цкий", "ый", "ий")):
            return surname[:-2] + "ая"
        return surname
    if low.endswith(("ова", "ева", "ина", "ына")):
        return surname[:-1]
    if low.endswith(("ская", "цкая")):
        return surname[:-2] + "ий"
    return surname


def to_gender_patronymic(patronymic: str, gender: str) -> str:
    """То же для отчества: разбор «Петровне» даёт мужскую лемму «Петрович»."""
    if not patronymic or gender == UNKNOWN:
        return patronymic
    low = fold(patronymic)
    # Только правильные пары -ович/-овна и -евич/-евна. Отчества вроде «Фомич» образуют
    # женскую форму не по этому правилу («Фоминична»), и механическая замена дала бы «Фомна» —
    # лучше оставить как есть, чем выдумать несуществующее слово.
    if gender == FEMALE and low.endswith(("ович", "евич")):
        return patronymic[:-2] + "на"
    if gender == MALE and low.endswith(("овна", "евна")):
        return patronymic[:-2] + "ич"
    return patronymic


def _both(fn, word: str) -> set[str]:
    return fn(word, MALE) | fn(word, FEMALE)


def _first_declension(word: str) -> set[str] | None:
    """Endings in -а/-я decline the same way whatever the gender; keeping one copy stops the
    two branches from drifting apart when the rule is tuned."""
    low = fold(word)
    base = word[:-1]
    if low.endswith("я"):
        return {word} | {base + s for s in ("и", "е", "ю", "ей", "ею")}
    if low.endswith("а"):
        genitive = "и" if base and fold(base[-1]) in VELAR else "ы"
        return {word} | {base + s for s in (genitive, "е", "у", "ой", "ою")}
    return None


def decline_surname(word: str, gender: str = UNKNOWN) -> set[str]:
    """Every case form of a surname, generated from its nominative.

    Forward declension is the tractable direction: the nominative tells us the pattern,
    whereas guessing a stem backwards from an arbitrary form does not have a unique answer.
    """
    if not word or len(word) < 2:
        return {word} if word else set()
    if "-" in word:
        # Both halves of a double surname inflect: Иванову-Петрову, not Иванов-Петрову.
        parts = [decline_surname(part, gender) for part in word.split("-") if part]
        combined = {""}
        for part_forms in parts:
            combined = {f"{prefix}-{form}" if prefix else form for prefix in combined for form in part_forms}
        return combined
    if gender == UNKNOWN:
        return _both(decline_surname, word)
    low = fold(word)
    forms = {word}
    if low.endswith(("ых", "их", "аго", "ово")):
        return forms
    if low.endswith(("енко", "ко", "ло", "во", "но", "то", "ю", "и", "у", "е", "о")):
        return forms
    if gender == FEMALE:
        if low.endswith(("ова", "ева", "ина", "ына")):
            base = word[:-1]
            return forms | {base + "ой", base + "у", base + "ою"}
        if low.endswith(("ская", "цкая", "жая", "шая", "ная", "ая")):
            base = word[:-2]
            return forms | {base + "ой", base + "ую", base + "ою"}
        return forms | (_first_declension(word) or set())
    if low.endswith(("ов", "ев", "ин", "ын")):
        return forms | {word + s for s in ("а", "у", "ым", "е", "ы", "ых", "ыми")}
    if low.endswith(("ский", "цкий", "жий", "ший", "чий", "щий", "ний", "ый", "ий")):
        base = word[:-2]
        return forms | {base + s for s in ("ого", "ому", "им", "ом", "ые", "ие", "ых", "их", "ыми", "ими")}
    if low.endswith("ой"):
        base = word[:-2]
        return forms | {base + s for s in ("ого", "ому", "ым", "ом")}
    if low.endswith("ь"):
        base = word[:-1]
        return forms | {base + s for s in ("я", "ю", "ем", "е")}
    simple = _first_declension(word)
    if simple is not None:
        return forms | simple
    if low.endswith("й"):
        base = word[:-1]
        return forms | {base + s for s in ("я", "ю", "ем", "е")}
    if re.search(r"[бвгджзклмнпрстфхцчшщ]$", low):
        instrumental = "ем" if fold(low[-1]) in HUSHING else "ом"
        return forms | {word + s for s in ("а", "у", instrumental, "е")}
    return forms


def decline_given(word: str, gender: str = UNKNOWN) -> set[str]:
    if not word or len(word) < 2:
        return {word} if word else set()
    if gender == UNKNOWN:
        gender = gender_of(given=word)
        if gender == UNKNOWN:
            return _both(decline_given, word)
    low = fold(word)
    forms = {word}
    if gender == FEMALE:
        if low.endswith("ия"):
            base = word[:-2]
            return forms | {base + s for s in ("ии", "ию", "ией", "ией")}
        if low.endswith("ья"):
            base = word[:-2]
            return forms | {base + s for s in ("ьи", "ье", "ью", "ьей")}
        simple = _first_declension(word)
        if simple is not None:
            return forms | simple
        if low.endswith("ь"):
            base = word[:-1]
            return forms | {base + "и", base + "ью"}
        return forms
    if low.endswith(("й", "ь")):
        base = word[:-1]
        return forms | {base + s for s in ("я", "ю", "ем", "е")}
    simple = _first_declension(word)
    if simple is not None:
        return forms | simple
    if re.search(r"[бвгджзклмнпрстфхцчшщ]$", low):
        stem = FLEETING_STEMS.get(low, word)
        instrumental = "ем" if fold(stem[-1]) in HUSHING else "ом"
        return forms | {stem + s for s in ("а", "у", instrumental, "е")}
    return forms


def decline_patronymic(word: str, gender: str = UNKNOWN) -> set[str]:
    if not word or len(word) < 4:
        return {word} if word else set()
    low = fold(word)
    forms = {word}
    if low.endswith(PATRONYMIC_FEMALE):
        base = word[:-1]
        return forms | {base + s for s in ("ы", "е", "у", "ой", "ою")}
    if low.endswith(PATRONYMIC_MALE):
        return forms | {word + s for s in ("а", "у", "ем", "е")}
    return forms


_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "shch", "ы": "y", "э": "e", "ю": "yu", "я": "ya", "ь": "", "ъ": "",
})


# Варианты латиницы для каждой русской буквы: загранпаспорт, почта, ISO 9, GOST и «как слышится» дают разные написания одного
# имени (Aleksandr / Alexander, Andrey / Andrei / Andrej, Zaytsev / Zaitsev). Совпасть должны все.
_LETTER_VARIANTS = {
    "а": ("a",), "б": ("b",), "в": ("v",), "г": ("g",), "д": ("d",), "е": ("e",), "ё": ("e", "yo", "io", "jo"),
    "ж": ("zh", "j"), "з": ("z",), "и": ("i", "y"), "й": ("y", "i", "j"), "к": ("k", "c"), "л": ("l",), "м": ("m",),
    "н": ("n",), "о": ("o",), "п": ("p",), "р": ("r",), "с": ("s",), "т": ("t",), "у": ("u",), "ф": ("f", "ph"),
    "х": ("kh", "h", "x"), "ц": ("ts", "c", "tc", "z"), "ч": ("ch", "c"), "ш": ("sh",), "щ": ("shch", "sch", "sh"),
    "ъ": ("",), "ы": ("y", "i"), "ь": ("", "y"), "э": ("e",), "ю": ("yu", "iu", "ju", "u"), "я": ("ya", "ia", "ja", "a"),
}
# Сочетания, у которых своё написание: «ий» — Dmitriy/Dmitry/Dmitri, «кс» — Alexander/Aleksandr, «ей» — Sergey/Sergei.
_COMBOS = {
    "ий": ("iy", "y", "i", "ii", "ij"), "ый": ("y", "yi", "iy", "ii", "yj", "yy"), "ей": ("ey", "ei", "ej", "y"),
    "ай": ("ay", "ai", "aj"), "ой": ("oy", "oi", "oj", "y"), "уй": ("uy", "ui"), "ия": ("ia", "iya", "iia", "ija"),
    "ье": ("ye", "ie", "je"), "ья": ("ya", "ia", "ja"), "кс": ("ks", "x"), "ьи": ("yi", "ii"),
}
MAX_TRANSLIT_VARIANTS = 48
_END_ONLY = frozenset({"ий", "ый", "ей", "ай", "ой", "уй"})
# Привычные английские формы имён: Александр — Alexander, Михаил — Michael.
_ENGLISH_EQUIVALENTS = {"александр": ("Alexander",), "александра": ("Alexandra",), "михаил": ("Michael",), "андрей": ("Andrew",),
                        "николай": ("Nicholas",), "пётр": ("Peter",), "петр": ("Peter",), "павел": ("Paul",), "елена": ("Helen",),
                        "татьяна": ("Tatiana",), "екатерина": ("Catherine", "Ekaterina"), "анна": ("Anne", "Ann"),
                        "мария": ("Mary",), "василий": ("Basil",), "константин": ("Konstantin",)}


def _initial_variants(letter: str) -> tuple[str, ...] | None:
    """Гласная в начале слова читается с йотом: «Евгений» — Yevgeny/Evgeny, «Юрий» — Yuri/Iurii."""
    return {"е": ("ye", "e", "je"), "ё": ("yo", "e", "io"), "ю": ("yu", "iu", "ju"), "я": ("ya", "ia", "ja"),
            "э": ("e",)}.get(letter)


def transliterate(word: str) -> set[str]:
    """Латинские написания русского слова: перебор вариантов букв и сочетаний, не больше `MAX_TRANSLIT_VARIANTS`."""
    if not word:
        return set()
    return set(_transliterate(word))


# Написания, которые перебор по буквам не даёт: «Евгений» — Eugeniy, «Наталия» — Natalya, «Александр» — Aleksander,
# «Андреев» — Andreyev. Добавляются поверх основных, чтобы не вытеснять их из лимита.
_VARIANT_RULES = (
    (re.compile(r"^ev"), "eu"),
    (re.compile(r"i[iy]?a$"), "ya"),
    (re.compile(r"(?<=[bdfgkpstvz])r$"), "er"),
    (re.compile(r"(?<=[aeiou])e"), "ye"),
)


@lru_cache(maxsize=65536)
def _transliterate(word: str) -> frozenset[str]:
    low = word.lower()
    partial: list[str] = [""]
    i = 0
    while i < len(low):
        pair = low[i:i + 2]
        if pair in _COMBOS and (pair not in _END_ONLY or i + 2 == len(low)):
            options = _COMBOS[pair]
            i += 2
        else:
            options = (_initial_variants(low[i]) if i == 0 else None) or _LETTER_VARIANTS.get(low[i], (low[i],))
            i += 1
        nxt = [p + o for p in partial for o in options]
        partial = nxt[:MAX_TRANSLIT_VARIANTS * 4]
    # Самые вероятные написания идут первыми: обрезка не должна выбросить основное.
    base = word.lower().translate(_TRANSLIT)
    ordered = [base] + sorted(set(partial) - {base}, key=lambda v: (abs(len(v) - len(base)), v))
    kept = list(dict.fromkeys(ordered))[:MAX_TRANSLIT_VARIANTS]
    extra = {base.replace("iya", "ia").replace("yy", "y"), base.replace("kh", "h").replace("ts", "c"),
             *_ENGLISH_EQUIVALENTS.get(low, ())}
    for pattern, replacement in _VARIANT_RULES:
        extra.update(pattern.sub(replacement, v) for v in kept if pattern.search(v))
    return frozenset(v.title() for v in [*kept, *extra] if len(v) >= 2)


def split_full_name(parts: list[str]) -> tuple[str, str, str]:
    """Assign surname / given / patronymic by position, using the patronymic as the anchor.

    Anchoring on the patronymic is what keeps surnames ending in -ич (Рабинович, Абрамович)
    out of the patronymic slot: position decides, shape only breaks ties.
    """
    parts = [p for p in parts if p]
    if not parts:
        return "", "", ""
    if len(parts) == 1:
        return parts[0], "", ""
    if len(parts) == 2:
        first_is_surname = looks_like_surname(parts[0]) and not is_given_name(parts[0])
        second_is_surname = looks_like_surname(parts[1]) and not is_given_name(parts[1])
        if is_given_name(parts[0]) and not is_given_name(parts[1]):
            return parts[1], parts[0], ""
        if is_given_name(parts[1]) and not is_given_name(parts[0]):
            return parts[0], parts[1], ""
        if second_is_surname and not first_is_surname:
            return parts[1], parts[0], ""
        return parts[0], parts[1], ""
    if looks_like_patronymic(parts[2]):
        return parts[0], parts[1], parts[2]
    if looks_like_patronymic(parts[1]):
        return parts[2], parts[0], parts[1]
    return parts[0], parts[1], parts[2]


def is_english_given(word: str) -> bool:
    """Английское или европейское имя из списка: они принимаются в паре с заглавным словом, как «John Smith»."""
    return fold(word) in ENGLISH_NAMES
