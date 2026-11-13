import logging
from datetime import UTC, datetime, timedelta, timezone

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


@pytest.mark.parametrize(
    "recorded,asked",
    [
        ("8 701 000 00 01", "+77010000001"),
        ("+77010000001", "8 (701) 000-00-01"),
        ("7010000001", "+7 701 000 0001"),
        ("  Info@Kofe.KZ ", "info@kofe.kz"),
        ("Kofe_Almaty", "  kofe_almaty"),
    ],
)
def test_another_spelling_of_the_handle_finds_the_same_refusal(db, recorded, asked):
    refusals.record(db, recorded, "просил не писать", NOW)

    assert refusals.is_refused(db, [asked]) is True


@pytest.mark.parametrize(
    "recorded,asked",
    [
        ("+77010000001", "https://wa.me/77010000001?text=%D0%9F%D0%B8%D1%88%D1%83"),
        ("https://wa.me/77010000001", "+77010000001"),
        ("https://wa.me/77010000001?text=%D0%9F", "wa.me/77010000001"),
    ],
)
def test_a_whatsapp_link_and_its_phone_find_the_same_refusal(db, recorded, asked):
    refusals.record(db, recorded, "стоп-слово: отпишитесь", NOW)

    assert refusals.is_refused(db, [asked]) is True
    assert db.execute("SELECT count(*) FROM refusals").fetchone()[0] == 1


@pytest.mark.parametrize(
    "raw,canonical",
    [
        ("8 701 000 00 01", "+77010000001"),
        ("https://wa.me/77010000001?text=x", "+77010000001"),
        ("  Info@Kofe.KZ ", "info@kofe.kz"),
        ("  ab ", "ab"),
        ("8 701 000", "8 701 000"),
        ("", ""),
    ],
)
def test_canonical_gives_the_form_to_compare_and_never_raises(raw, canonical):
    """Old readers compare contacts with refusals through it; what is no contact
    comes back stripped, as the migration kept such legacy rows."""
    assert refusals.canonical(raw) == canonical


def test_a_handle_that_is_no_contact_is_skipped_by_the_check(db):
    refusals.record(db, "+77010000001", "просил не писать", NOW)

    assert refusals.is_refused(db, ["", "12", "+77010000001"]) is True
    assert refusals.is_refused(db, ["", "12"]) is False


def test_a_handle_of_three_characters_is_a_contact(db):
    refusals.record(db, "abc", "просил не писать", NOW)

    assert refusals.is_refused(db, ["ABC"]) is True


@pytest.mark.parametrize("handle", ["", "   ", "ab", "8 701 000", "+770100"])
def test_an_empty_short_or_malformed_handle_is_a_domain_error(db, handle):
    with pytest.raises(DomainError) as error:
        refusals.record(db, handle, "просил не писать", NOW)

    assert error.value.code == "errors.handleInvalid"
    assert error.value.status_code == 422


@pytest.mark.parametrize("reason", ["", "  ", "ок", " ок "])
def test_a_reason_shorter_than_three_characters_is_a_domain_error(db, reason):
    with pytest.raises(DomainError) as error:
        refusals.record(db, "+77010000001", reason, NOW)

    assert error.value.code == "errors.reasonInvalid"
    assert error.value.status_code == 422
    assert refusals.is_refused(db, ["+77010000001"]) is False


def test_the_moment_is_stored_in_utc(db):
    almaty = timezone(timedelta(hours=5))
    refusals.record(db, "+77010000001", "просил", datetime(2026, 11, 12, 15, 0, tzinfo=almaty))

    assert (
        db.execute("SELECT refused_at FROM refusals").fetchone()[0] == "2026-11-12T10:00:00+00:00"
    )


def test_a_repeat_keeps_the_first_record(db):
    refusals.record(db, "+77010000001", "стоп-слово: отпишитесь", NOW)
    refusals.record(db, "8 701 000 00 01", "агент: просил не писать", NOW + timedelta(days=1))

    rows = db.execute("SELECT handle, reason, refused_at FROM refusals").fetchall()
    assert [tuple(row) for row in rows] == [
        ("+77010000001", "стоп-слово: отпишитесь", "2026-11-12T10:00:00+00:00")
    ]


def test_the_log_gets_the_fact_and_never_the_handle(db, caplog):
    with caplog.at_level(logging.INFO):
        refusals.record(db, "+77010000001", "просил не писать", NOW)

    assert any("refusal" in record.getMessage() for record in caplog.records)
    assert not any("7010000001" in record.getMessage() for record in caplog.records)


def test_a_repeat_logs_nothing(db, caplog):
    refusals.record(db, "+77010000001", "просил не писать", NOW)

    with caplog.at_level(logging.INFO):
        refusals.record(db, "+77010000001", "ещё раз", NOW)

    assert not any("refusal" in record.getMessage() for record in caplog.records)


def test_the_count_is_the_number_of_refused_handles(db):
    assert refusals.count(db) == 0
    refusals.record(db, "+77010000001", "просил не писать", NOW)
    refusals.record(db, "8 701 000 00 01", "повтор", NOW)
    refusals.record(db, "info@kofe.kz", "по почте", NOW)

    assert refusals.count(db) == 2
