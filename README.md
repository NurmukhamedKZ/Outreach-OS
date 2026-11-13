# Outreach-OS — лиды по Казахстану

Документы: [BRD](docs/BRD.md) · [PRD](docs/PRD.md) · [TRD](docs/TRD.md) ·
[ARCHITECTURE_v3](docs/ARCHITECTURE_v3.md) · [ADR](docs/adr/) · прежние версии — `docs/legacy/`

Все три системы лежат в `backend/` под одним venv и одним `backend/main.py`:
система 1 — модули `companies/`, `research/`, `refusals/` поверх `core/`;
система 2 — `conversations/`; система 3 — `sender/`. `collector/` — сборщик приложения
и старые ручки дашборда (до #7).

## Путь лида

Сегмент (город × рубрика 2GIS, «Настройки») → сбор 2GIS: списки и карточки,
склейка филиалов в компании → партия: до N компаний по приоритету → исследование:
отзывы и сайт, один вызов модели → досье с дословными цитатами → лид (зацепка,
канал для WhatsApp, нет отказа). Запуск — кнопка «Исследовать партию» на «Лидах»;
подробности — `docs/ARCHITECTURE_v3.md`.

## Команды

Из `backend/`:

```bash
uv run pytest                            # тесты всех систем, без сети
```

Модель зовётся через OpenRouter: ключ `OPENROUTER_API_KEY` в `backend/.env`
(шаблон — `backend/.env.example`), имя модели исследования — на «Настройках».

## Веб-дашборд

Два процесса; фронтенд проксирует `/api/*` на бэкенд, так что браузеру нужен
только порт 3000.

```bash
cd backend && uv run python main.py      # FastAPI, все три системы
cd frontend && npm run dev               # Next.js -> http://localhost:3000
```

Страницы: «Сегодня» (задачи оператора и счётчики), «Лиды» (партия и выдача) и
карточка лида, «Холодные» (первые письма, «Черновики топ-N»), «Диалоги»,
«Аналитика», «Отправка», «Процессы», «Настройки». «Черновики топ-N» ставит джобу
в очередь (`state.jobs`), прогресс и лог приходят по SSE `GET /api/events`.

## Данные

Одна невосстановимая база `backend/data/state.db` (ADR 0002): компании, партии,
досье, настройки, отказы, переписка, очередь. Схема модулей — Alembic
(`backend/migrations/`, применяется при старте), старые таблицы — через
`CREATE TABLE IF NOT EXISTS`, `DROP` запрещён. Рядом — скачанные страницы
`backend/data/pages/`. Данные старого конвейера (`backend/collector/data/`:
`raw/`, `derived.db`) код больше не читает.

## Бэкап

Невосстановимое — `backend/data/state.db`, скачанные страницы
`backend/data/pages/` и сессии WhatsApp `backend/sender/node/sessions/` —
бэкапится в папку iCloud Drive (`BACKUP_DIR` в `backend/.env`, по умолчанию
`~/Library/Mobile Documents/com~apple~CloudDocs/Outreach-OS-backup`) раз в
сутки, пока запущен бэкенд, и кнопкой «Сделать бэкап» на «Процессах». Нет
папки iCloud Drive (Docker, не Mac) — бэкап выключен, и на «Сегодня» висит
задача.

Раскладка: `<UTC-время>/state.db` и `<UTC-время>/sessions/` — снимок, хранится
последний удачный за каждые из 14 последних суток UTC; `pages/` — общее зеркало
страниц, докладывается только новыми.

В снимке — ключи сессий WhatsApp и переписка с лидами: включите в iCloud
«Расширенную защиту данных» (Advanced Data Protection), иначе ключи
шифрования хранит Apple. `backend/.env` в бэкап не входит — храните ключи
отдельно.

Восстановление — бэкенд и Node остановлены, снимок — самый свежий:

```bash
SNAP="$HOME/Library/Mobile Documents/com~apple~CloudDocs/Outreach-OS-backup/<UTC-время>"
cd backend
rm -f data/state.db-wal data/state.db-shm
cp "$SNAP/state.db" data/state.db
rm -rf sender/node/sessions && cp -R "$SNAP/sessions" sender/node/sessions
rsync -a --ignore-existing "$SNAP/../pages/" data/pages/
echo off > sender/autopilot
```

Снимок отстаёт от базы до суток, и **отказы за это время пропали** (F21).
Поэтому `autopilot` — `off` до сверки: перед тем как включать отправку,
внесите заново отказы из переписки с момента снимка (`POST /api/refusals` или
«Больше не писать» в карточке лида).

Тот же путь проверяет тест `backup/tests/test_service.py::test_a_restored_database_returns_refusals_and_settings`.

## Отказы — юридический контур

Кому больше не писать (закон РК №94-V, PRD F21). Источник истины — таблица
`refusals` в невосстановимой базе (модуль `backend/refusals/`), а не файл.
Контакт хранится в канонической форме, удалить
или изменить запись не дают триггеры базы. Список никогда не очищается;
выгрузка — `GET /api/refusals/export.csv` по требованию.

Пополняется из веб-интерфейса (`POST /api/refusals`) и стоп-словом в WhatsApp;
`research` проверяет отказ при чтении лидов, а отправка — перед каждым сообщением.