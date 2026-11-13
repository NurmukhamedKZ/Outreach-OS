"""Блок «Сбор лидов» на «Сегодня» считает по companies, research и refusals (#13)."""

from contextlib import closing

import paths
from collector.services import metrics
from core import db as core_db
from refusals import public as refusals
from conversations.tests.leads import NOW, add_lead


def test_lead_counters_come_from_companies_research_and_refusals(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    monkeypatch.setattr(paths, "PRODUCTION_STATE", path)
    core_db.migrate(path)
    with closing(core_db.connect(path)) as db:
        add_lead(db, "lead", [("whatsapp", "+77010000001")])
        add_lead(db, "refused", [("whatsapp", "+77010000002")])
        add_lead(db, "city-line", [("phone", "+77272960782")])
        refusals.record(db, "+77010000002", "просил не писать", NOW)

    assert metrics.sourcing_stats(NOW) == {"companies": 3, "reachable": 2, "leads": 1,
                                           "refusals": 1}


def test_a_fresh_install_counts_zeros(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "PRODUCTION_STATE", tmp_path / "state.db")

    assert metrics.sourcing_stats(NOW) == {"companies": 0, "reachable": 0, "leads": 0,
                                           "refusals": 0}


def test_a_failing_counter_leaves_the_rest_of_the_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "PRODUCTION_STATE", tmp_path / "state.db")

    def broken(_now):
        raise KeyError("2gis:gone")

    monkeypatch.setattr(metrics, "sourcing_stats", broken)

    snapshot = metrics.snapshot()

    assert snapshot["sourcing"] is None
    assert set(snapshot["writer"]) == {"threads", "drafts", "sent", "replies"}
