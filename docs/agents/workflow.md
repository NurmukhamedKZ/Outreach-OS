# Воркфлоу: настройки проекта

Факты о проекте для скиллов плагина `workflow` (`launch-main`, `launch-coder`, `review-axes`, `ci-watch`, `manual-qa`). Скиллы описывают процесс, а этот файл — где что лежит и какими командами проверяется. Правится руками; создаёт `/workflow:setup`. Скилл ссылается на раздел по его заголовку: «workflow.md, «Ветки»».

## Проект

- **Название:** Outreach-OS
- **Репо:** `NurmukhamedKZ/Outreach-OS` (GitHub, работа через `gh`; соглашения трекера — `docs/agents/issue-tracker.md`)
- **Родительский issue тикетов:** #3 — новые тикеты заводятся его sub-issue (`gh api --method POST repos/NurmukhamedKZ/Outreach-OS/issues/3/sub_issues -F sub_issue_id=<database id>`), связи — «blocked by».
- **Метка готового тикета:** `ready-for-agent` (метки — `docs/agents/triage-labels.md`)

## Ветки

- **База тикетов и единственная постоянная ветка:** `main` — тикеты вливаются squash.
- **Ветка тикета:** `ticket/<N>-<slug>`, worktree — `.claude/worktrees/ticket-<N>-<slug>` от `origin/main`.
- **Удалённую ветку после мержа** удаляет GitHub (`delete_branch_on_merge` включён); локальную ветку и worktree убирает main-сессия (`/workflow:launch-main`, шаг 0).

## Что читать

- **Порядок чтения перед кодингом:** `CLAUDE.md`, «Что прочитать перед кодингом».
- **Спека:** `docs/PRD.md` (требования F-номерами — на них ссылаются критерии приёмки тикета), `docs/TRD.md`, `docs/ARCHITECTURE_v3.md`. Описывают код до редизайна (#4); после него — источник правды для нового кода.
- **Стандарты кода:** `CLAUDE.md`, `backend/CLAUDE.md`, `frontend/AGENTS.md`.
- **Глоссарий и ADR:** `GLOSSARY.md`, `docs/adr/` (`docs/agents/domain.md`).
- **Журнал багов:** `docs/agents/bugs.md`.

## Параллельные кодеры

Сверх общих условий `/workflow:launch-main` (нет «blocked by» между тикетами, не трогают одни файлы):

- схему `state.db` (`backend/collector/db/schema.sql`, `backend/sender/db/migrate.py`, `thread_store._ensure_columns`) меняет не больше одного: владелец таблицы доливает колонки сам, и две правки одной таблицы разойдутся;
- сборщик приложения `backend/collector/api.py` правит не больше одного.

## Effort

| Тикет | Effort |
|---|---|
| Страницы дашборда, тексты, `config.toml`, документация | `medium` |
| Новая операция или эндпоинт, разбор в `sources.py`, промпты, скоринг | `high` |
| Схема `state.db`, гейты и очередь отправки, вебхук, отказы (F21), песочница | `xhigh` |

Смешанный тикет — по самой сложной части. Модель кодера — `opus`.

## Проверки

Полный набор — один раз на финальном коде после ревью (`/workflow:launch-coder`, шаг 4). В цикле TDD — только отдельные тест-файлы.

- **`backend/`:** `cd backend && uv run pytest` (сети не требует, около 15 секунд).
- **`frontend/`, если дифф его трогает:** `cd frontend && npx tsc --noEmit && npm run build`.

Линтеров и проверки типов бэкенда пока нет — #5. CI в репо нет: проверки идут локально, `/workflow:ci-watch` пропускается.

## Ревью

- **Модель субагентов осей:** `sonnet` (пять ревьюеров на Opus съедают лимит и падают с 429).
- **Доменные edge cases** (ось «Баги», сверх общих): граница окна 10:00–18:00 и суток в Asia/Almaty; повторная доставка вебхука (дедуп по `provider_id`); отказ лида в любой момент — проверяется до выдачи и до отправки; лид без канала (F19); падение посреди прогона `rebuild` или посреди тика; перезапуск процесса; пустое или медиа-входящее; номер в неканонической форме; смена `autopilot` при непустой очереди; кэш-хит ответа модели против нового платного вызова.
- **Чувствительные пути** (ось «Безопасность» запускается, только если дифф их трогает): `backend/sender/` (самые чувствительные — `services/gates.py`, `services/worker.py`, вебхук, `transport.py`, `node/`), `backend/collector/services/suppression.py`, `backend/collector/services/pipeline/export.py`, `backend/config.py`, `backend/collector/db/schema.sql` и миграции, `backend/sandbox/`.
- **Правила безопасности:** `CLAUDE.md`, «Безопасность».

## Ручной QA

- **Инструмент:** Playwright MCP (`mcp__plugin_playwright_playwright__*`); QA делает Claude, не пользователь.
- **Где:** локально, preview-деплоев нет. `cd backend && uv run python main.py` (порт 8787) и `cd frontend && npm run dev`, страницы — `http://localhost:3000`.
- **Переписка и отправка** проверяются только в песочнице: `SANDBOX=1` и `SENDER_NODE_URL=http://127.0.0.1:8787/api/sandbox/node` в `backend/.env`. Боевой Node (`backend/sender/node`) для QA не запускается; `backend/sender/autopilot` остаётся `off`.
- **Устройства:** десктоп `1280×800`, телефон `390×844`.
- **Языки:** только русский.
- **Данные:** слой данных пуст. Страницы с данными проверяются после мини-сбора (одна рубрика, один город), который один раз делает main-сессия, а не каждый кодер; сбор ходит в сеть и платит за модель.
- **Скриншоты:** только в `.playwright-mcp/` (в `.gitignore`).
- **Тикет доделок:** открытый issue «Доделки QA: …», ещё не взятый в работу; образца пока нет — заводится при первых доделках sub-issue #3.
