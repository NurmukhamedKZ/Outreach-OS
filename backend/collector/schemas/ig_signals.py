from typing import Literal

from pydantic import BaseModel, Field

# Словарь сигналов ленты. Enum, а не свободная строка: модель не должна изобретать
# свои типы — вес каждого типа лежит в config.toml, и типу, которого там нет,
# неоткуда взять цену.
IG_SIGNAL_TYPES = ("direct_selling", "promo", "hiring_sales")


class IgSignal(BaseModel):
    """Одна находка в подписи к посту.

    Ни даты, ни ссылки, ни веса здесь нет намеренно: observed_at берётся из
    taken_at поста, url собирается из его shortcode, weight — из config.toml.
    Модель говорит только «вот в этом посте вот это», цену назначают правила (§11).
    """

    post_index: int = Field(description="номер поста из списка, начиная с 1")
    type: Literal[IG_SIGNAL_TYPES] = Field(
        description=(
            "direct_selling — призыв написать/позвонить/оставить контакт;"
            " promo — акция, скидка, ограниченное предложение;"
            " hiring_sales — ищут менеджера по продажам или коммерческого директора"
        )
    )
    quote: str = Field(
        description="ДОСЛОВНАЯ фраза из подписи именно этого поста, не пересказ"
    )


class IgSignals(BaseModel):
    """Находки по всей ленте аккаунта. Пустой список — законный ответ.

    Отсутствие сигнала не значит отрицательный вес: аккаунт, который просто
    рассказывает о работе, — это ноль, а не минус.
    """

    signals: list[IgSignal] = Field(default_factory=list)
