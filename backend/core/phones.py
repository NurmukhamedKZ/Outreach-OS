"""The canonical phone form: the one copy for new code.

The old copies (collector/services/resolve.py::e164, sender/db/numbers.py::normalize)
stay until their systems are rewritten (#7, #13).
"""

import re

# Digits and the punctuation people type in phones; a leading + only.
PHONE = re.compile(r"\+?[\d\s()\-‐-―]+")
KAZAKHSTAN_DIGITS = 11


def canonical(raw: str) -> str:
    """Return the phone as + and digits; Kazakhstan numbers as +7XXXXXXXXXX.

    Without a plus the number is read as Kazakh: ten digits get the 7, a leading
    8 becomes 7. With a plus the digits are taken as dialled; WhatsApp delivers
    foreign leads too.

    Raises:
        ValueError("errors.phoneInvalid"): not a phone shape, or a digit count
            no number has.
    """
    text = raw.strip()
    if not PHONE.fullmatch(text):
        raise ValueError("errors.phoneInvalid")
    digits = re.sub(r"\D", "", text)
    if not text.startswith("+"):
        if len(digits) == KAZAKHSTAN_DIGITS - 1:
            digits = "7" + digits
        elif len(digits) == KAZAKHSTAN_DIGITS and digits.startswith("8"):
            digits = "7" + digits[1:]
    valid = (len(digits) == KAZAKHSTAN_DIGITS if digits.startswith("7")
             else 10 <= len(digits) <= 15)
    if not valid:
        raise ValueError("errors.phoneInvalid")
    return "+" + digits
