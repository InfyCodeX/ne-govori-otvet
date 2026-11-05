"""Ограниченная символьная проверка в отдельном процессе."""

import ast
import asyncio
import json
import re
import sys

from app.learning_models import TutorAnalysis


async def checked_symbolic(problem: str, steps: list[str]) -> TutorAnalysis | None:
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            "-m",
            "app.symbolic_check",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            **({"creationflags": 0x08000000} if sys.platform == "win32" else {}),
        )
        payload = json.dumps({"problem": problem, "steps": steps}).encode()
        output, _ = await asyncio.wait_for(process.communicate(payload), timeout=6)
        if process.returncode != 0 or len(output) > 12000:
            return None
        result = json.loads(output)
        return TutorAnalysis.model_validate(result, strict=True) if result else None
    except (TimeoutError, OSError, ValueError):
        return None
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


def _text(source: str) -> str:
    source = source.casefold().replace("х", "x").replace("у", "y").replace("−", "-")
    source = source.replace("²", "^2").replace("³", "^3").replace("^", "**")
    source = source.replace("×", "*").replace("·", "*").replace(":", "/")
    source = re.sub(r"(?<=\d),(?=\d)", ".", source)
    source = source.replace("≤", "<=").replace("≥", ">=").replace("≠", "!=")
    source = re.sub(r"√\s*(\([^()]*\)|\d+(?:\.\d+)?|[a-z])", r"sqrt(\1)", source)
    return source.strip()


def _condition(source: str) -> str:
    return re.sub(
        r"(?i)^(?:(?:реши(?:те)?|упрости(?:те)?|вычисли(?:те)?|раскрой(?:те)?)\s+)?"
        r"(?:(?:систем[ау]|уравнение|неравенство|выражение|скобки|функция)\s*)?[:/]?\s*",
        "",
        source.strip().rstrip("."),
        count=1,
    ).strip()


class _Math:
    def __init__(self):
        import sympy as sp

        self.sp = sp
        self.symbols = {key: sp.Symbol(key, real=True) for key in "xyabctz"}

    def expression(self, source: str):
        sp = self.sp
        source = _text(source)
        if not source or len(source) > 240 or not re.fullmatch(r"[a-z\d .+*/()\-]+", source):
            raise ValueError
        tokens, cursor = [], 0
        pattern = re.compile(r"\s*(sqrt|abs|\d+(?:\.\d*)?|\.\d+|[xyabctz]|\*\*|[+*/()\-])")
        while cursor < len(source):
            match = pattern.match(source, cursor)
            if match is None:
                if source[cursor:].isspace():
                    break
                raise ValueError
            token = match[1]
            if tokens:
                before = tokens[-1]
                if (before == ")" or before in self.symbols or before[0].isdigit()) and (
                    token == "(" or token in self.symbols or token in {"sqrt", "abs"}
                ):
                    tokens.append("*")
            tokens.append(token)
            cursor = match.end()
        source = " ".join(tokens)
        tree = ast.parse(source, mode="eval").body
        pending, count = [(tree, 0)], 0
        while pending:
            node, depth = pending.pop()
            count += 1
            if count > 70 or depth > 14:
                raise ValueError
            pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))

        def degree(node):
            if isinstance(node, ast.Constant):
                return 0, 0
            if isinstance(node, ast.Name):
                return 1, 0
            if isinstance(node, ast.UnaryOp):
                return degree(node.operand)
            if isinstance(node, ast.Call) and len(node.args) == 1:
                return degree(node.args[0])
            if not isinstance(node, ast.BinOp):
                raise ValueError
            a, b = degree(node.left)
            c, d = degree(node.right)
            if isinstance(node.op, (ast.Add, ast.Sub)):
                result = max(a + d, c + b), b + d
            elif isinstance(node.op, ast.Mult):
                result = a + c, b + d
            elif isinstance(node.op, ast.Div):
                result = a + d, b + c
            elif isinstance(node.op, ast.Pow):
                exponent = node.right
                sign = 1
                if isinstance(exponent, ast.UnaryOp) and isinstance(exponent.op, ast.USub):
                    sign, exponent = -1, exponent.operand
                if not isinstance(exponent, ast.Constant) or type(exponent.value) is not int:
                    raise ValueError
                power = sign * exponent.value
                if abs(power) > 4:
                    raise ValueError
                result = (a * power, b * power) if power >= 0 else (-b * power, -a * power)
            else:
                raise ValueError
            if max(result) > 8:
                raise ValueError
            return result

        degree(tree)
        restrictions = []

        def convert(node):
            if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
                literal = ast.get_source_segment(source, node)
                if len(literal) > 30 or not re.fullmatch(r"\d*\.?\d+", literal):
                    raise ValueError
                return sp.Rational(literal)
            if isinstance(node, ast.Name) and node.id in self.symbols:
                return self.symbols[node.id]
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                value = convert(node.operand)
                return value if isinstance(node.op, ast.UAdd) else -value
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id not in {"sqrt", "abs"} or len(node.args) != 1 or node.keywords:
                    raise ValueError
                value = convert(node.args[0])
                if node.func.id == "sqrt":
                    restrictions.append(sp.Ge(value, 0))
                    return sp.sqrt(value)
                return sp.Abs(value)
            if isinstance(node, ast.BinOp):
                left, right = convert(node.left), convert(node.right)
                if isinstance(node.op, ast.Add):
                    return left + right
                if isinstance(node.op, ast.Sub):
                    return left - right
                if isinstance(node.op, ast.Mult):
                    return left * right
                if isinstance(node.op, ast.Div):
                    if right == 0:
                        raise ValueError
                    restrictions.append(sp.Ne(right, 0))
                    return left / right
                if isinstance(node.op, ast.Pow) and right.is_Integer and -4 <= right <= 4:
                    if left.is_number and abs(left) > 10**30:
                        raise ValueError
                    if right <= 0:
                        if left == 0:
                            raise ValueError
                        restrictions.append(sp.Ne(left, 0))
                    return left**right
            raise ValueError

        result = convert(tree)
        if (
            result.has(sp.zoo, sp.nan, sp.oo, -sp.oo)
            or result.is_real is False
            or sp.count_ops(result) > 60
        ):
            raise ValueError
        if any(power.exp.is_Integer and abs(power.exp) > 8 for power in result.atoms(sp.Pow)):
            raise ValueError
        for expression in [result, *(r.lhs - r.rhs for r in restrictions if r.is_Relational)]:
            numerator, denominator = sp.fraction(sp.together(expression))
            if not expression.has(sp.Abs) and not expression.has(sp.Pow):
                continue
            for part in (numerator, denominator):
                if part.is_polynomial():
                    variables = sorted(part.free_symbols, key=str)
                    if variables:
                        polynomial = sp.Poly(part, *variables)
                        if polynomial.total_degree() > 4 or len(polynomial.terms()) > 30:
                            raise ValueError
        return result, restrictions

    def relation(self, source: str):
        sp = self.sp
        text = _text(source)
        parts = re.split(r"(<=|>=|!=|=|<|>)", text)
        if len(parts) < 3 or len(parts) > 7:
            raise ValueError
        expressions, restrictions, relations = [], [], []
        for item in parts[::2]:
            value, domain = self.expression(item)
            expressions.append(value)
            restrictions.extend(domain)
        operations = {"=": sp.Eq, "!=": sp.Ne, "<": sp.Lt, ">": sp.Gt, "<=": sp.Le, ">=": sp.Ge}
        for index, operator in enumerate(parts[1::2]):
            relations.append(operations[operator](expressions[index], expressions[index + 1]))
        return relations, restrictions, expressions

    def solution(self, relations, restrictions, symbol):
        sp = self.sp
        result = sp.S.Reals
        for relation in [*relations, *restrictions]:
            if relation == sp.true:
                continue
            if relation == sp.false:
                return sp.S.EmptySet
            if relation.free_symbols - {symbol}:
                raise ValueError
            delta = relation.lhs - relation.rhs
            if isinstance(relation, (sp.Equality, sp.Unequality)):
                roots = sp.solveset(delta, symbol, domain=sp.S.Reals)
                if roots.has(sp.ConditionSet, sp.ImageSet):
                    raise ValueError
                subset = sp.S.Reals - roots if isinstance(relation, sp.Unequality) else roots
            else:
                subset = sp.solve_univariate_inequality(relation, symbol, relational=False)
            result = result.intersect(subset)
        return result


def _analysis(error: int | None, complete: bool, skill: str) -> TutorAnalysis:
    help_text = {
        "Арифметика": (
            "Проверь порядок действий и знаки в вычислении.",
            "Сначала выполни умножение и деление, затем сложение и вычитание. "
            "Отдельно проверь знак разности.",
            "Выдели действие, которое выполняется первым, и вычисли его отдельно. "
            "Подставь свой промежуточный результат в исходную запись. "
            "Выполни оставшиеся действия, сохраняя знаки. "
            "Сравни полученное значение со своей строкой и проверь вычисление обратным действием.",
        ),
        "Преобразования выражений": (
            "Сохранилось ли значение выражения при допустимых значениях букв?",
            "Проверь распределительный закон и формулы сокращённого умножения. "
            "При сокращении дроби учитывай её исходный знаменатель.",
            "Определи, какое правило применено. Сравни каждое слагаемое до и после "
            "преобразования, затем отдельно проверь знаки и коэффициенты. "
            "Убедись, что новое выражение определено при всех исходных допустимых значениях.",
        ),
        "Неравенства": (
            "Как знак множителя или делителя влияет на направление неравенства?",
            "При умножении или делении на отрицательное число знак неравенства меняется. "
            "Если знак выражения неизвестен, его нельзя считать положительным.",
            "Сравни множества решений до и после действия. Проверь знак делителя, "
            "запреты на нулевой знаменатель и включение граничных точек. "
            "При записи интервалов отдельно сверь каждую круглую и квадратную скобку.",
        ),
        "Функции и координаты": (
            "Как связаны присланные координаты с формулой функции?",
            "Первая координата — аргумент. Подставь своё значение аргумента в исходную "
            "формулу и сравни вычисление со второй координатой.",
            "Найди присланное значение аргумента. Проверь, допускает ли его исходная формула. "
            "Выполни подстановку, соблюдая порядок действий. Сравни свой результат "
            "со значением функции или второй координатой точки.",
        ),
    }.get(
        skill,
        (
            "Сохраняются ли исходные условия и допустимые значения в этом действии?",
            "Проверь знаки, раскрытие скобок и ограничения на делители и корни.",
            "Сравни запись до и после действия. Проверь равносильность и исходные ограничения. "
            "Деление на выражение с неизвестным требует проверки его нулевых значений. "
            "Подставь свой результат в каждое исходное уравнение.",
        ),
    )
    return TutorAnalysis(
        status="error" if error else "correct",
        first_error_step=error,
        complete=not error and complete,
        skill=skill,
        question=help_text[0],
        hint=help_text[1],
        detail=help_text[2],
        discovery="Найди первое неверное действие, исправь его и объясни правило.",
    )


def _answer_set(math, text, symbol):
    sp = math.sp
    text = _text(text)
    membership = re.fullmatch(rf"{symbol}\s*∈\s*(.+)", text)
    if membership:
        result = sp.S.EmptySet
        for interval in re.split(r"[∪|]", membership[1]):
            match = re.fullmatch(r"\s*([\[(])\s*(.+?)\s*;\s*(.+?)\s*([\])])\s*", interval)
            if match is None:
                raise ValueError
            endpoints = []
            for item in match.group(2, 3):
                item = item.replace(" ", "")
                if item in {"∞", "+∞", "-∞"}:
                    endpoints.append(-sp.oo if item == "-∞" else sp.oo)
                else:
                    value, domain = math.expression(item)
                    if value.free_symbols or any(r != sp.true for r in domain):
                        raise ValueError
                    endpoints.append(value)
            result = result.union(
                sp.Interval(*endpoints, left_open=match[1] == "(", right_open=match[4] == ")")
            )
        return result
    result = sp.S.EmptySet
    for alternative in re.split(r"\s*(?:\||или)\s*", text):
        intersection = sp.S.Reals
        for clause in re.split(r"\s*(?:&|\bи\b)\s*", alternative):
            relations, domain, expressions = math.relation(clause)
            if any(e.free_symbols - {symbol} for e in expressions):
                raise ValueError
            intersection = intersection.intersect(math.solution(relations, domain, symbol))
        result = result.union(intersection)
    return result


def _system(math, problem: str, steps: list[str]):
    sp = math.sp

    def equations(text):
        text = text.strip().rstrip(".").strip("{} \n\r")
        pair = re.fullmatch(
            r"\(\s*([xyabctz])\s*;\s*([xyabctz])\s*\)\s*=\s*"
            r"\(\s*([^;]+)\s*;\s*([^;]+)\s*\)",
            _text(text),
        )
        if pair:
            text = f"{pair[1]}={pair[3]};{pair[2]}={pair[4]}"
        rows = re.split(r"[;\n]+", text)
        relations, expressions = [], []
        for row in rows:
            rs, domain, es = math.relation(row)
            if domain or len(rs) != 1 or not isinstance(rs[0], sp.Equality):
                raise ValueError
            relations.extend(rs)
            expressions.extend(es)
        return relations, expressions

    original, expressions = equations(problem)
    symbols = sorted(set().union(*(e.free_symbols for e in expressions)), key=str)
    if len(original) != 2 or len(symbols) != 2:
        return None
    matrix, vector = sp.linear_eq_to_matrix(original, symbols)
    if matrix.det() == 0:
        return None
    values = dict(zip(symbols, matrix.inv() * vector, strict=True))
    assignments, numeric_calculations = {}, set()
    for index, step in enumerate(steps, 1):
        relations, expressions = equations(step)
        for relation in relations:
            if relation.free_symbols - set(symbols):
                return None
            if relation.subs(values) != sp.true:
                left, right = relation.lhs, relation.rhs
                arithmetic = not relation.free_symbols or (
                    left in numeric_calculations and not right.free_symbols
                )
                return _analysis(index, False, "Арифметика" if arithmetic else "Уравнения")
        for left, right in zip(expressions[::2], expressions[1::2], strict=True):
            if left in symbols and not right.free_symbols:
                assignments[left] = right
                if re.search(r"[+*/^−-]", step.split("=", 1)[1]):
                    numeric_calculations.add(left)
            elif right in symbols and not left.free_symbols:
                assignments[right] = left
    return _analysis(None, set(assignments) == set(symbols), "Уравнения")


def _function(math, problem: str, steps: list[str]):
    sp = math.sp
    match = re.fullmatch(r"(?:y|f\(x\))\s*=\s*(.+)", _text(problem))
    if not match:
        return None
    formula, domain = math.expression(match[1])
    x, y = math.symbols["x"], math.symbols["y"]
    if formula.free_symbols != {x}:
        return None
    assigned, checked = {}, False
    for index, row in enumerate(steps, 1):
        row = _text(row)
        point = re.fullmatch(r"\(\s*([^;]+);\s*([^;]+)\)", row)
        function = re.fullmatch(r"f\((.+)\)\s*=\s*(.+)", row)
        if point or function:
            argument, value = [math.expression(item)[0] for item in (point or function).groups()]
        else:
            rs, restrictions, expressions = math.relation(row)
            if restrictions or len(rs) != 1 or not isinstance(rs[0], sp.Equality):
                return None
            left, right = expressions
            if left not in {x, y} or right.free_symbols:
                return None
            assigned[left] = right
            if set(assigned) != {x, y}:
                continue
            argument, value = assigned[x], assigned[y]
        if argument.free_symbols or value.free_symbols:
            return None
        if any(r.subs(x, argument) != sp.true for r in domain):
            return _analysis(index, False, "Функции и координаты")
        if sp.simplify(formula.subs(x, argument) - value) != 0:
            return _analysis(index, False, "Функции и координаты")
        checked = True
        assigned.clear()
    if assigned:
        checked = False
    return _analysis(None, checked, "Функции и координаты")


def _check(problem: str, steps: list[str]) -> TutorAnalysis | None:
    if not isinstance(problem, str) or not isinstance(steps, list) or not 1 <= len(steps) <= 30:
        return None
    if len(problem) > 1500 or any(not isinstance(s, str) or len(s) > 1000 for s in steps):
        return None
    math = _Math()
    sp = math.sp
    problem = _condition(problem)
    if (";" in problem or "\n" in problem) and "=" in problem:
        return _system(math, problem, steps)
    if re.match(r"(?:y|f\(x\))\s*=", _text(problem)):
        result = _function(math, problem, steps)
        if result is not None:
            return result
    if not re.search(r"[=<>≤≥≠]", problem):
        original, restrictions = math.expression(problem.rstrip("="))
        symbols = original.free_symbols
        if not symbols or len(symbols) > 2:
            return None
        for index, row in enumerate(steps, 1):
            parts = row.split("=")
            values, domains = [], []
            for part in parts:
                value, domain = math.expression(part)
                values.append(value)
                domains.extend(domain)
            if any(value.free_symbols - symbols for value in values):
                return None
            if len(symbols) == 1:
                symbol = next(iter(symbols))
                allowed = math.solution([], restrictions, symbol)
                proposed = math.solution([], domains, symbol)
                if allowed.intersect(proposed) != allowed:
                    return _analysis(index, False, "Преобразования выражений")
            elif restrictions or domains:
                return None
            if any(sp.cancel(original - value) != 0 for value in values):
                return _analysis(index, False, "Преобразования выражений")
        changed = re.sub(r"\s", "", _text(steps[-1].split("=")[-1])) != re.sub(
            r"\s", "", _text(problem)
        )
        return _analysis(None, changed, "Преобразования выражений")
    relations, restrictions, expressions = math.relation(problem)
    symbols = set().union(*(e.free_symbols for e in expressions))
    if len(symbols) != 1:
        return None
    symbol = next(iter(symbols))
    allowed = math.solution([], restrictions, symbol)
    original = math.solution(relations, restrictions, symbol)
    inequality = any(not isinstance(r, sp.Equality) for r in relations)
    skill = "Неравенства" if inequality else "Уравнения"
    complete = False
    supplied = set()
    for index, row in enumerate(steps, 1):
        row = re.sub(rf"\b{symbol}_?[12]\b", str(symbol), _text(row))
        if re.search(r"[∈∪|&]|\b(?:или|и)\b", row):
            answer = _answer_set(math, row, symbol)
            if answer != original:
                return _analysis(index, False, skill)
            complete = True
            continue
        rs, domain, es = math.relation(row)
        current_symbols = set().union(*(e.free_symbols for e in es))
        if current_symbols - {symbol}:
            return None
        if not current_symbols:
            if any(r != sp.true for r in [*rs, *domain]):
                return _analysis(index, False, "Арифметика")
            continue
        isolated = (
            len(rs) == 1
            and isinstance(rs[0], sp.Equality)
            and (
                (es[0] == symbol and not es[1].free_symbols)
                or (es[1] == symbol and not es[0].free_symbols)
            )
        )
        if isolated and isinstance(original, sp.FiniteSet):
            value = es[1] if es[0] == symbol else es[0]
            if original.contains(value) != sp.true or any(r != sp.true for r in domain):
                return _analysis(index, False, skill)
            supplied.add(value)
            complete = sp.FiniteSet(*supplied) == original
            continue
        proposed = math.solution(rs, domain, symbol)
        current = proposed.intersect(allowed)
        if current != original:
            return _analysis(index, False, skill)
        complete = bool(
            inequality
            and proposed == original
            and all(
                (left == symbol and not right.free_symbols)
                or (right == symbol and not left.free_symbols)
                for left, right in zip(es, es[1:], strict=False)
            )
        )
    return _analysis(None, complete, skill)


if __name__ == "__main__":
    try:
        data = json.loads(sys.stdin.buffer.read(35000))
        result = _check(data["problem"], data["steps"])
        print(json.dumps(result.model_dump(), ensure_ascii=True) if result else "null")
    except Exception:
        print("null")
