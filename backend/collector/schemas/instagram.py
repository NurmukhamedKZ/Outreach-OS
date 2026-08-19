from pydantic import BaseModel, Field


class Question(BaseModel):
    text: str = Field(description="вопрос клиента, дословно")
    quote: str = Field(description="ДОСЛОВНАЯ цитата вопроса из комментария")
    media_url: str = Field(description="ссылка на пост, где задан вопрос")


class Promo(BaseModel):
    text: str = Field(description="описание акции/скидки")
    quote: str = Field(description="ДОСЛОВНАЯ цитата из подписи")


class InstagramAnalysis(BaseModel):
    bio_summary: str | None = Field(None)
    content_themes: list[str] = Field(default_factory=list)
    selling_style: str | None = Field(None)
    unanswered_questions: list[Question] = Field(default_factory=list)
    promo_activity: list[Promo] = Field(default_factory=list)
    audience_reaction: str | None = Field(None)
