"""Слоп не проходит схему, а не «не рекомендуется промптом».

Запрет живёт в валидаторе намеренно: with_structured_output повторит вызов
на невалидном ответе, а инструкция в промпте была бы просьбой, которую
модель нарушает ровно в тех случаях, ради которых написана система 2.
"""

from writer.schemas.outreach import BANNED, MAX_CHARS, Draft


def test_draft_passes():
    good = Draft(text="Здравствуйте! Увидел на сайте виджет Bitrix24...",
                 angle="crm_widget", stop=False)
    assert good.angle == "crm_widget"


def test_slop_is_rejected():
    for slop in ("Просто напоминаю о себе", "just checking in on this", "Поднимаю наверх"):
        try:
            Draft(text=slop, angle="followup", stop=False)
        except ValueError:
            continue
        raise AssertionError(f"пустой follow-up прошёл схему: {slop!r}")


def test_too_long_is_rejected():
    try:
        Draft(text="а" * (MAX_CHARS + 1), angle="crm_widget", stop=False)
    except ValueError:
        pass
    else:
        raise AssertionError("сообщение длиннее потолка прошло схему")


def test_stop_defaults_to_false():
    assert Draft(text="Здравствуйте!", angle="none").stop is False, "stop по умолчанию не False"
    assert len(BANNED) > 0 and MAX_CHARS > 0