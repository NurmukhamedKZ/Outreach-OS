# Журнал багов

Читается при каждой отладке (`CLAUDE.md`, «Отладка»): сначала `grep` по симптому. Новая запись — сверху.

Формат записи:

```markdown
## <Симптом одной строкой> (#<тикет или PR>)

- **Симптом:** что видно и где.
- **Корень:** одной строкой, с доказательством (трейс, лог, воспроизведение).
- **Исправление:** что и где поменяли.
- **Класс:** какой это вид ошибки — по нему видно повтор.
- **Как ловили:** тест, повтор, QA.
```

## Партия висит: компания в «исследуется» больше 11 минут, живое соединение только к OpenRouter (#27)

- **Симптом:** ручной QA #16, партия на 5 компаний: одна компания висела в «исследуется» больше 11 минут; у бэкенда единственное живое соединение — к OpenRouter. Ни закончить партию, ни запустить новую до перезапуска.
- **Корень:** `REQUEST_TIMEOUT_MS` уходит в httpx таймаутом каждого ожидания (пауза между байтами), а не всего запроса; OpenRouter, пока ждёт провайдера, шлёт пробелы в тело обычного ответа, и таймер чтения не срабатывает. Воспроизведение: локальный сервер шлёт пробел раз в 0,2 с, `httpx.Client(timeout=0.5).post` висит дольше 5 с.
- **Исправление:** `core/model.py` — `openrouter_client()` отдаёт SDK-клиент с `DeadlineTransport`: на каждом куске тела сверяет `CALL_DEADLINE_S` (180 с от начала запроса) и по сроку бросает `httpx.ReadTimeout` — ретрай, как обрыв, после `TRANSPORT_RETRIES` — `ModelError`. Без потоков: проверка в вызывающем потоке. Через него же — `writer/services/agent.py::client` (продавец). Старый `collector/services/pipeline/llm.py` не тронут — уходит в #13.
- **Класс:** таймаут чтения принят за общий срок запроса.
- **Как ловили:** QA #16; тесты `core/tests/test_model.py` с подменным транспортом httpx, отдающим бесконечный поток пробелов.

## `npm run dev` или `npm run build` падают на всех страницах: next/font под Turbopack (#24, корень — #51)

- **Симптом:** в worktree кодера #8 `npm run dev` (Turbopack, Next 16.3.1) отвечал 500 на каждой странице: «Module not found: Can't resolve '@vercel/turbopack-next/internal/font/google/font' … next/font/google queries have exactly one entry». `npx next dev --webpack` и `npm run build` работали.
- **Первая версия (#8):** корень не установлен. В свежем worktree после `npm ci` во `frontend/` ошибка не воспроизводится: `npm run dev` отдаёт 200 на `/`, `/activity`, `/settings`, `/leads` (13 ноября 2026). Вероятнее всего, у кодера были не те `node_modules` (скопированные или от другой версии), а не дефект кода.
- **Повтор (#17, 13 ноября 2026):** воспроизвелось в свежем worktree и после `npm ci`, и после удаления `frontend/.next`, так что версия «не те `node_modules`» не подтвердилась. Корень по-прежнему не установлен. `npx next dev --webpack` работает. Там же SSE не доезжал до страницы: в worktree нет `frontend/.env.local` (он в `.gitignore`), и `EventSource` шёл через буферизующий прокси Next — список не перечитывался после завершения джобы.
- **Корень (#51, 16 ноября 2026):** не код и не `node_modules`, а ответ Google Fonts. Иногда он отдаёт CSS с адресами `https://fonts.gstatic.com/l/font?kit=…&skey=…&v=v20` вместо `https://fonts.gstatic.com/s/<шрифт>/…woff2`; загрузчик `next/font/google` под Turbopack вкладывает адрес в запрос модуля `@vercel/turbopack-next/internal/font/google/font?{"url":"…"}`, `&` делит его на несколько частей — «queries have exactly one entry». Ответ оседает в постоянном кэше Turbopack в `frontend/.next`, поэтому сбой держится от запуска к запуску и «не лечится» `npm ci`, а свежий `.next` помогает до следующего такого ответа. Воспроизвелось на `next build` в `scripts/check --all`: бисекция `.next` — без `.next/cache/turbopack` сборка проходит, с ним падает; в сломанном кэше есть `l/font%3Fkit=…&skey=…`, в рабочем — только `/s/inter/v20/…woff2`. Webpack этот путь не проходит и работает.
- **Исправление (#63):** шрифты — `next/font/local` с файлами Inter и JetBrains Mono в `frontend/app/fonts/` (вариативные `woff2` из релизов `rsms/inter` и `JetBrains/JetBrainsMono`, латиница и кириллица; у Inter ось `opsz` закреплена на 14, как отдаёт Google); `next/font/google` во фронте не импортируется — сторожит `backend/tests/test_retired.py`. Ни dev, ни сборка в сеть за шрифтами не ходят, и ответу Google нечего оставить в кэше Turbopack. Подсказка `--webpack` и сам флаг из `scripts/stand` убраны. Старый `frontend/.next` с отравленным кэшем может упасть ещё раз — `rm -rf frontend/.next`. `NEXT_PUBLIC_API_ORIGIN` из `frontend/.env.local` после #38 никто не читает — копировать его не нужно. Не копировать и не ссылаться на `node_modules` основного дерева.
- **Класс:** окружение worktree.
- **Как ловили:** повтор в свежем worktree с `npm ci` (#8, #17); корень — бисекцией `frontend/.next` и поиском `skey=` в кэше Turbopack (#51); возврат к Google Fonts ловит `test_the_dashboard_takes_no_fonts_from_google` (#63).

