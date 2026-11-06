"""Контракты учебного сервиса."""

from typing import Any, Protocol

from app.learning_models import (
    CorrectionProposal,
    ExplanationAssessment,
    PracticeAssessment,
    SubmissionExtraction,
    TutorAnalysis,
)


class TutorPort(Protocol):
    async def analyze(self, problem: str, steps: list[str]) -> TutorAnalysis: ...

    async def generate_rematch(self, skill: str, previous_problem: str) -> str: ...

    async def validate_practice(self, skill: str, problem: str) -> PracticeAssessment: ...

    async def extract_submission(
        self, text: str, image: bytes | None = None, mime_type: str | None = None
    ) -> SubmissionExtraction: ...

    async def check_explanation(
        self, problem: str, steps: list[str], corrected_step: str, explanation: str
    ) -> ExplanationAssessment: ...

    async def interpret_correction(
        self, problem: str, steps: list[str], text: str
    ) -> CorrectionProposal: ...

    async def classify_skill(
        self, problem: str, steps: list[str], index: int, known_topics: list[str]
    ) -> str: ...


class TutorError(Exception):
    def __init__(
        self,
        message: str,
        *,
        kind: str = "protocol",
        status_code: int | None = None,
        output_limit: bool = False,
    ) -> None:
        super().__init__(message)
        self.kind, self.status_code = kind, status_code
        self.output_limit = output_limit


class LearningStore(Protocol):
    def learning_topics(self, learner: str) -> list[str]: ...
    def register_topic(self, learner: str, skill: str) -> None: ...
    def remember_exercise(self, learner: str, problem: str) -> bool: ...
    def exercise_seen(self, learner: str, problem: str) -> bool: ...
    def learning_session(self, learner: str) -> dict[str, Any] | None: ...
    def save_learning_session(self, learner: str, state: dict[str, Any]) -> None: ...
    def forget_learning(self, learner: str, *, progress: bool = False) -> None: ...
    def record_progress(self, learner: str, skill: str, metric: str) -> None: ...
    def learning_progress(self, learner: str) -> list[tuple]: ...
    def learning_dynamics(self, learner: str) -> list[tuple]: ...
    def reserve_ai_call(
        self, daily_limit: int, learner: str | None = None, learner_limit: int = 20
    ) -> bool: ...
