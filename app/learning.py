"""Учебные сценарии и состояние диалога."""

import asyncio
import hashlib
import logging
import re
import secrets
from typing import Any

from app.config import Settings
from app.domain import LearningStore, TutorError, TutorPort
from app.input_policy import input_feedback
from app.learning_models import StudySession, SubmissionExtraction, TutorAnalysis, TutorReply
from app.skills import (
    MAP_TOPICS,
    PERCENTAGES,
    PROPORTIONS,
    REVIEW,
    ROUNDING,
    UNITS,
    fallback_exercise,
    practice_shape_matches,
    topic_buttons,
    topic_id,
    valid_topic,
)
from app.submissions import (
    action_lines,
    align_correction,
    clean_transcript,
    first_change,
    merge_actions,
    normalize,
    parse_text,
    short_answer,
)

EXERCISES = (
    {
        "problem": "3(x + 2) = 18",
        "wrong": ["3x + 2 = 18", "x = 16/3"],
        "correct": ["3x + 6 = 18", "3x = 12", "x = 4"],
    },
    {
        "problem": "2(x + 3) = 16",
        "wrong": ["2x + 3 = 16", "x = 13/2"],
        "correct": ["2x + 6 = 16", "2x = 10", "x = 5"],
    },
)
logger = logging.getLogger(__name__)
SKILL = "Раскрытие скобок"
BUTTONS = [
    [{"type": "callback", "text": "Где я свернул не туда?", "payload": "first_error"}],
    [{"type": "callback", "text": "Не говори ответ", "payload": "hints"}],
    [{"type": "callback", "text": "Поймай свою ошибку", "payload": "discovery"}],
    [{"type": "callback", "text": "Реванш", "payload": "rematch"}],
    [{"type": "callback", "text": "Карта слабых мест", "payload": "map"}],
]
HINT_BUTTONS = [
    [{"type": "callback", "text": title, "payload": f"hint{level}"}]
    for level, title in enumerate(("Намёк", "Подсказка", "Подробный разбор"), 1)
]
COMPLETION_BUTTONS = [
    [{"type": "callback", "text": "Новое задание", "payload": "reset"}],
    [{"type": "callback", "text": "Реванш", "payload": "rematch"}],
    [{"type": "callback", "text": "Карта слабых мест", "payload": "map"}],
]


def learner_key(namespace: str, identity: str | int) -> str:
    return hashlib.sha256(f"{namespace}:{identity}".encode()).hexdigest()


def contains_answer(text: str) -> bool:

    return bool(
        re.search(
            r"(?i)(?:(?:ответ|answer|итог)\s*[:=]|\b[a-zа-я](?:_?[12]|[₁₂])?\s*=\s*[+−-]?\d)", text
        )
    )


def example_payload() -> dict[str, Any]:
    return {"problem": EXERCISES[0]["problem"], "steps": EXERCISES[0]["wrong"]}


def without_repeated_condition(extraction: SubmissionExtraction) -> SubmissionExtraction:

    steps = []
    for original in extraction.steps:
        line = original
        while re.match(r"^\d+[.)]\s+", line):
            line = re.sub(r"^\d+[.)]\s+", "", line, count=1)
        steps.append(line if line.strip() else original)
    while steps and normalize(steps[0]) == normalize(extraction.problem):
        steps.pop(0)
    problem, steps = clean_transcript(extraction.problem, steps)
    return extraction.model_copy(update={"problem": problem, "steps": steps})


def canonical_skill(skill: str, error_step: str = "") -> str:

    if re.match(r"\s*(?:затем\s+|поэтому\s+)?округл\w*\b", error_step, re.I):
        return ROUNDING

    if contains_answer(skill) or re.search(r"[=\d]", skill):
        return REVIEW
    value = skill.casefold()
    for fragments, category in (
        (("вероятн", "равновозмож", "число исходов", "подсчёт исходов"), "Вероятность"),
        (("статист", "медиан", "среднее арифмет", "размах"), "Статистика"),
        (("округлен", "округлён", "целая часть"), ROUNDING),
        (("процент", "скидк", "наценк"), PERCENTAGES),
        (("пропорц", "масштаб"), PROPORTIONS),
        (("единиц", "размерност"), UNITS),
        (("неравен",), "Неравенства"),
        (("функц", "координат"), "Функции и координаты"),
        (("преобразован", "тождеств", "многочлен"), "Преобразования выражений"),
        (("скоб", "распределитель"), "Раскрытие скобок"),
        (("знак", "перенос"), "Знаки и перенос слагаемых"),
        (("дроб",), "Дроби"),
        (("арифмет", "вычислен"), "Арифметика"),
        (("уравнен",), "Уравнения"),
        (("степен", "корн", "корен"), "Степени и корни"),
        (("геомет", "угол", "треугольн", "площад"), "Геометрия"),
        (("логик", "рассужден"), "Логика рассуждений"),
    ):
        if any(fragment in value for fragment in fragments):
            return category
    return REVIEW


def help_levels(session: StudySession) -> tuple[str, str, str]:

    analysis = session.analysis
    index = analysis.first_error_step or 1
    action = session.steps[index - 1] if index <= len(session.steps) else ""
    context = " ".join([action, analysis.question, analysis.hint, analysis.detail]).casefold()
    if analysis.skill == "Уравнения" and (
        "виет" in context or re.search(r"x[_₁₂12]", action.casefold())
    ):
        return (
            "Как связаны сумма и произведение корней с коэффициентами уравнения?",
            "В теореме Виета у суммы корней знак линейного коэффициента меняется. "
            "Произведение связано со свободным членом. Учти старший коэффициент.",
            "1. Перенеси слагаемые влево и запиши уравнение в стандартном виде. "
            "Обозначь старший, линейный и свободный коэффициенты буквами a, b, c со знаками.\n"
            "2. Для суммы корней используй −b/a, для произведения — c/a. "
            "Пока оставь эти отношения буквенными.\n"
            "3. Сам подставь коэффициенты и сравни с каждой своей строкой.\n"
            "4. Проверь найденную пару одновременно по сумме и произведению; "
            "затем подставь каждый свой корень в исходное уравнение. "
            "Какое из условий нарушено первым?",
        )
    if analysis.skill == SKILL:
        return (
            "На все ли части выражения подействовал множитель перед скобками?",
            "Распределительный закон: внешний множитель относится к каждому слагаемому в скобках.",
            "1. Выдели внешний множитель и каждое слагаемое внутри скобок.\n"
            "2. Умножь на него каждое слагаемое отдельно, сохраняя знаки.\n"
            "3. Сложи полученные выражения и сравни с выбранной строкой.\n"
            "4. Только после проверки раскрытия скобок продолжай преобразования. "
            "Какой член у тебя изменился не по этому правилу?",
        )
    hint, detail = analysis.hint, analysis.detail
    if normalize(hint) == normalize(analysis.question):
        hint = (
            "Запиши применённое правило отдельно и сравни с выбранным действием. "
            "Проверь порядок операций, знаки и ограничения."
        )
    if normalize(detail) in {normalize(analysis.question), normalize(analysis.hint)}:
        detail = (
            "Раздели выбранное действие на отдельные операции. "
            "Установи порядок их выполнения по правилу. "
            "Выполни их по очереди, сохраняя знаки и допустимые значения. "
            "Сравни полученную запись со своей и проверь результат обратным действием."
        )
    return (
        analysis.question,
        "Направление проверки: " + hint,
        "Правило: " + detail + "\n\nПорядок самопроверки:\n"
        "1. Сопоставь выбранное действие с правилом.\n"
        "2. Проверь знаки, ограничения и вычисления в нём.\n"
        "3. Запиши исправленную строку самостоятельно и объясни, почему она допустима.",
    )


def reviewed_analysis(problem: str, steps: list[str]) -> TutorAnalysis | None:
    exercise = next((e for e in EXERCISES if normalize(e["problem"]) == normalize(problem)), None)
    if exercise is None or not steps:
        return None
    valid = [normalize(s) for s in exercise["correct"]]
    submitted = [normalize(s) for s in steps]

    if submitted == valid[: len(submitted)] and len(submitted) <= len(valid):
        return TutorAnalysis(
            status="correct",
            skill=SKILL,
            question="Продолжи решение самостоятельно.",
            hint="Сохраняй равенство при преобразованиях.",
            detail="Применяй одну и ту же операцию к обеим частям равенства.",
            discovery="Объясни, почему преобразование сохраняет равенство.",
            complete=len(submitted) == len(valid),
        )
    if submitted[0] != normalize(exercise["wrong"][0]):
        return None
    return TutorAnalysis(
        status="error",
        first_error_step=1,
        skill=SKILL,
        question="Что произошло со вторым слагаемым при раскрытии скобок?",
        hint="Внешний множитель относится к каждому слагаемому внутри скобок.",
        detail="Представь скобки как сумму двух частей. Умножь внешнее число на первую "
        "часть, затем на вторую и запиши сумму. Только после этого переноси "
        "постоянное слагаемое. Последующие вычисления выполни самостоятельно.",
        discovery="Просмотри все действия и найди, где впервые нарушилось правило. "
        "Пришли исправленную строку, "
        "а на следующей строке объясни, к каким слагаемым относится множитель.",
    )


class LearningService:
    def __init__(self, store: LearningStore, settings: Settings, tutor: TutorPort | None = None):
        self.store, self.settings, self.tutor = store, settings, tutor

        self.locks = [asyncio.Lock() for _ in range(64)]
        self.checker_slots = asyncio.Semaphore(2)
        self.last_provider_error = False

    def _lock(self, learner: str) -> asyncio.Lock:
        return self.locks[int(hashlib.sha256(learner.encode()).hexdigest()[:8], 16) % 64]

    @property
    def ai_active(self) -> bool:
        return bool(
            self.tutor
            and self.settings.ai_enabled
            and (self.settings.ai_owner_managed or self.settings.ai_daily_request_limit > 0)
        )

    def _reserve_ai(self, learner: str) -> bool:
        if self.settings.ai_owner_managed:
            return self.ai_active
        return self.ai_active and self.store.reserve_ai_call(
            self.settings.ai_daily_request_limit, learner=learner
        )

    def session(self, learner: str) -> StudySession | None:
        data = self.store.learning_session(learner)
        return StudySession.model_validate(data) if data else None

    def save(self, learner: str, session: StudySession) -> None:
        if session.problem.strip():
            self.store.remember_exercise(learner, session.problem)
        self.store.save_learning_session(learner, session.model_dump())

    def _record_mistake(self, learner: str, session: StudySession, analysis: TutorAnalysis) -> None:
        index = analysis.first_error_step
        if analysis.status != "error" or index is None:
            return
        before = session.problem if index == 1 else session.steps[index - 2]
        material = f"{index}\n{normalize(before)}\n{normalize(session.steps[index - 1])}"
        key = hashlib.sha256(material.encode()).hexdigest()[:24]
        if key not in session.mistake_keys:
            self._record_progress(learner, analysis.skill, "mistakes")
            session.mistake_keys = [*session.mistake_keys[-127:], key]

    def _record_progress(self, learner: str, skill: str, metric: str) -> None:
        if skill in self._map_topics(learner):
            self.store.record_progress(learner, skill, metric)

    def _map_topics(self, learner: str) -> set[str]:
        return set(MAP_TOPICS) | {
            skill for skill in self.store.learning_topics(learner) if valid_topic(skill)
        }

    def _map_rows(self, learner: str) -> list[tuple]:
        topics = self._map_topics(learner)
        return [row for row in self.store.learning_progress(learner) if row[0] in topics]

    @staticmethod
    def _unavailable() -> TutorReply:
        return TutorReply(
            text="Сейчас проверка с ИИ временно недоступна. Задача сохранена. "
            "Когда соединение восстановится, можно будет продолжить. "
            "Сохранённые подсказки доступны."
        )

    def _provider_failure(self, error: TutorError, *, stage: str = "") -> TutorReply:
        self.last_provider_error = True
        kind = error.kind
        reason = {
            "protocol": "формат ответа",
            "authentication": "доступ отклонён",
            "unavailable": "сервис недоступен",
            "rate_limit": "лимит запросов",
            "budget": "лимит расходов",
            "input": "формат запроса",
        }.get(kind, "неизвестная причина")
        logger.warning(
            "ИИ · Проверка не завершена (%s, HTTP %s)",
            "достигнут лимит токенов" if error.output_limit else reason,
            error.status_code or "—",
        )
        if error.output_limit:
            messages = {
                "analysis": (
                    "Эта запись оказалась слишком сложной для проверки целиком. "
                    "Пришли часть выражения и действия к ней "
                    "или начни новую задачу командой /reset."
                ),
                "correction": (
                    "Проверка исправления прервалась. Отправь исправленную строку ещё раз. "
                    "Задача и подсказки сохранены."
                ),
                "explanation": (
                    "Проверка пояснения прервалась. Отправь пояснение ещё раз, "
                    "вычисления сохранены."
                ),
            }
            return TutorReply(
                text=messages.get(
                    stage, "Обработка прервалась. Повтори попытку или пришли запись короче."
                )
            )
        if kind == "protocol":
            return TutorReply(
                text="Проверка прервалась. Повтори попытку или пришли условие и действия текстом."
            )
        return self._unavailable()

    @staticmethod
    def _limit() -> TutorReply:
        return LearningService._unavailable()

    def welcome(self) -> TutorReply:
        mode = (
            "Пришли условие и свои действия текстом или одним фото. "
            "Голосовые пока не поддерживаются."
        )
        return TutorReply(
            text=(
                "Привет! Я «Не говори ответ», бот команды «Кодоворот». "
                "Помогу исправить ошибку самому.\n\n"
                "1. Где свернул не туда — покажу первое ошибочное действие.\n"
                "2. Не говори ответ — намёк, подсказка или подробный разбор правила.\n"
                "3. Поймай свою ошибку — исправь действие и объясни правило; я проверю.\n"
                "4. Реванш — похожая задача после завершения решения.\n"
                "5. Карта слабых мест — ошибки, исправления и тренировка навыка.\n\n"
                + mode
                + "\n/reset — новая задача; /forget — удалить учебные данные. "
                ""
            ),
            buttons=[[{"type": "callback", "text": "Учебный пример", "payload": "example"}]]
            + BUTTONS,
        )

    async def handle(self, learner: str, text: str | None) -> TutorReply:
        async with self._lock(learner):
            return await self._handle(learner, text)

    async def ingest(
        self,
        learner: str,
        text: str | None,
        image: bytes | None = None,
        mime_type: str | None = None,
    ) -> TutorReply:
        async with self._lock(learner):
            if image is None:
                return await self._handle(learner, text)
            feedback = input_feedback(text or "")
            if feedback:
                return TutorReply(text=feedback)
            if not self.ai_active:
                return TutorReply(
                    text="Распознавание фото сейчас недоступно. "
                    "Можно открыть учебный пример /example."
                )
            if len(image) > 8 * 1024 * 1024 or mime_type not in {
                "image/jpeg",
                "image/png",
                "image/webp",
            }:
                return TutorReply(
                    text="Пришли одну фотографию JPEG, PNG или WebP размером до 8 МБ."
                )
            if len(text or "") > 4000:
                return TutorReply(text="Сократи подпись к фотографии до 4000 символов.")
            current = self.session(learner)
            if current and current.pending_confirmation:
                return TutorReply(
                    text="Сначала подтверди текст предыдущей фотографии "
                    "или нажми «Исправить текст», затем пришли новый снимок."
                )
            return await self._extract(learner, text or "", image=image, mime_type=mime_type)

    async def _extract(
        self,
        learner: str,
        text: str,
        *,
        image: bytes | None = None,
        mime_type: str | None = None,
    ) -> TutorReply:

        if len(text) > 12000:
            return TutorReply(
                text="Пришли короткое условие и до 30 действий, всего до 12000 символов."
            )
        if not self._reserve_ai(learner):
            return self._limit()
        previous = self.session(learner)
        context = text
        if previous and not previous.pending_confirmation and image is None:
            context = (
                "Контекст текущей задачи (не добавляй новых действий): "
                + previous.problem
                + "\nСообщение ученика:\n"
                + text
            )
        try:
            extraction = await self.tutor.extract_submission(
                context, image=image, mime_type=mime_type
            )
        except TutorError as error:
            return self._provider_failure(error)
        self.last_provider_error = False
        extraction = without_repeated_condition(extraction)
        feedback = input_feedback("\n".join([extraction.problem, *extraction.steps]))
        if feedback:
            return TutorReply(text=feedback)
        if extraction.confidence != "high" or not extraction.problem.strip():
            clarification = extraction.clarification
            if not clarification or contains_answer(clarification):
                clarification = (
                    "Не все строки читаются уверенно. Пришли более чёткий снимок "
                    "или перепиши условие и действия текстом."
                )
            return TutorReply(text="Нужны уточнения. " + clarification)
        candidate = StudySession(problem=extraction.problem, steps=extraction.steps, source="ai")
        if (
            image is None
            and previous
            and normalize(previous.problem) == normalize(candidate.problem)
            and normalize(previous.problem) not in normalize(text)
        ):
            count = (
                (previous.analysis.first_error_step or 1) - 1
                if previous.awaiting_correction and previous.analysis
                else len(previous.steps)
            )
            candidate.steps = (
                merge_actions(previous.steps, candidate.steps, count)
                if candidate.steps
                else previous.steps[:]
            )
        if image is not None:
            same_task = bool(
                previous and normalize(previous.problem) == normalize(extraction.problem)
            )
            pending = (
                previous if same_task else StudySession(problem=extraction.problem, source="ai")
            )
            pending.pending_id = secrets.token_hex(8)
            pending.pending_confirmation = True
            pending.pending_submission = extraction
            pending.pending_preserves_session = same_task
            pending.pending_caption = text if same_task and previous.self_discovery else ""
            pending.confirmed_photo = None
            self.save(learner, pending)
            transcript = "\n".join(f"{i}. {line}" for i, line in enumerate(extraction.steps, 1))
            return TutorReply(
                text="Я прочитал на фотографии:\n\nУсловие: "
                + extraction.problem
                + ("\nДействия:\n" + transcript if transcript else "\nДействия пока не найдены.")
                + "\n\nЭто запись с твоего снимка, ещё не проверка решения. Всё прочитано верно? "
                "Если нет, нажми «Исправить текст» и пришли правильную запись.",
                buttons=[
                    [
                        {
                            "type": "callback",
                            "text": "Всё верно — проверить",
                            "payload": "confirm_submission@" + pending.pending_id,
                        }
                    ],
                    [
                        {
                            "type": "callback",
                            "text": "Исправить текст",
                            "payload": "reject_submission@" + pending.pending_id,
                        }
                    ],
                ],
            )
        return await self._evaluate(learner, candidate, previous)

    async def _handle(self, learner: str, text: str | None) -> TutorReply:
        if not text or not text.strip():
            return TutorReply(
                text="Отправь условие и действия в решении текстом, фотографию или /example."
            )
        text = text.strip()
        if len(text) > 12000:
            return TutorReply(
                text="Нужно до 12000 символов, до 30 действий и условие до 1500 символов."
            )
        feedback = input_feedback(text)
        if feedback:
            return TutorReply(text=feedback)
        aliases = {
            "/example": "example",
            "/first_error": "first_error",
            "/hint": "hints",
            "/hint1": "hint1",
            "/hint2": "hint2",
            "/hint3": "hint3",
            "/discover": "discovery",
            "/rematch": "rematch",
            "/map": "map",
            "/reset": "reset",
            "/forget": "forget",
            "/confirm": "confirm_submission",
            "/practice": "practice",
        }
        command = text.split(maxsplit=1)[0].lower()
        if command == "/start":
            self.store.forget_learning(learner)
            return self.welcome()
        if command == "/help":
            return TutorReply(
                text="Отправь условие и действия текстом или одну фотографию решения.\n"
                "/example — учебный пример\n/first_error — первое ошибочное действие\n"
                "/hint — три уровня помощи\n/discover — найди действие, исправь и объясни\n"
                "/rematch — похожая задача после решения\n/map — карта практики\n"
                "/reset — новая задача\n/forget — удалить учебные данные",
                buttons=BUTTONS,
            )
        if command == "/diagnostic":
            return TutorReply(text="Бот на связи. Пришли условие и своё решение.")
        if command in aliases:
            return await self._action(learner, aliases[command])
        if command.startswith("/"):
            return TutorReply(text="Неизвестная команда. Отправь /help.")
        session = self.session(learner)
        if session and session.pending_confirmation:
            if text.casefold() in {"верно", "подтверждаю", "да"}:
                return await self._action(learner, "confirm_submission")
            if text.casefold() in {"нет", "неверно"}:
                return await self._action(learner, "reject_submission")
            return TutorReply(
                text="Сначала подтверди распознанный текст кнопкой или нажми "
                "«Исправить текст». После этого пришли исправленную запись.",
                buttons=[
                    [
                        {
                            "type": "callback",
                            "text": "Исправить текст",
                            "payload": "reject_submission",
                        }
                    ]
                ],
            )
        if (
            session
            and session.correction_draft
            and session.self_discovery
            and session.awaiting_correction
            and "=" not in text
            and parse_text(text, session.problem) is None
            and not re.search(r"(?:корн|корен|ответ).{0,20}\d", text, re.I)
        ):
            candidate = session.model_copy(deep=True)
            candidate.steps = session.correction_draft[:]
            return await self._evaluate(learner, candidate, session, explanation=text)
        if (
            session
            and session.self_discovery
            and session.awaiting_correction
            and self.ai_active
            and hasattr(self.tutor, "interpret_correction")
        ):
            return await self._free_correction(learner, session, text)
        if (
            session
            and session.confirmed_photo
            and session.self_discovery
            and session.awaiting_correction
        ):
            first_line = text.splitlines()[0].strip()
            if not re.match(r"(?i)^(?:действие|шаг)\s+\d+\s*[:.)-]\s*\S", first_line):
                return await self._photo_correction(learner, session, text)
        raw_lines = [line.strip() for line in text.splitlines() if line.strip()]
        lines = [re.sub(r"^(?:\d+\)\s*|\d+\.\s+)", "", line) for line in raw_lines]
        if (
            len(lines[0]) > 1500
            or any(len(line) > 1000 for line in lines[1:])
            or (session and len(lines[0]) > 1000)
        ):
            return TutorReply(
                text="Для этой версии нужно до 30 действий и короткое условие задачи."
            )
        if not (session and short_answer(text)) and not re.search(
            r"^[\d\s.,():;−+\-*/^√²³]+$|[=+*/^√²³<>≤≥≠]|\b(?:уравнен\w*|вычисл\w*|докаж\w*|треуголь\w*|"
            r"угол\w*|отрез\w*|дроб\w*|корен\w*|корн\w*|степен\w*|слагаем\w*|"
            r"равен\w*|периметр\w*|площад\w*|окруж\w*|если|следовательно|сколько|найди|остал\w*)\b",
            text.casefold(),
        ):
            return TutorReply(
                text="Не вижу в сообщении условия задачи или действия в решении. "
                "Пришли математическую запись текстом или одно фото. "
                "Текущая задача и карта сохранены."
            )
        parsed = parse_text(text, session.problem if session else None)
        if parsed:
            if session and session.self_discovery and session.awaiting_correction:
                if parsed.problem and normalize(parsed.problem) == normalize(session.problem):
                    chosen = first_change(session.steps, parsed.steps)
                else:
                    verified = (
                        (session.analysis.first_error_step or 1) - 1 if session.analysis else 0
                    )
                    chosen, parsed.steps = align_correction(
                        text, session.steps, parsed.steps, verified
                    )
                if chosen is None:
                    return TutorReply(text="Уточни, какую запись ты исправляешь.")
                if not session.analysis or chosen != session.analysis.first_error_step:
                    return TutorReply(text="Пересмотри решение: начни с первого нарушения правила.")
            if (
                len(parsed.steps) > 30
                or len(parsed.problem or "") > 1500
                or any(len(step) > 1000 for step in parsed.steps)
            ):
                return TutorReply(text="Пришли условие до 1500 символов и до 30 действий.")
            if parsed.problem:
                candidate = StudySession(problem=parsed.problem, steps=parsed.steps, source="ai")
            elif session:
                candidate = session.model_copy(deep=True)
                count = (
                    (session.analysis.first_error_step or 1) - 1
                    if session.awaiting_correction and session.analysis
                    else len(session.steps)
                )
                candidate.steps = merge_actions(session.steps, parsed.steps, count)
            else:
                return await self._extract(learner, text)
            return await self._evaluate(learner, candidate, session, explanation=parsed.explanation)
        if self.ai_active and not (
            session and session.self_discovery and session.awaiting_correction
        ):
            return await self._extract(learner, text)
        explanation = ""
        if session and session.self_discovery and session.awaiting_correction:
            if normalize(lines[0]) == normalize(session.problem):
                return TutorReply(
                    text="В этом режиме пришли номер выбранного действия, исправленную "
                    "строку и пояснение. Полное решение пока не нужно."
                )
            chosen = re.match(r"(?i)^(?:действие|шаг)\s+(\d+)\s*[:.)-]?\s*(.*)$", raw_lines[0])
            if chosen:
                index, corrected = int(chosen[1]), chosen[2].strip()
                remaining = lines[1:]
                if not corrected and remaining:
                    corrected, remaining = remaining[0], remaining[1:]
                if not session.analysis or index != session.analysis.first_error_step:
                    return TutorReply(
                        text="Пересмотри выбранное действие: найди именно первое нарушение "
                        "правила. Его номер пока не раскрываю.",
                        buttons=BUTTONS,
                    )
                lines = [corrected, *remaining] if corrected else []
            elif session.source == "ai":
                return TutorReply(
                    text="Уточни, какую запись ты исправляешь и почему. "
                    "Можно написать своими словами."
                )
            if len(lines) < 2:
                return TutorReply(
                    text="Пришли исправленную строку и короткое пояснение "
                    "на следующей строке: почему ты меняешь это действие?"
                )
        if session and len(lines) > 1 and normalize(lines[0]) == normalize(session.problem):
            candidate = StudySession(
                problem=session.problem,
                steps=lines[1:],
                source=session.source,
                practice_skill=session.practice_skill,
            )
        elif session and session.awaiting_correction and session.analysis:
            submitted, explanation = action_lines(lines)
            if not submitted:
                return TutorReply(text="Пришли исправленное действие математической записью.")
            index = session.analysis.first_error_step or 1
            candidate = session.model_copy(deep=True)
            candidate.steps = merge_actions(session.steps, submitted, index - 1)
        elif session and not session.steps:
            candidate = session.model_copy(deep=True)
            if normalize(lines[0]) == normalize(session.problem):
                lines = lines[1:]
            if not lines:
                return TutorReply(text="Теперь пришли собственные действия в решении этой задачи.")
            candidate.steps = lines
        elif (
            session
            and session.analysis
            and session.analysis.status == "correct"
            and not session.analysis.complete
        ):
            candidate = session.model_copy(deep=True)
            candidate.steps = merge_actions(session.steps, lines, len(session.steps))
        elif len(lines) >= 2:
            candidate = StudySession(problem=lines[0], steps=lines[1:], source="ai")
        elif self.ai_active:
            return await self._extract(learner, text)
        else:
            return TutorReply(text="Пришли условие и свои вычисления. Или открой /example.")
        if (
            len(candidate.steps) > 30
            or len(candidate.problem) > 1500
            or any(len(s) > 1000 for s in candidate.steps)
        ):
            return TutorReply(text="Для этой версии нужно до 30 действий, каждый до 1000 символов.")
        return await self._evaluate(learner, candidate, session, explanation=explanation)

    async def _evaluate(
        self,
        learner: str,
        candidate: StudySession,
        previous: StudySession | None,
        *,
        explanation: str = "",
    ) -> TutorReply:

        if (
            len(candidate.problem) + sum(map(len, candidate.steps)) > 12000
            or len(candidate.steps) > 30
            or any(len(step) > 1000 for step in candidate.steps)
        ):
            return TutorReply(
                text="Сократи запись: нужно до 30 действий и всего до 12000 символов."
            )
        if not candidate.steps:
            candidate.analysis = None
            candidate.awaiting_correction = False
            self.save(learner, candidate)
            return TutorReply(
                text="Условие принято. Пришли собственные действия в решении — "
                "готовое решение за тебя не составляю."
            )
        same_problem = bool(
            previous and normalize(previous.problem) == normalize(candidate.problem)
        )
        correcting = bool(same_problem and previous.awaiting_correction)
        from app.arithmetic_check import checked_arithmetic
        from app.math_check import check_linear
        from app.quadratic_check import checked_quadratic

        reviewed = reviewed_analysis(candidate.problem, candidate.steps)
        analysis = (
            reviewed
            or checked_arithmetic(candidate.problem, candidate.steps)
            or checked_quadratic(candidate.problem, candidate.steps)
        )
        if analysis is None and self.settings.ai_owner_managed:
            from app.diagnostics import verified_linear_analysis

            if check_linear(candidate.problem, candidate.steps) is not None:
                baseline = TutorAnalysis(
                    status="uncertain",
                    skill="Уравнения",
                    question="Проверь правило действия.",
                    hint="Сохраняй равенство.",
                    detail="Выполни одинаковое действие с обеими частями.",
                    discovery="Выбери ошибочное действие.",
                )
                analysis = verified_linear_analysis(baseline, candidate.problem, candidate.steps)
        if analysis is None and check_linear(candidate.problem, candidate.steps) is None:
            from app.symbolic_check import checked_symbolic

            async with self.checker_slots:
                analysis = await checked_symbolic(candidate.problem, candidate.steps)
        model_analysis = False
        if analysis is not None:
            candidate.source = "example" if reviewed else "ai"
        elif self.ai_active:
            if not self._reserve_ai(learner):
                return self._limit()
            try:
                analysis = await self.tutor.analyze(candidate.problem, candidate.steps)
            except TutorError as error:
                if previous is None:
                    self.save(learner, candidate)
                reply = self._provider_failure(
                    error, stage="correction" if correcting else "analysis"
                )
                if not error.output_limit and (
                    previous is None
                    or previous.pending_confirmation
                    or (same_problem and previous.steps == candidate.steps and not correcting)
                ):
                    reply.buttons = [
                        [
                            {
                                "type": "callback",
                                "text": "Повторить проверку",
                                "payload": "retry_analysis",
                            }
                        ]
                    ]
                return reply
            self.last_provider_error = False
            candidate.source = "ai"
            model_analysis = True
        else:
            return TutorReply(
                text="Проверка этой задачи сейчас недоступна. "
                "Попробуй позже или открой учебный пример /example."
            )
        if analysis.status == "error" and (
            analysis.first_error_step is None or analysis.first_error_step > len(candidate.steps)
        ):
            return TutorReply(
                text="Не удалось сопоставить проверку с присланными действиями. "
                "Пришли условие и запись решения ещё раз; текущая задача сохранена."
            )
        if candidate.source == "ai":
            error_step = (
                candidate.steps[analysis.first_error_step - 1] if analysis.first_error_step else ""
            )
            analysis.skill = canonical_skill(analysis.skill, error_step)
            if (
                analysis.status == "error"
                and model_analysis
                and self.ai_active
                and hasattr(self.tutor, "classify_skill")
                and self._reserve_ai(learner)
            ):
                analysis.skill = REVIEW
                try:
                    topic = await self.tutor.classify_skill(
                        candidate.problem,
                        candidate.steps,
                        analysis.first_error_step,
                        sorted(self._map_topics(learner)),
                    )
                    if valid_topic(topic):
                        topic = next(
                            (
                                known
                                for known in self._map_topics(learner)
                                if known.casefold() == topic.casefold()
                            ),
                            topic,
                        )
                        canonical = canonical_skill(topic, error_step)
                        analysis.skill = topic if canonical == REVIEW else canonical
                        self.store.register_topic(learner, analysis.skill)
                except TutorError:
                    logger.warning("Карта · Не удалось уточнить тему ошибки")
        if same_problem:
            candidate.mistake_keys = previous.mistake_keys[:]
            candidate.rematch_skill = previous.rematch_skill
            candidate.practice_skill = previous.practice_skill
        discovering = bool(correcting and previous.self_discovery)
        if discovering:
            advanced = bool(
                analysis.status == "error"
                and previous.analysis
                and analysis.first_error_step > previous.analysis.first_error_step
            )
            if analysis.status == "error" and not advanced:
                self._record_mistake(learner, candidate, analysis)
                previous.mistake_keys = candidate.mistake_keys[:]
                self.save(learner, previous)
                return TutorReply(
                    text="Исправление пока не подтверждено. Перепроверь правило "
                    "и выбранное действие, затем пришли исправление с объяснением.",
                    buttons=BUTTONS,
                )
            from app.quadratic_check import checked_vieta_explanation

            verified_reason = checked_vieta_explanation(
                candidate.problem,
                candidate.steps[(previous.analysis.first_error_step or 1) - 1],
                explanation,
            )
            if verified_reason is not True:
                from app.arithmetic_check import checked_order_explanation

                verified_reason = checked_order_explanation(
                    previous.steps,
                    previous.analysis.first_error_step or 1,
                    candidate.steps[(previous.analysis.first_error_step or 1) - 1],
                    explanation,
                )
            if not explanation.strip():
                previous.correction_draft = candidate.steps[:]
                self.save(learner, previous)
                return TutorReply(
                    text="Исправленная запись проверена. Добавь короткое пояснение, "
                    "почему это правило подходит. Переписывать вычисления не нужно.",
                    buttons=BUTTONS,
                )
            if self.ai_active and verified_reason is not True:
                if not self._reserve_ai(learner):
                    return self._limit()
                try:
                    assessment = await self.tutor.check_explanation(
                        previous.problem,
                        previous.steps,
                        candidate.steps[(previous.analysis.first_error_step or 1) - 1],
                        explanation,
                    )
                except TutorError as error:
                    previous.correction_draft = candidate.steps[:]
                    self.save(learner, previous)
                    return self._provider_failure(error, stage="explanation")
                self.last_provider_error = False
                if assessment.status != "accepted":
                    previous.correction_draft = candidate.steps[:]
                    self.save(learner, previous)

                    feedback = (
                        "Твоё объяснение пока не подтверждено. Своими словами укажи правило "
                        "и объясни, почему оно делает выбранное действие допустимым."
                        if assessment.status == "retry"
                        else "Оценка объяснения получилась неоднозначной. Уточни, какое "
                        "правило ты применил и почему его можно использовать в этом действии."
                    )
                    return TutorReply(
                        text="Исправленная строка проверена. " + feedback,
                        buttons=BUTTONS,
                    )
                if analysis.status == "uncertain":
                    analysis = analysis.model_copy(update={"status": "correct", "complete": False})
            elif verified_reason is not True:
                reason = explanation.casefold()
                if not (
                    re.search(r"кажд|об[ао]|дв[ау]", reason)
                    and re.search(r"слагаем|част|скоб", reason)
                    and re.search(r"множ|умнож|распредел", reason)
                ):
                    return TutorReply(
                        text="Объясни, к каким частям суммы относится множитель "
                        "перед скобками. Одного «так правильно» недостаточно."
                    )
        candidate.analysis = analysis
        candidate.awaiting_correction = analysis.status == "error"
        candidate.self_discovery = False
        candidate.pending_confirmation = False
        candidate.pending_id = ""
        candidate.pending_submission = None
        candidate.pending_caption = ""
        candidate.confirmed_photo = None
        candidate.correction_draft = []
        candidate.hint_level = 0
        skill = previous.analysis.skill if correcting and previous.analysis else analysis.skill
        if analysis.status == "error":
            candidate.rematch_skill = analysis.skill
            advanced_past_error = bool(
                correcting
                and previous.analysis
                and previous.analysis.first_error_step is not None
                and analysis.first_error_step is not None
                and analysis.first_error_step > previous.analysis.first_error_step
            )
            if advanced_past_error:
                self._record_progress(learner, skill, "corrections")
            self._record_mistake(learner, candidate, analysis)
            message = (
                "Первое исправление принято. В более позднем действии есть ошибка. "
                if advanced_past_error
                else "В решении есть ошибка. "
            ) + "Финальный ответ не раскрываю. Выбери, как помочь."
        elif analysis.status == "correct":
            if correcting:
                self._record_progress(learner, skill, "corrections")
            if analysis.complete and not (
                same_problem and previous.analysis and previous.analysis.complete
            ):
                finished_skill = next(
                    (
                        topic
                        for topic in (candidate.practice_skill, candidate.rematch_skill)
                        if topic and topic not in {"Другое", REVIEW}
                    ),
                    skill,
                )
                self._record_progress(learner, finished_skill, "solved")
            message = (
                "Решение верное, ошибок нет! Работа над этой задачей завершена. "
                "Отправь /reset для нового задания или выбери «Реванш» для тренировки."
                if analysis.complete
                else "Действия согласованы с условием. Продолжи решение самостоятельно."
            )
            if discovering:
                message += " Объяснение применённого правила принято."
        else:
            message = (
                "Недостаточно уверенности для вывода. Уточни условие и промежуточные действия."
            )
            if analysis.question and not contains_answer(analysis.question):
                message += " " + analysis.question
        self.save(learner, candidate)
        return TutorReply(
            text=message,
            buttons=COMPLETION_BUTTONS if analysis.complete else BUTTONS,
            accepted=analysis.status == "correct",
        )

    async def action(self, learner: str, action: str) -> TutorReply:
        async with self._lock(learner):
            return await self._action(learner, action)

    async def _photo_correction(
        self, learner: str, session: StudySession, caption: str
    ) -> TutorReply:

        lines = [line.strip() for line in caption.splitlines() if line.strip()]
        selected = (
            re.match(r"(?i)^(?:действие|шаг)\s+(\d+)\s*[:.)-]?\s*$", lines[0]) if lines else None
        )
        explanation = "\n".join(lines[1:]) if len(lines) > 1 else ""
        if not selected or not explanation:
            if caption.strip() and self.ai_active and hasattr(self.tutor, "interpret_correction"):
                return await self._free_correction(learner, session, caption)
            return TutorReply(
                text="Исправление со снимка сохранено. Расскажи своими словами, где была ошибка "
                "и почему ты меняешь это действие. "
                "Переписывать исправленную строку с фотографии не нужно."
            )
        index = int(selected[1])
        if not session.analysis or index != session.analysis.first_error_step:
            return TutorReply(
                text="Пересмотри выбранное действие: нужно первое действие с нарушением "
                "правила. Его номер пока не раскрываю.",
                buttons=BUTTONS,
            )
        photo = (
            without_repeated_condition(session.confirmed_photo) if session.confirmed_photo else None
        )
        if not photo or normalize(photo.problem) != normalize(session.problem):
            return TutorReply(
                text="На снимке не удалось связать исправление с текущим условием. "
                "Пришли текстом: Действие N: исправленная строка, затем объяснение."
            )
        if len(photo.steps) == 1:
            corrected = photo.steps[0]
        elif len(photo.steps) == len(session.steps) and 1 <= index <= len(photo.steps):
            corrected = photo.steps[index - 1]
        else:
            return TutorReply(
                text="На снимке несколько строк, и соответствие выбранному действию "
                "неоднозначно. Пришли текстом: Действие N: исправленная строка, "
                "затем объяснение. Не буду угадывать строку за тебя."
            )
        candidate = session.model_copy(deep=True)
        candidate.steps = session.steps[: index - 1] + [corrected]
        return await self._evaluate(learner, candidate, session, explanation=explanation)

    async def _action(self, learner: str, action: str) -> TutorReply:
        if "@" in action:
            action, nonce = action.split("@", 1)
            current = self.session(learner)
            if not current or not current.pending_confirmation or nonce != current.pending_id:
                return TutorReply(
                    text="Эта кнопка уже использована или относится к предыдущему снимку. "
                    "Текущая задача не изменилась."
                )
            if action == "confirm_submission":
                current.pending_id = secrets.token_hex(8)
                self.save(learner, current)
        if action == "retry_analysis":
            current = self.session(learner)
            if not current or not current.steps:
                return TutorReply(text="Сначала пришли условие и свои действия.")
            if current.pending_confirmation:
                return TutorReply(text="Сначала подтверди распознанный текст фотографии.")
            return await self._evaluate(learner, current.model_copy(deep=True), current)
        if action in {"reset", "forget"}:
            self.store.forget_learning(learner, progress=action == "forget")
            return TutorReply(
                text=(
                    "Учебные данные удалены." if action == "forget" else "Текущая задача сброшена."
                )
                + " Отправь /example или новую задачу."
            )
        if action == "map":
            rows = self._map_rows(learner)
            if not rows:
                return TutorReply(
                    text="Пока нет наблюдений по конкретным темам. "
                    "Пришли свою задачу или открой /example.",
                    buttons=self._topic_buttons(learner),
                )
            text = "Карта слабых мест — наблюдения по твоим попыткам:\n\n"
            text += "\n".join(
                f"• {s}: ошибок {m}, исправлений {c}, завершено {v}, подсказок {h}"
                for s, m, c, v, h in rows
            )
            dynamics = [
                row
                for row in self.store.learning_dynamics(learner)
                if row[0] in self._map_topics(learner)
            ]
            if dynamics:
                text += "\n\nДинамика за последние 7 дней / предыдущие 7 дней:\n"
                text += "\n".join(
                    f"• {s}: ошибки {m}/{pm}, исправления {c}/{pc}" for s, m, c, pm, pc in dynamics
                )
            priority = next((row[0] for row in rows if row[1] > row[2]), rows[0][0])
            text += (
                f"\n\nРекомендуем потренировать: {priority}. "
                "Можно выбрать эту или другую тему из своей карты."
            )
            return TutorReply(
                text=text + "\n\nЭто история практики, а не оценка знаний. "
                "Частота ошибок зависит и от количества попыток.",
                buttons=[
                    [{"type": "callback", "text": "Выбрать тему тренировки", "payload": "practice"}]
                ],
            )
        if action == "example":
            exercise = EXERCISES[0]
            analysis = reviewed_analysis(exercise["problem"], exercise["wrong"])
            session = StudySession(
                problem=exercise["problem"],
                steps=exercise["wrong"],
                analysis=analysis,
                awaiting_correction=True,
            )
            self._record_mistake(learner, session, analysis)
            self.save(learner, session)
            steps = "\n".join(f"{i}. {s}" for i, s in enumerate(session.steps, 1))
            return TutorReply(
                text=f"Учебный пример:\n{session.problem}\n{steps}"
                "\n\nВ решении есть ошибка. Финальный ответ не раскрываю. Выбери режим помощи.",
                buttons=BUTTONS,
            )
        session = self.session(learner)
        if action in {"confirm_submission", "reject_submission"}:
            if not session or not session.pending_confirmation or not session.pending_submission:
                return TutorReply(text="Сейчас нет фотографии, ожидающей подтверждения.")

            extracted = without_repeated_condition(session.pending_submission)
            caption = session.pending_caption
            if action == "reject_submission":
                if session.pending_preserves_session:
                    session.pending_confirmation = False
                    session.pending_id = ""
                    session.pending_submission = None
                    session.pending_preserves_session = True
                    session.pending_caption = ""
                    self.save(learner, session)
                else:
                    self.store.forget_learning(learner)
                return TutorReply(
                    text="Пришли правильную запись условия и решения в удобном виде. "
                    "Для другой задачи сначала /reset."
                )
            if (
                session.self_discovery
                and session.awaiting_correction
                and normalize(extracted.problem) == normalize(session.problem)
            ):
                session.pending_confirmation = False
                session.pending_submission = None
                session.pending_preserves_session = True
                session.pending_caption = ""
                session.confirmed_photo = extracted
                self.save(learner, session)
                return await self._photo_correction(learner, session, caption)
            candidate = StudySession(problem=extracted.problem, steps=extracted.steps, source="ai")
            result = await self._evaluate(learner, candidate, session)
            remaining = self.session(learner)
            if remaining and remaining.pending_confirmation:
                retry = (
                    [
                        [
                            {
                                "type": "callback",
                                "text": "Повторить проверку",
                                "payload": "confirm_submission@" + remaining.pending_id,
                            }
                        ],
                    ]
                    if result.buttons
                    else []
                )
                result.buttons = [
                    *retry,
                    [
                        {
                            "type": "callback",
                            "text": "Исправить текст",
                            "payload": "reject_submission@" + remaining.pending_id,
                        }
                    ],
                ]
            return result
        if session and session.pending_confirmation and not action.startswith("practice"):
            return TutorReply(
                text="Сначала подтверди текст фотографии или нажми «Исправить текст»."
            )
        if action == "practice" or re.fullmatch(r"practice_page_\d{1,6}", action):
            page = int(action.removeprefix("practice_page_")) if action != "practice" else 0
            choices = self._topic_buttons(learner, page)
            return TutorReply(
                text="Выбери тему из своей карты. Можно менять тему, не заканчивая задание."
                if choices
                else "Пока в карте нет тем. Пришли свою задачу или открой /example.",
                buttons=choices,
            )
        if action.startswith("practice_"):
            selected = action.removeprefix("practice_")
            skill = next(
                (row[0] for row in self._map_rows(learner) if topic_id(row[0]) == selected),
                None,
            )
            if skill is None:
                return TutorReply(
                    text="Список тем изменился. Выбери тему из текущей карты.",
                    buttons=self._topic_buttons(learner),
                )
            if self.ai_active:
                if not self._reserve_ai(learner):
                    return self._limit()

                try:
                    problem = await self._training_problem(
                        learner,
                        skill,
                        session.problem if session else "Тренировка навыка: " + skill,
                        allow_fallback=True,
                    )
                except TutorError as error:
                    return self._provider_failure(error)
                if problem is None:
                    return TutorReply(
                        text="Не удалось подготовить новую тренировку. Текущая задача сохранена.",
                        buttons=self._topic_buttons(learner),
                    )
                self.last_provider_error = False
                source = "ai"
            elif skill == SKILL:
                problem, source = EXERCISES[1]["problem"], "example"
            else:
                return TutorReply(
                    text="Для новой тренировки этого навыка нужен ИИ. /example доступен."
                )
            self.save(learner, StudySession(problem=problem, source=source, practice_skill=skill))
            return TutorReply(
                text=f"Тренировка навыка «{skill}»:\n{problem}\n\nПришли свои вычисления.",
                buttons=[
                    [{"type": "callback", "text": "Выбрать другую тему", "payload": "practice"}]
                ],
            )
        if not session or not session.analysis:
            return TutorReply(text="Сначала пришли решение или открой /example.")
        analysis = session.analysis
        if action == "rematch":
            if analysis.status != "correct" or not analysis.complete:
                return TutorReply(
                    text=(
                        "Ошибка уже исправлена, но решение ещё не завершено. "
                        "Пришли оставшиеся действия до результата — затем откроется реванш."
                        if analysis.status == "correct"
                        else "Сначала исправь ошибочное действие и заверши решение. "
                        "Для помощи выбери «Не говори ответ» или «Поймай свою ошибку»."
                        if analysis.status == "error"
                        else "Сначала нужно надёжно проверить и завершить текущую задачу. "
                        "Уточни условие и свои действия."
                    ),
                    buttons=BUTTONS,
                )
            rematch_skill = session.practice_skill or session.rematch_skill or analysis.skill
            exercise = (
                EXERCISES[1]
                if normalize(session.problem) == normalize(EXERCISES[0]["problem"])
                else EXERCISES[0]
            )
            if session.source == "example" and not self.ai_active:
                problem = exercise["problem"]
                if self.store.exercise_seen(learner, problem):
                    return TutorReply(text="Учебные примеры уже пройдены. Пришли свою задачу.")
            elif self._reserve_ai(learner):
                try:
                    problem = await self._training_problem(
                        learner, rematch_skill, session.problem, allow_fallback=True
                    )
                except TutorError as error:
                    return self._provider_failure(error)
            else:
                return self._limit()
            if (
                not isinstance(problem, str)
                or not 1 <= len(problem.strip()) <= 1500
                or normalize(problem) == normalize(session.problem)
                or (session.source == "ai" and contains_answer(problem))
            ):
                return TutorReply(text="Не удалось подготовить безопасную задачу для реванша.")
            self.save(
                learner,
                StudySession(
                    problem=problem,
                    source="ai" if self.ai_active else session.source,
                    practice_skill=rematch_skill,
                ),
            )
            return TutorReply(
                text=f"Реванш — задача на тот же навык:\n{problem}\n\n"
                "Пришли свои действия в решении. Готового ответа здесь нет."
            )
        if analysis.status == "correct" and analysis.complete:
            return TutorReply(
                text="Решение верное, ошибок нет. Отправь /reset для новой задачи "
                "или выбери «Реванш» для тренировки."
            )
        if analysis.status != "error":
            return TutorReply(
                text=(
                    "Присланные действия верны. Решение ещё не закончено — "
                    "пришли оставшиеся вычисления."
                    if analysis.status == "correct"
                    else "По этой записи пока нельзя надёжно проверить решение. "
                    "Уточни исходное условие и свои действия."
                )
            )
        if action == "first_error":
            content = (
                f"Ошибка в действии {analysis.first_error_step}:\n"
                f"«{session.steps[analysis.first_error_step - 1]}»"
            )
        elif action == "hints":
            return TutorReply(
                text="Выбери уровень помощи. Финальный ответ останется скрыт.", buttons=HINT_BUTTONS
            )
        elif action in {"hint1", "hint2", "hint3"}:
            level = int(action[-1])
            content = help_levels(session)[level - 1]
            content = f"{('Намёк', 'Подсказка', 'Подробный разбор')[level - 1]}:\n{content}"
            if level > session.hint_level:
                self._record_progress(learner, analysis.skill, "hints")
                session.hint_level = level
        elif action == "discovery":
            content = (
                "Найди первое ошибочное действие самостоятельно. Расскажи своими словами, "
                "где ошибка, как ты её исправляешь и почему. Можно одним сообщением, "
                "без специального формата. Номер действия и готовое исправление не раскрываю."
            )

            if session.source == "example":
                content += " Просмотри все действия и объясни, к чему относится множитель."
            session.self_discovery = True
        else:
            return TutorReply(text="Неизвестное действие. Отправь /help.")
        if session.source == "ai" and action != "first_error" and contains_answer(content):
            content = (
                "Проверь правило, использованное в этом действии, и попробуй исправить действие."
            )
        session.awaiting_correction = True
        self.save(learner, session)
        return TutorReply(
            text=content, buttons=HINT_BUTTONS if action.startswith("hint") else BUTTONS
        )

    async def _free_correction(self, learner: str, session: StudySession, text: str) -> TutorReply:

        if len(text) > 4000:
            return TutorReply(text="Сократи объяснение до 4000 символов и отправь снова.")

        from app.learning_models import CorrectionProposal

        parsed = parse_text(text, session.problem)
        if parsed and (
            any(len(step) > 1000 for step in parsed.steps)
            or len(parsed.steps) > 30
            or len(parsed.explanation) > 2000
        ):
            return TutorReply(
                text="Сократи запись: до 30 действий по 1000 символов и пояснение до 2000 символов."
            )
        if parsed and parsed.problem and normalize(parsed.problem) != normalize(session.problem):
            return TutorReply(
                text="Это другое условие. Для новой задачи отправь /reset; "
                "текущая задача сохранена."
            )
        verified = (session.analysis.first_error_step or 1) - 1 if session.analysis else 0
        index, submitted = (
            align_correction(text, session.steps, parsed.steps, verified) if parsed else (None, [])
        )
        if parsed and parsed.problem and normalize(parsed.problem) == normalize(session.problem):
            index = first_change(session.steps, parsed.steps)
            submitted = parsed.steps[index - 1 :] if index is not None else []
        if index is not None and not 1 <= index <= len(session.steps):
            return TutorReply(
                text="Такого действия нет в присланном решении. "
                "Опиши своими словами, какую запись ты исправляешь."
            )
        if parsed and index is not None:
            proposal = CorrectionProposal(
                selected_step=index,
                corrected_step=submitted[0],
                steps=submitted,
                explanation=parsed.explanation,
                clarification="",
            )
        else:
            for attempt in range(2):
                if not self._reserve_ai(learner):
                    return self._limit()
                try:
                    proposal = await self.tutor.interpret_correction(
                        session.problem, session.steps, text
                    )
                except TutorError as error:
                    if attempt or error.kind != "protocol" or error.status_code is not None:
                        return self._provider_failure(error, stage="correction")
                    logger.info("ИИ · Повтор разбора исправления после сбоя формата")
                else:
                    break
            records = proposal.steps or (
                [proposal.corrected_step] if proposal.corrected_step else []
            )
            index, records = align_correction(text, session.steps, records, verified)
            if index is not None:
                proposal.selected_step = index
                proposal.steps = records
                if records:
                    proposal.corrected_step = records[0]
        self.last_provider_error = False
        if proposal.selected_step is None:
            return TutorReply(text="Уточни своими словами, в каком действии ты нашёл ошибку.")
        if not session.analysis or proposal.selected_step != session.analysis.first_error_step:
            return TutorReply(
                text="Пересмотри действия: нужно найти первое нарушение правила. "
                "Объясни, какое действие ты меняешь.",
                buttons=BUTTONS,
            )
        corrected = proposal.corrected_step
        if not corrected and session.confirmed_photo:
            photo = without_repeated_condition(session.confirmed_photo)
            if len(photo.steps) == 1:
                corrected = photo.steps[0]
        if not corrected:
            return TutorReply(
                text="Добавь своё исправление и объясни, почему меняешь это действие. "
                "Можно написать обычным сообщением."
            )
        candidate = session.model_copy(deep=True)
        submitted = proposal.steps or [corrected]
        candidate.steps = session.steps[: proposal.selected_step - 1] + submitted
        return await self._evaluate(learner, candidate, session, explanation=proposal.explanation)

    async def _practice_valid(self, learner: str, skill: str, problem: str) -> bool:
        if not practice_shape_matches(skill, problem):
            logger.info("Тренировка · Вариант отклонён локальной проверкой темы")
            return False
        if self.ai_active and hasattr(self.tutor, "validate_practice"):
            if not self._reserve_ai(learner):
                logger.info("Тренировка · Достигнут лимит проверки новых вариантов")
                return False
            verdict = await self.tutor.validate_practice(skill, problem)
            if not verdict.matches_skill:
                logger.info("Тренировка · Вариант не требует нужного навыка")
            if not verdict.well_posed:
                logger.info("Тренировка · Условие неполное или неоднозначное")
            return verdict.matches_skill and verdict.well_posed
        return True

    def _topic_buttons(self, learner: str, page: int = 0) -> list[list[dict[str, str]]]:
        skills = [row[0] for row in self._map_rows(learner)]
        page = min(page, max(0, (len(skills) - 1) // 28))
        buttons = topic_buttons(skills[page * 28 : (page + 1) * 28])
        for target, label in ((page - 1, "Предыдущие темы"), (page + 1, "Следующие темы")):
            if 0 <= target <= (len(skills) - 1) // 28:
                buttons.append(
                    [
                        {
                            "type": "callback",
                            "text": label,
                            "payload": f"practice_page_{target}",
                        }
                    ]
                )
        return buttons

    async def _training_problem(
        self, learner: str, skill: str, previous: str, *, allow_fallback: bool = False
    ) -> str | None:
        try:
            problem = await self.tutor.generate_rematch(skill, previous)
            if not isinstance(problem, str) or not 1 <= len(problem.strip()) <= 1500:
                logger.info("Тренировка · Некорректная длина условия")
            elif contains_answer(problem):
                logger.info("Тренировка · В условии обнаружен ответ")
            elif normalize(problem) == normalize(previous):
                logger.info("Тренировка · Вариант повторяет прежнее условие")
            elif self.store.exercise_seen(learner, problem):
                logger.info("Тренировка · Вариант уже встречался этому ученику")
            elif await self._practice_valid(learner, skill, problem):
                self.store.remember_exercise(learner, problem)
                logger.info("Тренировка · Выдан проверенный вариант Яндекс AI")
                return problem
        except TutorError as error:
            if not allow_fallback or error.kind != "protocol":
                raise
            logger.info("Тренировка · Формат ответа ИИ не прошёл проверку")
        if allow_fallback:
            for number in range(2, 18):
                fallback = fallback_exercise(skill, previous, number=number)
                if fallback and self.store.remember_exercise(learner, fallback):
                    logger.info("Тренировка · Выдано резервное задание")
                    return fallback
        return None
