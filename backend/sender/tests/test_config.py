"""Конфиг системы 3: единственное место порогов, пути от каталога sender/."""

import pytest

from sender.services import config


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


def test_reply_window_is_round_the_clock_by_default():
    """Лид написал сам и ждёт сейчас; молчание пятнадцать часов убивает диалог.
    Окно всё же параметром, а не отсутствием проверки: сузить его потом —
    правка конфига, а не кода."""
    window = config.load()["window"]
    assert window["reply"]["hours"] == [0, 24]
    assert window["reply"]["weekdays"] == [1, 2, 3, 4, 5, 6, 7]
    # Часовой пояс у окна ответа свой не заводится: он свойство человека на том
    # конце, а не вида сообщения.
    assert "timezone" not in window["reply"]


def test_stopwords_are_configuration_not_code():
    """Список правит человек без программиста — как окна и каденцию."""
    patterns = config.load()["stopwords"]["patterns"]
    assert "отпиш" in patterns
    assert all(isinstance(pattern, str) and pattern for pattern in patterns)


def test_handle_attempts_limit_is_configured():
    assert config.load()["limits"]["max_handle_attempts"] == 3


@pytest.fixture
def switch(tmp_path, monkeypatch):
    """Переключатель в tmp: боевой файл трогать нельзя — тест не имеет права
    включить автопилот на рабочей машине."""
    override = tmp_path / "autopilot"
    monkeypatch.setattr(config, "OVERRIDE", override)
    return override


def test_autopilot_defaults_to_the_config_file(switch):
    assert config.autopilot() == "off"


def test_override_wins_over_the_config(switch):
    config.set_autopilot("replies")
    assert config.autopilot() == "replies"
    assert switch.read_text(encoding="utf-8").strip() == "replies"


def test_garbage_in_the_override_falls_back_to_the_config(switch):
    """Битый файл не имеет права включить отправку: неизвестное значение —
    это «мы не знаем режим», а не «шли всё подряд»."""
    switch.write_text("fulll\n", encoding="utf-8")
    assert config.autopilot() == "off"


def test_set_autopilot_rejects_an_unknown_mode(switch):
    with pytest.raises(ValueError):
        config.set_autopilot("turbo")
    assert not switch.exists()


def test_no_temporary_file_survives_the_switch(switch):
    """Запись атомарна: недописанный файл, прочитанный тиком, — это режим,
    которого никто не выбирал."""
    config.set_autopilot("full")
    assert [path.name for path in switch.parent.iterdir()] == ["autopilot"]
