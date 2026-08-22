from pydantic import BaseModel, Field

from llm_schema import LLMSchema


class Hiring(BaseModel):
    role: str = Field(description="кого ищут")
    quote: str = Field(description="ДОСЛОВНАЯ цитата со страницы вакансий")


class SiteAnalysis(LLMSchema):
    what_they_do: str = Field(description="чем занимаются, 3-6 слов")
    positioning: str | None = Field(None)
    target_clients: str | None = Field(None)
    proof_points: list[str] = Field(default_factory=list)
    pricing_visible: bool = Field(default=False, description="нет цен = продают через звонок")
    hiring: list[Hiring] = Field(default_factory=list)
    weak_spots: list[str] = Field(default_factory=list)
    last_updated_hint: str | None = Field(None, description="«© 2019» и подобное")
    tone: str = Field(description="тон сайта")
