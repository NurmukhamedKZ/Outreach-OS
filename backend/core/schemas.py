"""Base of every request and response schema."""

from datetime import UTC
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict

# Only with a timezone; kept in UTC, Asia/Almaty is for the window gate and display.
Timestamp = Annotated[AwareDatetime, AfterValidator(lambda moment: moment.astimezone(UTC))]


class Schema(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
