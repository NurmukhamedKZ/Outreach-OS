"""Журнал фоновой работы: кто, когда и с каким исходом что сделал.

Верхний уровень backend/, рядом с config.py и observability.py, по той же
причине: писать сюда обязаны и система 1, и система 3, а импортировать друг
друга они не имеют права.

Путь к state.db приходит швом use() — тем же приёмом, каким collector/api.py
подключает sender_refusal.use(). Соединение открывается на запись и сразу
закрывается: событие «пытались, и вот что вышло» обязано пережить откат
транзакции вызывающего, иначе сломавшийся тик стирал бы след о себе.

Таблицей владеет этот модуль, а не collector/db/schema.sql: тот же принцип,
по которому numbers и outbox живут в sender/db/migrate.py.
"""

import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

RETENTION_DAYS = 14

SCHEMA = """
CREATE TABLE IF NOT EXISTS activity (
  id      INTEGER PRIMARY KEY,
  at      TEXT NOT NULL,           -- когда случилось впервые
  last_at TEXT NOT NULL,           -- когда в последний раз
  repeats INTEGER NOT NULL DEFAULT 1,
  actor   TEXT NOT NULL,           -- sender.tick | sender.monitor | sender.warmup | jobs | webhook
  outcome TEXT NOT NULL,
  subject TEXT,
  detail  TEXT
);
CREATE INDEX IF NOT EXISTS activity_recent ON activity (last_at DESC);
"""

# В state.db одновременно пишут вебхук, тик воркера и воркер джоб. Журнал
# приходит четвёртым писателем со своим соединением, и без ожидания первая же
# встреча двух записей дала бы «database is locked» на ровном месте.
BUSY_TIMEOUT_MS = 5000

_path: Path | None = None


class NotConfiguredError(RuntimeError):
    """use() не звали."""


def use(path: str | Path | None) -> None:
    """Шов: где лежит state.db, знает сборщик приложения, а не журнал.

    Схема применяется здесь, один раз за процесс: executescript на каждой
    записи открывал бы лишнюю транзакцию записи три раза в минуту — ровно
    там, где за право писать и так стоит очередь.
    """
    global _path
    _path = Path(path) if path is not None else None
    if _path is None:
        return
    with closing(_open()) as db, db:
        db.executescript(SCHEMA)


def record(actor: str, outcome: str, subject: str | None = None,
           detail: str | None = None) -> dict:
    """Событие в журнал. Повтор последней строки не создаёт новую.

    Зовётся ВНЕ транзакции вызывающего: собственное соединение, попав внутрь
    чужого `with db:`, ждало бы освобождения базы, которое наступит только
    после его же возврата, — и падало бы по таймауту. Это не педантичность:
    отдельное соединение здесь и нужно затем, чтобы след пережил откат тика.
    """
    stamp = _now()
    event = {"at": stamp, "last_at": stamp, "repeats": 1, "actor": actor,
             "outcome": outcome, "subject": subject, "detail": detail}
    with closing(_connect()) as db, db:
        last = db.execute(
            "SELECT id, actor, outcome, subject, at, repeats FROM activity"
            " ORDER BY id DESC LIMIT 1").fetchone()
        if last is not None and (last[1], last[2], last[3]) == (actor, outcome, subject):
            db.execute("UPDATE activity SET repeats = repeats + 1, last_at = ?"
                       " WHERE id = ?", (stamp, last[0]))
            return {**event, "at": last[4], "repeats": last[5] + 1}
        db.execute(
            "INSERT INTO activity (at, last_at, actor, outcome, subject, detail)"
            " VALUES (?, ?, ?, ?, ?, ?)", (stamp, stamp, actor, outcome, subject, detail))
    return event


def record_crash(actor: str, detail: str = "см. логи процесса") -> None:
    """Запись из обработчика аварии демона — единственное место, где журнал
    молчит о собственной беде.

    Обычный record() шумит намеренно: потерянное событие — это та самая
    невидимая работа, которую журнал и заводился показывать. Но в `except`
    цикла демона исключение отсюда вылетело бы наружу while и убило бы
    asyncio-задачу — ровно тот отказ, ради которого этот `except` и стоит.
    Между «не записали аварию» и «после аварии некому работать» выбор
    очевиден.
    """
    try:
        record(actor, "crashed", detail=detail)
    except Exception:
        log.exception("журнал не смог записать аварию %s", actor)


def recent(limit: int = 200, actor: str | None = None) -> list[dict]:
    where = " WHERE actor = ?" if actor else ""
    arguments = (actor, limit) if actor else (limit,)
    with closing(_connect()) as db:
        rows = db.execute(
            "SELECT at, last_at, repeats, actor, outcome, subject, detail FROM activity"
            + where + " ORDER BY id DESC LIMIT ?", arguments).fetchall()
    return [dict(row) for row in rows]


def workers() -> list[dict]:
    """Пульс демонов. Считается из журнала, а не из переменной в памяти:
    так он переживает перезапуск процесса и не врёт после него."""
    with closing(_connect()) as db:
        rows = db.execute(
            "SELECT actor, MAX(last_at) AS last_at, COUNT(*) AS events"
            " FROM activity GROUP BY actor ORDER BY actor").fetchall()
    return [dict(row) for row in rows]


def prune(days: int = RETENTION_DAYS) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    # <=, а не <: cutoff округлён до секунд, и событие, записанное в ту же
    # секунду, что и порог, для ретенции уже не существует. С `<` demo(days=0)
    # зависел бы от того, успел ли пройти следующий тик.
    with closing(_connect()) as db, db:
        return db.execute("DELETE FROM activity WHERE last_at <= ?", (cutoff,)).rowcount


def _connect() -> sqlite3.Connection:
    if _path is None:
        raise NotConfiguredError(
            "activity.use(path) не звали — журналу некуда писать. "
            "В приложении это делает collector/api.py, в тестах — фикстура.")
    return _open()


def _open() -> sqlite3.Connection:
    db = sqlite3.connect(_path)
    db.row_factory = sqlite3.Row
    db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def demo():
    """Журнал пишет, схлопывает и чистит — без сети и без чужих модулей."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        use(Path(tmp) / "state.db")
        record("demo", "idle")
        record("demo", "idle")
        assert recent()[0]["repeats"] == 2
        assert prune(days=0) == 1
        use(None)
    print("activity demo ok — запись, схлопывание, ретенция")


if __name__ == "__main__":
    demo()
