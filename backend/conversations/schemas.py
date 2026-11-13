from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from core.schemas import Schema, Timestamp
from llm_schema import LLMSchema
from research.public import SignalType


class Role(StrEnum):
    OUTGOING = "outgoing"
    INCOMING = "incoming"


class ThreadStatus(StrEnum):
    """Who leads the chat; the automaton writes only from queued and active."""

    QUEUED = "queued"
    ACTIVE = "active"
    EXHAUSTED = "exhausted"
    ESCALATED = "escalated"
    UNREACHABLE = "unreachable"
    CLOSED_REFUSED = "closed_refused"
    CLOSED_JUNK = "closed_junk"
    BLOCKED_CHANNEL = "blocked_channel"


class Stage(StrEnum):
    """The four stages of a cold dialogue, in order."""

    CONTACT = "contact"
    PROBING = "probing"
    OFFER = "offer"
    CLOSING = "closing"


class Outcome(StrEnum):
    MEETING_AGREED = "meeting_agreed"
    MEETING_HELD = "meeting_held"
    REFUSED = "refused"
    LOST = "lost"


class MoveKind(StrEnum):
    FIRST = "first"
    REPLY = "reply"
    FOLLOWUP = "followup"


class RunStatus(StrEnum):
    RUNNING = "running"
    DONE = "done"
    STOPPED = "stopped"
    FAILED = "failed"


class Decision(StrEnum):
    """What the seller does with the lead's reply: answer it, or hand the thread on."""

    ANSWER = "answer"
    INTERESTED = "interested"
    REFUSAL = "refusal"
    WRONG_NUMBER = "wrong_number"
    JUNK = "junk"


# --- operator settings (ADR 0003) -------------------------------------------

DEFAULT_OFFER = (
    "Мы строим систему исходящего лидогена под ключ: находим компании, которым услуга нужна"
    " прямо сейчас (по вакансиям, рекламе, сайту и соцсетям), пишем каждому персонально по его"
    " же ситуации и доводим до встречи. Клиент даёт профиль своего покупателя и получает"
    " встречи в календаре, а не список контактов. Работаем по Казахстану, первые результаты —"
    " за 2-3 недели."
)


class OfferVariant(Schema):
    """One wording of the offer, tested apart from the message and the ICP."""

    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    text: str = Field(min_length=1, max_length=2000)


def _default_variants() -> list[OfferVariant]:
    return [
        OfferVariant(
            id="free_first_meetings",
            text=(
                "Находим компании, которым ваша услуга нужна прямо сейчас, пишем каждой лично и"
                " доводим до встречи. Первые две встречи — за наш счёт: платите только за те, что"
                " состоялись после них. Списков контактов не продаём, работаем по Казахстану."
            ),
        ),
        OfferVariant(
            id="pay_per_meeting",
            text=(
                "Находим компании, которым ваша услуга нужна прямо сейчас, пишем каждой лично и"
                " доводим до встречи. Оплата только за состоявшуюся встречу — не за письма, не за"
                " базу, не помесячно. Работаем по Казахстану."
            ),
        ),
    ]


class ConversationSettings(Schema):
    model: str = Field("deepseek/deepseek-v4-flash", min_length=1, max_length=200)
    # How many new leads one «Черновики топ-N» run writes to.
    top_n: int = Field(10, ge=1, le=100)
    # Signals worth naming in a message; the rest only weigh in research's score:
    # "a CRM widget on your site" is not a pain.
    pitchable: list[SignalType] = Field(
        default_factory=lambda: [
            SignalType.REVIEWS_UNANSWERED_COMPLAINT,
            SignalType.REVIEWS_MISSED_LEAD,
            SignalType.SITE_HIRING_SALES,
            SignalType.SITE_NO_PRICING,
        ]
    )
    # A cut-off, not a decay: "I saw you hire a salesperson" about a vacancy three
    # months old kills trust in one sentence.
    max_age_days: int = Field(60, ge=1, le=3650)
    # The model leans on it and invents nothing beyond: a product change needs no prompt edit.
    offer: str = Field(DEFAULT_OFFER, min_length=1, max_length=2000)
    # A thread argues one variant for good; none at all falls back to `offer`.
    variants: list[OfferVariant] = Field(default_factory=_default_variants, max_length=10)

    @field_validator("pitchable")
    @classmethod
    def _once_each(cls, pitchable: list[SignalType]) -> list[SignalType]:
        return list(dict.fromkeys(pitchable))

    @model_validator(mode="after")
    def _variant_ids_unique(self) -> Self:
        ids = [variant.id for variant in self.variants]
        if len(ids) != len(set(ids)):
            raise ValueError("errors.variantIdTaken")
        return self


# --- what the model must answer -------------------------------------------------

# The phrases system 2 exists to avoid: a follow-up without a new reason is the
# same template sent twice, and that is what turns outreach into spam.
BANNED = (
    "checking in",
    "just bumping",
    "просто напоминаю",
    "напоминаю о себе",
    "поднимаю наверх",
    "хотел узнать, видели ли вы",
)
# Longer than this a WhatsApp message is not read: it comes as one screen.
MAX_CHARS = 700


class Draft(LLMSchema):
    """One move of the writer. Whether to write at all is the operator's call."""

    text: str = Field(
        description=(
            "сообщение в WhatsApp этому человеку: по-русски, на «вы», без приветственных"
            " шаблонов и без списков. Опирается на факт из данных о компании"
        )
    )
    angle: str = Field(
        description=(
            "чем цепляем: ровно один тип сигнала из блока «Сигналы» выше или"
            " 'answer', если это ответ на реплику лида. Машинный тип, не описание"
            " сообщения фразой"
        )
    )
    stop: bool = Field(
        False, description="true, если писать не о чем: нового повода нет или лид явно отказался"
    )

    @field_validator("text")
    @classmethod
    def _without_slop(cls, text: str) -> str:
        # In the schema, not the prompt: structured output retries an invalid
        # answer, a prompt rule is a request the model breaks where it matters.
        lowered = text.lower()
        used = [phrase for phrase in BANNED if phrase in lowered]
        if used:
            raise ValueError(
                f"пустое напоминание вместо нового повода: {used}. "
                "Каждое сообщение обязано нести факт о компании, которого не было раньше"
            )
        if len(text) > MAX_CHARS:
            raise ValueError(f"{len(text)} символов при потолке {MAX_CHARS}")
        return text


class SellerAnswer(LLMSchema):
    """The seller's move on a lead's reply: an answer, or a verdict that hands the thread on."""

    decision: Decision = Field(
        description=(
            "answer — ответить самому; interested — заинтересован, разговор пора отдать"
            " человеку; refusal — отказался, писать больше нельзя; wrong_number — не тот"
            " человек; junk — бессмыслица, спам, автоответ"
        )
    )
    reason: str = Field(description="одна фраза: почему именно это решение")
    text: str = Field(
        "", description="ответ собеседнику, только при decision=answer; иначе пустая строка"
    )

    @model_validator(mode="after")
    def _an_answer_has_text(self) -> Self:
        if self.decision == Decision.ANSWER and not self.text.strip():
            raise ValueError("decision=answer без текста ответа")
        return self


# --- requests -------------------------------------------------------------------


class MoveRequest(Schema):
    kind: MoveKind


class IncomingRequest(Schema):
    text: str = Field(min_length=1, max_length=4000)


class OutcomeRequest(Schema):
    outcome: Outcome


# --- what the API serves ----------------------------------------------------------


class HistoryMessage(Schema):
    """What happened: sent by us or received from the lead. Drafts never appear here."""

    message_id: int
    role: Role
    text: str
    angle: str | None
    sent_at: Timestamp | None
    # The kind of touch from the send queue: cold, followup or reply; empty for incoming.
    kind: str | None


class PendingDraft(Schema):
    message_id: int
    draft_text: str
    angle: str | None
    created_at: Timestamp


class ThreadView(Schema):
    """A company's thread, or where its first draft would open one."""

    company_id: str
    channel: str
    # The automaton's state; None until the first draft opens the thread.
    status: ThreadStatus | None
    status_reason: str | None
    our_number: str | None
    messages: list[HistoryMessage]
    draft: PendingDraft | None
    # The model advises not to write: nothing new to say. The operator decides.
    stop: bool = False


class InboxRow(Schema):
    company_id: str
    company_name: str
    channel: str
    created_at: Timestamp
    status: ThreadStatus
    status_reason: str | None
    our_number: str | None
    sent: int
    replies: int
    drafts: int
    last_at: Timestamp | None
    last_message: str | None


class Inbox(Schema):
    threads: list[InboxRow]


class ColdDraft(Schema):
    """A first touch waiting for review, with what research knows of the company."""

    company_id: str
    company_name: str
    city: str | None
    score: float
    channel: str
    message_id: int
    draft_text: str
    angle: str | None
    model: str | None
    has_prompt: bool


class ColdDrafts(Schema):
    drafts: list[ColdDraft]


class PromptView(Schema):
    """The full request that went to the model; None for drafts older than the record."""

    prompt: list[tuple[Literal["system", "human", "ai"], str]] | None
    model: str | None


class OutcomeView(Schema):
    outcome: Outcome


class OutreachRun(Schema):
    run_id: int
    status: RunStatus
    size: int
    taken: int
    drafted: int
    skipped: int
    stop_requested: bool
    error: str | None
    started_at: Timestamp
    finished_at: Timestamp | None
