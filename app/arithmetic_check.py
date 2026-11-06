"""Проверка числовых вычислений."""

import ast
import copy
import re
from fractions import Fraction
from math import isqrt
from operator import add, mul, sub, truediv

from app.learning_models import TutorAnalysis
from app.math_check import (
    MAX_AST_DEPTH,
    MAX_AST_NODES,
    MAX_EXPRESSION_LENGTH,
    MAX_FRACTION_BITS,
    MAX_INTEGER_DIGITS,
    _expression,
)

_OPERATIONS = {ast.Add: add, ast.Sub: sub, ast.Mult: mul, ast.Div: truediv}
_SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺", "0123456789-+")


def _notation(source: str) -> str:
    if len(source) > MAX_EXPRESSION_LENGTH:
        raise ValueError
    text = source.strip().strip("$").strip()
    text = re.sub(r"\\(?:left|right)\s*(?=[()[\]{}])", "", text)
    for symbol, replacement in (
        (r"\cdot", "*"),
        (r"\times", "*"),
        (r"\div", "/"),
        ("×", "*"),
        ("·", "*"),
        ("÷", "/"),
        (":", "/"),
        ("−", "-"),
        (",", "."),
    ):
        text = text.replace(symbol, replacement)
    text = re.sub(r"\^\{\s*([+-]?\d+)\s*\}", r"^(\1)", text)
    for _ in range(MAX_AST_DEPTH):
        converted = re.sub(r"\\[dt]?frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}", r"((\1)/(\2))", text)
        converted = re.sub(r"\\sqrt\s*\{([^{}]+)\}", r"sqrt(\1)", converted)
        if converted == text:
            break
        text = converted
    brackets = []
    for char in text:
        if char in "([{":
            brackets.append(char)
        elif char in ")]}" and (not brackets or "([{".index(brackets.pop()) != ")]}".index(char)):
            raise ValueError
    if brackets:
        raise ValueError
    text = text.translate(str.maketrans("[]{}", "()()"))
    text = re.sub(r"√\s*(\d+(?:\.\d+)?)", r"sqrt(\1)", text).replace("√", "sqrt")
    text = re.sub(
        r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+", lambda match: "^(" + match[0].translate(_SUPERSCRIPTS) + ")", text
    )
    text = text.replace("^", "**")
    if not re.fullmatch(r"(?:sqrt|[0-9.()+*/\-\s])+", text):
        raise ValueError
    return re.sub(r"(?<=[\d)])\s*(?=\(|sqrt\()", "*", text)


def _tree(source: str) -> ast.expr:
    text = _notation(source)
    tree = ast.parse(text, mode="eval").body
    if not _bounded_tree(tree):
        raise ValueError
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            literal = ast.get_source_segment(text, node)
            if not re.fullmatch(r"\d+(?:\.\d*)?|\.\d+", literal or ""):
                raise ValueError
            if sum(char.isdigit() for char in literal) > MAX_INTEGER_DIGITS:
                raise ValueError
            node.value = Fraction(literal)
    return tree


def _value(node: ast.expr) -> Fraction:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _value(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "sqrt"
        and len(node.args) == 1
        and not node.keywords
    ):
        value = _value(node.args[0])
        numerator, denominator = isqrt(value.numerator), isqrt(value.denominator)
        root = Fraction(numerator, denominator)
        if root * root != value:
            raise ValueError
        return root
    if not isinstance(node, ast.BinOp):
        raise ValueError
    left, right = _value(node.left), _value(node.right)
    operation = _OPERATIONS.get(type(node.op))
    if operation:
        result = operation(left, right)
    elif (
        isinstance(node.op, ast.Pow)
        and right.denominator == 1
        and abs(right) <= 32
        and (left or right > 0)
    ):
        result = left ** int(right)
    else:
        raise ValueError
    if max(result.numerator.bit_length(), result.denominator.bit_length()) > MAX_FRACTION_BITS:
        raise ValueError
    return result


def _key(node: ast.expr) -> tuple:
    if isinstance(node, ast.Constant):
        return ("number", Fraction(str(node.value)))
    if isinstance(node, ast.UnaryOp):
        return (type(node.op).__name__, _key(node.operand))
    if isinstance(node, ast.Call):
        return ("sqrt", _key(node.args[0]))
    if isinstance(node, ast.BinOp):
        left, right = _key(node.left), _key(node.right)
        if isinstance(node.op, (ast.Add, ast.Mult)):
            left, right = sorted((left, right), key=repr)
        return (type(node.op).__name__, left, right)
    raise ValueError


def _replace(node: ast.expr, target: tuple, replacement: ast.expr) -> tuple[ast.expr, bool]:
    if _key(node) == target:
        return copy.deepcopy(replacement), True
    if isinstance(node, ast.Call):
        node.args[0], found = _replace(node.args[0], target, replacement)
        return node, found
    found = False
    for name, child in ast.iter_fields(node):
        if isinstance(child, ast.expr):
            result, changed = _replace(child, target, replacement)
            setattr(node, name, result)
            found |= changed
    return node, found


def _fraction_action(node: ast.expr) -> bool:
    return any(
        isinstance(part, ast.BinOp)
        and isinstance(part.op, (ast.Add, ast.Sub, ast.Mult, ast.Div))
        and any(isinstance(child, ast.Div) for child in ast.walk(part))
        for part in ast.walk(node)
    )


def _skill(node: ast.expr) -> str:
    if any(isinstance(part, (ast.Pow, ast.Call)) for part in ast.walk(node)):
        return "Степени и корни"
    return "Дроби" if _fraction_action(node) else "Арифметика"


def _finished(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp):
        return _finished(node.operand)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        if isinstance(node.left, ast.Constant) and isinstance(node.right, ast.Constant):
            left, right = Fraction(str(node.left.value)), Fraction(str(node.right.value))
            value = left / right
            return left == value.numerator and right == value.denominator and right > 1
    return False


def _uncertain(skill: str) -> TutorAnalysis:
    return TutorAnalysis(
        status="uncertain",
        skill=skill,
        question="Покажи, как эта строка связана с исходным выражением.",
        hint="Запиши вычисление исходного выражения или одной из его частей.",
        detail="Верного равенства недостаточно: нужно показать вычисления именно для этой задачи.",
        discovery="Объясни, какую часть исходной записи ты вычисляешь.",
    )


def _bounded_tree(tree: ast.expr) -> bool:
    pending, count = [(tree, 0)], 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > MAX_AST_NODES or depth > MAX_AST_DEPTH:
            return False
        pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    return True


def checked_order_explanation(
    steps: list[str], error_index: int, corrected: str, reason: str
) -> bool | None:
    reason = re.sub(r"\s+", " ", reason.casefold()).strip().rstrip(".! ")
    rule = re.fullmatch(
        r"(?:сначала|первым делом)(?: выполняем| выполняется| нужно выполнить)? "
        r"(умножение(?: \(или деление\)| и деление)?|деление(?: и умножение)?)"
        r",? (?:а )?(?:потом|затем) "
        r"(разность(?: \(или сложение\))?|вычитание(?: и сложение)?|сложение(?: и вычитание)?)",
        reason,
    )
    if rule is None or not 2 <= error_index <= len(steps):
        return None

    def equation(row):
        row = row.replace("−", "-").replace("×", "*").replace("·", "*").replace(":", "/")
        parts = row.split("=")
        if len(parts) != 2:
            raise ValueError
        return re.sub(r"\s", "", parts[0]), parts[1].strip()

    def number(text):
        value = _expression(text)[0]
        if value.a:
            raise ValueError
        return value.b

    try:
        left, source = equation(steps[error_index - 2])
        wrong_left, wrong = equation(steps[error_index - 1])
        new_left, proposed = equation(corrected)
        if left != wrong_left or left != new_left:
            return None
        literal = r"([+-]?\d+(?:\.\d+)?)"
        formula = re.fullmatch(
            rf"\s*{literal}\s*([+-])\s*{literal}\s*([*/])\s*{literal}\s*", source
        )
        if formula is None:
            return None
        if not re.search("умнож" if formula[4] == "*" else "делен", rule[1]):
            return None
        if not re.search("сложен" if formula[2] == "+" else "вычит|разност", rule[2]):
            return None
        a, b, c = (number(formula[i]) for i in (1, 3, 5))
        first = a + b if formula[2] == "+" else a - b
        incorrect = first * c if formula[4] == "*" else first / c
        correct = number(source)
        if incorrect != correct and number(wrong) == incorrect and number(proposed) == correct:
            return True
    except (ValueError, ArithmeticError, SyntaxError, RecursionError):
        pass
    return None


def checked_arithmetic(problem: str, steps: list[str]) -> TutorAnalysis | None:
    try:
        if not steps or not 1 <= len(steps) <= 30:
            return None
        condition = (
            re.sub(
                r"^вычисли(?:те)?(?:\s+значение)?(?:\s+выражения)?\s*:?\s*",
                "",
                problem.strip(),
                flags=re.I,
            )
            .strip("$")
            .rstrip("=")
            .strip()
        )
        if "=" in condition:
            return None
        current = _tree(condition)
        _value(current)
        first_error = None
        skill = _skill(current)
        for index, step in enumerate(steps, 1):
            step = re.sub(r"^(?:\d+\.\s+|\d+\)\s*)", "", step.strip())
            parts = step.strip("$").split("=")
            if len(parts) == 1:
                answer = _tree(parts[0])
                if index != len(steps) or not _finished(answer):
                    return None
                if not _finished(current):
                    return _uncertain(skill)
                if _value(current) != _value(answer):
                    first_error = index
                break
            left = _tree(parts[0])
            for source in parts[1:]:
                right = _tree(source)
                expected = _value(left)
                try:
                    actual = _value(right)
                except ZeroDivisionError:
                    actual = None
                current, linked = _replace(current, _key(left), right)
                if not linked or not _bounded_tree(current):
                    return _uncertain(skill)
                if expected != actual:
                    first_error = index
                    skill = _skill(left)
                    break
                left = right
            if first_error:
                break
        complete = first_error is None and _finished(current)
        return TutorAnalysis(
            status="error" if first_error else "correct",
            first_error_step=first_error,
            complete=first_error is None and complete,
            skill=skill,
            question=(
                "Какое правило действий с дробями применено в этой строке?"
                if skill == "Дроби"
                else "В каком порядке выполняются действия в этом выражении?"
            ),
            hint=(
                "Проверь знак операции. Для сложения и вычитания нужен общий знаменатель."
                if skill == "Дроби"
                else "Проверь скобки, затем умножение и деление, потом сложение и вычитание."
            ),
            detail=(
                "Назови правило для выбранной операции. Проверь числитель и знаменатель "
                "отдельно, затем запиши результат самостоятельно."
                if skill == "Дроби"
                else "Выпиши промежуточные действия. Пересчитай каждое отдельно и сверь равенство "
                "левой и правой частей. Затем проверь порядок действий в исходном выражении."
            ),
            discovery="Выбери неверное действие, исправь его и объясни правило.",
        )
    except (ValueError, ArithmeticError, RecursionError, SyntaxError):
        return None
