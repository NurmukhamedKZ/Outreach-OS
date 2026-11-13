"""Что требуется от человека прямо сейчас. Одна строка — одна задача.

Верхний уровень backend/, рядом с analytics.py и activity.py, по той же причине:
задача собирается из трёх систем (тред и черновик — система 2, очередь — система
3, имя компании — модуль companies), и ни одна из них не имеет права знать две
другие. Компанию спрашиваем у её фасада, а не своим SQL.

Счётчик отвечает на «сколько», задача — на «кто и почему». `escalated: 3` не
говорит, кто эти трое, и оператор идёт искать их руками.

Только чтение: базы открываются в режиме ro.
"""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import sqlite_tools
from companies import public as companies
from conversations import public as conversations

KINDS = ("escalated", "draft", "stuck", "backup")

NO_REASON = "причина не сохранилась"
STUCK_WHY = "не знаем, ушло ли — проверь в телефоне"
DRAFT_WHY = "черновик первого письма ждёт проверки"

_state: Path | None = None
_companies: Path | None = None


def use(state_db: Path | None, companies_db: Path | None = None) -> None:
    """Шов из collector/api.py, как analytics.use(): модуль верхнего уровня не
    ищет базы сам. companies_db — боевая база с компаниями (в песочнице треды
    в базе прогона, а компании — в боевой, как у conversations); None — имя = id."""
    global _state, _companies
    _state, _companies = state_db, companies_db


@dataclass(frozen=True)
class Task:
    """Задача оператора. `href` не здесь: куда ведёт строка — вопрос фронтенда."""
    kind: str            # escalated | draft | stuck | backup (без компании и треда)
    company_id: str
    company_name: str
    channel: str         # номер лида, на котором открыт тред (#36: тред — компания)
    our_number: str | None
    why: str             # причина словами, готовая к показу
    at: str | None       # когда стало задачей, ISO


# Колонки переписки создаёт ревизия Alembic 0005 (#36): терпеть их отсутствие
# больше незачем. ponytail: SQL к таблицам conversations и outbox — разнести по
# фасадам вместе с overview (#7).
ESCALATED_SQL = (
    "SELECT t.company_id, t.channel, t.status_reason, t.our_number,"
    "       (SELECT max(m.handled_at) FROM messages m"
    "        WHERE m.company_id = t.company_id AND m.role = 'incoming') AS at"
    " FROM threads t WHERE t.status = 'escalated'"
)

STUCK_SQL = (
    "SELECT t.channel, t.company_id, o.our_number, o.updated_at AS at"
    " FROM outbox o JOIN threads t ON t.company_id = o.thread_id"
    " WHERE o.status = 'stuck'"
)


def tasks(limit: int = 50, backup_why: str | None = None) -> list[dict]:
    """Задачи в порядке срочности: эскалированный тред важнее непроверенного
    черновика — правило предметной области, и оно принадлежит бэкенду, а не
    странице.

    `backup_why` — почему бэкап требует человека (решает модуль backup); задача
    встаёт после тредов, которые ждут сейчас, и перед черновиками.
    """
    if _state is None:
        raise RuntimeError("tasks.use() не вызван: путь к state.db неизвестен")
    backup = ([Task("backup", "", "Бэкап", "", None, backup_why, None)]
              if backup_why else [])
    urgent, drafts, names = _thread_tasks()
    rows = (urgent + backup + drafts)[:limit]
    return [asdict(replace(row, company_name=names(row.company_id)))
            if row.company_id else asdict(row) for row in rows]


def _thread_tasks() -> tuple[list[Task], list[Task], Callable[[str], str]]:
    """Задачи тредов: срочные (эскалация, застрявшее) и черновики — и имена их компаний."""
    if not Path(_state).exists():
        # Чистая установка: переписки ещё нет ни строки. sqlite3 в
        # режиме ro на отсутствующем файле бросает — страница отвечала бы 500
        # вместо «задач нет».
        return [], [], str
    with closing(sqlite_tools.connect_readonly(_state)) as db:
        if not sqlite_tools.has_table(db, "threads"):
            return [], [], str
        urgent = [Task("escalated", r["company_id"], "", r["channel"], r["our_number"],
                       r["status_reason"] or NO_REASON, r["at"])
                  for r in db.execute(ESCALATED_SQL)]
        if sqlite_tools.has_table(db, "outbox"):
            urgent += [Task("stuck", r["company_id"], "", r["channel"], r["our_number"],
                            STUCK_WHY, r["at"])
                       for r in db.execute(STUCK_SQL)]
        # «Черновик первого касания» определён одним местом — у conversations.
        drafts = [Task("draft", r["company_id"], "", r["channel"], None,
                       DRAFT_WHY, r["created_at"])
                  for r in conversations.cold_drafts(db)]
    found = _names({row.company_id for row in urgent + drafts})
    return urgent, drafts, lambda company_id: found.get(company_id, company_id)


def _names(company_ids: set[str]) -> dict[str, str]:
    """Имена компаний одним вызовом фасада на весь список, а не запросом на строку."""
    if not company_ids or _companies is None or not Path(_companies).exists():
        return {}
    with closing(sqlite_tools.connect_readonly(_companies)) as db:
        if not sqlite_tools.has_table(db, "companies"):   # база ещё не мигрирована
            return {}
        return {company_id: company.name
                for company_id, company in companies.by_id(db, company_ids).items()}
