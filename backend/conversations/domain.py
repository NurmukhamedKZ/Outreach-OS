"""Rules of a conversation without I/O: stages, offers, prompts, the seed, the seller's guards."""

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from conversations.schemas import (
    ConversationSettings,
    Decision,
    Role,
    Stage,
    ThreadStatus,
)
from core.model import Message
from research import public as research

# --- stages -------------------------------------------------------------------
# A stage is the thread's state, not a wish in the prompt: seeing "how much",
# the model happily jumps to closing and names a price the offer does not have.

STAGES: tuple[Stage, ...] = tuple(Stage)

RULES: dict[Stage, str] = {
    Stage.CONTACT: (
        "Этап — первый контакт. Задача одна: втянуть в тематический разговор.\n"
        "Структура сообщения: обращение по имени (нет имени — просьба передать"
        " тому, кто отвечает за продажи) -> наблюдение с дословной цитатой из"
        " данных -> чем это им обходится -> один короткий вопрос.\n"
        "Запрещено: продавать, называть услугу, звать на разговор, упоминать"
        " цену и сроки."
    ),
    Stage.PROBING: (
        "Этап — разговорить. Задача: понять, как они ищут клиентов сейчас.\n"
        "Задай один вопрос про их процесс, опираясь на то, что они уже"
        " ответили.\n"
        "Запрещено: делать предложение, называть цену."
    ),
    Stage.OFFER: (
        "Этап — предложение. Назови оффер своими словами и сними ровно одно"
        " возражение, которое собеседник высказал.\n"
        "Запрещено: назначать время, придумывать цифры, которых нет в оффере."
    ),
    Stage.CLOSING: (
        "Этап — договориться о разговоре. Предложи конкретное окно и спроси,"
        " удобно ли.\n"
        "Запрещено: новые аргументы и новые темы — решение уже принято."
    ),
}


def advance(stage: Stage) -> Stage:
    """Return the next stage; the last one stays.

    Each reply of the lead moves the talk one step: silence brings no deal
    closer, an answer does. The fact of an incoming message moves it, not the
    model's opinion of an unsent draft.
    """
    return STAGES[min(STAGES.index(stage) + 1, len(STAGES) - 1)]


# --- the cadence -------------------------------------------------------------------

EXHAUSTED_REASON = "касания кончились, лид не отвечал"


def next_touch_in(touch_no: int, follow_up_days: Sequence[int]) -> int | None:
    """Return in how many days the touch after `touch_no` is due; None when the cadence ended."""
    return follow_up_days[touch_no - 1] if touch_no <= len(follow_up_days) else None


def exhausted(
    touch_no: int, follow_up_days: Sequence[int], max_touches: int, has_replies: bool
) -> bool:
    """Return whether the last touch went without a reply: the automaton has nothing to say."""
    return touch_no > len(follow_up_days) and touch_no >= max_touches and not has_replies


# --- offers ---------------------------------------------------------------------


def offer_for(channel: str, settings: ConversationSettings) -> tuple[str | None, str]:
    """Return the id and text of the offer variant a thread argues; stable across processes.

    No variants: no id and the base offer. Hashed by the channel, not the
    company: threads opened before #36 were keyed by it, and a thread must not
    change its cohort. Not random and not hash(): PYTHONHASHSEED would
    reshuffle the cohorts on every restart.
    """
    if not settings.variants:
        return None, settings.offer
    digest = hashlib.sha1(channel.encode("utf-8")).hexdigest()
    variant = settings.variants[int(digest, 16) % len(settings.variants)]
    return variant.id, variant.text


# --- the seed -------------------------------------------------------------------


@dataclass(frozen=True)
class PitchRules:
    """What a message may hook on: three parts of one decision, so they travel together."""

    pitchable: frozenset[str]
    max_age_days: int
    today: date


def pitch_rules(settings: ConversationSettings, today: date) -> PitchRules:
    return PitchRules(frozenset(settings.pitchable), settings.max_age_days, today)


def _fresh(observed_at: datetime, rules: PitchRules) -> bool:
    return (rules.today - observed_at.date()).days <= rules.max_age_days


def seed(lead: research.Lead | research.CompanyResearch, rules: PitchRules) -> dict[str, Any]:
    """Return the thread's seed: the dossier, its fresh hooks and fresh pitchable signals.

    The seed freezes in threads.seed and feeds every later move of the thread,
    so an observation too old to name never gets into it.
    """
    dossier = lead.dossier
    return {
        "name": lead.name,
        "city": lead.city,
        "dossier": {
            "summary": dossier.summary,
            "decision_maker": dossier.decision_maker_hint,
            "hooks": [
                hook.model_dump(mode="json")
                for hook in dossier.hooks
                if _fresh(hook.observed_at, rules)
            ],
            "pains": [pain.model_dump(mode="json") for pain in dossier.pains],
            "approach": dossier.approach,
        }
        if dossier
        else {},
        "signals": [
            {
                "type": part.signal.value,
                "quote": part.quote,
                "url": part.url,
                "observed_at": part.observed_at.isoformat(),
            }
            for part in lead.breakdown
            if part.signal in rules.pitchable and _fresh(part.observed_at, rules)
        ],
    }


def has_something_to_say(seed: dict[str, Any]) -> bool:
    """Return whether the seed names anything fresh: without it the model would say stop."""
    return bool(seed["dossier"].get("hooks") or seed["signals"])


# --- prompts ---------------------------------------------------------------------

ANSWER_ANGLE = "answer"
OTHER_ANGLE = "other"

WRITER_SYSTEM = """# Роль
Ты пишешь исходящие сообщения в WhatsApp от лица команды, которая предлагает:
{offer}

# Инструкции
- Одно сообщение — один конкретный факт об этой компании, взятый из данных ниже.
  Без факта сообщение не отправляется: пиши stop=true.
- Пиши так, как пишет человек в мессенджере: на «вы», без списков, без
  «Надеюсь, у вас всё хорошо», без слова «уникальный».
- Не выдумывай фактов о компании. Всё, чего нет в данных, не существует.
- Цены, сроки и любые цифры бери только из оффера выше. Их там нет — значит их
  нет и в сообщении: на вопрос о цене отвечай, что назовём после короткого
  разговора. Выдуманная цифра — это обещание, которое даёт живому человеку
  компания, а не модель.
- Каждое следующее сообщение несёт новый повод. Напоминание о предыдущем
  письме поводом не является.

# Ограничения этапа
{stage_rules}

# Формат
- 3-5 коротких предложений, заканчивай одним вопросом, на который легко
  ответить «да» или «нет».
- angle — машинный тип повода из данных, а не описание сообщения фразой."""

SELLER_SYSTEM = """Ты ведёшь переписку в WhatsApp от лица команды, которая предлагает:
{offer}

Тебе пишет живой человек, уже получивший наше первое сообщение. Ответь ему сам
(decision=answer и текст ответа) ИЛИ передай тред дальше другим решением. Третьего не дано.

Правила, которые не обсуждаются:
- Цены, сроки и любые цифры бери только из оффера выше. Их там нет — значит их
  нет и в ответе: скажи, что назовём после короткого разговора. Выдуманная
  цифра — это обещание, которое даёт живому человеку компания, а не модель.
- Не выдумывай фактов о компании собеседника. Всё, чего нет в данных, не существует.
- Пиши как человек в мессенджере: 2-4 коротких предложения, на «вы», без
  списков и без «Надеюсь, у вас всё хорошо».
- Не отвечай сам, когда отвечать больше нечего: собеседник заинтересован и
  разговор пора отдать человеку (interested), отказался (refusal), это не тот
  человек (wrong_number) или пишет бессмыслицу (junk).
- Если сомневаешься между ответом и interested — выбирай interested.
  Живой разговор о деньгах ведёт человек."""

FIRST_TASK = (
    "Задача: первое сообщение этой компании. Возьми самый сильный сигнал, назови"
    " конкретный факт о ней и спроси, актуально ли это сейчас."
)


def writer_system(offer: str, stage: Stage) -> str:
    return WRITER_SYSTEM.format(offer=offer, stage_rules=RULES[stage])


def normalize_angle(angle: str, pitchable: frozenset[str]) -> str:
    """Collapse an angle that is no signal type into one "other".

    Dropping a ready draft over a bad word is pointless, but a free phrase in
    the by_angle breakdown would fill it with wordings of one reason.
    """
    return angle if angle in pitchable or angle == ANSWER_ANGLE else OTHER_ANGLE


def unused_angles(seed: dict[str, Any], used: Iterable[str]) -> list[str]:
    taken = set(used)
    return [signal["type"] for signal in seed["signals"] if signal["type"] not in taken]


def followup_task(days: int, unused: Sequence[str]) -> str:
    """Return the follow-up task; without a new angle the honest answer is stop."""
    if not unused:
        return (
            f"Задача: лид молчит {days} дней, и неиспользованных поводов больше нет."
            " Верни stop=true и пустое по смыслу сообщение — писать не о чем."
        )
    return (
        f"Задача: лид молчит {days} дней. Напиши сообщение с НОВЫМ поводом —"
        f" возьми угол {unused[0]} из сигналов выше. Про предыдущее сообщение не"
        " упоминай вообще."
    )


def seller_task(stage: Stage) -> str:
    # The stage rules go into the task, not the system prompt: the stage changes
    # from move to move.
    return (
        f"Ограничения этапа:\n{RULES[stage]}\n\n"
        "Задача: ответить на последнюю реплику собеседника — или передать тред дальше"
        " другим решением."
    )


def prompt(seed: dict[str, Any], history: Sequence[dict[str, Any]], task: str) -> str:
    """Return the human part of a move: the lead's card, the dialogue so far and the task."""
    dossier = seed.get("dossier") or {}
    parts = [f"Компания: {seed.get('name', '')}", f"Город: {seed.get('city', '')}"]
    if dossier.get("summary"):
        parts.append(f"Чем занимается: {dossier['summary']}")
    if dossier.get("decision_maker"):
        parts.append(f"Кто решает: {dossier['decision_maker']}")
    if dossier.get("approach"):
        parts.append(f"Как заходить: {dossier['approach']}")
    if dossier.get("hooks"):
        parts.append("Зацепки:")
        parts += [f"  [{h['source']}] {h['angle']}: «{h['quote']}»" for h in dossier["hooks"]]
    if seed.get("signals"):
        parts.append("Сигналы (возможные поводы):")
        parts += [f"  {s['type']}: {s['quote'] or ''}".rstrip() for s in seed["signals"]]
    if history:
        parts.append("Переписка:")
        parts += [
            f"  {'мы' if m['role'] == Role.OUTGOING else 'они'}: {m['text']}" for m in history
        ]
    parts.append(task)
    return "\n".join(parts)


def writer_messages(
    seed: dict[str, Any], history: Sequence[dict[str, Any]], task: str, offer: str, stage: Stage
) -> list[Message]:
    return [("system", writer_system(offer, stage)), ("human", prompt(seed, history, task))]


def seller_messages(
    seed: dict[str, Any], history: Sequence[dict[str, Any]], offer: str, stage: Stage
) -> list[Message]:
    return [
        ("system", SELLER_SYSTEM.format(offer=offer)),
        ("human", prompt(seed, history, seller_task(stage))),
    ]


# --- the seller's guards -----------------------------------------------------------
# All of them are `if`s, none a prompt: a prompt is a request the model breaks
# exactly where it matters.

# A digit next to a currency. The dearest mistake is an invented price: a
# promise the company gives a living person, not the model.
PRICE = re.compile(r"\d[\d\s.,]*\s*(₸|тг\b|тенге|руб|₽|\$|usd|kzt|eur|€)", re.IGNORECASE)

# Where a verdict moves the thread; all four end the automaton's part.
VERDICT_STATUS: dict[Decision, ThreadStatus] = {
    Decision.INTERESTED: ThreadStatus.ESCALATED,
    Decision.REFUSAL: ThreadStatus.CLOSED_REFUSED,
    Decision.WRONG_NUMBER: ThreadStatus.CLOSED_JUNK,
    Decision.JUNK: ThreadStatus.CLOSED_JUNK,
}

# States the automaton does not write from. escalated is a dead end: only a
# human leaves it.
AUTOMATON_STOPS: tuple[ThreadStatus, ...] = (
    ThreadStatus.ESCALATED,
    ThreadStatus.UNREACHABLE,
    ThreadStatus.EXHAUSTED,
    ThreadStatus.CLOSED_REFUSED,
    ThreadStatus.CLOSED_JUNK,
)


def needs_human(text: str, max_chars: int) -> bool:
    """Return whether an answer must go to a human instead: too long, or it names a price."""
    return len(text) > max_chars or PRICE.search(text) is not None
