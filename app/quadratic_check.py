"""Проверка квадратных уравнений и формул Виета."""

import ast
import re
from fractions import Fraction
from math import isqrt

from app.learning_models import TutorAnalysis
from app.math_check import MAX_AST_DEPTH, MAX_AST_NODES, MAX_FRACTION_BITS, MAX_INTEGER_DIGITS

_SUM = {"x_1+x_2", "x_2+x_1"}
_PRODUCT = {"x_1*x_2", "x_2*x_1"}
_ROOTS = {"x_1", "x_2", "x"}


def _clean(text: str) -> str:
    text = text.casefold().replace("х", "x").replace("−", "-")
    text = text.replace("²", "**2").replace("^", "**").replace("×", "*").replace("·", "*")
    text = text.replace("÷", "/").replace(":", "/")
    text = re.sub(r"√\s*(\([^()]*\)|\d+(?:[.,]\d+)?|d\b)", r"sqrt(\1)", text)
    return re.sub(r"(?<=\d),(?=\d)", ".", text)


def _polynomial(source: str, constants: dict[str, Fraction] | None = None) -> tuple:
    source = _clean(source).strip()
    if not source or len(source) > 1000:
        raise ValueError
    if not re.fullmatch(r"(?:sqrt|[\d\s.xabcd+*/()\-])+", source):
        raise ValueError
    if any(len(n.replace(".", "")) > MAX_INTEGER_DIGITS for n in re.findall(r"\d[\d.]*", source)):
        raise ValueError
    source = re.sub(r"(?<=[\dx)])\s*(?=[xabcd(]|sqrt\()", "*", source)
    tree = ast.parse(source, mode="eval").body
    pending, count = [(tree, 0)], 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > MAX_AST_NODES or depth > MAX_AST_DEPTH:
            raise ValueError
        pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))

    def bound(values):
        if any(
            max(x.numerator.bit_length(), x.denominator.bit_length()) > MAX_FRACTION_BITS
            for x in values
        ):
            raise ValueError
        return tuple(values)

    def multiply(left, right):
        values = [Fraction(0)] * 5
        for i, a in enumerate(left):
            for j, b in enumerate(right):
                values[i + j] += a * b
        if any(values[3:]):
            raise ValueError
        return bound(values[:3])

    def value(node):
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            return bound((Fraction(ast.get_source_segment(source, node)), Fraction(0), Fraction(0)))
        if isinstance(node, ast.Name):
            if constants is not None and node.id in constants:
                return constants[node.id], Fraction(0), Fraction(0)
            if node.id == "x" and constants is None:
                return Fraction(0), Fraction(1), Fraction(0)
            raise ValueError
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            result = value(node.operand)
            return result if isinstance(node.op, ast.UAdd) else bound([-x for x in result])
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "sqrt"
            and len(node.args) == 1
            and not node.keywords
        ):
            number, linear, quadratic = value(node.args[0])
            if number < 0 or linear or quadratic:
                raise ValueError
            root = Fraction(isqrt(number.numerator), isqrt(number.denominator))
            if root * root != number:
                raise ValueError
            return root, Fraction(0), Fraction(0)
        if isinstance(node, ast.BinOp):
            left, right = value(node.left), value(node.right)
            if isinstance(node.op, ast.Add):
                return bound([a + b for a, b in zip(left, right, strict=True)])
            if isinstance(node.op, ast.Sub):
                return bound([a - b for a, b in zip(left, right, strict=True)])
            if isinstance(node.op, ast.Mult):
                return multiply(left, right)
            if isinstance(node.op, ast.Div) and not any(right[1:]) and right[0]:
                return bound([a / right[0] for a in left])
            if isinstance(node.op, ast.Pow) and not any(right[1:]) and right[0] in {0, 1, 2}:
                if right[0] == 0 and any(left):
                    return Fraction(1), Fraction(0), Fraction(0)
                if right[0] == 1:
                    return left
                if right[0] == 2:
                    return multiply(left, left)
        raise ValueError

    return value(tree)


def _coefficients(problem: str) -> dict[str, Fraction]:
    text = re.sub(
        r"(?i)^(?:реши(?:те)?\s+)?(?:квадратное\s+)?уравнение\s*:?\s*", "", problem.strip()
    )
    if text.count("=") != 1:
        raise ValueError
    left, right = map(_polynomial, text.split("="))
    c, b, a = [x - y for x, y in zip(left, right, strict=True)]
    if not a:
        raise ValueError
    return {"a": a, "b": b, "c": c}


def _root_notation(text: str) -> str:
    text = _clean(text).replace("₁", "_1").replace("₂", "_2")
    return re.sub(r"x\s*_?\s*([12])", r"x_\1", text)


def _number(source: str, constants: dict[str, Fraction]) -> Fraction:
    number, linear, quadratic = _polynomial(source, constants)
    if linear or quadratic:
        raise ValueError
    return number


def _analysis(error: int | None, complete: bool, skill: str) -> TutorAnalysis:
    arithmetic = skill == "Арифметика"
    return TutorAnalysis(
        status="error" if error else "correct",
        first_error_step=error,
        skill=skill,
        complete=not error and complete,
        question=(
            "Как проверить, что соседние части равенства дают одно значение?"
            if arithmetic
            else "Как связаны корни с коэффициентами уравнения?"
        ),
        hint=(
            "Вычисли каждую часть равенства отдельно и сравни значения."
            if arithmetic
            else "Проверь знаки и старший коэффициент. Проверь корни подстановкой."
        ),
        detail=(
            "Пересчитай промежуточные значения и сверь их по порядку. При делении "
            "проверь частное обратным умножением. Затем подставь результат в уравнение."
            if arithmetic
            else "Приведи уравнение к стандартному виду. Сверь формулы с коэффициентами "
            "и проверь найденные корни подстановкой."
        ),
        discovery="Найди первую неверную запись и объясни применённое правило.",
    )


def quadratic_chain_error(problem: str, steps: list[str]) -> TutorAnalysis | None:
    try:
        _coefficients(problem)
    except (ValueError, ArithmeticError, SyntaxError, RecursionError):
        return None
    for index, row in enumerate(steps, 1):
        left, *parts = _root_notation(row).split("=")
        if not 2 <= len(parts) <= 11 or re.sub(r"\s", "", left) not in _ROOTS | {"d", "sqrt(d)"}:
            continue
        previous = None
        for part in parts:
            try:
                value = _number(part, {})
            except (ValueError, ArithmeticError, SyntaxError, RecursionError):
                previous = None
                continue
            if previous is not None and previous != value:
                return _analysis(index, False, "Арифметика")
            previous = value
    return None


def checked_quadratic(problem: str, steps: list[str]) -> TutorAnalysis | None:
    try:
        current = _coefficients(problem)
        a, b, c = (current[key] for key in ("a", "b", "c"))
        roots, unlabeled = {}, []
        error = None
        skill = "Уравнения"
        for index, row in enumerate(steps, 1):
            parts = _root_notation(row).split("=")
            if not 2 <= len(parts) <= 12:
                return None
            key = re.sub(r"\s", "", parts[0])
            if key not in _SUM | _PRODUCT | _ROOTS and any("x" in part for part in parts):
                equation = _coefficients(row)
                if equation["b"] / equation["a"] != b / a or equation["c"] / equation["a"] != c / a:
                    error = index
                    break
                current = equation
                continue
            d = current["b"] ** 2 - 4 * current["a"] * current["c"]
            constants = {**current, "d": d}
            number = _number(parts[1], constants)
            if key in _SUM:
                valid = number == -b / a
            elif key in _PRODUCT:
                valid = number == c / a
            elif key in _ROOTS:
                other = "x_2" if key == "x_1" else "x_1"
                repeated = key != "x" and roots.get(other) == number and d != 0
                valid = a * number**2 + b * number + c == 0 and not repeated
                roots[key] = number
                if key == "x":
                    unlabeled.append(number)
            else:
                valid = _number(parts[0], constants) == number
                if not valid and not re.search(r"[abcd]", key):
                    skill = "Арифметика"
            if not valid:
                error = index
                break
            if any(_number(part, constants) != number for part in parts[2:]):
                error = index
                skill = "Арифметика"
                break
        complete = (
            "x_1" in roots
            and "x_2" in roots
            and roots["x_1"] + roots["x_2"] == -b / a
            and roots["x_1"] * roots["x_2"] == c / a
        )
        supplied = set(roots.values()) | set(unlabeled)
        if len(supplied) == 2:
            first, second = supplied
            complete = first + second == -b / a and first * second == c / a
        if b * b - 4 * a * c == 0 and roots:
            complete = True
        return _analysis(error, complete, skill) if steps else None
    except (ValueError, ArithmeticError, SyntaxError, RecursionError):
        return None


def checked_vieta_explanation(problem: str, corrected: str, reason: str) -> bool | None:
    try:
        constants = _coefficients(problem)
        row = _root_notation(corrected).replace(" ", "")
        reason = re.sub(r"\s+", " ", _clean(reason)).strip().rstrip(".! ")
        if row.startswith(("x_1+x_2=", "x_2+x_1=")):
            formula = "-b" if constants["a"] == 1 else "-b/a"
            if re.fullmatch(
                rf"(?:по )?теореме ви[еа]та сумма корней равна\s*{re.escape(formula)}", reason
            ):
                return True
        if row.startswith(("x_1*x_2=", "x_2*x_1=")):
            formula = "c" if constants["a"] == 1 else "c/a"
            if re.fullmatch(
                rf"(?:по )?теореме ви[еа]та произведение корней равно\s*{re.escape(formula)}",
                reason,
            ):
                return True
        return None
    except (ValueError, ArithmeticError, SyntaxError, RecursionError):
        return None
