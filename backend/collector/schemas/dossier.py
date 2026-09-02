from typing import Literal

from pydantic import BaseModel, Field

from llm_schema import LLMSchema


class Hook(BaseModel):
    angle: str = Field(description="угол: «хвалят за скорость, но три жалобы на недозвон»")
    quote: str = Field(description="ДОСЛОВНАЯ цитата")
    url: str = Field(description="ссылка на источник")
    source: Literal["reviews", "site", "instagram"]
    observed_at: str = Field(description="свежая зацепка сильнее старой")


class Pain(BaseModel):
    statement: str = Field(description="формулировка боли")
    evidence: list[str] = Field(default_factory=list)
    severity: Literal["видно явно", "предполагается", "не видно"]


class Dossier(LLMSchema):
    summary: str
    hooks: list[Hook] = Field(default_factory=list)
    pains: list[Pain] = Field(default_factory=list, max_length=4)
    approach: str = Field(description="как заходить, включая как коснуться боли")
    decision_maker_hint: str | None = Field(None, description=(
        "имя и роль человека, принимающего решение, если они названы прямо в"
        " данных: страница «Команда», био инстаграма, подпись под ответом на"
        " отзыв. Формат «Айгуль, основатель». Не выдумывать и не выводить из"
        " названия компании — не названо, значит null"
    ))
    sources: list[str] = Field(default_factory=list)
    confidence: float = Field(description="уверенность, 0..1")
