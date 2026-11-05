"""Точная проверка линейных уравнений."""

import ast
import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal

MAX_EXPRESSION_LENGTH = 1000
MAX_AST_NODES = 200
MAX_AST_DEPTH = 40
MAX_INTEGER_DIGITS = 20
MAX_FRACTION_BITS = 512


@dataclass(frozen=True)
class LinearCheck:
    status: Literal["correct", "error"]
    first_error_step: int | None
    complete: bool
    skill: str = "Уравнения"


class _Unsupported(ValueError):
    pass


@dataclass(frozen=True)
class _Affine:
    a: Fraction
    b: Fraction


def _bounded(a: Fraction, b: Fraction) -> _Affine:
    for value in (a, b):
        if (
            value.numerator.bit_length() > MAX_FRACTION_BITS
            or value.denominator.bit_length() > MAX_FRACTION_BITS
        ):
            raise _Unsupported
    return _Affine(a, b)


def _expression(source: str) -> tuple[_Affine, bool, bool]:
    if not source or len(source) > MAX_EXPRESSION_LENGTH:
        raise _Unsupported

    tokens: list[str] = []
    position = 0
    token_pattern = re.compile(r"\s*(\d+(?:\.\d*)?|\.\d+|x|\*\*|[+*/()-])")
    while position < len(source):
        if source[position:].isspace():
            break
        token = token_pattern.match(source, position)
        if token is None:
            raise _Unsupported
        value = token[1]
        if value[0].isdigit() or value.startswith("."):
            if sum(character.isdigit() for character in value) > MAX_INTEGER_DIGITS:
                raise _Unsupported
        tokens.append(value)
        position = token.end()
    if not tokens:
        raise _Unsupported
    expanded: list[str] = []
    for token in tokens:
        if expanded:
            previous = expanded[-1]
            previous_is_value = (
                previous == "x"
                or previous == ")"
                or previous[0].isdigit()
                or previous.startswith(".")
            )
            if previous_is_value and token in {"x", "("}:
                expanded.append("*")
        expanded.append(token)
    expression = " ".join(expanded)
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        raise _Unsupported from None
    queue = [(tree, 0)]
    count = 0
    while queue:
        node, depth = queue.pop()
        count += 1
        if count > MAX_AST_NODES or depth > MAX_AST_DEPTH:
            raise _Unsupported
        queue.extend((child, depth + 1) for child in ast.iter_child_nodes(node))

    def evaluate(node: ast.AST) -> _Affine:
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            literal = ast.get_source_segment(expression, node)
            if literal is None:
                raise _Unsupported
            try:
                return _bounded(Fraction(0), Fraction(literal))
            except (ValueError, ZeroDivisionError):
                raise _Unsupported from None
        if isinstance(node, ast.Name) and node.id == "x":
            return _Affine(Fraction(1), Fraction(0))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = evaluate(node.operand)
            return value if isinstance(node.op, ast.UAdd) else _bounded(-value.a, -value.b)
        if isinstance(node, ast.BinOp):
            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, ast.Add):
                return _bounded(left.a + right.a, left.b + right.b)
            if isinstance(node.op, ast.Sub):
                return _bounded(left.a - right.a, left.b - right.b)
            if isinstance(node.op, ast.Mult):
                if left.a and right.a:
                    raise _Unsupported
                return _bounded(left.a * right.b + right.a * left.b, left.b * right.b)
            if isinstance(node.op, ast.Div):
                if right.a or not right.b:
                    raise _Unsupported
                return _bounded(left.a / right.b, left.b / right.b)
            if isinstance(node.op, ast.Pow) and not right.a:
                if right.b == 1:
                    return left
                if right.b == 0 and not left.a and left.b:
                    return _Affine(Fraction(0), Fraction(1))
        raise _Unsupported

    value = evaluate(tree.body)
    return (
        value,
        isinstance(tree.body, ast.Name) and tree.body.id == "x",
        not any(isinstance(node, ast.Name) for node in ast.walk(tree)),
    )


def _equation(source: str, *, problem: bool = False) -> tuple[_Affine, bool]:
    if not isinstance(source, str) or not source.strip() or len(source) > 2100:
        raise _Unsupported
    text = source.strip().casefold().replace("х", "x").replace("−", "-")
    text = text.replace("×", "*").replace("·", "*")
    text = re.sub(r"(?<=\d),(?=\d)", ".", text)
    if problem:
        text = re.sub(r"^(?:реши|решите)\s+уравнение\s*:?\s*", "", text)
    else:
        text = re.sub(r"^(?:\d+\)\s*|\d+\.\s+)", "", text)
    if text.count("=") != 1:
        raise _Unsupported
    left_text, right_text = text.split("=")
    left, left_is_x, left_is_constant = _expression(left_text)
    right, right_is_x, right_is_constant = _expression(right_text)
    result = _bounded(left.a - right.a, left.b - right.b)

    if not result.a:
        raise _Unsupported
    complete = (left_is_x and right_is_constant) or (right_is_x and left_is_constant)
    return result, complete


def check_linear(problem: str, steps: list[str]) -> LinearCheck | None:

    if not isinstance(steps, list) or not 1 <= len(steps) <= 30:
        return None
    try:
        original, _ = _equation(problem, problem=True)
        parsed_steps = [_equation(step) for step in steps]
        root = _bounded(Fraction(0), -original.b / original.a).b
        for index, (equation, _) in enumerate(parsed_steps, 1):
            step_root = _bounded(Fraction(0), -equation.b / equation.a).b
            if step_root != root:
                return LinearCheck(status="error", first_error_step=index, complete=False)
        return LinearCheck(status="correct", first_error_step=None, complete=parsed_steps[-1][1])
    except (_Unsupported, OverflowError, RecursionError, ZeroDivisionError):
        return None
