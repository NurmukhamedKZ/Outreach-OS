import pytest

from core import phones


@pytest.mark.parametrize("raw", [
    "+77011234567", "+7 (701) 123-45-67", "8 701 123 45 67", "87011234567",
    "7011234567", "  77011234567  ", "8‒701‒123‒4567",
])
def test_kazakhstan_numbers_in_any_spelling_become_one_form(raw):
    assert phones.canonical(raw) == "+77011234567"


@pytest.mark.parametrize("raw,phone", [
    ("+1 555 123 4567", "+15551234567"), ("  +77011234567", "+77011234567"),
    ("+1234567890", "+1234567890"), ("+123456789012345", "+123456789012345"),
    ("97011234567", "+97011234567"),
    # sender's numbers.normalize accepts these as thread ids: a refusal on one must not fail
    ("+7701123456", "+7701123456"), ("+770112345678", "+770112345678"),
])
def test_a_number_with_a_plus_keeps_its_digits(raw, phone):
    """WhatsApp delivers foreign leads too; their thread is +<digits>."""
    assert phones.canonical(raw) == phone


@pytest.mark.parametrize("raw", ["", "  ", "12345", "+123456789", "8 701 123",
                                 "+1234567890123456", "701-ABC", "+-()",
                                 "\uff18\uff17\uff10\uff11\uff11\uff12\uff13\uff14\uff15\uff16\uff17"])
def test_not_a_phone_is_rejected_with_a_code(raw):
    with pytest.raises(ValueError, match="errors.phoneInvalid"):
        phones.canonical(raw)


@pytest.mark.parametrize("raw,phone", [
    ("8 701 123 45 67", True), ("+7(701)1234567", True), ("info@kofe.kz", False),
    ("kofe_almaty", False), ("12345", True),
])
def test_the_phone_shape_tells_a_phone_from_other_handles(raw, phone):
    assert bool(phones.PHONE.fullmatch(raw.strip())) is phone
