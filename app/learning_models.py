from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.input_policy import readable_help

StepText = Annotated[str, Field(min_length=1, max_length=1000)]


class SubmissionExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    problem: str = Field(max_length=1500)
    steps: list[StepText] = Field(max_length=30)
    confidence: Literal["high", "low"]
    clarification: str = Field(max_length=600)

    @model_validator(mode="after")
    def bounded_submission(self) -> "SubmissionExtraction":
        if self.confidence == "high" and not self.problem.strip():
            raise ValueError("A clear submission must include a problem")
        if len(self.problem) + sum(map(len, self.steps)) > 12000:
            raise ValueError("Submission is too long")
        return self


class ExplanationAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["accepted", "retry", "uncertain"]
    feedback: str = Field(min_length=1, max_length=600)


class TutorAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["error", "correct", "uncertain"]
    first_error_step: int | None = Field(default=None, ge=1, le=30)
    skill: str = Field(min_length=1, max_length=80)
    question: str = Field(min_length=1, max_length=600)
    hint: str = Field(min_length=1, max_length=700)
    detail: str = Field(min_length=1, max_length=1000)
    discovery: str = Field(min_length=1, max_length=600)
    complete: bool = False

    @field_validator("question", "hint", "detail", "discovery")
    @classmethod
    def clean_help(cls, value, info):
        replacements = {
            "question": "Какое правило применено в этом действии?",
            "hint": "Проверь условия применения правила и знаки при преобразовании.",
            "detail": "Запиши правило отдельно. Сопоставь его с действием, затем "
            "проверь вычисления самостоятельно. Сохрани равенство обеих частей.",
            "discovery": "Выбери первое неверное действие, исправь и объясни его.",
        }
        return (
            value
            if readable_help(value, 250 if info.field_name == "question" else 700)
            else replacements[info.field_name]
        )

    @model_validator(mode="after")
    def consistent_result(self) -> "TutorAnalysis":
        if self.status == "error" and self.first_error_step is None:
            raise ValueError("An error must identify a step")
        if self.status != "error" and self.first_error_step is not None:
            raise ValueError("Only an error may identify an incorrect step")
        if self.status != "correct" and self.complete:
            raise ValueError("Only a correct solution may be complete")
        return self


class StudySession(BaseModel):
    problem: str = Field(min_length=1, max_length=1500)
    steps: list[StepText] = Field(default_factory=list, max_length=30)
    analysis: TutorAnalysis | None = None
    source: Literal["example", "ai"] = "example"
    awaiting_correction: bool = False
    hint_level: int = 0
    self_discovery: bool = False
    pending_id: str = ""
    pending_confirmation: bool = False
    practice_skill: str = ""
    rematch_skill: str = ""
    mistake_keys: list[str] = Field(default_factory=list, max_length=128)
    pending_submission: SubmissionExtraction | None = None
    pending_preserves_session: bool = True
    pending_caption: str = Field(default="", max_length=4000)
    confirmed_photo: SubmissionExtraction | None = None
    correction_draft: list[StepText] = Field(default_factory=list, max_length=30)


class TutorReply(BaseModel):
    text: str
    buttons: list[list[dict[str, str]]] = Field(default_factory=list)
    accepted: bool = False


class CorrectionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    selected_step: int | None = Field(ge=1, le=30)
    corrected_step: str = Field(max_length=1000)
    explanation: str = Field(max_length=2000)
    clarification: str = Field(max_length=600)
    steps: list[StepText] = Field(default_factory=list, max_length=30)


class PracticeAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    matches_skill: bool
    well_posed: bool
