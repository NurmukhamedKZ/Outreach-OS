"""Календарь прогрева номера: день от регистрации -> что ему сегодня можно.

Источник чисел — рекомендации по прогреву номера WhatsApp, перенесённые в
config.toml. Три вещи, в которых календарь строже интуиции: первые сутки сокет
вообще не привязывается, дни 2-4 номер только принимает, а холодные касания
начинаются примерно с одиннадцатого дня, не с четвёртого.
"""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class Phase(StrEnum):
    socket_delay = "socket_delay"
    passive = "passive"
    internal = "internal"
    cold = "cold"


@dataclass(frozen=True)
class Plan:
    day: int
    phase: Phase
    daily_limit: int
    cold_allowed: bool


def day_of(started_at: str, now: datetime) -> int:
    """День прогрева: сутки регистрации — первый."""
    started = date.fromisoformat(started_at[:10])
    return (now.date() - started).days + 1


def plan(started_at: str, now: datetime, warmup_config: dict) -> Plan:
    day = day_of(started_at, now)
    phase = _phase(day, warmup_config)
    limit = _limit(day, phase, warmup_config)
    return Plan(day=day, phase=phase, daily_limit=limit, cold_allowed=phase is Phase.cold)


def _phase(day: int, warmup_config: dict) -> Phase:
    if day <= warmup_config["socket_delay_hours"] // 24:
        return Phase.socket_delay
    if day <= 1 + warmup_config["passive_days"]:
        return Phase.passive
    if day < warmup_config["cold_start_day"]:
        return Phase.internal
    return Phase.cold


def _limit(day: int, phase: Phase, warmup_config: dict) -> int:
    if phase in (Phase.socket_delay, Phase.passive):
        return 0
    if phase is Phase.internal:
        return _from_ramp(day - (1 + warmup_config["passive_days"]),
                          warmup_config["internal_ramp"], warmup_config["ceiling"])
    return _from_ramp(day - warmup_config["cold_start_day"] + 1,
                      warmup_config["cold_ramp"], warmup_config["ceiling"])


def _from_ramp(step: int, ramp: list[int], ceiling: int) -> int:
    """Шаг за пределами рампы — потолок: рампа кончается, доверие нет."""
    if step > len(ramp):
        return ceiling
    return min(ramp[step - 1], ceiling)
