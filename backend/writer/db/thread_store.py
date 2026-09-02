"""Переписка: threads.db. Невосстановимый слой системы 2.

Единственное отличие от базы collector'а — и оно важное: схема создаётся через
CREATE TABLE IF NOT EXISTS, а не DROP. leads.db пересобирается из raw/ за
секунды, а переписку восстановить неоткуда: она существует только здесь.
Отсюда же запрет на миграции через пересоздание — таблицы правятся ALTER'ом.

Память агента — эта таблица. Строка со статусом «черновик» (sent_text пуст) в
историю не попадает: агент должен видеть то, что лид получил, а не то, что мы
ему предлагали отправить.
"""

import json
import sqlite3
from datetime import date, datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
  thread_id  TEXT PRIMARY KEY,   -- номер WhatsApp, +7XXXXXXXXXX
  company_id TEXT NOT NULL,
  seed       TEXT NOT NULL,      -- json: контекст лида из системы 1 на момент открытия
  created_at TEXT NOT NULL,
  stage      TEXT NOT NULL DEFAULT 'contact',  -- contact | probing | offer | closing
  outcome    TEXT,   -- meeting_agreed | meeting_held | refused | lost
  meeting_at TEXT    -- когда созвон состоялся
);

CREATE TABLE IF NOT EXISTS messages (
  message_id INTEGER PRIMARY KEY,
  thread_id  TEXT NOT NULL REFERENCES threads (thread_id),
  role       TEXT NOT NULL CHECK (role IN ('outgoing', 'incoming')),
  draft_text TEXT,               -- что предложила модель; у incoming пусто
  sent_text  TEXT,               -- что реально ушло или пришло; пусто = черновик
  angle      TEXT,
  prompt     TEXT,               -- json: полный запрос, ушедший в модель
  model      TEXT,               -- чем сгенерировано
  offer_variant TEXT,
  created_at TEXT NOT NULL,
  sent_at    TEXT
);

CREATE INDEX IF NOT EXISTS messages_thread ON messages (thread_id, message_id);
"""


def connect(path):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    _ensure_columns(db)
    return db


def _ensure_columns(db):
    """CREATE TABLE IF NOT EXISTS не трогает существующую таблицу, а базы
    переписки у всех давно созданы. Колонки владельца доливает владелец:
    полагаться на то, что до него добежит migrate системы 3, значит уронить
    writer везде, где система 3 не стартовала."""
    for table, columns in (
        ("messages", (("prompt", "TEXT"), ("model", "TEXT"), ("offer_variant", "TEXT"))),
        ("threads", (("stage", "TEXT NOT NULL DEFAULT 'contact'"), ("outcome", "TEXT"), ("meeting_at", "TEXT"))),
    ):
        existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for column, definition in columns:
            if column not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    db.commit()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_thread(db, thread_id, company_id, seed):
    """False, если тред уже есть: seed переписывать нельзя — он снимок момента,
    когда мы решили писать, и именно на него ссылается первое сообщение."""
    if thread(db, thread_id):
        return False
    db.execute(
        "INSERT INTO threads (thread_id, company_id, seed, created_at)"
        " VALUES (?, ?, ?, ?)",
        (thread_id, company_id, json.dumps(seed, ensure_ascii=False), now()),
    )
    db.commit()
    return True


def thread(db, thread_id):
    row = db.execute(
        "SELECT thread_id, company_id, seed, created_at, stage, outcome, meeting_at FROM threads WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    if not row:
        return None
    return {"thread_id": row[0], "company_id": row[1],
            "seed": json.loads(row[2]), "created_at": row[3], "stage": row[4],
            "outcome": row[5], "meeting_at": row[6]}


def set_stage(db, thread_id: str, stage: str) -> None:
    db.execute("UPDATE threads SET stage = ? WHERE thread_id = ?", (stage, thread_id))
    db.commit()


OUTCOMES: tuple[str, ...] = ("meeting_agreed", "meeting_held", "refused", "lost")


def set_outcome(db, thread_id: str, outcome: str, at: str | None = None) -> None:
    """Исход треда. meeting_held — единица оплаты, поэтому значение проверяется
    здесь: опечатка в исходе стоит денег, а не строки в журнале.

    meeting_at заполняется только у встреч: колонка с таким именем, хранящая
    момент отказа, врала бы всякому, кто прочитает её через полгода.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"исход {outcome!r} не из {OUTCOMES}")
    moment = (at or now()) if outcome.startswith("meeting_") else None
    db.execute("UPDATE threads SET outcome = ?, meeting_at = ? WHERE thread_id = ?",
               (outcome, moment, thread_id))
    db.commit()


def inbox(db):
    """Все треды одной сводкой: последняя реплика и счётчики — инбокс системы 2.

    Черновик в last_message не попадает: до отправки лид ничего не получил, и
    очередь «кому ответить» из несостоявшихся сообщений не собирается.

    Порядок — срочность: эскалированный тред ждёт человека здесь и сейчас,
    тред с последним словом лида — почти, остальные — по последней реплике.
    «Когда-либо отвечал» наверх не поднимает: на каждый живой диалог, на
    который мы уже ответили, смотреть незачем.
    """
    # status принадлежит системе 3 и появляется её миграцией. Долить его
    # здесь нельзя: sender при добавлении колонки размечает старые треды с
    # перепиской в escalated, и колонка, созданная раньше него, украла бы эту
    # разметку. Значит терпим отсутствие — как history терпит отсутствие outbox.
    status = "t.status" if _has_column(db, "threads", "status") else "'queued'"
    rows = db.execute(
        f"SELECT t.thread_id, t.company_id, t.created_at, {status},"
        " (SELECT count(*) FROM messages m WHERE m.thread_id = t.thread_id"
        "  AND m.sent_text IS NOT NULL AND m.role = 'outgoing'),"
        " (SELECT count(*) FROM messages m WHERE m.thread_id = t.thread_id"
        "  AND m.role = 'incoming'),"
        " (SELECT count(*) FROM messages m WHERE m.thread_id = t.thread_id"
        "  AND m.sent_text IS NULL),"
        " (SELECT sent_at FROM messages m WHERE m.thread_id = t.thread_id"
        "  AND m.sent_text IS NOT NULL ORDER BY m.message_id DESC LIMIT 1),"
        " (SELECT sent_text FROM messages m WHERE m.thread_id = t.thread_id"
        "  AND m.sent_text IS NOT NULL ORDER BY m.message_id DESC LIMIT 1)"
        f" FROM threads t ORDER BY CASE {status} WHEN 'escalated' THEN 0 ELSE 1 END,"
        "   CASE (SELECT m.role FROM messages m WHERE m.thread_id = t.thread_id"
        "         AND m.sent_text IS NOT NULL ORDER BY m.message_id DESC LIMIT 1)"
        "     WHEN 'incoming' THEN 0 ELSE 1 END,"
        "   8 DESC NULLS LAST, t.created_at DESC"
    ).fetchall()
    return [dict(zip(
        ("thread_id", "company_id", "created_at", "status", "sent", "replies",
         "drafts", "last_at", "last_message"), row,
    )) for row in rows]


def thread_of_company(db, company_id):
    row = db.execute(
        "SELECT thread_id FROM threads WHERE company_id = ?", (company_id,)
    ).fetchone()
    return thread(db, row[0]) if row else None


def history(db, thread_id):
    """Состоявшееся: отправленное оператором и пришедшее от лида. Больше ничего.

    kind живёт в outbox — он собственность системы 3, а не переписки, — и
    таблицы может не быть вовсе: её создаёт миграция системы 3, а writer
    открывает базу и без неё. Тот же приём, которым migrate.py страхуется от
    отсутствия чужих таблиц; импортировать его сюда нельзя — граница систем.
    """
    kind = "NULL"
    if _has_table(db, "outbox"):
        # Подзапрос, а не join: уникальность в outbox держится только по живым
        # строкам, и у сообщения, кончившегося в failed и поставленного
        # заново, строк две. Join раздвоил бы саму переписку — и на экране, и
        # во входе агента, который строится из этой же history.
        kind = ("(SELECT o.kind FROM outbox o WHERE o.message_id = m.message_id"
                " ORDER BY o.outbox_id DESC LIMIT 1)")
    rows = db.execute(
        f"SELECT m.role, m.sent_text, m.angle, m.sent_at, m.message_id, {kind} FROM messages m"
        " WHERE m.thread_id = ? AND m.sent_text IS NOT NULL ORDER BY m.message_id",
        (thread_id,))
    return [{"role": role, "text": text, "angle": angle, "sent_at": sent_at,
             "message_id": message_id, "kind": kind}
            for role, text, angle, sent_at, message_id, kind in rows]


def _has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                      (name,)).fetchone() is not None


def _has_column(db, table, column):
    return any(row[1] == column for row in db.execute(f"PRAGMA table_info({table})"))


def pending_draft(db, thread_id):
    row = db.execute(
        "SELECT message_id, draft_text, angle, created_at FROM messages"
        " WHERE thread_id = ? AND role = 'outgoing' AND sent_text IS NULL"
        " ORDER BY message_id DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    if not row:
        return None
    return {"message_id": row[0], "draft_text": row[1], "angle": row[2], "created_at": row[3]}


def add_draft(db, thread_id, text, angle, prompt=None, model=None, offer_variant=None):
    cursor = db.execute(
        "INSERT INTO messages (thread_id, role, draft_text, angle, prompt, model, offer_variant, created_at)"
        " VALUES (?, 'outgoing', ?, ?, ?, ?, ?, ?)",
        (thread_id, text, angle,
         json.dumps(prompt, ensure_ascii=False) if prompt else None,
         model, offer_variant, now()),
    )
    db.commit()
    return cursor.lastrowid


def cold_drafts(db):
    """Треды, где есть черновик и не отправлено ни одного сообщения, — то есть
    ровно первое касание. Порядок задаёт вызывающий: скор живёт в базе лидов."""
    rows = db.execute(
        "SELECT t.thread_id, t.company_id, m.message_id, m.draft_text, m.angle,"
        "       m.model, m.prompt IS NOT NULL"
        " FROM threads t JOIN messages m ON m.thread_id = t.thread_id"
        " WHERE m.role = 'outgoing' AND m.sent_text IS NULL"
        "   AND NOT EXISTS (SELECT 1 FROM messages s WHERE s.thread_id = t.thread_id"
        "                   AND s.sent_text IS NOT NULL)"
        # Последний черновик треда, тот же, что отдаёт pending_draft:
        # «Перегенерировать» оставляет предыдущий вариант в таблице (разница
        # предложенного и отправленного — разметка для промпта), но в очередь
        # проверки тред обязан попасть один раз и с новым текстом.
        "   AND m.message_id = (SELECT max(l.message_id) FROM messages l"
        "                       WHERE l.thread_id = t.thread_id AND l.role = 'outgoing'"
        "                         AND l.sent_text IS NULL)"
        " ORDER BY m.message_id DESC"
    ).fetchall()
    keys = ("thread_id", "company_id", "message_id", "draft_text", "angle", "model")
    # has_prompt приводится к bool здесь: SQLite отдаёт 0/1, а контракт
    # фронтенда обещает булево — приводить его в TypeScript значило бы чинить
    # тип не там, где он рождается.
    return [{**dict(zip(keys, row[:6])), "has_prompt": bool(row[6])} for row in rows]


def message_exists(db, message_id):
    return db.execute("SELECT 1 FROM messages WHERE message_id = ?",
                      (message_id,)).fetchone() is not None


def draft_prompt(db, message_id):
    """Полный запрос, ушедший в модель. None — черновик написан до того, как
    промпт начали сохранять."""
    row = db.execute("SELECT prompt, model FROM messages WHERE message_id = ?",
                     (message_id,)).fetchone()
    if not row or not row[0]:
        return None
    return {"prompt": json.loads(row[0]), "model": row[1]}


def add_incoming(db, thread_id, text, provider_id=None):
    """Ответ лида. Правкам не подлежит, поэтому draft_text у него пуст.

    provider_id пуст у того, что оператор ввёл руками, и заполнен у того, что
    принёс вебхук: по нему транспорт узнаёт уже записанное событие.
    """
    stamp = now()
    db.execute(
        "INSERT INTO messages (thread_id, role, sent_text, provider_id, created_at, sent_at)"
        " VALUES (?, 'incoming', ?, ?, ?, ?)",
        (thread_id, text, provider_id, stamp, stamp),
    )
    db.commit()


def used_angles(db, thread_id):
    """Углы уже отправленных сообщений: follow-up обязан взять новый."""
    rows = db.execute(
        "SELECT DISTINCT angle FROM messages WHERE thread_id = ?"
        " AND sent_text IS NOT NULL AND angle IS NOT NULL ORDER BY message_id",
        (thread_id,),
    )
    return [angle for (angle,) in rows]


def silent_days(db, thread_id, today):
    """Сколько дней прошло с последнего касания. None — писать ещё не начинали."""
    last = db.execute(
        "SELECT max(sent_at) FROM messages WHERE thread_id = ? AND sent_text IS NOT NULL",
        (thread_id,),
    ).fetchone()[0]
    if not last:
        return None
    return (date.fromisoformat(today[:10]) - date.fromisoformat(last[:10])).days
