from typing import Literal

from pydantic import BaseModel, Field

from llm_schema import LLMSchema


class Complaint(BaseModel):
    type: Literal["не дозвонились", "не ответили на заявку", "долго ждали ответа",
                  "сорвали срок", "качество работы", "цена", "другое"]
    quote: str = Field(description="ДОСЛОВНАЯ фраза из отзыва, не пересказ")
    date: str = Field(description="дата отзыва, как в сырье")


class ReviewsAnalysis(LLMSchema):
    complaints: list[Complaint] = Field(default_factory=list)
    praise_themes: list[str] = Field(default_factory=list)
    unanswered_complaints: int = Field(default=0)
    responsiveness: str | None = Field(None, description="сухо / шаблонно / живо / никак")
    service_language: list[str] = Field(default_factory=list)
