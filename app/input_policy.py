import re
import unicodedata

_HOMOGLYPHS = str.maketrans(
    {
        "a": "а",
        "c": "с",
        "e": "е",
        "o": "о",
        "p": "р",
        "x": "х",
        "y": "у",
        "k": "к",
        "m": "м",
        "t": "т",
        "b": "в",
        "h": "н",
    }
)
_RUDE = re.compile(
    r"\b(?:бля(?:ть|д\w*)?|хуй\w*|ху[еёяю]\w*|пизд\w*|"
    r"(?:за|на|по|у|вы|от|с|раз|про)?[её]б(?:а|у|и|н|о|л)\w*|"
    r"сук[аиуе]\w*|муд(?:ак|ил)\w*|дебил\w*|идиот\w*)\b"
)


def input_feedback(text: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", text).casefold().translate(_HOMOGLYPHS)

    marked = re.sub(r"(?<=[а-яё])[*_.\-]+(?=[а-яё])", "", normalized)
    marked = re.sub(
        r"\b(?:[а-яё][\s*_\.\-]+){2,}[а-яё]\b",
        lambda match: re.sub(r"[\s*_\.\-]", "", match[0]),
        marked,
    )
    if _RUDE.search(marked):
        return "Давай без оскорблений. Пришли учебную задачу — помогу разобраться."
    if re.search(
        r"(?:игнорируй|забудь)\s+(?:все\s+)?(?:правила|инструкции)|"
        r"(?:покажи|раскрой|выдай)\b.{0,60}(?:токен|секрет|ключ|системн\w*\s+промпт)",
        normalized,
    ):
        return "Я помогаю разбирать учебные решения. Пришли условие и свои вычисления."
    compact = re.sub(r"\s", "", normalized)
    if (
        re.search(r"(.)\1{23,}", compact)
        or re.fullmatch(r"(?:а?ха){5,}[\W\d]*", compact)
        or re.fullmatch(r"https?://\S+", text.strip(), re.I)
    ):
        return "Пришли условие задачи и свои вычисления — тогда смогу помочь."
    return None


def readable_help(text: str, limit: int = 700) -> bool:
    if len(text) > limit or re.search(
        r"[\x00-\x08\x0b\x0c\x0e-\x1f\u202a-\u202e\u2066-\u2069]|"
        r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]|"
        r"\b(?:hint|detail|discovery|question|status|feedback)\s*:|[{}]",
        text,
        re.I,
    ):
        return False
    words = re.sub(r"\b(?:sqrt|abs|sin|cos|tan|log|ln)\b", "", text, flags=re.I)
    return not re.search(r"[a-z]{3,}", words, re.I)
