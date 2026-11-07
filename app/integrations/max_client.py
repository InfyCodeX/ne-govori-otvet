"""Адаптер MAX Bot API."""

from __future__ import annotations

import asyncio
import random
import ssl
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

BASE_URL = "https://platform-api2.max.ru"
MAX_GET_ATTEMPTS = 3
MAX_RETRY_DELAY = 30.0


class MaxApiError(Exception):
    def __init__(
        self,
        message: str = "Ошибка MAX API.",
        *,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after


class MaxAuthError(MaxApiError):
    pass


class MaxProtocolError(MaxApiError):
    pass


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
        if seconds != seconds or seconds == float("inf"):
            return None
    except ValueError:
        try:
            moment = parsedate_to_datetime(value)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            seconds = (moment - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return min(MAX_RETRY_DELAY, max(0.0, seconds))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class MaxClient:
    def __init__(
        self,
        token: str,
        *,
        ca_bundle: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not token or not token.strip() or "\r" in token or "\n" in token:
            raise ValueError("Токен MAX отсутствует или имеет неверный формат.")
        context = ssl.create_default_context()
        if ca_bundle is not None:
            context.load_verify_locations(cafile=str(ca_bundle))
        self._media_context = context
        self._transport = transport
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": token, "Accept": "application/json"},
            verify=context,
            follow_redirects=False,
            timeout=httpx.Timeout(40.0, connect=10.0, write=10.0, pool=10.0),
            transport=transport,
        )

    async def __aenter__(self) -> MaxClient:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._client.__aexit__(*args)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        read_timeout: float = 40.0,
    ) -> dict[str, Any]:
        attempts = MAX_GET_ATTEMPTS if method == "GET" else 1
        for attempt in range(attempts):
            error: MaxApiError
            try:
                response = await self._client.request(
                    method,
                    path,
                    params=params,
                    json=json,
                    timeout=httpx.Timeout(read_timeout, connect=10.0, write=10.0, pool=10.0),
                )
            except httpx.TransportError:
                error = MaxApiError(
                    "Не удалось связаться с MAX API. Проверьте сеть и доверенные сертификаты.",
                    retryable=True,
                )
            else:
                status = response.status_code
                if 200 <= status < 300:
                    try:
                        data = response.json()
                    except (ValueError, UnicodeError):
                        raise MaxProtocolError("MAX API вернул некорректный JSON.") from None
                    if not isinstance(data, dict):
                        raise MaxProtocolError("MAX API вернул неожиданный формат ответа.")
                    return data
                if status in (401, 403):
                    raise MaxAuthError(
                        "MAX API отклонил токен или доступ бота.", status_code=status
                    ) from None
                error = MaxApiError(
                    f"MAX API вернул HTTP {status}.",
                    status_code=status,
                    retryable=status == 429 or 500 <= status < 600,
                    retry_after=_retry_after(response.headers.get("Retry-After")),
                )
            if not error.retryable or attempt + 1 >= attempts:
                raise error from None
            delay = error.retry_after
            if delay is None:
                delay = min(MAX_RETRY_DELAY, 2.0**attempt + random.uniform(0.0, 0.3))
            await asyncio.sleep(delay)
        raise AssertionError("Unreachable")

    async def get_me(self) -> dict[str, Any]:
        data = await self._request("GET", "/me")
        if (
            data.get("is_bot") is not True
            or not _is_int(data.get("user_id"))
            or (data.get("username") is not None and not isinstance(data["username"], str))
        ):
            raise MaxProtocolError("MAX API вернул некорректную информацию о боте.")
        return data

    async def get_subscriptions(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/subscriptions")
        subscriptions = data.get("subscriptions")
        if not isinstance(subscriptions, list) or not all(
            isinstance(item, dict) for item in subscriptions
        ):
            raise MaxProtocolError("MAX API вернул некорректный список Webhook-подписок.")
        return subscriptions

    async def set_commands(self, commands: list[dict[str, str]]) -> None:
        if not 1 <= len(commands) <= 32 or any(
            set(command) != {"name", "description"}
            or not 1 <= len(command["name"]) <= 64
            or not 1 <= len(command["description"]) <= 128
            for command in commands
        ):
            raise ValueError("Некорректный список команд MAX.")
        result = await self._request("PATCH", "/me/commands", json={"commands": commands})
        returned = result.get("commands")
        if (
            not isinstance(returned, list)
            or len(returned) != len(commands)
            or any(
                not isinstance(item, dict)
                or not isinstance(item.get("name"), str)
                or not isinstance(item.get("description"), str)
                for item in returned
            )
            or sorted(
                (item.get("name"), item.get("description"))
                for item in returned
                if isinstance(item, dict)
            )
            != sorted((item["name"], item["description"]) for item in commands)
        ):
            raise MaxProtocolError("MAX не подтвердил список команд.")

    async def get_updates(
        self,
        marker: int | None,
        timeout: int = 30,
        limit: int = 100,
    ) -> dict[str, Any]:
        if marker is not None and not _is_int(marker):
            raise ValueError("marker должен быть целым числом или None.")
        if not _is_int(timeout) or not 0 <= timeout <= 90:
            raise ValueError("timeout должен быть целым числом от 0 до 90.")
        if not _is_int(limit) or not 1 <= limit <= 1000:
            raise ValueError("limit должен быть целым числом от 1 до 1000.")
        params: dict[str, Any] = {
            "timeout": timeout,
            "limit": limit,
            "types": "message_created,bot_started,message_callback",
        }
        if marker is not None:
            params["marker"] = marker
        data = await self._request(
            "GET", "/updates", params=params, read_timeout=float(timeout + 10)
        )
        updates = data.get("updates")
        next_marker = data.get("marker")
        if (
            not isinstance(updates, list)
            or not all(isinstance(item, dict) for item in updates)
            or "marker" not in data
            or (next_marker is not None and not _is_int(next_marker))
        ):
            raise MaxProtocolError("MAX API вернул некорректный пакет событий.")
        return data

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        buttons: list[list[dict[str, str]]] | None = None,
        image_url: str | None = None,
        image_token: str | None = None,
    ) -> dict[str, Any]:
        if not _is_int(chat_id):
            raise ValueError("chat_id должен быть целым числом.")
        if not isinstance(text, str) or not 1 <= len(text) <= 4000:
            raise ValueError("Текст сообщения должен содержать от 1 до 4000 символов.")
        body: dict[str, Any] = {"text": text}
        if buttons:
            body["attachments"] = [{"type": "inline_keyboard", "payload": {"buttons": buttons}}]
        if image_url is not None:
            if not isinstance(image_url, str) or not image_url.startswith("https://"):
                raise ValueError("Нужна HTTPS-ссылка изображения.")
            body.setdefault("attachments", []).insert(
                0, {"type": "image", "payload": {"url": image_url}}
            )
        if image_token:
            body.setdefault("attachments", []).insert(
                0, {"type": "image", "payload": {"token": image_token}}
            )
        data = await self._request("POST", "/messages", params={"chat_id": chat_id}, json=body)
        if not isinstance(data.get("message"), dict):
            raise MaxProtocolError("MAX API не подтвердил создание сообщения.")
        return data

    async def remove_keyboard(self, message_id: str, image: dict[str, str] | None = None) -> None:
        attachments = [{"type": "image", "payload": image}] if image else []
        result = await self._request(
            "PUT", "/messages", params={"message_id": message_id}, json={"attachments": attachments}
        )
        if result.get("success") is not True:
            raise MaxProtocolError("MAX не подтвердил удаление кнопок.")

    async def upload_image(self, path: Path) -> str:
        upload = await self._request("POST", "/uploads", params={"type": "image"})
        url = upload.get("url")
        try:
            parsed = urlsplit(url)
            host = parsed.hostname or ""
            if (
                parsed.scheme != "https"
                or parsed.username
                or parsed.password
                or parsed.port not in {None, 443}
                or not any(
                    host == domain or host.endswith("." + domain)
                    for domain in ("oneme.ru", "max.ru", "okcdn.ru")
                )
            ):
                raise ValueError
            image = await asyncio.to_thread(path.read_bytes)
            if not 1 <= len(image) <= 8 * 1024 * 1024:
                raise ValueError
            async with httpx.AsyncClient(
                verify=self._media_context,
                follow_redirects=False,
                timeout=30,
                transport=self._transport,
            ) as media:
                response = await media.post(url, files={"data": ("bot.png", image, "image/png")})
            if response.status_code != 200:
                raise ValueError
            result = response.json()
            photos = result.get("photos", {})
            tokens = [p.get("token") for p in photos.values() if isinstance(p, dict)]
            token = result.get("token") or next(
                (t for t in tokens if isinstance(t, str) and t), None
            )
            if not isinstance(token, str) or not token:
                raise ValueError
            return token
        except (ValueError, AttributeError, TypeError, OSError, httpx.HTTPError):
            raise MaxProtocolError("Не удалось загрузить логотип MAX.") from None
