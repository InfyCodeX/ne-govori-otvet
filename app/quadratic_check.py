"""Проверка квадратных уравнений и формул Виета."""

import ast
import re
from fractions import Fraction

from app.learning_models import TutorAnalysis
from app.math_check import MAX_AST_DEPTH, MAX_AST_NODES, MAX_FRACTION_BITS, MAX_INTEGER_DIGITS


def _clean(text: str) -> str:
    text = text.casefold().replace("х", "x").replace("−", "-")
    text = text.replace("²", "**2").replace("^", "**").replace("×", "*").replace("·", "*")
    return re.sub(r"(?<=\d),(?=\d)", ".", text)


def _polynomial(source: str, constants: dict[str, Fraction] | None = None) -> tuple:
    source = _clean(source).strip()
    if not source or len(source) > 1000:
        raise ValueError
    if not re.fullmatch(r"[\d\s.xabc+*/()\-]+", source):
        raise ValueError
    if any(len(n.replace(".", "")) > MAX_INTEGER_DIGITS for n in re.findall(r"\d[\d.]*", source)):
        raise ValueError
    source = re.sub(r"(?<=[\dx)])\s*(?=[xabc(])", "*", source)
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


def checked_quadratic(problem: str, steps: list[str]) -> TutorAnalysis | None:
    try:
        constants = _coefficients(problem)
        a, b, c = (constants[key] for key in ("a", "b", "c"))
        roots, unlabeled = {}, []
        error = None
        for index, row in enumerate(steps, 1):
            line = _root_notation(row)
            if line.count("=") != 1:
                return None
            left, right = [part.strip() for part in line.split("=")]
            key = re.sub(r"\s", "", left)
            if key in {"x_1+x_2", "x_2+x_1", "x_1*x_2", "x_2*x_1", "x_1", "x_2", "x"}:
                number, linear, quadratic = _polynomial(right, constants)
                if linear or quadratic:
                    return None
                if "+" in key:
                    correct = number == -b / a
                elif "*" in key:
                    correct = number == c / a
                else:
                    correct = a * number**2 + b * number + c == 0
                    other = "x_2" if key == "x_1" else "x_1"
                    if (
                        key != "x"
                        and other in roots
                        and roots[other] == number
                        and b * b != 4 * a * c
                    ):
                        correct = False
                    roots[key] = number
                    if key == "x":
                        unlabeled.append(number)
            else:
                equation = _coefficients(row)
                correct = all(equation[k] / equation["a"] == constants[k] / a for k in ("b", "c"))
            if not correct:
                error = index
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
        return (
            TutorAnalysis(
                status="error" if error else "correct",
                first_error_step=error,
                skill="Уравнения",
                complete=bool(not error and complete),
                question="Как связаны сумма и произведение корней с коэффициентами уравнения?",
                hint="Сверь знак и старший коэффициент в формулах Виета. "
                "Проверь выбранные корни подстановкой.",
                detail="Приведи уравнение к стандартному виду. Сопоставь сумму и произведение "
                "корней с коэффициентами, затем проверь свою пару в исходном уравнении.",
                discovery="Найди первую неверную запись и объясни применённое правило.",
            )
            if steps
            else None
        )
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
