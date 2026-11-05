import asyncio
import base64
import binascii
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from dotenv import dotenv_values, set_key
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from filelock import FileLock, Timeout
from pydantic import BaseModel, Field, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import __version__
from app.config import Settings, load_settings
from app.integrations.max_client import MaxClient
from app.learning import COMPLETION_BUTTONS, LearningService, example_payload, learner_key
from app.logging_setup import configure_logging
from app.media import MAX_IMAGE_BYTES, PhotoError, prepare_photo
from app.poller import Poller, WorkerState
from app.storage import Store

logger = logging.getLogger(__name__)


class DemoInput(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class ActionInput(BaseModel):
    action: str = Field(min_length=1, max_length=80)


def create_app(
    settings: Settings | None = None,
    client_factory: Callable[..., MaxClient] = MaxClient,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        try:
            config = settings or load_settings()
        except ValidationError as exc:
            fields = ", ".join(
                ".".join(map(str, err["loc"])) or "configuration"
                for err in exc.errors(include_input=False)
            )
            raise RuntimeError(
                f"Ошибка конфигурации ({fields}). Проверьте .env по README; "
                "в режиме max нужен MAX_BOT_TOKEN"
            ) from None
        configured_secrets = [
            value.get_secret_value()
            for value in (config.max_bot_token, config.yandex_api_key)
            if value
        ]
        configure_logging(config.log_level, configured_secrets)
        application.state.config = config
        application.state.worker = WorkerState()
        config.database_path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(config.database_path.resolve()) + ".lock")
        try:
            lock.acquire(timeout=0)
        except Timeout:
            raise RuntimeError(
                "Эта база уже используется другим процессом. Остановите второй экземпляр бота"
            ) from None
        store, worker_task, tutor = None, None, None
        try:
            store = Store(config.database_path)
            if (
                config.ai_enabled
                and (config.ai_owner_managed or config.ai_daily_request_limit > 0)
                and config.yandex_api_key
                and config.yandex_model_uri
            ):
                from app.integrations.yandex_client import YandexTutor

                tutor = YandexTutor(
                    config.yandex_api_key.get_secret_value(),
                    config.yandex_model_uri,
                    budget_store=None if config.ai_owner_managed else store,
                    trial_budget_rub=config.ai_trial_budget_rub,
                )
            learning = LearningService(store, config, tutor)
            application.state.learning = learning
            application.state.setup_token = secrets.token_urlsafe(32)
            if config.bot_mode == "demo":
                application.state.worker.status = "demo"
                logger.info("Local browser ready; MAX disabled")
                yield
            else:
                async with client_factory(
                    config.max_bot_token.get_secret_value(), ca_bundle=config.max_ca_bundle
                ) as client:
                    poller = Poller(config, client, store, application.state.worker, learning)
                    await poller.bootstrap()
                    worker_task = asyncio.create_task(poller.run(), name="max-poller")
                    try:
                        yield
                    finally:
                        worker_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await worker_task
                        worker_task = None
        finally:
            if worker_task is not None:
                worker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await worker_task
            if store is not None:
                store.close()
            if tutor is not None:
                await tutor.aclose()
            lock.release()
            logger.info("Application stopped")

    application = FastAPI(title="Не говори ответ", version=__version__, lifespan=lifespan)
    application.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )
    application.mount(
        "/static", StaticFiles(directory=Path(__file__).with_name("static")), name="static"
    )

    @application.middleware("http")
    async def local_origin_guard(request: Request, call_next):
        if request.method == "POST":
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.headers.get('host')}"
            if origin is not None and origin != expected:
                return JSONResponse(
                    status_code=403, content={"detail": "Открой локальную страницу бота"}
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path not in {"/", "/static"} and not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.get("/")
    async def index():
        return FileResponse(Path(__file__).with_name("dashboard.html"), media_type="text/html")

    @application.get("/health/live")
    async def live():
        return {"status": "alive"}

    @application.get("/health/ready")
    async def ready():
        state, config = application.state.worker, application.state.config
        spent, reserved = application.state.learning.store.ai_budget_totals()
        fresh = state.last_poll_monotonic is not None and (
            time.monotonic() - state.last_poll_monotonic < config.max_poll_timeout + 120
        )
        healthy = state.status == "demo" or (state.status == "running" and fresh)
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={
                "status": state.status,
                "mode": config.bot_mode,
                "ai": (
                    "unavailable" if application.state.learning.last_provider_error else "yandex"
                )
                if application.state.learning.ai_active
                else "not_connected",
                "ai_budget": "owner_managed" if config.ai_owner_managed else "disabled",
                "last_poll_at": state.last_poll_at,
                "last_error": state.last_error,
                "incomplete_messages": state.incomplete_messages,
                "trial_budget_rub": None if config.ai_owner_managed else config.ai_trial_budget_rub,
                "trial_spent_rub": None if config.ai_owner_managed else spent / 100,
                "trial_reserved_rub": None if config.ai_owner_managed else reserved / 100,
            },
        )

    @application.post("/demo/message")
    async def demo(body: DemoInput, request: Request, response: Response):
        learner = browser_learner(request, response)
        reply = await application.state.learning.handle(learner, body.text)
        return local_result(learner, reply)

    @application.post("/learning/photo")
    async def photo(request: Request, response: Response):
        learner = browser_learner(request, response)
        if not application.state.learning.ai_active:
            raise HTTPException(status_code=409, detail="Для фото нужно подключение ИИ")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_IMAGE_BYTES * 4 // 3 + 8192:
                raise HTTPException(status_code=413, detail="Фото должно быть до 8 МБ")
        try:
            data = json.loads(raw)
            caption, encoded = data.get("text", ""), data["image_base64"]
            if not isinstance(caption, str) or len(caption) > 4000 or not isinstance(encoded, str):
                raise ValueError
            image = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError, KeyError, AttributeError, binascii.Error):
            raise HTTPException(
                status_code=422, detail="Не удалось прочитать фото и подпись"
            ) from None
        try:
            image, mime = await asyncio.to_thread(prepare_photo, image)
            reply = await application.state.learning.ingest(learner, caption, image, mime)
        except PhotoError as exc:
            from app.learning_models import TutorReply

            reply = TutorReply(text=str(exc))
        return local_result(learner, reply)

    def local_result(learner, reply):
        state = application.state.learning.session(learner)
        input_mode = "problem_and_steps"
        if state:
            if state.pending_confirmation:
                input_mode = "pending_confirmation"
            elif state.self_discovery and state.awaiting_correction:
                input_mode = (
                    "photo_correction_reason"
                    if getattr(state, "confirmed_photo", None)
                    else "correction_with_reason"
                )
            elif state.awaiting_correction:
                input_mode = "correction"
            elif not state.steps:
                input_mode = "steps"
            elif state.analysis and state.analysis.status == "correct":
                input_mode = "complete" if state.analysis.complete else "continue"
        return {
            "reply": reply.text,
            "mode": "local",
            "input_mode": input_mode,
            "problem": state.problem if state else None,
            "accepted": reply.accepted,
            "buttons": reply.buttons,
        }

    def browser_learner(request: Request, response: Response) -> str:
        identity = request.cookies.get("learning_session", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,80}", identity):
            identity = secrets.token_urlsafe(32)
            response.set_cookie(
                "learning_session", identity, httponly=True, samesite="strict", max_age=7 * 86400
            )
        return learner_key("browser", identity)

    @application.get("/learning/example")
    async def example():
        return example_payload()

    @application.get("/learning/state")
    async def resume(request: Request, response: Response):
        from app.learning_models import TutorReply

        learner = browser_learner(request, response)
        state = application.state.learning.session(learner)
        if state is None:
            return {**local_result(learner, TutorReply(text="")), "exists": False}
        extraction = state.pending_submission if state.pending_confirmation else None
        problem, steps = (
            (extraction.problem, extraction.steps) if extraction else (state.problem, state.steps)
        )
        text = "Сохранённая запись:\n" + problem
        if steps:
            text += "\n" + "\n".join(f"{i}. {step}" for i, step in enumerate(steps, 1))
        buttons = []
        if extraction:
            text += "\n\nПроверь распознанные строки перед проверкой решения."
            buttons = [
                [
                    {
                        "type": "callback",
                        "text": "Всё верно — проверить",
                        "payload": "confirm_submission"
                        + ("@" + state.pending_id if state.pending_id else ""),
                    }
                ],
                [
                    {
                        "type": "callback",
                        "text": "Исправить текст",
                        "payload": "reject_submission"
                        + ("@" + state.pending_id if state.pending_id else ""),
                    }
                ],
            ]
        else:
            if state.analysis and state.analysis.complete:
                text += "\n\nЗадача завершена. Можно выбрать новую задачу или реванш."
                buttons = COMPLETION_BUTTONS
            else:
                text += "\n\nПродолжи эту тренировку или нажми «Начать заново» для другой задачи."
            if state.steps and state.analysis is None:
                buttons = [
                    [
                        {
                            "type": "callback",
                            "text": "Повторить проверку",
                            "payload": "retry_analysis",
                        }
                    ]
                ]
        return {**local_result(learner, TutorReply(text=text, buttons=buttons)), "exists": True}

    @application.post("/learning/action")
    async def action(body: ActionInput, request: Request, response: Response):
        learner = browser_learner(request, response)
        reply = await application.state.learning.action(learner, body.action)
        return local_result(learner, reply)

    @application.get("/setup/ai", response_class=HTMLResponse, include_in_schema=False)
    async def setup_page():
        if not application.state.config.enable_ai_setup:
            raise HTTPException(status_code=404)
        content = Path(__file__).with_name("ai_setup.html").read_text(encoding="utf-8")
        return HTMLResponse(
            content.replace("__CSRF__", application.state.setup_token),
            headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"},
        )

    @application.post("/setup/ai", include_in_schema=False)
    async def save_ai_configuration(request: Request):
        if not application.state.config.enable_ai_setup:
            raise HTTPException(status_code=404)
        origin = request.headers.get("origin")
        expected_origin = f"{request.url.scheme}://{request.headers.get('host')}"
        if origin != expected_origin or not secrets.compare_digest(
            request.headers.get("x-setup-token", ""), application.state.setup_token
        ):
            raise HTTPException(status_code=403, detail="Откройте локальную страницу настройки")
        raw = await request.body()
        if len(raw) > 8192:
            raise HTTPException(status_code=413, detail="Слишком большой запрос")
        try:
            data = json.loads(raw)
            key, folder = data["api_key"], data["folder_id"]
            model = data.get("model", "qwen3.6-35b-a3b")
            if (
                not isinstance(key, str)
                or not re.fullmatch(r"[!-~]{10,512}", key)
                or not isinstance(folder, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", folder)
                or model != "qwen3.6-35b-a3b"
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise HTTPException(
                status_code=422, detail="Проверьте ID каталога и API-ключ"
            ) from None
        private_dir = (
            Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share")))
            / "NeGovoriOtvet"
        )
        private_dir.mkdir(parents=True, exist_ok=True)
        private_file = private_dir / ".env"
        if not private_file.exists():
            private_file.touch()
            for name, value in dotenv_values(".env").items():
                if value is not None and name != "BOT_ENV_FILE":
                    set_key(private_file, name, value)
            set_key(Path(".env"), "BOT_ENV_FILE", private_file.as_posix())
        set_key(private_file, "YANDEX_API_KEY", key)
        set_key(private_file, "YANDEX_MODEL_URI", f"gpt://{folder}/{model}")
        existing = dotenv_values(private_file)
        if "AI_ENABLED" not in existing:
            set_key(private_file, "AI_ENABLED", "false")
        if "AI_DAILY_REQUEST_LIMIT" not in existing:
            set_key(private_file, "AI_DAILY_REQUEST_LIMIT", "0")
        if "AI_TRIAL_BUDGET_RUB" not in existing:
            set_key(private_file, "AI_TRIAL_BUDGET_RUB", "0")
        return {
            "message": "Ключ сохранён локально. Сохранение не запускает запросы. "
            "Для применения перезапусти приложение."
        }

    return application


app = create_app()
