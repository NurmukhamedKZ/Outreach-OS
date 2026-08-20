"""logctx: contextvars для job_id/entity — без протаскивания через сигнатуры
функций, которые об этом бизнес-смысле знать не должны (fetch.py, scrapling)."""

import pytest

import logctx


def test_no_context_by_default():
    assert logctx.current_job_id() is None
    assert logctx.current_entity() is None


def test_job_sets_and_resets():
    with logctx.job("job-1"):
        assert logctx.current_job_id() == "job-1"
    assert logctx.current_job_id() is None


def test_entity_sets_and_resets():
    with logctx.entity("adelex.kz"):
        assert logctx.current_entity() == "adelex.kz"
    assert logctx.current_entity() is None


def test_entity_resets_even_on_exception():
    with pytest.raises(ValueError):
        with logctx.entity("adelex.kz"):
            raise ValueError("boom")
    assert logctx.current_entity() is None


def test_nested_entity_restores_previous():
    with logctx.entity("outer"):
        with logctx.entity("inner"):
            assert logctx.current_entity() == "inner"
        assert logctx.current_entity() == "outer"
    assert logctx.current_entity() is None


def test_job_and_entity_are_independent():
    with logctx.job("job-1"):
        with logctx.entity("adelex.kz"):
            assert logctx.current_job_id() == "job-1"
            assert logctx.current_entity() == "adelex.kz"


def test_set_job_id_direct_assignment_without_context_manager():
    logctx.set_job_id("job-9")
    assert logctx.current_job_id() == "job-9"
    logctx.set_job_id(None)   # уборка за собой — иначе следующий тест в этом же процессе увидит "job-9"
