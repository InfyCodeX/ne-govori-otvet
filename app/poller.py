import asyncio
import hashlib
import json
import logging
import random
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import Settings
from app.handlers import MAX_COMMANDS, IncomingEvent, event_shape, parse_update
from app.integrations.max_client import MaxApiError, MaxAuthError, MaxClient
from app.learning import LearningService, learner_key
from app.learning_models import TutorReply
from app.media import PhotoError, download_photo
from app.storage import Store, event_key

logger = logging.getLogger(__name__)

COMMANDS = {"/" + command["name"] for command in MAX_COMMANDS} | {
    "/confirm",
    "/diagnostic",
    "/hint1",
    "/hint2",
    "/hint3",
}
ACTION_NAMES = {
    "example": "Учебный пример",
    "first_error": "Где я свернул не туда?",
    "hints": "Не говори ответ",
    "hint1": "Намёк",
    "hint2": "Подсказка",
    "hint3": "Подробный разбор",
    "discovery": "Поймай свою ошибку",
    "rematch": "Реванш",
    "map": "Карта слабых мест",
    "reset": "Новая задача",
    "forget": "Удалить учебные данные",
    "confirm_submission": "Подтвердить фото",
    "reject_submission": "Исправить текст фото",
    "practice": "Выбор темы тренировки",
    "retry_analysis": "Повторить проверку",
    "back": "Назад",
}


def event_label(event: IncomingEvent) -> str:
    if event.action:
        action = event.action.split("@", 1)[0]
        return ACTION_NAMES.get(action, "Тема тренировки")
    if event.unsupported_attachment or len(event.image_urls) > 1:
        return "Неподдерживаемое вложение"
    if event.image_urls:
        return "Фото"
    command = (event.text or "").strip().casefold().split(maxsplit=1)
    if command and command[0].startswith("/"):
        return command[0] if command[0] in COMMANDS else "Неизвестная команда"
    return "Текст"


def keyboard_key(chat_id: int) -> str:
    return "max_keyboard:" + learner_key("chat", chat_id)


def message_parts(text: str) -> list[str]:

    parts = []
    while len(text) > 3900:
        cut = text.rfind("\n", 0, 3900)
        if cut < 1500:
            cut = 3900
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        parts.append(text)
    return parts


@dataclass
class WorkerState:
    status: str = "starting"
    last_poll_at: str | None = None
    last_poll_monotonic: float | None = None
    last_error: str | None = None
    incomplete_messages: int = 0


class Poller:
    def __init__(
        self,
        settings: Settings,
        client: MaxClient,
        store: Store,
        state: WorkerState,
        learning: LearningService | None = None,
    ) -> None:
        self.settings, self.client, self.store, self.state = settings, client, store, state
        self.next_send_at = 0.0
        self.learning = learning or LearningService(store, settings)
        self._revisions: dict[str, int] = {}
        self._active: dict[str, asyncio.Task] = {}
        self._resets: deque[str] = deque(maxlen=128)
        self.welcome_image_url: str | None = None
        self.welcome_image_token: str | None = None

    async def bootstrap(self) -> None:
        bot = await self.client.get_me()
        if bot.get("username") != self.settings.expected_bot_username:
            raise RuntimeError("Токен относится к другому боту: проверьте EXPECTED_BOT_USERNAME")
        subscriptions = await self.client.get_subscriptions()
        if subscriptions:
            raise RuntimeError(
                "У бота есть Webhook-подписка. Long Polling несовместим с ней. "
                "Подписка не изменена; см. README"
            )
        self.store.bind_bot(bot["user_id"])
        if hasattr(self.client, "set_commands"):
            digest = hashlib.sha256(json.dumps(MAX_COMMANDS, sort_keys=True).encode()).hexdigest()
            if self.store._get("commands_digest") != digest:
                try:
                    await self.client.set_commands(MAX_COMMANDS)
                    self.store._set("commands_digest", digest)
                    logger.info("MAX command menu registered")
                except MaxApiError:
                    logger.warning("MAX command menu unavailable; typed commands remain available")
        if self.settings.max_welcome_logo and hasattr(self.client, "upload_image"):
            logo = Path(__file__).with_name("static") / "bot.png"
            digest = hashlib.sha256(await asyncio.to_thread(logo.read_bytes)).hexdigest()
            cache_key = "welcome_logo:" + digest
            self.welcome_image_token = self.store._get(cache_key)
            if not self.welcome_image_token:
                try:
                    self.welcome_image_token = await self.client.upload_image(logo)
                    self.store._set(cache_key, self.welcome_image_token)
                except MaxApiError:
                    logger.warning("Welcome logo upload unavailable; text remains available")
        self.state.status = "running"
        logger.info("MAX подключён, приём событий запущен")

    async def _event_reply(self, event: IncomingEvent, learner: str) -> TutorReply:
        try:
            if event.action:
                pending = (
                    self.learning.session(learner) if hasattr(self.learning, "session") else None
                )
                if (
                    event.action in {"confirm_submission", "reject_submission"}
                    and pending
                    and pending.pending_id
                ):
                    reply = TutorReply(
                        text="Эта кнопка устарела. Используй кнопки под последней фотографией."
                    )
                else:
                    reply = await self.learning.action(learner, event.action)
            elif event.unsupported_attachment or len(event.image_urls) > 1:
                reply = TutorReply(
                    text="Пришли одно фото решения как изображение "
                    "или запиши условие и действия текстом. "
                    "Файлы, голосовые и несколько фото за раз пока не поддерживаются."
                )
            elif event.image_urls:
                if not self.learning.ai_active:
                    reply = TutorReply(
                        text="Для разбора фото нужен подключённый ИИ. Пока можно открыть /example."
                    )
                else:
                    image, mime = await download_photo(event.image_urls[0])
                    reply = await self.learning.ingest(learner, event.text or "", image, mime)
            else:
                reply = await self.learning.handle(learner, event.text)
        except PhotoError as exc:
            reply = TutorReply(text=str(exc))
        except Exception as exc:
            logger.error("Learning request failed; error_type=%s", type(exc).__name__)
            reply = TutorReply(
                text="Не удалось разобрать сообщение. Текущая задача сохранена. "
                "Попробуй ещё раз или отправь /reset для новой задачи."
            )
        return reply

    def _observe_page(self, page: dict[str, Any]) -> dict[str, int]:
        revisions = {}
        for update in page["updates"]:
            event = parse_update(update)
            if event is None:
                continue
            learner = learner_key("max", event.user_id or event.chat_id)
            key = event_key(update)
            command = (event.text or "").split(maxsplit=1)[:1]
            reset = command and command[0].casefold() in {"/start", "/reset", "/forget"}
            if event.action in {"reset", "forget"} and event.message_id:
                menu = self._keyboard(event.chat_id)
                reset = menu is not None and event.message_id == menu["id"]
            if reset and key not in self._resets:
                self._resets.append(key)
                self._revisions[learner] = self._revisions.get(learner, 0) + 1
                active = self._active.get(learner)
                if active is not None:
                    active.cancel()
            revisions[key] = self._revisions.get(learner, 0)
        return revisions

    async def process_page(
        self, page: dict[str, Any], revisions: dict[str, int] | None = None
    ) -> None:
        revisions = self._observe_page(page) if revisions is None else revisions
        for update in page["updates"]:
            key = event_key(update)
            if not self.store.reserve(key):
                continue
            event = parse_update(update)
            if event is None:
                if update.get("update_type") == "message_created" and not isinstance(
                    update.get("message"), dict
                ):
                    self.state.incomplete_messages += 1
                    logger.warning("MAX omitted message payload; no dialog or sender to reply to")
                    logger.debug("Incomplete MAX event; shape=%s", event_shape(update))
                    self.store.finish(key, "failed")
                    continue
                logger.debug("MAX event skipped; shape=%s", event_shape(update))
                self.store.finish(key, "skipped")
                continue
            learner = learner_key("max", event.user_id or event.chat_id)
            revision = revisions[key]
            if revision != self._revisions.get(learner, 0):
                self.store.finish(key, "skipped")
                continue
            label = event_label(event)
            keyboard = self._keyboard(event.chat_id)
            if event.message_id and keyboard is not None and event.message_id != keyboard["id"]:
                self.store.finish(key, "skipped")
                logger.info("MAX · Устаревшая кнопка: %s", label)
                continue
            logger.info("MAX · Получено: %s", label)
            request = asyncio.create_task(self._event_reply(event, learner))
            self._active[learner] = request
            try:
                reply = await request
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                self.store.finish(key, "skipped")
                logger.info("MAX · Проверка отменена: %s", label)
                continue
            finally:
                self._active.pop(learner, None)

            await asyncio.sleep(max(0.0, self.next_send_at - time.monotonic()))
            self.next_send_at = time.monotonic() + 0.6
            if revision != self._revisions.get(learner, 0):
                self.store.finish(key, "skipped")
                logger.info("MAX · Отправка отменена: %s", label)
                continue
            try:
                parts = message_parts(reply.text)
                superseded = False
                sent = None
                for index, part in enumerate(parts):
                    if index:
                        await asyncio.sleep(max(0.0, self.next_send_at - time.monotonic()))
                        self.next_send_at = time.monotonic() + 0.6
                    if revision != self._revisions.get(learner, 0):
                        superseded = True
                        break
                    kwargs: dict[str, Any] = {"chat_id": event.chat_id, "text": part}
                    if reply.buttons and index == len(parts) - 1:
                        kwargs["buttons"] = reply.buttons
                    if (
                        index == 0
                        and (self.welcome_image_url or self.welcome_image_token)
                        and (
                            event.is_start or (event.text or "").split(maxsplit=1)[:1] == ["/start"]
                        )
                    ):
                        if self.welcome_image_token:
                            kwargs["image_token"] = self.welcome_image_token
                        else:
                            kwargs["image_url"] = self.welcome_image_url
                    try:
                        sent = await self.client.send_message(**kwargs)
                    except MaxApiError as exc:
                        if exc.status_code == 400 and (
                            "image_url" in kwargs or "image_token" in kwargs
                        ):
                            kwargs.pop("image_url", None)
                            kwargs.pop("image_token", None)
                            sent = await self.client.send_message(**kwargs)
                        else:
                            raise
            except MaxAuthError as exc:
                self.store.finish(key, "failed")

                if exc.status_code == 401:
                    logger.error("MAX · Авторизация отклонена (HTTP 401)")
                    raise
                logger.warning(
                    "MAX · Нет доступа к отправке (HTTP 403), без автоповтора: %s", label
                )
            except MaxApiError as exc:
                status = (
                    "failed"
                    if exc.status_code is not None and 400 <= exc.status_code < 500
                    else "uncertain"
                )
                self.store.finish(key, status)
                logger.warning(
                    "MAX · Отправка не подтверждена: %s (HTTP %s, %s)",
                    label,
                    exc.status_code or "—",
                    status,
                )

                pause = min(60.0, max(1.0, exc.retry_after or 1.0))
                self.next_send_at = max(self.next_send_at, time.monotonic() + pause)
            else:
                self.store.finish(key, "skipped" if superseded else "sent")
                if superseded:
                    logger.info("MAX · Отправка отменена: %s", label)
                elif parts:
                    logger.info("MAX · Ответ отправлен: %s", label)
                    if reply.buttons and isinstance(sent, dict):
                        message = sent.get("message", {})
                        body = message.get("body", {}) if isinstance(message, dict) else {}
                        mid = body.get("mid") if isinstance(body, dict) else None
                        if isinstance(mid, str) and mid:
                            image = (
                                {"token": kwargs["image_token"]}
                                if "image_token" in kwargs
                                else {"url": kwargs["image_url"]}
                                if "image_url" in kwargs
                                else None
                            )
                            await self._move_keyboard(keyboard, event, mid, image)
                    elif label in {
                        "/reset",
                        "/forget",
                        "Новая задача",
                        "Удалить учебные данные",
                    }:
                        await self._move_keyboard(keyboard, event)
                    elif label in {"Подтвердить фото", "Исправить текст фото"} and hasattr(
                        self.learning, "session"
                    ):
                        current = self.learning.session(learner)
                        if not current or not current.pending_confirmation:
                            await self._move_keyboard(keyboard, event)

        self.store.save_marker(page["marker"])

    async def _move_keyboard(
        self,
        old: dict | None,
        event: IncomingEvent,
        mid: str = "",
        image: dict[str, str] | None = None,
    ) -> None:
        self.store._set(keyboard_key(event.chat_id), json.dumps({"id": mid, "image": image}))
        previous = old["id"] if old is not None else event.message_id
        if not previous or previous == mid or (old is None and event.has_other_attachments):
            return
        try:
            await self.client.remove_keyboard(previous, old.get("image") if old else None)
        except MaxApiError as exc:
            logger.info("MAX · Старые кнопки не скрыты (HTTP %s)", exc.status_code or "—")

    def _keyboard(self, chat_id: int) -> dict | None:
        saved = self.store._get(keyboard_key(chat_id))
        return json.loads(saved) if saved is not None else None

    async def _read_pages(self, queue: asyncio.Queue) -> None:
        failures = 0
        marker = self.store.marker()
        while True:
            try:
                page = await self.client.get_updates(
                    marker,
                    timeout=self.settings.max_poll_timeout,
                    limit=self.settings.max_poll_limit,
                )
                revisions = self._observe_page(page)
                await queue.put((page, revisions))
                marker = page["marker"]
                self.state.last_poll_at = datetime.now(UTC).isoformat()
                self.state.last_poll_monotonic = time.monotonic()
                self.state.status, self.state.last_error = "running", None
                failures = 0
                await asyncio.sleep(0.2)
            except MaxApiError as exc:
                if not exc.retryable:
                    self.state.last_error = f"MAX HTTP {exc.status_code or 'protocol'}"
                    raise
                failures += 1
                delay = min(60.0, max(exc.retry_after or 0.0, 2 ** min(failures, 5)))
                self.state.status, self.state.last_error = "retrying", "MAX unavailable"
                logger.warning("MAX poll unavailable; retry in %.1fs", delay)
                await asyncio.sleep(delay + random.uniform(0, 0.5))

    async def _process_pages(self, queue: asyncio.Queue) -> None:
        while True:
            page, revisions = await queue.get()
            try:
                await self.process_page(page, revisions)
            finally:
                queue.task_done()

    async def run(self) -> None:
        queue = asyncio.Queue(maxsize=4)
        tasks = [
            asyncio.create_task(self._read_pages(queue)),
            asyncio.create_task(self._process_pages(queue)),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            self.state.status = "stopped"
            raise
        except Exception as exc:
            self.state.status = "failed"
            self.state.last_error = self.state.last_error or type(exc).__name__
            logger.error("Polling stopped; error_type=%s", type(exc).__name__)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
