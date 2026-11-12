"""The canonical form of a contact handle: phone, WhatsApp link, email, nickname.

One copy for new code and the refusals migration: a refusal is compared with a
company's contacts only in this form.
"""

from core import phones

MIN_LENGTH = 3


def canonical(raw: str) -> str:
    """Return the handle in canonical form.

    A phone and a WhatsApp link to it become one +digits form (core.phones): a
    stop word arrives as the phone, the company's channel as a wa.me link. A link
    with no number in it and everything else are stripped and lower-cased.

    Raises:
        ValueError("errors.handleInvalid"): empty, shorter than three characters
            or a malformed phone.
    """
    text = raw.strip()
    if link := phones.WHATSAPP_LINK.match(text):
        try:
            return phones.canonical("+" + link.group(1))
        except ValueError:
            pass
    elif phones.PHONE.fullmatch(text):
        try:
            return phones.canonical(text)
        except ValueError:
            raise ValueError("errors.handleInvalid") from None
    if len(text) < MIN_LENGTH:
        raise ValueError("errors.handleInvalid")
    return text.lower()
