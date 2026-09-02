"""Воронка холодной переписки: где мы теряем и по какой из трёх переменных.

Верхний уровень backend/, рядом с activity.py и config.py, по той же причине:
данные лежат в трёх системах (threads/messages — система 2, outbox — система 3,
рубрика и город — derived системы 1), и ни одна из них не имеет права знать
две другие.

Единица счёта — тред, а не сообщение: «412 отправленных сообщений» и «7
согласившихся тредов» в одной воронке дали бы проценты, которые ничего не
значат.

Только чтение: обе базы открываются в режиме ro.
"""

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

STEPS = ("sent", "delivered", "replied", "dialog", "meeting_agreed", "meeting_held")

# Меньше этого числа ответов диагноз не ставится: на трёх ответах он шум, а
# страница, посоветовавшая «чинить ICP» после первого молчащего лида, научит
# оператора себе не верить.
MIN_REPLIES_FOR_DIAGNOSIS = 10
LOW_REPLY_RATE = 0.02

_state: Path | None = None
_leads: Path | None = None


def use(state_db: Path | None, leads_db: Path | None = None) -> None:
    """Шов из collector/api.py, как activity.use(): модуль верхнего уровня не
    ищет базы сам."""
    global _state, _leads
    _state, _leads = state_db, leads_db


def report(days: int = 30) -> dict:
    if _state is None:
        raise RuntimeError("analytics.use() не вызван: путь к state.db неизвестен")
    if not Path(_state).exists():
        # На чистой установке переписки ещё нет, а sqlite3 в режиме ro на
        # отсутствующем файле бросает — и страница отвечала бы 500 вместо
        # пустой воронки. activity.py этой беды не знает: он открывает базу на
        # запись и создаёт её сам, а аналитика писать не имеет права.
        return _empty(days)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with closing(_connect()) as db:
        if not _has_table(db, "threads"):
            return _empty(days)
        rows = _funnel(db, since)
        return {
            "days": days,
            "funnel": rows,
            "by_offer": _breakdown(db, since, _column(db, "messages", "offer_variant")),
            "by_angle": _breakdown(db, since, _column(db, "messages", "angle")),
            "by_segment": _segments(db, since),
            "edited_share": _edited_share(db, since),
            "diagnosis": diagnose(rows),
        }


def _empty(days: int) -> dict:
    return {"days": days, "funnel": [{"step": step, "count": 0} for step in STEPS],
            "by_offer": [], "by_angle": [], "by_segment": [],
            "edited_share": 0.0, "diagnosis": "ok"}


def diagnose(funnel: list[dict]) -> str:
    counts = {row["step"]: row["count"] for row in funnel}
    sent, replied = counts.get("sent", 0), counts.get("replied", 0)
    if sent and replied / sent < LOW_REPLY_RATE:
        return "reply_rate_low"
    if replied >= MIN_REPLIES_FOR_DIAGNOSIS and not counts.get("meeting_held"):
        return "icp_mismatch"
    return "ok"


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{_state}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    if _leads is not None and Path(_leads).exists():
        db.execute(f"ATTACH DATABASE 'file:{_leads}?mode=ro' AS leads")
    return db


SENT_THREADS = (
    " FROM threads t JOIN messages m ON m.thread_id = t.thread_id"
    " AND m.role = 'outgoing' AND m.sent_text IS NOT NULL AND m.sent_at >= ?"
)


def _funnel(db: sqlite3.Connection, since: str) -> list[dict]:
    """Шаг, которому нечем считаться, показывает ноль, а не роняет страницу.

    Аналитика читает три чужих слоя и не создаёт ни одного: outbox доливает
    миграция системы 3, stage и outcome — thread_store системы 2. Любой из них
    может ещё не пройти по этой базе, и требовать их — значит отдать 500 там,
    где честный ответ «пока нечего показывать».
    """
    delivered = (
        f"SELECT count(DISTINCT t.thread_id){SENT_THREADS}"
        " JOIN outbox o ON o.message_id = m.message_id AND o.delivered_at IS NOT NULL"
    ) if _has_column(db, "outbox", "delivered_at") else None
    dialog = (
        f"SELECT count(DISTINCT t.thread_id){SENT_THREADS}"
        " WHERE t.stage IN ('probing', 'offer', 'closing')"
    ) if _has_column(db, "threads", "stage") else None
    has_outcome = _has_column(db, "threads", "outcome")
    counts = {
        "sent": _scalar(db, f"SELECT count(DISTINCT t.thread_id){SENT_THREADS}", since),
        "delivered": _scalar(db, delivered, since),
        "replied": _scalar(db, (
            f"SELECT count(DISTINCT t.thread_id){SENT_THREADS}"
            " WHERE EXISTS (SELECT 1 FROM messages i WHERE i.thread_id = t.thread_id"
            "               AND i.role = 'incoming')"), since),
        "dialog": _scalar(db, dialog, since),
        "meeting_agreed": _scalar(db, (
            f"SELECT count(DISTINCT t.thread_id){SENT_THREADS}"
            " WHERE t.outcome IN ('meeting_agreed', 'meeting_held')"
        ) if has_outcome else None, since),
        "meeting_held": _scalar(db, (
            f"SELECT count(DISTINCT t.thread_id){SENT_THREADS}"
            f" WHERE {HELD}"
        ) if has_outcome else None, since),
    }
    return [{"step": step, "count": counts[step]} for step in STEPS]


def _scalar(db: sqlite3.Connection, sql: str | None, since: str) -> int:
    if sql is None:
        return 0
    return db.execute(sql, (since,)).fetchone()[0] or 0


def _has_table(db: sqlite3.Connection, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                      (name,)).fetchone() is not None


def _has_column(db: sqlite3.Connection, table: str, column: str) -> bool:
    return _has_table(db, table) and any(
        row["name"] == column for row in db.execute(f"PRAGMA table_info({table})"))


def _column(db: sqlite3.Connection, table: str, name: str) -> str | None:
    """Имя колонки для GROUP BY или None, если её ещё нет в этой базе."""
    return f"m.{name}" if _has_column(db, table, name) else None


BEFORE_AB = "до A/B"

REPLIED = ("EXISTS (SELECT 1 FROM messages i WHERE i.thread_id = t.thread_id"
           "        AND i.role = 'incoming')")
HELD = "t.outcome = 'meeting_held'"


def _held(db: sqlite3.Connection) -> str:
    """Условие «встреча состоялась» или заведомо ложное, пока колонки нет."""
    return HELD if _has_column(db, "threads", "outcome") else "0"


FIRST_SENT = (
    " AND m.message_id = (SELECT min(f.message_id) FROM messages f"
    "                     WHERE f.thread_id = t.thread_id AND f.role = 'outgoing'"
    "                     AND f.sent_text IS NOT NULL)"
)


def _breakdown(db: sqlite3.Connection, since: str, column: str | None) -> list[dict]:
    """Разрез по одной из трёх переменных, считая тред по ЕГО ПЕРВОМУ письму.

    Иначе тред попадал бы в несколько строк сразу: follow-up системы 3 пишет
    свой угол и не пишет offer_variant вовсе, так что тред считался бы и под
    своим вариантом, и под «до A/B», а сумма по разрезу превышала бы sent из
    воронки. Первое письмо — то, на которое отвечают, и именно его повод и
    оффер проверяются.
    """
    if column is None:
        return []
    rows = db.execute(
        f"SELECT coalesce({column}, '') AS key,"
        "        count(DISTINCT t.thread_id) AS sent,"
        f"       count(DISTINCT CASE WHEN {REPLIED} THEN t.thread_id END) AS replied,"
        f"       count(DISTINCT CASE WHEN {_held(db)} THEN t.thread_id END) AS meetings"
        + SENT_THREADS + FIRST_SENT +
        " GROUP BY key ORDER BY sent DESC",
        (since,),
    )
    return [{"key": row["key"] or BEFORE_AB, "sent": row["sent"],
             "replied": row["replied"], "meetings": row["meetings"]} for row in rows]


def _segments(db: sqlite3.Connection, since: str) -> list[dict]:
    """Рубрика и город лида. Подпись рубрики остаётся идентификатором 2GIS:
    таблицы имён рубрик в derived нет, а придумывать её ради заголовка колонки
    дороже, чем прочитать id.

    Пустой список, когда derived не приаттачен: аналитика системы 2 не обязана
    падать оттого, что база системы 1 ещё не собрана.
    """
    if _leads is None or not Path(_leads).exists():
        return []
    rows = db.execute(
        "SELECT c.rubric_id || ' · ' || c.city AS key,"
        "       count(DISTINCT t.thread_id) AS sent,"
        f"      count(DISTINCT CASE WHEN {REPLIED} THEN t.thread_id END) AS replied,"
        f"      count(DISTINCT CASE WHEN {_held(db)} THEN t.thread_id END) AS meetings"
        + SENT_THREADS +
        " JOIN leads.companies c ON c.company_id = t.company_id"
        " GROUP BY key ORDER BY sent DESC",
        (since,),
    )
    return [dict(row) for row in rows]


def _edited_share(db: sqlite3.Connection, since: str) -> float:
    """Доля отправленных сообщений, которые оператор правил, — единственная
    бесплатная разметка качества промпта."""
    row = db.execute(
        "SELECT count(*) AS sent,"
        "       sum(CASE WHEN draft_text IS NOT NULL AND sent_text != draft_text"
        "                THEN 1 ELSE 0 END) AS edited"
        " FROM messages WHERE role = 'outgoing' AND sent_text IS NOT NULL"
        " AND sent_at >= ?",
        (since,),
    ).fetchone()
    return round((row["edited"] or 0) / row["sent"], 2) if row["sent"] else 0.0
