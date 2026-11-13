-- state.db — порождённое: невосстановимо, CREATE TABLE IF NOT EXISTS, DROP запрещён.
-- Переписка (threads, messages) — у модуля conversations, ревизия Alembic 0005.
-- Очередь и история запусков операций (state.db). result — json результата операции.
CREATE TABLE IF NOT EXISTS jobs (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL,
  title      TEXT NOT NULL,
  status     TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
  step       INTEGER NOT NULL DEFAULT 0,
  steps      TEXT NOT NULL,          -- json: [имена операций]
  log        TEXT NOT NULL DEFAULT '',
  progress   TEXT,
  result     TEXT,
  exit_code  INTEGER,
  error      TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
