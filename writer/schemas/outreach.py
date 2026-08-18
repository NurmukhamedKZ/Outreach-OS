"""Что модель обязана вернуть на каждый ход переписки.

Схема узкая по той же причине, что и CompanyProfile у системы 1: решение
«писать или не писать» принимает человек, модель только предлагает текст.
Поле stop — не решение, а сигнал оператору: «данных для нового повода нет».
"""

from pydantic import BaseModel, Field, field_validator

# Фразы, ради отсутствия которых и написана система 2. Follow-up без нового
# повода — это тот же шаблон, отправленный второй раз, и именно он превращает
# исходящую переписку в спам. Список пополняется по живым прогонам.
BANNED = (
    "checking in",
    "just bumping",
    "просто напоминаю",
    "напоминаю о себе",
    "поднимаю наверх",
    "хотел узнать, видели ли вы",
)

# Длиннее этого сообщение в WhatsApp не читают: оно приходит одним экраном.
MAX_CHARS = 700


class Draft(BaseModel):
    text: str = Field(description=(
        "сообщение в WhatsApp этому человеку: по-русски, на «вы», без приветственных"
        " шаблонов и без списков. Опирается на факт из данных о компании"
    ))
    angle: str = Field(description=(
        "чем цепляем — тип сигнала из данных (crm_widget, ads_platform, ig_promo)"
        " или 'answer', если это ответ на реплику лида"
    ))
    stop: bool = Field(False, description=(
        "true, если писать не о чем: нового повода нет или лид явно отказался"
    ))

    @field_validator("text")
    @classmethod
    def without_slop(cls, text):
        lowered = text.lower()
        used = [phrase for phrase in BANNED if phrase in lowered]
        if used:
            raise ValueError(
                f"пустое напоминание вместо нового повода: {used}. "
                "Каждое сообщение обязано нести факт о компании, которого не было раньше"
            )
        if len(text) > MAX_CHARS:
            raise ValueError(f"{len(text)} символов при потолке {MAX_CHARS}")
        return text
