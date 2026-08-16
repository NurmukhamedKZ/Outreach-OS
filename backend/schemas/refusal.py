from pydantic import BaseModel, Field


class Refusal(BaseModel):
    """Отказ от касания. reason обязателен: список никогда не очищается, и через
    полгода «почему этот номер здесь» будет не у кого спросить."""

    handle: str = Field(min_length=3, max_length=200)
    reason: str = Field(min_length=3, max_length=500)
