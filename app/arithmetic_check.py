"""Проверка числовых вычислений."""

import ast
import copy
import re
from fractions import Fraction

from app.learning_models import TutorAnalysis
from app.math_check import MAX_AST_DEPTH, MAX_AST_NODES, _expression


def _tree(source: str) -> ast.expr:

    value = _expression(source)[0]
    if value.a:
        raise ValueError
    text = re.sub(r"(?<=[\d)])\s*(?=\()", "*", source.strip())
    tree = ast.parse(text, mode="eval").body
    if any(isinstance(node, ast.Name) for node in ast.walk(tree)):
        raise ValueError
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            node.value = Fraction(ast.get_source_segment(text, node))
    return tree


def _key(node: ast.expr) -> tuple:
    if isinstance(node, ast.Constant):
        return ("number", Fraction(str(node.value)))
    if isinstance(node, ast.UnaryOp):
        return (type(node.op).__name__, _key(node.operand))
    if isinstance(node, ast.BinOp):
        left, right = _key(node.left), _key(node.right)
        if isinstance(node.op, (ast.Add, ast.Mult)):
            left, right = sorted((left, right), key=repr)
        return (type(node.op).__name__, left, right)
    raise ValueError


def _replace(node: ast.expr, target: tuple, replacement: ast.expr) -> tuple[ast.expr, bool]:
    if _key(node) == target:
        return copy.deepcopy(replacement), True
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
    def clean(value):
        value = value.strip().replace(":", "/").replace("·", "*").replace("×", "*")
        value = value.replace("−", "-").replace(",", ".")
        return value

    def number(value):
        expression = _expression(clean(value))[0]
        if expression.a:
            raise ValueError
        return expression.b

    try:
        if not steps or not 1 <= len(steps) <= 30:
            return None
        condition = clean(problem).rstrip("=").strip()
        if "=" in condition:
            return None
        number(condition)
        current = _tree(condition)
        first_error = None
        skill = "Дроби" if _fraction_action(current) else "Арифметика"
        for index, step in enumerate(steps, 1):
            step = re.sub(r"^(?:\d+\.\s+|\d+\)\s*)", "", step.strip())
            parts = step.split("=")
            if len(parts) != 2:
                return None
            left, right = map(number, parts)
            left_tree, right_tree = map(_tree, map(clean, parts))
            current, linked = _replace(current, _key(left_tree), right_tree)
            if not linked or not _bounded_tree(current):
                return _uncertain(skill)
            if left != right and first_error is None:
                first_error = index
                skill = "Дроби" if _fraction_action(left_tree) else "Арифметика"
                break
        complete = _finished(current)
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
