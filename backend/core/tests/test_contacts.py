import pytest

from core import contacts


@pytest.mark.parametrize("raw", [
    "8 701 000 00 01", "+77010000001", "https://wa.me/77010000001",
    "https://wa.me/77010000001?text=%D0%9F%D0%B8%D1%88%D1%83", "wa.me/77010000001",
    "WA.ME/+77010000001/", "http://api.whatsapp.com/send?phone=77010000001&text=hi",
    "https://api.whatsapp.com/send/?text=hi&phone=77010000001",
])
def test_a_phone_and_a_whatsapp_link_to_it_are_one_contact(raw):
    assert contacts.canonical(raw) == "+77010000001"


@pytest.mark.parametrize("raw,handle", [
    ("  Info@Kofe.KZ ", "info@kofe.kz"), ("Kofe_Almaty", "kofe_almaty"), ("abc", "abc"),
    # no number to take out of the link: an ordinary handle
    ("https://wa.me/Kofe", "https://wa.me/kofe"), ("https://wa.me/123", "https://wa.me/123"),
])
def test_other_handles_are_lower_cased_and_stripped(raw, handle):
    assert contacts.canonical(raw) == handle


@pytest.mark.parametrize("raw", ["", "   ", "ab", "8 701 000", "+770100"])
def test_an_empty_short_or_malformed_handle_is_rejected_with_a_code(raw):
    with pytest.raises(ValueError, match="errors.handleInvalid"):
        contacts.canonical(raw)
