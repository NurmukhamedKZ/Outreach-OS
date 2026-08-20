"""configure(): backend.log — приложение, network.log — scrapling-шум, врозь."""

import logging

import pytest

import logctx
import logging_setup


@pytest.fixture
def clean_logging(tmp_path, monkeypatch):
    """Настраивает logging_setup на временный каталог и убирает добавленные
    хендлеры после теста — иначе логгеры остаются глобально грязными между
    тестами и утекают в боевой backend/logs/."""
    monkeypatch.setattr(logging_setup, "LOG_DIR", tmp_path)
    touched = ("", "uvicorn", "uvicorn.access", "scrapling", "collector.fetch")
    before = {name: list(logging.getLogger(name).handlers) for name in touched}
    yield tmp_path
    for name in touched:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            if handler not in before[name]:
                logger.removeHandler(handler)
        logger.propagate = True


def test_configure_creates_both_log_files(clean_logging):
    logging_setup.configure()
    logging.getLogger("app.demo").info("app line")
    logging.getLogger("scrapling").warning("network line")
    for handler in logging.getLogger().handlers:
        handler.flush()
    for handler in logging.getLogger("scrapling").handlers:
        handler.flush()

    assert (clean_logging / "backend.log").exists()
    assert (clean_logging / "network.log").exists()
    assert "app line" in (clean_logging / "backend.log").read_text(encoding="utf-8")
    assert "network line" in (clean_logging / "network.log").read_text(encoding="utf-8")


def test_scrapling_noise_does_not_leak_into_backend_log(clean_logging):
    logging_setup.configure()
    logging.getLogger("scrapling").error("curl: (6) Could not resolve host: dead.kz")
    for handler in logging.getLogger("scrapling").handlers:
        handler.flush()

    assert "dead.kz" not in (clean_logging / "backend.log").read_text(encoding="utf-8")


def test_context_fields_appear_only_when_set(clean_logging):
    logging_setup.configure()
    logger = logging.getLogger("app.demo2")
    logger.info("no context")
    with logctx.job("job-7"), logctx.entity("adelex.kz"):
        logger.info("with context")
    for handler in logging.getLogger().handlers:
        handler.flush()

    lines = (clean_logging / "backend.log").read_text(encoding="utf-8").splitlines()
    no_context_line = next(l for l in lines if "no context" in l)
    with_context_line = next(l for l in lines if "with context" in l)
    assert "job_id=" not in no_context_line and "entity=" not in no_context_line
    assert "job_id=job-7" in with_context_line
    assert "entity=adelex.kz" in with_context_line


def test_configure_is_idempotent(clean_logging):
    logging_setup.configure()
    logging_setup.configure()
    backend_handlers = [h for h in logging.getLogger().handlers
                        if getattr(h, "baseFilename", "").endswith("backend.log")]
    network_handlers = [h for h in logging.getLogger("scrapling").handlers
                        if getattr(h, "baseFilename", "").endswith("network.log")]
    assert len(backend_handlers) == 1, "повторный configure() задвоил хендлер backend.log"
    assert len(network_handlers) == 1, "повторный configure() задвоил хендлер network.log"
