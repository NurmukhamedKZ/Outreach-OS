"""Конфиг системы 3: единственное место порогов, пути от каталога sender/."""

from pathlib import Path

from sender.services import config


def test_load_resolves_state_db_to_absolute_path():
    loaded = config.load()
    assert isinstance(loaded["state_db"], Path)
    assert loaded["state_db"].is_absolute()
    assert loaded["state_db"].name == "state.db"


def test_load_carries_warmup_calendar_from_spec():
    warmup = config.load()["warmup"]
    assert warmup["socket_delay_hours"] == 24
    assert warmup["passive_days"] == 3
    assert warmup["internal_ramp"] == [6, 12, 20, 30, 45, 60]
    assert warmup["cold_start_day"] == 11
    assert warmup["cold_ramp"] == [5, 10, 15, 20, 25, 30]
    assert warmup["ceiling"] == 30


def test_autopilot_starts_off():
    """Kill switch по умолчанию закрыт: система, приезжающая в 'full',
    начала бы писать лидам в момент первого запуска."""
    assert config.load()["autopilot"]["mode"] == "off"
