from pydantic import Field

from core.schemas import Schema, Timestamp


class NewRefusal(Schema):
    """A refusal as the operator sends it."""

    # Both are checked by the service, not here: the facade takes them without
    # this schema and must answer with the same codes.
    handle: str = Field(max_length=200)
    reason: str = Field(max_length=500)


class Refusal(Schema):
    handle: str
    reason: str
    refused_at: Timestamp
