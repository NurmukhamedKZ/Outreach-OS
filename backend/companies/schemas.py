from enum import StrEnum
from typing import Annotated, NamedTuple

from pydantic import Field

from companies import domain
from core.schemas import Schema, Timestamp

# A few hundred segments are already a day of collection; the limits only stop junk.
Item = Annotated[str, Field(max_length=40)]


class Segment(NamedTuple):
    city: str
    rubric: str


class SegmentStatus(StrEnum):
    NEW = "new"
    COLLECTING = "collecting"
    COLLECTED = "collected"
    FAILED = "failed"


class SegmentSettings(Schema):
    """Where to look: segments are every city crossed with every rubric (ADR 0003)."""

    cities: list[Item] = Field(default_factory=lambda: list(domain.DEFAULT_CITIES),
                               max_length=20)
    rubrics: list[Item] = Field(default_factory=lambda: list(domain.DEFAULT_RUBRICS),
                                max_length=200)


class CatalogItem(Schema):
    id: str
    name: str


class SegmentState(Schema):
    city: str
    rubric: str
    status: SegmentStatus
    branches: int
    companies: int
    capped: bool
    error: str | None
    updated_at: Timestamp | None


class SegmentsView(Schema):
    """The segments section of the settings page: what is chosen, what can be, how it went."""

    settings: SegmentSettings
    cities: list[CatalogItem]
    rubrics: list[CatalogItem]
    segments: list[SegmentState]


class Contact(Schema):
    kind: str
    handle: str


class Branch(Schema):
    branch_id: str
    name: str
    address: str | None
    source_url: str
    rule: str
    confidence: float


class Company(Schema):
    company_id: str
    name: str
    city: str
    review_count: int
    contacts: list[Contact]
    branches: list[Branch]
