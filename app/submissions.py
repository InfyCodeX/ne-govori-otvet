"""Разбор условия, действий и пояснения ученика."""

import re
from dataclasses import dataclass


def normalize(text: str) -> str:
    value = re.sub(r"\s", "", text.lower().replace("х", "x").replace("−", "-"))
    value = value.replace("²", "^2").replace("**", "^").replace("₁", "_1").replace("₂", "_2")
    value = re.sub(r"x_?([12])", r"x_\1", value)
    return re.sub(r"(?<!\*)\*(?!\*)|[×·]", "", value)


def action_lines(lines: list[str]) -> tuple[list[str], str]:
    actions, reason = [], []
    for line in lines:
        record = _math_record(line)
        if record is not None:
            actions.append(record)
        elif re.search(r"[=<>≤≥≠]", line):
            actions.append(line)
        else:
            reason.append(line)
    return actions, "\n".join(reason)


@dataclass
class ParsedText:
    problem: str | None
    steps: list[str]
    explanation: str = ""


_SECTION = re.compile(r"^(?:решени[ея]|действи[ея]|шаги|вычисления)\s*:?\s*$", re.I)
_CONDITION = re.compile(r"^(?:услови[ея]|задача|задание)\s*:\s*(.*)$", re.I)
_MATH = re.compile(r"[\d\sA-Za-zа-вхХ_₁₂.,;{}\[\]()+−\-*/×·^√²³=<>≤≥≠∈∞∪!|&:%]+")
_SIMPLE = re.compile(r"[\da-zа-вх\s.=+−\-*/×·^()²³,√:%]+", re.I)


def _math_record(line: str) -> str | None:
    value = (
        re.sub(r"(?i)^(?:проверка|вычисления|вычисляем|получаем|ответ|итог)\s*:\s*", "", line)
        .strip()
        .rstrip(".")
    )
    value = re.sub(r"\s+или\s+", " | ", value, flags=re.I)
    value = re.sub(r"\s+и\s+", " & ", value, flags=re.I)
    if _MATH.fullmatch(value):
        return value
    return None


def _partition(lines: list[str]) -> tuple[list[str], str] | None:
    steps, reasons = [], []
    for line in lines:
        if _SECTION.fullmatch(line):
            continue
        record = _math_record(line)
        if record is not None:
            steps.append(record)
        elif re.search(r"[=<>≤≥≠]", line):
            return None
        else:
            reasons.append(line)
    return steps, "\n".join(reasons)


def _simple_condition(text: str) -> bool:
    letters = set(re.findall(r"[a-zа-я]", text.casefold().replace("х", "x")))
    return bool(_SIMPLE.fullmatch(text)) and len(letters) <= 1


_NUMBER_WORDS = dict(
    zip(
        (
            "ноль один два три четыре пять шесть семь восемь девять десять одиннадцать "
            "двенадцать тринадцать четырнадцать пятнадцать шестнадцать семнадцать "
            "восемнадцать девятнадцать"
        ).split(),
        range(20),
        strict=True,
    )
)
_NUMBER_WORDS.update({"одна": 1, "две": 2})


def _answer_values(text: str) -> list[str]:
    text = re.sub(r"(?i)^(?:ответ|корни)\s*(?:равны)?\s*:?\s*", "", text.strip()).rstrip(".!")
    parts = re.split(r"\s+(?:и|или)\s+|\s*;\s*|,\s+", text.casefold())
    result = []
    for part in parts:
        part = part.strip()
        sign = "-" if part.startswith("минус ") else ""
        word = part.removeprefix("минус ").removeprefix("плюс ")
        if word in _NUMBER_WORDS:
            result.append(sign + str(_NUMBER_WORDS[word]))
        elif re.fullmatch(r"[+−-]?\d+(?:[.,/]\d+)?", part):
            result.append(part.replace("−", "-"))
        else:
            return []
    return result


def short_answer(text: str) -> bool:
    return bool(_answer_values(text))


def split_records(text: str) -> list[str]:
    text = re.sub(r"(?m)^\s*\*\*(.*?)\*\*\s*$", r"\1", text).replace("`", "")
    text = re.sub(r"(?i)\*\*((?:услови[ея]|действи[ея]|решение)\s*:?)\*\*", r"\1", text)
    text = re.sub(r"(?i)\s+(?=(?:действи[ея]|решение)\s*:)", "\n", text)
    text = re.sub(r"(?i)((?:действи[ея]|решение)\s*:)\s*(?=\S)", r"\1\n", text)
    pieces, cursor, list_closings, expected = [], 0, 0, 1
    for match in re.finditer(r"(?<!\S)\d+[.)]\s+(?=[\w(−-])", text):
        prefix = text[: match.start()]
        depth = prefix.count("(") - prefix.count(")") + list_closings
        at_line_start = not prefix.rsplit("\n", 1)[-1].strip()
        number = int(re.match(r"\d+", match[0])[0])
        inline_item = number == expected and bool(
            re.search(r"(?i)(?:действи[ея]|решение)\s*:\s*$", prefix) or (cursor and expected > 1)
        )
        following = text[match.end() :].split("\n", 1)[0]
        mathematical = bool(re.match(r"[\dA-Za-zхХ(−+\-]", following)) and bool(
            re.search(r"[=<>≤≥+*/^√]", following)
        )
        if depth <= 0 and mathematical and (at_line_start or inline_item):
            pieces.extend([text[cursor : match.start()], "\n"])
            cursor = match.end()
            list_closings += ")" in match[0]
            expected = number + 1
    text = "".join(pieces) + text[cursor:]
    text = re.sub(r"(?i)первый\s+корень\s*(?:равен|=)\s*", "x_1 = ", text)
    text = re.sub(r"(?i)второй\s+корень\s*(?:равен|=)\s*", "x_2 = ", text)
    text = re.sub(
        r"(?i)^(?:ответ\s*:\s*)?корни\s*(?:равны|:)\s*([+−\-]?\d+(?:/\d+)?)\s*(?:и|;)\s*([+−\-]?\d+(?:/\d+)?)\s*$",
        r"x_1 = \1\nx_2 = \2",
        text.strip(),
    )
    text = re.sub(r"(?i),\s*(?=[xх](?:_?\s*[12]|[₁₂])\s*=)", "\n", text)
    text = re.sub(r"(?i)\s+и\s+(?=[xх](?:_?\s*[12]|[₁₂])\s*=)", "\n", text)
    text = re.sub(r"(?i)(?<=[\d)abc.])\s+(?=[xх](?:_?\s*[12]|[₁₂])\s*=)", "\n", text)
    pieces, depth = [], 0
    for offset, character in enumerate(text):
        if character in "({[":
            depth += 1
        elif character in ")}]":
            depth = max(0, depth - 1)
        comma = character == "," and re.match(
            r"\s*[\dA-Za-zхХуУ(][\dA-Za-zхХуУ_₁₂ +*/^().−\-]*\s*[=<>≤≥]",
            text[offset + 1 :],
        )
        pieces.append("\n" if (character in ";→⇒" or comma) and not depth else character)
    text = re.sub(r"\s*=>\s*", "\n", "".join(pieces))
    lines = []
    for line in text.splitlines():
        line = line.strip()
        line = re.sub(r"(?i)^ответ\s*:\s*", "", line)
        if line:
            lines.append(line)
    return lines


def parse_text(text: str, current_problem: str | None = None) -> ParsedText | None:
    values = _answer_values(text) if current_problem else []
    if current_problem and _simple_condition(current_problem):
        letters = set(re.findall(r"[a-zа-я]", current_problem.casefold().replace("х", "x")))
        if len(letters) == 1 and len(values) == 1:
            text = f"{next(iter(letters))}={values[0]}"
        elif len(values) == 2 and re.search(r"x(?:\^2|²)", normalize(current_problem)):
            text = f"x_1={values[0]}\nx_2={values[1]}"
    lines = split_records(text)
    if not lines:
        return None
    boundary = next((i for i, line in enumerate(lines) if _SECTION.fullmatch(line)), None)
    condition = _CONDITION.match(lines[0])
    if boundary is not None:
        head = lines[:boundary]
        if head and _CONDITION.match(head[0]):
            head[0] = _CONDITION.match(head[0])[1]
        head = [line for line in head if not re.fullmatch(r"(?i)услови[ея]\s*:?", line)]
        problem = "\n".join(line for line in head if line.strip()) or current_problem
        partition = _partition(lines[boundary + 1 :])
        if partition is None:
            return None
        steps, reason = partition
        if problem and steps:
            return ParsedText(problem, steps, reason)
        return None
    if condition:
        if not condition[1]:
            return None
        first = next(
            (
                i
                for i in range(1, len(lines))
                if _math_record(lines[i]) is not None and re.search(r"[=<>≤≥≠+*/^√]", lines[i])
            ),
            len(lines),
        )
        if any(re.search(r"[=<>≤≥≠]", line) for line in lines[1:first]):
            return None
        problem = "\n".join([condition[1], *lines[1:first]])
        partition = _partition(lines[first:])
        if partition is not None:
            return ParsedText(problem, *partition)
        return None
    if current_problem and normalize(lines[0]) == normalize(current_problem):
        steps, reason = action_lines(lines[1:])
        if steps and all(_MATH.fullmatch(step) for step in steps):
            return ParsedText(current_problem, steps, reason)
    if all(_MATH.fullmatch(line) and re.search(r"[=<>≤≥≠]", line) for line in lines):
        if current_problem:
            return ParsedText(None, lines)
        if len(lines) >= 2:
            return ParsedText(lines[0], lines[1:])
    if current_problem:
        partition = _partition(lines)
        if partition is not None and partition[0]:
            return ParsedText(None, *partition)
    if (
        not current_problem
        and len(lines) >= 2
        and _simple_condition(lines[0])
        and all(_MATH.fullmatch(x) for x in lines)
    ):
        return ParsedText(lines[0], lines[1:])
    if not current_problem:
        first = next(
            (i for i, line in enumerate(lines) if _MATH.fullmatch(line) and "=" in line), 0
        )
        if first and all(_MATH.fullmatch(line) for line in lines[first:]):
            return ParsedText("\n".join(lines[:first]), lines[first:])
    return None


def clean_transcript(problem: str, steps: list[str]) -> tuple[str, list[str]]:
    label = _CONDITION.match(problem.strip())
    problem = label[1] if label and label[1] else problem
    cleaned = []
    for row in steps:
        for line in split_records(row):
            if not _SECTION.fullmatch(line):
                cleaned.append(line)
    while cleaned and normalize(cleaned[0]) == normalize(problem):
        cleaned.pop(0)
    return problem, cleaned


def _explicit_action(text: str) -> int | None:
    explicit = re.search(r"(?i)(?:в\s+)?(?:действи[еиия]|шаг[еау]?)\s*(\d+)", text)
    if explicit:
        return int(explicit[1])
    words = {"перв": 1, "втор": 2, "треть": 3, "четвёрт": 4, "четверт": 4}
    for stem, index in words.items():
        if re.search(rf"(?i)\b{stem}\w*\s+(?:действ|шаг)", text):
            return index
    return None


def selected_action(text: str, old: list[str], submitted: list[str]) -> int | None:
    explicit = _explicit_action(text)
    if explicit is not None:
        return explicit
    if not submitted:
        return None
    left = normalize(submitted[0].split("=", 1)[0])
    matches = [i for i, row in enumerate(old, 1) if normalize(row.split("=", 1)[0]) == left]
    if len(matches) == 1:
        return matches[0]
    if "=" in submitted[0]:
        right = normalize(submitted[0].split("=", 1)[1])
        matches = [
            i
            for i, row in enumerate(old, 1)
            if "=" in row and normalize(row.split("=", 1)[1]) == right
        ]
        if len(matches) == 1:
            return matches[0]
    return 1 if len(old) == 1 else None


def align_correction(
    text: str, old: list[str], submitted: list[str], verified_count: int
) -> tuple[int | None, list[str]]:
    if not submitted:
        return _explicit_action(text), submitted
    anchors = []
    for start in range(min(verified_count, len(old))):
        count = 0
        while (
            count < len(submitted)
            and start + count < verified_count
            and normalize(submitted[count]) == normalize(old[start + count])
        ):
            count += 1
        if count and count < len(submitted):
            anchors.append((count, start + count + 1))
    explicit = _explicit_action(text)
    if anchors:
        longest = max(count for count, _ in anchors)
        targets = {target for count, target in anchors if count == longest}
        if len(targets) == 1:
            target = targets.pop()
            if explicit is None or explicit == target:
                return target, submitted[longest:]
    if explicit is not None:
        return explicit, submitted
    if verified_count < len(old) and "=" in submitted[0]:
        first = normalize(submitted[0])
        target = normalize(old[verified_count])
        repeated = any(first == normalize(row) for row in old[:verified_count])
        if not repeated and first != target and first.split("=", 1)[0] == target.split("=", 1)[0]:
            return verified_count + 1, submitted
    return selected_action(text, old, submitted), submitted


def first_change(old: list[str], submitted: list[str]) -> int | None:
    for index, row in enumerate(submitted, 1):
        if index > len(old) or normalize(row) != normalize(old[index - 1]):
            return index
    return None


def merge_actions(existing: list[str], submitted: list[str], verified_count: int) -> list[str]:

    for index, line in enumerate(existing[:verified_count]):
        if normalize(line) == normalize(submitted[0]):
            return existing[:index] + submitted
    return existing[:verified_count] + submitted
