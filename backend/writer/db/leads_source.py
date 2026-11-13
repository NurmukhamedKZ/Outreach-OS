"""Leads for writer: research decides who is a lead, writer decides what to write about.

Who is a lead (a verified hook, a WhatsApp channel, no refusal) lives in
research alone and is read through research.public. What stays here is the
choice of reasons to write: a signal of a pitchable type and an observation
fresh enough to name in a message.
"""

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

import paths
import sqlite_tools
from research import public as research


def connect() -> sqlite3.Connection:
    """Open the production database read-only, the sandbox's too.

    A sandbox run is a database of its own with no research in it: its lead is
    a real company, read from the production database, and nothing is written.
    """
    return sqlite_tools.connect_readonly(paths.PRODUCTION_STATE, None)


@dataclass(frozen=True)
class PitchRules:
    """What a message may hook on: three parts of one decision, so they travel together."""

    pitchable: frozenset[str]
    max_age_days: int
    today: date


def pitch_rules(config: dict, today: date) -> PitchRules:
    return PitchRules(pitchable=frozenset(config["signals"]["pitchable"]),
                      max_age_days=config["signals"]["max_age_days"], today=today)


def _fresh(observed_at: datetime, rules: PitchRules) -> bool:
    return (rules.today - observed_at.date()).days <= rules.max_age_days


def seed(lead: research.Lead | research.CompanyResearch, rules: PitchRules) -> dict:
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
            "hooks": [hook.model_dump(mode="json") for hook in dossier.hooks
                      if _fresh(hook.observed_at, rules)],
            "pains": [pain.model_dump(mode="json") for pain in dossier.pains],
            "approach": dossier.approach,
        },
        "signals": [{"type": part.signal.value, "quote": part.quote, "url": part.url,
                     "observed_at": part.observed_at.isoformat()}
                    for part in lead.breakdown
                    if part.signal in rules.pitchable and _fresh(part.observed_at, rules)],
    }


def candidates(db: sqlite3.Connection, rules: PitchRules, now: datetime,
               limit: int | None = None, skip: frozenset[str] = frozenset(),
               skip_companies: frozenset[str] = frozenset()) -> list[dict]:
    """Return new leads with something fresh to write about, the highest score first.

    skip holds thread ids, skip_companies the companies that already have a
    thread: a company keeps its thread when a new branch gives it another
    channel. Two companies may share a number; the first one takes it. All of it
    is applied before limit, so a taken top never hides a fresh lead further down.
    """
    found = []
    taken = set(skip)
    for lead in research.leads(db, now):
        if limit is not None and len(found) == limit:
            break
        if lead.channel.handle in taken or lead.company_id in skip_companies:
            continue
        lead_seed = seed(lead, rules)
        # Nothing fresh to name: the model would say stop, and the thread it
        # opened would keep the company out of every next run.
        if not lead_seed["dossier"]["hooks"] and not lead_seed["signals"]:
            continue
        taken.add(lead.channel.handle)
        found.append({"company_id": lead.company_id, "thread_id": lead.channel.handle,
                      "channel_kind": lead.channel.kind, "seed": lead_seed})
    return found


def cards(db: sqlite3.Connection, company_ids: list[str], now: datetime) -> dict[str, dict]:
    """Return the name, city and score of each company research knows, by its id.

    A thread outlives the lead it was opened for (a refusal, a new dossier), so
    any company is read, not just a lead; an id research does not know is left out.
    """
    # ponytail: one research.company per thread (a few queries each) — fine for
    # hundreds of threads; a batch read in research.public when the inbox grows.
    found = {}
    for company_id in dict.fromkeys(company_ids):
        try:
            card = research.company(db, company_id, now)
        except research.CompanyNotFoundError:
            continue
        found[company_id] = {"company_name": card.name, "city": card.city, "score": card.score}
    return found
