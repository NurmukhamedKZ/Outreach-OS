from pydantic import BaseModel, Field


class CompanyProfile(BaseModel):
    """Что модель извлекает о компании — и только извлекает.

    Решения принимают правила (§11), поэтому здесь нет ни веса, ни скоринга, ни
    вердикта «писать или нет». Схема строгая ровно затем, чтобы этого не
    появилось: добавить сюда поле «стоит ли писать» будет заметно.

    Почти все поля допускают null: у модели нет права выдумывать. Нет основания
    в данных — приходит null, и это честнее строки, которой неоткуда взяться.
    """

    industry: str | None = Field(None, description="чем компания занимается, 3-6 слов")
    size_hint: str | None = Field(None, description="оценка размера, если видна")
    has_sales_team: bool | None = Field(None, description="виден ли отдел продаж")
    why_now: str | None = Field(
        None,
        description="одно предложение: почему ей нужны клиенты сейчас, конкретно про неё",
    )
    quote: str | None = Field(
        None, description="дословная фраза С САЙТА, подтверждающая why_now"
    )
    confidence: float = Field(description="уверенность в why_now, от 0 до 1")
