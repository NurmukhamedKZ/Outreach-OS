import logging
from datetime import UTC, datetime, timedelta

import pytest

from core.errors import DomainError
from refusals import public as refusals

COMPANY = ["+77010000001", "info@kofe.kz", "kofe_almaty"]

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)


def test_a_refusal_on_one_handle_closes_every_handle_of_the_company(db):
    refusals.record(db, "info@kofe.kz", "просил не писать", NOW)

    assert refusals.is_refused(db, COMPANY) is True


def test_a_company_without_refused_handles_may_be_written_to(db):
    refusals.record(db, "+77019999999", "другая компания", NOW)

    assert refusals.is_refused(db, COMPANY) is False
    assert refusals.is_refused(db, []) is False


def test_the_refused_handle_stays_closed_for_any_company(db):
    refusals.record(db, "+77010000001", "просил не писать", NOW)

    assert refusals.is_refused(db, ["+77010000001", "other@mail.kz"]) is True


@pytest.mark.parametrize("recorded,asked", [
    ("8 701 000 00 01", "+77010000001"),
    ("+77010000001", "8 (701) 000-00-01"),
    ("7010000001", "+7 701 000 0001"),
    ("  Info@Kofe.KZ ", "info@kofe.kz"),
    ("Kofe_Almaty", "  kofe_almaty"),
])
def test_another_spelling_of_the_handle_finds_the_same_refusal(db, recorded, asked):
    refusals.record(db, recorded, "просил не писать", NOW)

    assert refusals.is_refused(db, [asked]) is True


def test_a_handle_that_is_no_contact_is_skipped_by_the_check(db):
    refusals.record(db, "+77010000001", "просил не писать", NOW)

    assert refusals.is_refused(db, ["", "12", "+77010000001"]) is True
    assert refusals.is_refused(db, ["", "12"]) is False


@pytest.mark.parametrize("handle", ["", "   ", "ab", "8 701 000", "+7701000000"])
def test_an_empty_short_or_malformed_handle_is_a_domain_error(db, handle):
    with pytest.raises(DomainError) as error:
        refusals.record(db, handle, "просил не писать", NOW)

    assert error.value.code == "errors.handleInvalid"
    assert error.value.status_code == 422


def test_a_repeat_keeps_the_first_record(db):
    refusals.record(db, "+77010000001", "стоп-слово: отпишитесь", NOW)
    refusals.record(db, "8 701 000 00 01", "агент: просил не писать", NOW + timedelta(days=1))

    rows = db.execute("SELECT handle, reason, refused_at FROM refusals").fetchall()
    assert [tuple(row) for row in rows] == [
        ("+77010000001", "стоп-слово: отпишитесь", "2026-11-12T10:00:00+00:00")]


def test_the_log_gets_the_fact_and_never_the_handle(db, caplog):
    with caplog.at_level(logging.INFO):
        refusals.record(db, "+77010000001", "просил не писать", NOW)

    assert any("refusal" in record.getMessage() for record in caplog.records)
    assert not any("7010000001" in record.getMessage() for record in caplog.records)
