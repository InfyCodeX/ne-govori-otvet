import re
from dataclasses import dataclass
from typing import Any

from app.skills import TOPICS

MAX_COMMANDS = [
    {"name": name, "description": description}
    for name, description in (
        ("start", "Знакомство и новая задача"),
        ("help", "Как пользоваться ботом"),
        ("reset", "Начать новую задачу, сохранив карту"),
        ("example", "Учебный пример"),
        ("first_error", "Первое ошибочное действие"),
        ("hint", "Выбрать уровень помощи"),
        ("discover", "Самостоятельно исправить ошибку"),
        ("rematch", "Похожая задача после завершения"),
        ("map", "Карта слабых мест"),
        ("practice", "Выбрать тему тренировки"),
        ("forget", "Удалить мои учебные данные"),
    )
]


@dataclass(frozen=True)
class IncomingEvent:
    chat_id: int
    text: str | None
    is_start: bool = False
    user_id: int | None = None
    callback_id: str | None = None
    action: str | None = None
    image_urls: tuple[str, ...] = ()
    unsupported_attachment: bool = False


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_update(update: dict[str, Any]) -> IncomingEvent | None:
    kind = update.get("update_type")
    if kind == "bot_started":
        chat_id = update.get("chat_id")
        user = update.get("user")
        if isinstance(user, dict) and user.get("is_bot"):
            return None
        if _is_int(chat_id):
            user_id = user.get("user_id") if isinstance(user, dict) else None
            return IncomingEvent(
                chat_id=chat_id,
                text="/start",
                is_start=True,
                user_id=user_id if _is_int(user_id) else chat_id,
            )
        return None
    if kind == "message_callback":
        callback, message = update.get("callback"), update.get("message")
        if not isinstance(callback, dict) or not isinstance(message, dict):
            return None
        user, recipient = callback.get("user"), message.get("recipient")
        if not isinstance(user, dict) or user.get("is_bot") is not False:
            return None
        if not isinstance(recipient, dict) or recipient.get("chat_type") != "dialog":
            return None
        chat_id, user_id = recipient.get("chat_id"), user.get("user_id")
        callback_id, action = callback.get("callback_id"), callback.get("payload")
        base_action = action.split("@", 1)[0] if isinstance(action, str) else action
        valid_nonce = isinstance(action, str) and bool(
            re.fullmatch(r"(?:confirm_submission|reject_submission)@[0-9a-f]{16}", action)
        )
        valid_topic = (
            isinstance(action, str)
            and action.startswith("practice_")
            and (
                action.removeprefix("practice_") in TOPICS
                or bool(re.fullmatch(r"practice_[0-9a-f]{12}", action))
                or bool(re.fullmatch(r"practice_page_\d{1,6}", action))
            )
        )
        if (
            not _is_int(chat_id)
            or not _is_int(user_id)
            or not isinstance(callback_id, str)
            or not callback_id
            or not isinstance(action, str)
            or (isinstance(action, str) and "@" in action and not valid_nonce)
            or (
                not valid_nonce
                and not valid_topic
                and base_action
                not in {
                    "example",
                    "first_error",
                    "hints",
                    "hint1",
                    "hint2",
                    "hint3",
                    "discovery",
                    "rematch",
                    "map",
                    "reset",
                    "forget",
                    "confirm_submission",
                    "reject_submission",
                    "practice",
                    "retry_analysis",
                }
            )
        ):
            return None
        return IncomingEvent(
            chat_id=chat_id, text=None, user_id=user_id, callback_id=callback_id, action=action
        )
    if kind != "message_created":
        return None
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    sender, recipient = message.get("sender"), message.get("recipient")
    if not isinstance(sender, dict) or sender.get("is_bot") is not False:
        return None
    if not isinstance(recipient, dict) or recipient.get("chat_type") != "dialog":
        return None
    chat_id = recipient.get("chat_id")
    if not _is_int(chat_id):
        return None
    body = message.get("body")
    text = body.get("text") if isinstance(body, dict) else None
    malformed_text = text is not None and not isinstance(text, str)
    if malformed_text:
        text = None
    user_id = sender.get("user_id")
    if not _is_int(user_id):
        return None
    attachments = body.get("attachments") if isinstance(body, dict) else None
    urls, unsupported = [], malformed_text
    if isinstance(attachments, list):
        for item in attachments:
            if not isinstance(item, dict):
                unsupported = True
                continue
            if item.get("type") == "image":
                payload = item.get("payload")
                url = payload.get("url") if isinstance(payload, dict) else None
                if isinstance(url, str) and 1 <= len(url) <= 8192:
                    urls.append(url)
                else:
                    unsupported = True
            elif item.get("type") != "share":
                unsupported = True
    elif attachments is not None:
        unsupported = True

    link = message.get("link")
    if isinstance(link, dict) and isinstance(link.get("message"), dict):
        linked = link["message"].get("attachments")
        if linked:
            unsupported = True
    return IncomingEvent(
        chat_id=chat_id,
        text=text,
        user_id=user_id,
        image_urls=tuple(urls),
        unsupported_attachment=unsupported,
    )


def event_shape(update: dict[str, Any]) -> dict[str, Any]:

    message = update.get("message")
    message = message if isinstance(message, dict) else {}
    sender, recipient, body = (message.get(key) for key in ("sender", "recipient", "body"))
    flag = sender.get("is_bot") if isinstance(sender, dict) else None
    chat_type = recipient.get("chat_type") if isinstance(recipient, dict) else None
    text = body.get("text") if isinstance(body, dict) else None
    attachments = body.get("attachments") if isinstance(body, dict) else None
    kinds = []
    if isinstance(attachments, list):
        for item in attachments[:10]:
            kind = item.get("type") if isinstance(item, dict) else None
            kinds.append(kind if kind in ("audio", "voice", "image", "file", "video") else "other")
    return {
        "update_type": str(update.get("update_type", "unknown"))[:40],
        "structure": _structure(update),
        "sender_present": isinstance(sender, dict),
        "sender_is_bot": flag if isinstance(flag, bool) else "unspecified",
        "chat_type": chat_type if chat_type in ("dialog", "chat", "channel") else "unspecified",
        "body_present": isinstance(body, dict),
        "text_type": type(text).__name__,
        "attachments": kinds,
    }


def _structure(value: Any, depth: int = 0) -> Any:
    if depth > 3:
        return type(value).__name__
    if isinstance(value, dict):
        return {
            key
            if isinstance(key, str) and re.fullmatch(r"[A-Za-z_]{1,40}", key)
            else "other": _structure(item, depth + 1)
            for key, item in list(value.items())[:16]
        }
    if isinstance(value, list):
        return [_structure(item, depth + 1) for item in value[:3]]
    return type(value).__name__
