import hashlib
import re
import secrets
from collections.abc import Iterable

ROUNDING = "Округление по условию"
PERCENTAGES = "Проценты"
REVIEW = "Разбор решения"
PROPORTIONS = "Пропорции"
UNITS = "Единицы измерения"

TOPICS = {
    "brackets": (
        "Раскрытие скобок",
        "Нужно выражение с множителем перед скобками и суммой внутри.",
    ),
    "signs": ("Знаки и перенос слагаемых", "Нужно уравнение с переносом слагаемых между частями."),
    "fractions": ("Дроби", "Нужны дроби и действия с ними."),
    "arithmetic": ("Арифметика", "Нужны числовые вычисления."),
    "rounding": (
        ROUNDING,
        "Нужно выбрать направление округления по условию, например при ограниченном "
        "бюджете или при расчёте минимального числа упаковок.",
    ),
    "percentages": (PERCENTAGES, "Нужен расчёт процента, скидки или изменения на процент."),
    "proportions": (PROPORTIONS, "Нужно использовать пропорцию или постоянное отношение."),
    "units": (UNITS, "Нужен перевод величины из одной единицы измерения в другую."),
    "probability": (
        "Вероятность",
        "Нужно найти вероятность события; способ случайного выбора должен быть понятен.",
    ),
    "statistics": (
        "Статистика",
        "Нужно вычислить характеристику набора данных, например среднее, медиану или размах.",
    ),
    "equations": ("Уравнения", "Нужно уравнение с неизвестным."),
    "powers": ("Степени и корни", "Нужны степени или корни как существенная часть задания."),
    "inequalities": (
        "Неравенства",
        "Нужно неравенство с неизвестным и проверяемым множеством решений.",
    ),
    "expressions": (
        "Преобразования выражений",
        "Нужно упростить или разложить буквенное выражение.",
    ),
    "functions": (
        "Функции и координаты",
        "Задай формулу в виде f(x) и попроси вычислить f(число).",
    ),
    "geometry": ("Геометрия", "Нужна геометрическая задача с достаточными исходными данными."),
    "logic": ("Логика рассуждений", "Нужна задача на логический вывод из достаточных условий."),
    "review": (REVIEW, "Нужна однозначная школьная математическая задача."),
    "other": ("Другое", "Нужна однозначная школьная математическая задача."),
}

MAP_TOPICS = frozenset(label for label, _ in TOPICS.values() if label not in {REVIEW, "Другое"})


def valid_topic(skill: str) -> bool:
    return bool(
        isinstance(skill, str)
        and re.fullmatch(r"[А-ЯЁа-яё][А-ЯЁа-яё ,()–—-]{2,59}", skill)
        and skill.casefold()
        not in {
            "другое",
            "разное",
            "разбор решения",
            "математика",
            "ошибка",
            "неизвестно",
        }
    )


def topic_id(skill: str) -> str:
    return next(
        (key for key, (label, _) in TOPICS.items() if label == skill),
        hashlib.sha256(skill.encode()).hexdigest()[:12],
    )


def topic_buttons(skills: Iterable[str]) -> list[list[dict[str, str]]]:
    return [
        [{"type": "callback", "text": label, "payload": "practice_" + topic_id(label)}]
        for label in dict.fromkeys(skills)
    ]


def fallback_exercise(skill: str, previous: str, *, number: int | None = None) -> str | None:

    for n in range(number if number is not None else 2 + secrets.randbelow(13), 18):
        candidates = {
            "Раскрытие скобок": f"Раскрой скобки и реши уравнение: {n}(x + 3) = {n * 7}",
            "Знаки и перенос слагаемых": f"Реши уравнение: {n}x + {n + 4} = {n * 6 + 4}",
            "Уравнения": f"Реши уравнение: {n}x - {n + 1} = {n * 4 - 1}",
            "Дроби": f"Вычисли и сократи дробь: 1/{n} + 1/{2 * n}",
            "Степени и корни": f"Упрости выражение: ({n}^3 * {n}^2) / {n}^4",
            "Неравенства": f"Реши неравенство: -{n}x + {n} < {n * 4}",
            "Преобразования выражений": f"Упрости выражение: (x + {n})^2 - x^2",
            "Функции и координаты": f"Дана функция f(x) = {n}x + 1. Вычисли f(3).",
            "Арифметика": f"Вычисли: ({n + 7} - 3) * 4 + 2",
            ROUNDING: f"Один блокнот стоит {n + 20} рублей. Какое наибольшее число блокнотов "
            f"можно купить на {(n + 20) * 5 + 7} рублей?",
            PERCENTAGES: f"Цена книги {n * 100} рублей. Какой станет цена после скидки 15%?",
            PROPORTIONS: f"На 4 порции нужно {n * 40} граммов риса. Сколько нужно на 7 порций?",
            UNITS: f"Вырази в сантиметрах {n} метров и 45 сантиметров.",
            "Вероятность": f"В мешке {n} красных и {n + 3} синих шаров. "
            "Наугад достают один шар. Какова вероятность, что он красный?",
            "Статистика": f"Даны результаты четырёх измерений: {n}, {n + 1}, "
            f"{n + 4}, {n + 7}. Найди среднее арифметическое и медиану.",
            "Геометрия": f"Стороны прямоугольника {n} см и {n + 3} см. Найди его периметр.",
            "Логика рассуждений": f"На полке стоят {n} красных и {n + 1} синих книг. "
            "Сколько книг нужно взять не глядя, чтобы среди них обязательно была синяя?",
            REVIEW: f"Продолжи последовательность ещё на два числа: {n}, {n + 3}, {n + 6}. "
            "Каждое следующее число больше предыдущего на 3.",
        }
        problem = candidates.get(REVIEW if skill == "Другое" else skill)
        if problem and problem.casefold() != previous.strip().casefold():
            return problem
    return None


def practice_shape_matches(skill: str, problem: str) -> bool:

    text = problem.casefold()
    if skill == ROUNDING:
        return bool(re.search(r"округл|целых|целое|наибольш|наименьш|максимальн|минимальн", text))
    if skill == PERCENTAGES:
        return bool(re.search(r"\d\s*%|процент", text))
    if skill == "Вероятность":
        return bool(re.search(r"вероятн|наугад|случайн|шанс", text))
    if skill == "Статистика":
        return bool(re.search(r"средн|медиан|мод[ауые]\b|размах|частот|квартил", text))
    if skill == "Неравенства":
        return bool(re.search(r"[<>≤≥]|неравен", text))
    if skill == "Преобразования выражений":
        return bool(re.search(r"[xyabх]", text) and re.search(r"упрост|разлож|тождеств", text))
    if skill == "Функции и координаты":
        return bool(re.search(r"функци|координат|f\(x\)|y\s*=", text))
    if skill == "Раскрытие скобок":
        return bool(re.search(r"(?:\d|[xyх])\s*\([^)]*[+−-][^)]*\)", text))
    if skill == "Дроби":
        return bool(re.search(r"[/⁄÷:]|дроб|делени", text))
    if skill == "Степени и корни":
        return bool(re.search(r"[\^√²³]|степен|корн|корен", text))
    if skill in {"Уравнения", "Знаки и перенос слагаемых"}:
        return "=" in text and bool(re.search(r"[xyzх]|неизвестн", text))
    if skill == "Геометрия":
        return bool(
            re.search(r"треуголь|угол|окруж|площад|отрез|периметр|квадрат|прямоуголь", text)
        )
    return True


def linear_error_skill(problem: str, steps: list[str], index: int) -> str:

    before = problem if index == 1 else steps[index - 2]
    after = steps[index - 1]
    if "(" in before and "(" not in after:
        return "Раскрытие скобок"
    if re.search(r"[\^√²³]", before) and not re.search(r"[\^√²³]", after):
        return "Степени и корни"

    if before.count("=") == after.count("=") == 1:
        for left, right in zip(before.split("="), after.split("="), strict=True):
            if re.search(r"[xх].*[+−-]\s*\d", left) and re.fullmatch(
                r"\s*[+−-]?\s*\d*\s*\*?\s*[xх]\s*", right
            ):
                return "Знаки и перенос слагаемых"
    return "Уравнения"
