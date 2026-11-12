from enum import StrEnum
from typing import Literal

from pydantic import Field

from core.schemas import Schema, Timestamp
from llm_schema import LLMSchema


class SignalType(StrEnum):
    REVIEWS_MISSED_LEAD = "reviews_missed_lead"
    REVIEWS_UNANSWERED_COMPLAINT = "reviews_unanswered_complaint"
    SITE_HIRING_SALES = "site_hiring_sales"
    SITE_NO_PRICING = "site_no_pricing"
    CRM_WIDGET = "crm_widget"
    ADS_PLATFORM = "ads_platform"
    INBOUND_WIDGET = "inbound_widget"


class BatchStatus(StrEnum):
    COLLECTING = "collecting"
    RESEARCHING = "researching"
    DONE = "done"
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"


RUNNING = (BatchStatus.COLLECTING, BatchStatus.RESEARCHING)


class ResearchStatus(StrEnum):
    PENDING = "pending"
    RESEARCHING = "researching"
    DONE = "done"
    FAILED = "failed"


# --- operator settings (ADR 0003) -------------------------------------------


class SignalWeights(Schema):
    """Weight of each signal type; a hypothesis until the first replies."""

    # Leaking leads, said out loud by a customer.
    reviews_missed_lead: float = Field(5.0, ge=0, le=100)
    reviews_unanswered_complaint: float = Field(5.0, ge=0, le=100)
    # Marketing works but hits a ceiling.
    site_hiring_sales: float = Field(3.0, ge=0, le=100)
    site_no_pricing: float = Field(1.5, ge=0, le=100)
    # Infrastructure nearly everyone has, so it is worth almost nothing.
    crm_widget: float = Field(3.0, ge=0, le=100)
    ads_platform: float = Field(0.5, ge=0, le=100)
    inbound_widget: float = Field(0.5, ge=0, le=100)


class ResearchSettings(Schema):
    batch_size: int = Field(50, ge=1, le=200)
    model: str = Field("deepseek/deepseek-v4-flash", min_length=1, max_length=200)
    weights: SignalWeights = Field(default_factory=SignalWeights)
    # Thirty, not ninety: the score answers "do they need leads NOW".
    half_life_days: float = Field(30, gt=0, le=3650)


# --- the model's answer -------------------------------------------------------


class Complaint(LLMSchema):
    type: Literal["не дозвонились", "не ответили на заявку", "долго ждали ответа",
                  "сорвали срок", "качество работы", "цена", "другое"]
    quote: str = Field(description="ДОСЛОВНАЯ фраза из одного отзыва, не пересказ")


class Vacancy(LLMSchema):
    role: str = Field(description="кого ищут")
    quote: str = Field(description="ДОСЛОВНАЯ фраза со страницы сайта о вакансии")
    sales: bool = Field(description="роль в продажах или привлечении клиентов")


class Facts(LLMSchema):
    what_they_do: str = Field(description="чем занимаются, 3–6 слов")
    complaints: list[Complaint] = Field(default_factory=list)
    vacancies: list[Vacancy] = Field(default_factory=list)
    pricing_visible: bool = Field(False, description="на сайте выложены цены")
    decision_maker_hint: str | None = Field(None, description=(
        "имя и роль человека, принимающего решения, если они прямо названы в данных:"
        " страница «Команда», подпись под ответом на отзыв. Формат «Айгуль, основатель»."
        " Не названо — null"))


class HookDraft(LLMSchema):
    angle: str = Field(description="угол: «хвалят за скорость, но три жалобы на недозвон»")
    quote: str = Field(description="ДОСЛОВНАЯ цитата из отзыва или со страницы сайта")


class Pain(LLMSchema):
    statement: str = Field(description="формулировка боли")
    evidence: list[str] = Field(default_factory=list)
    severity: Literal["видно явно", "предполагается", "не видно"]


class DossierDraft(LLMSchema):
    summary: str = Field(description="кто они и чем живут, два-три предложения")
    hooks: list[HookDraft] = Field(default_factory=list, description="2–5 зацепок")
    pains: list[Pain] = Field(default_factory=list, description="до четырёх, от сильной к слабой")
    approach: str = Field(description="как заходить в первом сообщении, включая боль")


class ResearchAnswer(LLMSchema):
    facts: Facts
    dossier: DossierDraft


# --- what the API serves --------------------------------------------------------


class Contact(Schema):
    kind: str
    handle: str


class Hook(Schema):
    """A hook whose quote the code found; url, source and date are where it was found."""

    angle: str
    quote: str
    url: str
    source: Literal["reviews", "site"]
    observed_at: Timestamp


class Dossier(Schema):
    summary: str
    what_they_do: str
    hooks: list[Hook]
    pains: list[Pain]
    approach: str
    decision_maker_hint: str | None


class Contribution(Schema):
    signal: SignalType
    weight: float
    age_days: int
    contribution: float
    quote: str
    url: str
    observed_at: Timestamp


class Lead(Schema):
    company_id: str
    name: str
    city: str
    channel: Contact
    score: float
    why_now: Hook
    breakdown: list[Contribution]
    dossier: Dossier


class BatchCompany(Schema):
    company_id: str
    name: str
    status: ResearchStatus
    attempt: int | None
    error: str | None
    rejected_quotes: int
    started_at: Timestamp | None
    finished_at: Timestamp | None


class BatchView(Schema):
    batch_id: int
    status: BatchStatus
    size: int
    stop_requested: bool
    started_at: Timestamp
    finished_at: Timestamp | None
    counts: dict[ResearchStatus, int]
    companies: list[BatchCompany]


class Attempt(Schema):
    batch_id: int
    status: ResearchStatus
    attempt: int | None
    error: str | None
    rejected_quotes: int
    started_at: Timestamp | None
    finished_at: Timestamp | None


class CompanyResearch(Schema):
    """Everything research knows about one company: the lead card."""

    company_id: str
    name: str
    city: str
    contacts: list[Contact]
    channel: Contact | None
    refused: bool
    lead: bool
    score: float
    breakdown: list[Contribution]
    why_now: Hook | None
    dossier: Dossier | None
    model: str | None
    researched_at: Timestamp | None
    versions: int
    attempts: list[Attempt]
