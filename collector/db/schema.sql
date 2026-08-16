-- Схема leads.db — восемь таблиц из ARCHITECTURE_v2 §4.
--
-- Миграций нет и не будет: схема правится здесь, база целиком пересобирается
-- build.py из raw/. На 1 500 строках это секунды и отменяет весь класс проблем
-- с частично применёнными изменениями и рассинхроном парсера с данными.
--
-- На Ф3 наполняются четыре таблицы: fetches, orgs, contacts, vacancies.
-- companies и company_links наполнит Ф5, signals — Ф6, suppression — Ф8.

-- Индекс к папке raw/: что скачано, куда приземлилось после редиректов, когда.
-- final_url — единственный честный признак молчаливой подмены содержимого.
DROP TABLE IF EXISTS fetches;
CREATE TABLE fetches (
  url        TEXT PRIMARY KEY,
  sha        TEXT NOT NULL,
  final_url  TEXT,
  status     INTEGER,
  fetched_at TEXT
);

-- 2GIS: организация = филиал. org_id общий у филиалов одной компании.
DROP TABLE IF EXISTS orgs;
CREATE TABLE orgs (
  branch_id    TEXT PRIMARY KEY,
  org_id       TEXT,
  name         TEXT,
  org_name     TEXT,
  branch_count INTEGER,
  city         TEXT,
  rubric_id    TEXT,
  address      TEXT,
  rating       REAL,
  review_count INTEGER
);

-- Каналы связи равноправны и лежат плоско: по строке на канал.
-- kind: phone | email | website | instagram | whatsapp
DROP TABLE IF EXISTS contacts;
CREATE TABLE contacts (
  branch_id  TEXT NOT NULL,
  kind       TEXT NOT NULL,
  handle     TEXT NOT NULL,
  source_url TEXT,
  PRIMARY KEY (branch_id, kind, handle)
);

-- hh.kz: полный текст вакансии — главный intent-сигнал проекта.
DROP TABLE IF EXISTS vacancies;
-- company_id проставляет Ф5 нечётким сравнением имени работодателя: у hh нет ни
-- домена, ни рубрики, и точный путь стоил бы лишнего запроса на каждую вакансию.
-- match_confidence хранится рядом, потому что связь именно нечёткая: непривязанная
-- вакансия intent-сигнала не даёт, а привязанная неверно даёт ложный.
CREATE TABLE vacancies (
  id               TEXT PRIMARY KEY,
  employer         TEXT,
  title            TEXT,
  text             TEXT,
  published_at     TEXT,
  city             TEXT,
  slug             TEXT,
  url              TEXT,
  company_id       TEXT,
  match_confidence REAL
);

-- Склейка филиалов в компании. Ф5.
DROP TABLE IF EXISTS companies;
CREATE TABLE companies (
  company_id TEXT PRIMARY KEY,
  name_norm  TEXT,
  domain     TEXT,
  city       TEXT,
  rubric_id  TEXT,
  first_seen TEXT
);

DROP TABLE IF EXISTS company_links;
CREATE TABLE company_links (
  company_id TEXT NOT NULL,
  branch_id  TEXT NOT NULL,
  rule       TEXT NOT NULL,
  confidence REAL,
  PRIMARY KEY (company_id, branch_id, rule)
);

-- Сигналы — события с датой, а не флаги: вакансия вчерашняя и полугодовой
-- давности дают разный intent_score. quote и url — материал для why_now. Ф6.
DROP TABLE IF EXISTS signals;
CREATE TABLE signals (
  company_id  TEXT NOT NULL,
  type        TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  weight      REAL,
  quote       TEXT,
  url         TEXT
);

-- Скоринг. Обе оси по правилам, breakdown хранится рядом: лид придётся объяснять
-- клиенту, а число без разбивки не объясняет ничего. Ф7.
DROP TABLE IF EXISTS scores;
CREATE TABLE scores (
  company_id   TEXT PRIMARY KEY,
  fit_score    REAL,
  intent_score REAL,
  breakdown    TEXT
);

-- Профили от модели. Ф9. Заполняется из кэшированных ответов в raw/*.llm.json,
-- поэтому сборка остаётся чистой функцией от сырья и не стоит ни цента.
-- Модель извлекает и классифицирует, решения принимают правила (§11): вердикта
-- «писать или нет» здесь нет и быть не должно.
DROP TABLE IF EXISTS profiles;
CREATE TABLE profiles (
  company_id     TEXT PRIMARY KEY,
  model          TEXT,
  industry       TEXT,
  size_hint      TEXT,
  has_sales_team INTEGER,
  why_now        TEXT,
  quote          TEXT,
  confidence     REAL
);

-- Юридический контур (закон РК №94-V). Проверяется до каждого касания
-- и не очищается никогда.
--
-- Источник истины — suppression.csv, здесь только его копия: таблица пересобирается
-- через DROP, и запрет, записанный прямо сюда, дожил бы ровно до следующей сборки.
-- Наполняет build.fill_suppression, пополняет api.py по кнопке оператора.
DROP TABLE IF EXISTS suppression;
CREATE TABLE suppression (
  handle   TEXT PRIMARY KEY,
  added_at TEXT NOT NULL,
  reason   TEXT
);
