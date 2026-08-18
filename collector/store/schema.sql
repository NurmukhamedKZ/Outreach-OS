-- STATE --
-- state.db — порождённое: невосстановимо, CREATE TABLE IF NOT EXISTS, DROP запрещён.
CREATE TABLE IF NOT EXISTS suppression (
  handle   TEXT PRIMARY KEY,
  added_at TEXT NOT NULL,
  reason   TEXT
);

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

CREATE TABLE IF NOT EXISTS llm_answers (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL,
  subject    TEXT NOT NULL,   -- (название, город) для profile, логин для ig_signals
  model      TEXT NOT NULL,
  prompt     TEXT NOT NULL,
  answer     TEXT NOT NULL    -- json ответа
);

-- Очередь и история запусков операций (спека §1: state.db = suppression, jobs,
-- threads, messages, llm_answers). result — json результата операции.
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
-- DERIVED --
-- derived.db — вычислимое: пересобирается прогонами, run_id в первичном ключе.
CREATE TABLE IF NOT EXISTS runs (
  run_id       INTEGER PRIMARY KEY,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  code_version TEXT,
  config_hash  TEXT,
  note         TEXT
);
-- Ровно одна строка (id=1). Указатель выдачи: view читают только её.
CREATE TABLE IF NOT EXISTS current_run (
  id     INTEGER PRIMARY KEY CHECK (id = 1),
  run_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS fetches_all (
  run_id     INTEGER NOT NULL,
  url        TEXT NOT NULL,
  sha        TEXT NOT NULL,
  final_url  TEXT,
  status     INTEGER,
  fetched_at TEXT,
  PRIMARY KEY (run_id, url)
);
CREATE TABLE IF NOT EXISTS orgs_all (
  run_id       INTEGER NOT NULL,
  branch_id    TEXT NOT NULL,
  org_id       TEXT,
  name         TEXT,
  org_name     TEXT,
  branch_count INTEGER,
  city         TEXT,
  rubric_id    TEXT,
  address      TEXT,
  rating       REAL,
  review_count INTEGER,
  PRIMARY KEY (run_id, branch_id)
);
CREATE TABLE IF NOT EXISTS contacts_all (
  run_id     INTEGER NOT NULL,
  branch_id  TEXT NOT NULL,
  kind       TEXT NOT NULL,
  handle     TEXT NOT NULL,
  source_url TEXT,
  PRIMARY KEY (run_id, branch_id, kind, handle)
);
CREATE TABLE IF NOT EXISTS companies_all (
  run_id     INTEGER NOT NULL,
  company_id TEXT NOT NULL,
  name_norm  TEXT,
  domain     TEXT,
  city       TEXT,
  rubric_id  TEXT,
  PRIMARY KEY (run_id, company_id)
);
CREATE TABLE IF NOT EXISTS company_links_all (
  run_id     INTEGER NOT NULL,
  company_id TEXT NOT NULL,
  branch_id  TEXT NOT NULL,
  rule       TEXT NOT NULL,
  confidence REAL,
  PRIMARY KEY (run_id, company_id, branch_id, rule)
);
CREATE TABLE IF NOT EXISTS signals_all (
  run_id      INTEGER NOT NULL,
  company_id  TEXT NOT NULL,
  type        TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  weight      REAL,
  quote       TEXT,
  url         TEXT,
  PRIMARY KEY (run_id, company_id, type, observed_at, url)
);
CREATE TABLE IF NOT EXISTS scores_all (
  run_id      INTEGER NOT NULL,
  company_id  TEXT NOT NULL,
  fit_score   REAL,
  intent_score REAL,
  breakdown   TEXT,
  PRIMARY KEY (run_id, company_id)
);
CREATE TABLE IF NOT EXISTS profiles_all (
  run_id         INTEGER NOT NULL,
  company_id     TEXT NOT NULL,
  model          TEXT,
  industry       TEXT,
  size_hint      TEXT,
  has_sales_team INTEGER,
  why_now        TEXT,
  quote          TEXT,
  confidence     REAL,
  PRIMARY KEY (run_id, company_id)
);

CREATE VIEW IF NOT EXISTS fetches AS
  SELECT f.* FROM fetches_all f JOIN current_run USING (run_id);
CREATE VIEW IF NOT EXISTS orgs AS
  SELECT o.* FROM orgs_all o JOIN current_run USING (run_id);
CREATE VIEW IF NOT EXISTS contacts AS
  SELECT c.* FROM contacts_all c JOIN current_run USING (run_id);
CREATE VIEW IF NOT EXISTS companies AS
  SELECT c.* FROM companies_all c JOIN current_run USING (run_id);
CREATE VIEW IF NOT EXISTS company_links AS
  SELECT l.* FROM company_links_all l JOIN current_run USING (run_id);
CREATE VIEW IF NOT EXISTS signals AS
  SELECT s.* FROM signals_all s JOIN current_run USING (run_id);
CREATE VIEW IF NOT EXISTS scores AS
  SELECT s.* FROM scores_all s JOIN current_run USING (run_id);
CREATE VIEW IF NOT EXISTS profiles AS
  SELECT p.* FROM profiles_all p JOIN current_run USING (run_id);
-- END --