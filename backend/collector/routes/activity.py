"""Журнал фоновой работы для страницы «Процессы».

Порог молчания демона считает бэкенд, а не страница: интервалы тиков живут в
sender/config.toml, и фронтенд про них знать не должен — тот же принцип, что
у календаря прогрева.
"""

from fastapi import APIRouter

import activity

router = APIRouter(prefix="/api/activity")

# Три интервала подряд без единой записи — демон молчит. Один пропущенный тик
# бывает от блокировки базы, три подряд не бывают ни от чего безобидного.
SILENCE_FACTOR = 3

DEFAULT_INTERVAL_SECONDS = 60
INTERVALS = {
    "sender.tick": 20,
    "sender.warmup": 900,
    "sender.monitor": 3600,
    "jobs": 60,
    "webhook": 3600,
}


@router.get("")
def journal(limit: int = 200, actor: str | None = None) -> dict:
    return {
        "events": activity.recent(limit=limit, actor=actor),
        "workers": [
            {**row,
             "silent_after_seconds":
                 INTERVALS.get(row["actor"], DEFAULT_INTERVAL_SECONDS) * SILENCE_FACTOR}
            for row in activity.workers()
        ],
    }
