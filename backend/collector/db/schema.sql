-- state.db — порождённое: невосстановимо, CREATE TABLE IF NOT EXISTS, DROP запрещён.
CREATE TABLE IF NOT EXISTS threads (
  thread_id  TEXT PRIMARY KEY,
  company_id TEXT NOT NULL,
  seed       TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  message_id INTEGER PRIMARY KEY,
  thread_id  TEXT NOT NULL REFERENCES threads (thread_id),
  role       TEXT NOT NULL CHECK (role IN ('outgoing', 'incoming')),
  draft_text TEXT,
  sent_text  TEXT,
  angle      TEXT,
  created_at TEXT NOT NULL,
  sent_at    TEXT
);
CREATE INDEX IF NOT EXISTS messages_thread ON messages (thread_id, message_id);

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
