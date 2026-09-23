"""Что требуется от человека прямо сейчас. Одна строка — одна задача.

Верхний уровень backend/, рядом с analytics.py и activity.py, по той же причине:
задача собирается из трёх систем (тред и черновик — система 2, очередь — система
3, имя компании — derived системы 1), и ни одна из них не имеет права знать две
другие.

Счётчик отвечает на «сколько», задача — на «кто и почему». `escalated: 3` не
говорит, кто эти трое, и оператор идёт искать их руками.

Только чтение: обе базы открываются в режиме ro.
"""

import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import sqlite_tools

KINDS = ("escalated", "draft", "stuck")

NO_REASON = "причина не сохранилась"
STUCK_WHY = "не знаем, ушло ли — проверь в телефоне"
DRAFT_WHY = "черновик первого письма ждёт проверки"

_state: Path | None = None
_leads: Path | None = None


def use(state_db: Path | None, leads_db: Path | None = None) -> None:
    """Шов из collector/api.py, как analytics.use(): модуль верхнего уровня не
    ищет базы сам."""
    global _state, _leads
    _state, _leads = state_db, leads_db


@dataclass(frozen=True)
class Task:
    """Задача оператора. `href` не здесь: куда ведёт строка — вопрос фронтенда."""
    kind: str            # escalated | draft | stuck
    company_id: str
    company_name: str
    thread_id: str
    our_number: str | None
    why: str             # причина словами, готовая к показу
    at: str | None       # когда стало задачей, ISO


def _escalated_sql(db: sqlite3.Connection) -> str:
    """Колонки status/status_reason/our_number/handled_at принадлежат системе 3
    и появляются только её миграцией — `threads` создаёт writer, и порядок
    старта не гарантирован. Терпимое чтение, как в
    writer/db/thread_store.py::thread: колонка есть — читаем, нет —
    подставляем литерал. `'queued' = 'escalated'` никогда не истинно, так что
    без миграции system 3 escalated-задач просто не находится, а не падает
    ошибка."""
    status = "t.status" if sqlite_tools.has_column(db, "threads", "status") else "'queued'"
    reason = "t.status_reason" if sqlite_tools.has_column(db, "threads", "status_reason") else "NULL"
    number = "t.our_number" if sqlite_tools.has_column(db, "threads", "our_number") else "NULL"
    handled_at = (
        "(SELECT max(m.handled_at) FROM messages m"
        "  WHERE m.thread_id = t.thread_id AND m.role = 'incoming')"
    ) if sqlite_tools.has_column(db, "messages", "handled_at") else "NULL"
    return (
        f"SELECT t.thread_id, t.company_id, {reason} AS status_reason,"
        f"       {number} AS our_number, {handled_at} AS at"
        f" FROM threads t WHERE {status} = 'escalated'"
    )

STUCK_SQL = (
    "SELECT o.thread_id, t.company_id, o.our_number, o.updated_at AS at"
    " FROM outbox o JOIN threads t ON t.thread_id = o.thread_id"
    " WHERE o.status = 'stuck'"
)

# Дословная копия WHERE/дедупа writer/db/thread_store.py::cold_drafts (кроме
# списка колонок) — импортировать writer.* из tasks.py нельзя, граница систем
# не обсуждается (см. докстринг модуля), а «черновик первого касания» определён
# ровно там, одним местом. Правка cold_drafts обязана доехать сюда; расхождение
# ловит test_a_draft_that_was_regenerated_appears_once.
DRAFT_SQL = (
    "SELECT t.thread_id, t.company_id, m.created_at AS at"
    " FROM threads t JOIN messages m ON m.thread_id = t.thread_id"
    " WHERE m.role = 'outgoing' AND m.sent_text IS NULL"
    "   AND NOT EXISTS (SELECT 1 FROM messages s WHERE s.thread_id = t.thread_id"
    "                   AND s.sent_text IS NOT NULL)"
    "   AND m.message_id = (SELECT max(l.message_id) FROM messages l"
    "                       WHERE l.thread_id = t.thread_id AND l.role = 'outgoing'"
    "                         AND l.sent_text IS NULL)"
    " ORDER BY m.message_id DESC"
)


def tasks(limit: int = 50) -> list[dict]:
    """Задачи в порядке срочности: эскалированный тред важнее непроверенного
    черновика — правило предметной области, и оно принадлежит бэкенду, а не
    странице."""
    if _state is None:
        raise RuntimeError("tasks.use() не вызван: путь к state.db неизвестен")
    if not Path(_state).exists():
        # Чистая установка: sender/writer ещё не написали ни строки. sqlite3 в
        # режиме ro на отсутствующем файле бросает — страница отвечала бы 500
        # вместо «задач нет».
        return []
    with closing(sqlite_tools.connect_readonly(_state, _leads)) as db:
        if not sqlite_tools.has_table(db, "threads"):
            return []
        rows = [Task("escalated", r["company_id"], "", r["thread_id"], r["our_number"],
                     r["status_reason"] or NO_REASON, r["at"])
                for r in db.execute(_escalated_sql(db))]
        if sqlite_tools.has_table(db, "outbox"):
            rows += [Task("stuck", r["company_id"], "", r["thread_id"], r["our_number"],
                         STUCK_WHY, r["at"])
                    for r in db.execute(STUCK_SQL)]
        rows += [Task("draft", r["company_id"], "", r["thread_id"], None,
                     DRAFT_WHY, r["at"])
                for r in db.execute(DRAFT_SQL)]
        rows = rows[:limit]
        names = _names(db, {row.company_id for row in rows})
        return [asdict(replace(row, company_name=names.get(row.company_id, row.company_id)))
                for row in rows]


def _names(db: sqlite3.Connection, company_ids: set[str]) -> dict[str, str]:
    """Имена компаний одним запросом на весь список — как analytics.py::_segments,
    а не запросом на строку."""
    if not company_ids or _leads is None or not Path(_leads).exists():
        return {}
    marks = ",".join("?" * len(company_ids))
    rows = db.execute(
        "SELECT c.company_id, coalesce(o.org_name, o.name, c.name_norm)"
        " FROM leads.companies c LEFT JOIN leads.company_links l"
        "   ON l.company_id = c.company_id AND l.rule = 'self'"
        " LEFT JOIN leads.orgs o ON o.branch_id = l.branch_id"
        f" WHERE c.company_id IN ({marks})",
        tuple(company_ids),
    )
    return dict(rows)
