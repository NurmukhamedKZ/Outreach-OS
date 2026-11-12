from contextlib import closing

import pytest

import paths
from core import db as core_db


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A migrated database in a temp file, also served to request dependencies."""
    path = tmp_path / "state.db"
    monkeypatch.setattr(paths, "PRODUCTION_STATE", path)
    core_db.migrate(path)
    return path


@pytest.fixture
def db(state):
    """The connection; the operator has chosen one segment, the one add_company fills."""
    from companies.schemas import SegmentSettings
    from core import operator_settings
    with closing(core_db.connect(state)) as connection:
        with connection:
            operator_settings.save(connection, "companies",
                                   SegmentSettings(cities=["almaty"], rubrics=["653"]))
        yield connection


from datetime import UTC, datetime
from threading import Lock
from types import SimpleNamespace

from companies.domain import ListedBranch, Placement
from companies.repository import CompanyRepository
from companies.schemas import Segment
from core.pages import FakePages
from research import domain

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
SEGMENT = Segment("almaty", "653")


def add_company(db, company_id: str, contacts: list[tuple[str, str]], reviews: int = 0,
                branches: int = 1) -> None:
    """A collected company of a default segment; branch ids are <company_id>-<n>."""
    repo = CompanyRepository(db)
    repo.claim(SEGMENT, NOW)
    repo.add_company(company_id, f"Компания {company_id}", "almaty", NOW)
    for n in range(branches):
        branch_id = f"{company_id}-{n}"
        repo.add_branch(ListedBranch(branch_id, None, "Филиал", None, f"Абая, {n + 1}", reviews),
                        "almaty", f"https://2gis.kz/almaty/firm/{branch_id}",
                        Placement(company_id, "self" if n == 0 else "org_id", 1.0))
        repo.replace_contacts(branch_id, contacts)
        repo.link_segment(branch_id, SEGMENT)
    repo.finish(SEGMENT, branches, False, NOW)
    db.commit()


def reviews_body(*reviews: tuple[str, str, bool]) -> str:
    """A reviews API answer wrapped as the page adapter wraps JSON; (text, date, answered)."""
    import json
    items = [{"text": text, "rating": 2, "date_created": date,
              "official_answer": {"text": "Спасибо"} if answered else None}
             for text, date, answered in reviews]
    return f"<html><body><p>{json.dumps({'reviews': items}, ensure_ascii=False)}</p></body></html>"


def reviews_url(branch_id: str) -> str:
    return domain.REVIEWS_API.format(branch_id=branch_id)


def answer(hooks=(), complaints=(), vacancies=(), pricing_visible=True, pains=()) -> dict:
    return {"facts": {"what_they_do": "бухгалтерия", "complaints": list(complaints),
                      "vacancies": list(vacancies), "pricing_visible": pricing_visible,
                      "decision_maker_hint": None},
            "dossier": {"summary": "Бухгалтерская фирма",
                        "hooks": [{"angle": "угол", "quote": q} for q in hooks],
                        "pains": list(pains), "approach": "Начать с отзывов"}}


class Answers:
    """Model double: the answer is chosen by the company named in the request.

    The batch researches companies in parallel, so a queue of answers in call
    order (core.model.FakeModel) would hand them to the wrong company.
    """

    def __init__(self, by_name: dict, on_call=None):
        self.by_name = by_name
        self.on_call = on_call
        self.calls: list[SimpleNamespace] = []
        self._lock = Lock()

    def answer(self, model, schema, messages, *, session_id, name):
        human = messages[1][1]
        company = next(n for n in self.by_name if f"Компания: {n}\n" in human)
        with self._lock:
            self.calls.append(SimpleNamespace(model=model, company=company, messages=messages,
                                              session_id=session_id, name=name))
        if self.on_call:
            self.on_call()
        value = self.by_name[company]
        if isinstance(value, Exception):
            raise value
        return schema.model_validate(value)


@pytest.fixture
def world():
    return SimpleNamespace(add_company=add_company, reviews_body=reviews_body,
                           reviews_url=reviews_url, answer=answer, Answers=Answers,
                           FakePages=FakePages, now=NOW)
