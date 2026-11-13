"""The canonical phone form: the one copy for new code.

The old copies (collector/services/resolve.py::e164, sender/db/numbers.py::normalize)
stay until their systems are rewritten (#7, #13).
"""

import re

# ASCII digits and the punctuation people type in phones; a leading + only.
# [0-9], not \d: a full-width digit would make a handle that matches no real number.
PHONE = re.compile(r"\+?[0-9\s()\-‐-―]+")
# wa.me/<number> and api.whatsapp.com/send?phone=<number>; the number is international.
WHATSAPP_LINK = re.compile(
    r"(?:https?://)?(?:www\.)?(?:wa\.me/|api\.whatsapp\.com/send/?\?(?:[^#]*&)?phone=)"
    r"\+?([0-9]+)(?=$|[/?#&])",
    re.IGNORECASE,
)
KAZAKHSTAN_DIGITS = 11
MIN_DIGITS, MAX_DIGITS = 10, 15  # E.164 without the country-code-only short numbers


def canonical(raw: str) -> str:
    """Return the phone as + and digits; Kazakhstan numbers as +7XXXXXXXXXX.

    Without a plus the number is read as Kazakh: ten digits get the 7, a leading
    8 becomes 7. With a plus the digits are taken as dialled, as sender's
    numbers.normalize does: WhatsApp delivers foreign leads too, and every
    thread id it accepts must stay recordable.

    Raises:
        ValueError("errors.phoneInvalid"): not a phone shape, or a digit count
            no number has.
    """
    text = raw.strip()
    if not PHONE.fullmatch(text):
        raise ValueError("errors.phoneInvalid")
    digits = re.sub(r"[^0-9]", "", text)
    if not text.startswith("+"):
        if len(digits) == KAZAKHSTAN_DIGITS - 1:
            digits = "7" + digits
        elif len(digits) == KAZAKHSTAN_DIGITS and digits.startswith("8"):
            digits = "7" + digits[1:]
    if not MIN_DIGITS <= len(digits) <= MAX_DIGITS:
        raise ValueError("errors.phoneInvalid")
    return "+" + digits
