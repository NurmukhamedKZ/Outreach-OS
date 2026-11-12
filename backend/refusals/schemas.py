from pydantic import Field

from core.schemas import Schema, Timestamp


class NewRefusal(Schema):
    """reason is required: the list is never cleared, and half a year later
    there is nobody left to ask why a contact is on it.

    handle is checked by the service, not here: the facade takes the same
    handles without this schema and answers with the same code.
    """

    handle: str = Field(max_length=200)
    reason: str = Field(min_length=3, max_length=500)


class Refusal(Schema):
    handle: str
    reason: str
    refused_at: Timestamp
