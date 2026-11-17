<!-- BEGIN:nextjs-agent-rules -->

# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` (resolved from this file's directory; in monorepos the `next` package may not be visible from the repo root) before writing any code. Heed deprecation notices.

This block is written and re-added by `next dev` — verify at `node_modules/next/dist/server/lib/generate-agent-files.js`. Removing it from a diff only re-creates the uncommitted change; committing it with your work keeps the tree clean.

<!-- END:nextjs-agent-rules -->

# E2E (#54)

`npx playwright test` отсюда (или `scripts/check`, запись `e2e`): поднимает свой стенд дерева
(`e2e/stand.ts` — `.stand/data` стирается, `scripts/stand up` собирает демо-мир), один воркер,
без повторов, гасит стенд в конце. Поднятый стенд — отказ, `scripts/stand down`. Браузер —
`npx playwright install chromium`. Упавший — скриншот и трейс в `e2e/results/`.

- **Файлы идут по порядку имени:** `01-walk` — обход по нетронутому миру, `02-world` —
  сценарии на прогоне мира (активный прогон не меняют), `03-conversation` — новый прогон песочницы,
  последним: пульт (часы, активный прогон) — состояние процесса бэкенда.
- **Импорт — `./fixtures`, не `@playwright/test`:** там адрес стенда и сторожа. Ожидаемый
  отказ называется явно: `guard.allow("/api/sandbox/chat", 409)`.
- **Компании — по роли мира** (`e2e/world.ts`, копия имён `backend/sandbox/world.py`), id —
  из `/api/research/leads` (`lead()`), не зашиты.
- **Действует кнопками страниц;** пульт по API (`sandbox.tick`, `clock`, `incoming`, `queue`) —
  только для того, чего на странице нет, и для подготовки данных.
- **Ждём по условию:** авто-ожидание локаторов и `expect.poll`; `waitForTimeout` и `sleep`
  запрещены. Страница опрашивает API раз в 3–30 секунд — после действия по API
  `page.reload()`, а не ожидание опроса. Строка с `::before` из `data-label` на телефоне
  входит в имя ячейки — ищем вхождением.
- **Новая страница** — строка в `PAGES` обхода; новая история с интерфейсом — сценарий.
