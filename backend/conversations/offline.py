"""The writer and the seller of a stand's offline mode (OFFLINE=1, #52): built from the prompt, no model.

The draft names the signal or hook the prompt carries; the seller's verdict is the
first word of VERDICTS in the lead's last reply. The price and the length still go
to a human by domain.needs_human, which this module does not repeat. Every text a
person reads opens with OFFLINE_MARK.
"""

import re
from collections.abc import Sequence

from pydantic import BaseModel

from conversations.schemas import Decision, Draft, SellerAnswer
from core.model import OFFLINE_MARK, Message

# One word a verdict, looked for in the lead's last reply, the first found wins. None is
# a delivery stop-word: a stop-word closes the thread before the seller is asked.
VERDICTS = {
    "отказываемся": Decision.REFUSAL,
    "созвонимся": Decision.INTERESTED,
    "ошиблись": Decision.WRONG_NUMBER,
    "абракадабра": Decision.JUNK,
}
# A question about the price is answered with one, and domain.needs_human hands it on.
PRICE_QUESTION = "сколько"
FACT_CHARS = 200
# A turn of the dialogue in domain.prompt, up to the next turn or the seller's task.
TURN = re.compile(
    r"^  (мы|они): (.*?)(?=\n  (?:мы|они): |\nОграничения этапа:|\Z)", re.MULTILINE | re.DOTALL
)


def answer(schema: type[BaseModel], messages: Sequence[Message]) -> Draft | SellerAnswer:
    """Return the seller's answer when `schema` is SellerAnswer, else the writer's draft."""
    prompt = messages[-1][1]
    return _seller(prompt) if schema is SellerAnswer else _draft(prompt)


def _draft(prompt: str) -> Draft:
    lines = prompt.splitlines()
    task = lines[-1]
    company = next(
        (line[len("Компания: ") :] for line in lines if line.startswith("Компания: ")), ""
    )
    signals = [
        (kind, quote.strip())
        for kind, _, quote in (line.strip().partition(":") for line in _block(lines, "Сигналы"))
    ]
    hooks = [line.rpartition(": «")[2].removesuffix("»") for line in _block(lines, "Зацепки")]
    if "stop=true" in task or not (signals or hooks):
        # The writer's own rule: no fact to name, no message.
        return Draft(text=f"{OFFLINE_MARK} писать не о чем", angle="other", stop=True)
    named = re.search(r"возьми угол (\S+)", task)
    angle = named[1] if named else signals[0][0] if signals else "other"
    fact = next((quote for kind, quote in signals if kind == angle and quote), None)
    fact = (fact or (hooks[0] if hooks else signals[0][1]))[:FACT_CHARS]
    return Draft(
        text=f"{OFFLINE_MARK} {company}, увидели у вас: «{fact}». Это для вас сейчас актуально?",
        angle=angle,
    )


def _seller(prompt: str) -> SellerAnswer:
    replies = [text for who, text in TURN.findall(prompt) if who == "они"]
    last = replies[-1].casefold() if replies else ""
    for word, decision in VERDICTS.items():
        if word in last:
            return SellerAnswer(decision=decision, reason=f"{OFFLINE_MARK} во входящем «{word}»")
    if PRICE_QUESTION in last:
        return SellerAnswer(
            decision=Decision.ANSWER,
            reason=f"{OFFLINE_MARK} спросили о цене",
            text=f"{OFFLINE_MARK} Стоит 150 000 тенге в месяц.",
        )
    return SellerAnswer(
        decision=Decision.ANSWER,
        reason=f"{OFFLINE_MARK} в ответе нет слова из таблицы",
        text=f"{OFFLINE_MARK} Спасибо за ответ! Расскажу подробнее — когда вам удобно?",
    )


def _block(lines: list[str], title: str) -> list[str]:
    """Return the indented lines under the prompt's line starting with `title`."""
    start = next((n + 1 for n, line in enumerate(lines) if line.startswith(title)), len(lines))
    block = []
    for line in lines[start:]:
        if not line.startswith("  "):
            break
        block.append(line)
    return block
