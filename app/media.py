import asyncio
import io
import ipaddress
import socket
import warnings
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


class PhotoError(Exception):
    pass


def prepare_photo(data: bytes) -> tuple[bytes, str]:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise PhotoError("Пришли одно фото размером до 8 МБ.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as original:
                if original.format not in {"JPEG", "PNG", "WEBP"}:
                    raise PhotoError("Нужна фотография в JPEG, PNG или WebP.")
                if getattr(original, "is_animated", False):
                    raise PhotoError("Пришли один неподвижный снимок решения.")
                if original.width * original.height > MAX_IMAGE_PIXELS:
                    raise PhotoError("Фото слишком большое. Уменьши его до 20 мегапикселей.")
                original.load()
                oriented = ImageOps.exif_transpose(original)
                oriented.thumbnail((1800, 1800))
                oriented = oriented.convert("RGBA")
                image = Image.new("RGBA", oriented.size, "white")
                image.alpha_composite(oriented)
                image = image.convert("RGB")
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=88)
                return output.getvalue(), "image/jpeg"
    except PhotoError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise PhotoError(
            "Не удалось прочитать фото. Пришли чёткий снимок в JPEG или PNG."
        ) from None


async def _validate_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or not any(
                host == d or host.endswith("." + d) for d in ("oneme.ru", "max.ru", "okcdn.ru")
            )
        ):
            raise ValueError
        addresses = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ValueError
    except (ValueError, OSError):
        raise PhotoError(
            "Ссылка на фото недоступна. Пришли изображение заново как фотографию."
        ) from None


async def download_photo(url: str) -> tuple[bytes, str]:

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=10), follow_redirects=False, trust_env=False
        ) as client:
            for _ in range(4):
                await _validate_url(url)
                async with client.stream("GET", url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            break
                        url = urljoin(url, location)
                        continue
                    if response.status_code != 200:
                        break
                    size = response.headers.get("content-length")
                    if size and int(size) > MAX_IMAGE_BYTES:
                        raise PhotoError("Пришли одно фото размером до 8 МБ.")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_IMAGE_BYTES:
                            raise PhotoError("Пришли одно фото размером до 8 МБ.")
                    return await asyncio.to_thread(prepare_photo, bytes(data))
    except PhotoError:
        raise
    except (httpx.HTTPError, ValueError):
        pass
    raise PhotoError(
        "Не удалось загрузить фото из MAX. Пришли его ещё раз или запиши действия текстом."
    )
