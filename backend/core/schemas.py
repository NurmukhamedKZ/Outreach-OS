"""Base of every request and response schema."""

from datetime import UTC
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict

# Only with a timezone; kept in UTC, Asia/Almaty is for the window gate and display.
Timestamp = Annotated[AwareDatetime, AfterValidator(lambda moment: moment.astimezone(UTC))]


class Schema(BaseModel):
    """Base of request and response schemas; strips string edges (Postel's law).

    Model reply schemas inherit llm_schema.LLMSchema instead: it maps null to
    the field default, which only a model needs.
    """

    model_config = ConfigDict(str_strip_whitespace=True)
