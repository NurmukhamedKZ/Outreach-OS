# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Что это

AI lead-generation продукт: находит нужных людей, пишет им персонализированные
письма и доставляет их так, чтобы они не попали в спам. Продукт — это три
взаимосвязанные системы, а не одна:

1. **Targeting & Enrichment** — многоисточниковый сбор лидов, ICP-фильтрация,
   intent-сигналы, верификация контактов, обогащение. Живёт в `collector/`.
2. **AI-персонализация** — тред переписки на каждого лида и черновик следующего
   сообщения. Живёт в `writer/` (см. секцию ниже).
3. **Sending infrastructure** — WhatsApp как главный канал: пул номеров,
   прогрев SIM, очередь исходящих, автопилот. Живёт в `sender/` (см. секцию).

Все три системы физически лежат в `backend/` (`backend/collector/`,
`backend/writer/`, `backend/sender/`) под одним venv и одним
`backend/main.py`; ниже относительные имена `collector/`, `writer/`,
`sender/` всегда подразумевают путь внутри `backend/`.

Реализованы системы 1, 2 и транспорт с пулом номеров системы 3; у первой и
второй есть своя секция ниже, у третьей — секция в «Веб как обвязка». Веб —
продуктовый дашборд (сайдбар, страница на систему, живые процессы по SSE),
не консоль запуска скриптов.

Бизнес-контекст, ICP и полная спецификация — в `docs/` (см. ниже).

## Команды

Продуктовый дашборд — во `frontend/`. Команды ниже, кроме веба, запускаются
из `backend/`. Сбор, анализ и прогоны — операции воркера
(`collector/services/pipeline/`), а не скрипты; из консоли они ставятся
джобами через веб, из тестов — вызовом функции с RunContext.

```bash
uv run pytest                                    # тесты обеих систем, без сети
uv run pytest collector/tests/test_parsers.py    # один раздел — одним файлом
cd writer && uv run pytest tests/                # тесты системы 2, тот же venv
```

Разведка источника вручную: `uv run python -c "from collector.services.pipeline import probe;
probe.gis_list(__import__('types').SimpleNamespace(log=print, progress=lambda *a: None,
check_cancelled=lambda: None))"` — пробы ходят в сеть и читают config.toml.

Веб — два процесса, браузеру нужен только порт 3000:

```bash
cd backend && uv run python main.py                            # FastAPI, все три системы
cd frontend && npm run dev                                    # Next.js -> http://localhost:3000
cd backend/sender/node && npm start                           # сокеты WhatsApp, порт 8788
```

`main.py` поднимает то же приложение, что раньше собирал `collector/api.py`
через `uvicorn api:app` — включая роутеры writer'а и sender'а. Для
reload-режима: `uv run uvicorn main:app --port 8787 --reload --timeout-graceful-shutdown 5`.
Флаг обязателен: `/api/events` — бесконечный SSE-стрим, и без таймаута
`Ctrl+C`/перезагрузка на файле виснут на «Waiting for connections to close»,
пока не закроется вкладка фронтенда — у graceful shutdown в uvicorn нет
таймаута по умолчанию.

**`config.py` — единственное место чтения переменных окружения**, общее для
всех трёх систем: `pydantic.BaseSettings` сам подхватывает `backend/.env` при
импорте, поэтому `--env-file` и `os.environ` в коде систем не нужны — ключ,
которому он нужен, импортирует `from config import settings`.

`SERPER_API_KEY` и `OPENROUTER_API_KEY` берутся из `backend/.env` (шаблон —
`backend/.env.example`); без них работают сбор/пересборка/веб, но не операции
`analyze.*`, `probe.serp` и черновики системы 2.

**`observability.py` — единственная точка подключения Langfuse**, тем же
принципом, что `config.py` для секретов: берёт `LANGFUSE_PUBLIC_KEY`/
`LANGFUSE_SECRET_KEY`/`LANGFUSE_HOST` из `settings`, отдаёт
`langfuse.langchain.CallbackHandler` (`None`, если ключей нет — трейсинг молча
выключен, LLM-вызовы работают как обычно). Handler цепляется в
`config={"callbacks": [...]}` на каждом `.invoke()` в
`collector/services/pipeline/llm.py::invoke()` и `writer/services/agent.py::draft()`
— единственных двух местах, где продукт реально ходит в LLM. Трейсы
группируются в сессию Langfuse по `job_id` (collector) или `thread_id`
(writer). Self-host Langfuse — `http://109.199.125.111:3000`. Кэш-хиты
`state.llm_answers` в Langfuse не попадают: трейсится только вызов, не
попавший в кэш.

## Архитектура collector/ (система 1)

**`services/pipeline/rebuild.py` — граница системы.** Слева от неё — сбор и
анализ (`collect.py`, `analyze.py`) и единственное, что нельзя восстановить
(`data/raw/` + `state.db`: страницы удаляются, вакансии закрываются, ответы
модели оплачены). Справа — всё, что пересобирается из `raw/` бесплатно
за секунды. Поэтому rebuild не импортирует ни `services/fetch.py`, ни
`scrapling` — невозможность похода в сеть обеспечена отсутствием инструмента,
а не дисциплиной, и тест графа импортов это проверяет.

Сборка идёт прогоном: `rebuild.run(ctx)` создаёт run_id, пишет все `*_all`
таблицы нового прогона и публикует его одним `activate_run()` в конце —
`api.py` читает старую выдачу всё время, пока идёт пересборка, и падение на
середине не портит рабочую. Публикуется только завершённый прогон: у брошенного
`finished_at` пуст, а таблицы пусты или наполнены наполовину, и откат на него
стёр бы выдачу одним нажатием. Откат — `activate_run(старый run_id)`.

**`services/sources.py` — единственная копия разбора.** Чистые функции без
сети, диска и `print`, использует их и rebuild, и pytest (на эталонных
страницах из `fixtures/`). Второй копии разбора HTML/JSON в проекте нет
и быть не должно.

**`config.toml` — единственное место конфигурации.** Рубрики 2GIS,
города, веса скоринга, LLM-модель, интервалы `[pacing]` (темп сети — на хост через `collect.Pacer`, а не на воркер). Ничего из этого не хардкодится
в коде; читается через `tomllib` (stdlib).

**Источников сигналов два: сайт компании и лента инстаграма.** hh.kz был третьим
и отклонён 18 августа 2026 — измеренное пересечение работодателей hh с ICP из
2GIS около 5%, сигналов в базе ноль. Числа и обоснование — `docs/TRD.md` §7a.
Сырьё осталось в `data/raw/`, решение обратимо; заново подключать источник без
способа спрашивать hh о конкретной компании — не надо.

**Юридический контур (`state.suppression`, PRD F21).** Источник истины —
база, невосстановимый слой (пересборкой не затрагивается). Отказ пишется
одной записью (`services/suppression.py`); `export.run` и API проверяют
suppression **до** выдачи, а не после. Список никогда не очищается; выгрузка —
`GET /api/suppression/export.csv` по требованию.

**LLM-кэш в `state.llm_answers`.** `analyze.profile` и `analyze.ig_signals`
кладут ответы модели в невосстановимую таблицу вместе с промптом; rebuild
читает их оттуда, поэтому пересборка базы не стоит ни цента — платится только
за компанию/аккаунт, увиденные впервые. Ключ — (kind, subject, model, prompt):
смена модели не обесценивает старые ответы. Хранилища ответов два — таблица и
файлы `raw/*.llm.json` (так платили до переезда), — и `load_llm_answers` читает
оба, а не выбирает одно по признаку «таблица пуста»: первая же новая запись
спрятала бы всё, что осталось файлами.

**Выдача (`services/pipeline/export.py`).** Три правила из PRD, не смягчаются
ради красивого числа: F19 — лид без канала (`whatsapp`/`phone`/`email`) не
попадает в выдачу; F21 — suppression проверяется до выдачи; F20 — у каждого
лида `why_now` — цитата и ссылка, а не голый скор. `services/leads.py` и
`routes/leads.py` не дублируют эту логику, а импортируют
`export.candidates`/`export.best_channel`.

**Веб как обвязка, не источник правды.** Продуктовые операции — `POST
/api/pipeline/{kind}` (`discover`, `classify`, `rebuild`) и `POST
/api/operations/{name}` (одиночные операции) — ставят джобу в очередь
(`services/jobs.py`, `state.jobs`), а воркер исполняет её шаги в том же
процессе: `OPERATIONS[name](ctx)` через `asyncio.to_thread`. Реестр функций —
единственная точка, где имя становится вызовом, так что белого списка argv
больше не нужно. Исполнение — одно за раз (rebuild пересобирает derived
прогоном), но очередь честная: ставить можно несколько. Шаг имеет стадию, дорожку и статус (`pending|running|done|failed|cancelled`) и прогресс; `discover` собирает Instagram параллельно ветке 2GIS/сайтов (`collect.instagram`+`ig_*` рядом с `collect.reviews/sites/site_pages`), а `rebuild` остаётся отдельной стадией после всего сбора. Прогресс и лог идут
по SSE `GET /api/events` (события `snapshot`, `job`, `log`, `refresh`,
`activity`); хвост
лога добирается `GET /api/jobs/{id}?offset=`. Операции исполняются в потоке
(`asyncio.to_thread`), поэтому `events.publish` возвращается в цикл через
`call_soon_threadsafe`: `put_nowait` из чужого потока цикл не будит, и ждущий
подписчик просыпается только когда цикл проснётся сам. Отмена кооперативная: флаг в
RunContext, операция проверяет его в `check_cancelled`. Счётчики трёх систем —
`GET /api/stats` (`services/metrics.py`), журнал фоновой работы и пульс
демонов — `GET /api/activity` (`collector/routes/activity.py`: порог молчания
считает бэкенд, потому что интервалы тиков живут в `sender/config.toml`), воронка
холодной переписки — `GET /api/analytics` (`backend/analytics.py`: читает обе
базы в ro, считает тредами, отдаёт diagnosis и три разреза). Система 3 (транспорт, пул номеров и
очередь): пул живёт в той же `state.db` (таблицы `numbers`, `outbox`),
`sender/transport.py` — единственный модуль системы, ходящий в сеть (сторожит
`sender/tests/test_import_graph.py`), за швом — Node-сервис на Baileys
(`backend/sender/node`, отдельный процесс, порт 8788). В lifespan — часовой
монитор здоровья (`sender/routes/sender.py::monitor_numbers`), тик прогрева
(`warm_numbers`) и воркер очереди (`sender.send_queue` → `sender/services/worker.py`:
тик раз в 20 секунд, одна созревшая строка за тик, исходы в `TICK_OUTCOMES`):
прогрев работает при `autopilot = off`, потому что пишет
нашим же номерам, а не лидам. **Ни один из них не работает молча**: исход
каждого тика ложится в журнал `state.activity` (`backend/activity.py`, путь к
базе приходит швом `use()` из `collector/api.py` — модуль верхнего уровня, как
`config.py`, потому что писать в него обязаны и система 1, и система 3).
Подряд идущие одинаковые `(actor, outcome, subject)` схлопываются в `repeats`:
тик раз в 20 секунд иначе давал бы 4300 строк в сутки и топил бы в них
единственное важное событие. Решение агента по входящему (`answered`,
`escalated`, `closed`) ложится туда же актором `sender.tick`, а его причина —
ещё и в `threads.status_reason`: журнал чистится через 14 дней, а вопрос
«почему автомат отдал этот тред человеку» задаётся и через месяц. Пульс
демона — `MAX(last_at)` по актору, а не
переменная в памяти процесса: так он переживает перезапуск и не врёт после
него. Запись идёт своим соединением и **вне транзакции вызывающего** — след
«пытались, и вот что вышло» обязан пережить откат тика, а внутри чужого
`with db:` он ждал бы освобождения базы, которое наступит только после его же
возврата. Из `except` демона пишет `record_crash()`, единственное место, где
журнал молчит о собственной беде: исключение оттуда убило бы asyncio-задачу,
ради живучести которой этот `except` и стоит. Ретенция — 14 дней, чистится на
часовом тике монитора. Номер приводится к канонической форме на
границе — из этой строки Node собирает путь к каталогу сессии. Постановка в
очередь — `POST /api/sender/queue`, а не эндпоинт writer'а: кнопка оператора и
тик воркера при `autopilot = "full"` проходят через одну
`sender/services/queue.py`. Гейты перед каждой отправкой (`services/gates.py`),
порядок не косметический: отказ и закрытые треды отменяют строку, окно
10:00–18:00 Asia/Almaty, дневной лимит и джиттер — переносят. `sent_text` пишет
ровно один автор — воркер, после ответа транспорта (`sent: false` — ретрай с
backoff 1/5/30, неопределённость — `stuck` человеку, не слепой повтор).
`autopilot` переключается файлом `backend/sender/autopilot` (off | replies |
full, он же kill switch — на каждом тике читается заново); в режиме `full` тик
ставит в очередь не больше, чем пул успеет отправить за сутки. `POST
/api/sender/webhook` принимает статусы доставки (доставкой считаются ровно коды
3 и 4: Node форвардит каждый `messages.update`, а ack сервера, принятый за
доставку, ослепил бы детектор `min_delivered_rate`) и входящие: `kind: "incoming"`,
подпись заголовком `X-Sender-Secret` (пустой секрет из `SENDER_WEBHOOK_SECRET`
выключает проверку — так работает локальная разработка). Ручка делает только
быстрое и детерминированное: канонизует JID отправителя в `thread_id`, пишет
входящее в `messages` (`sent_text` сразу: ответ лида правкам не подлежит),
дедуплицирует по `provider_id`, одной транзакцией гасит расписание треда
(`outbox -> cancelled`, `next_touch_at = NULL`), проверяет стоп-слова регуляркой
из `config.toml` ([stopwords], ДО модели: классификатор вероятностный, а
проценты его ошибок приходятся ровно на тех, кто и жмёт Report), и отдаёт 200.
Стоп-слово закрывает тред в `closed_refused` и пишет отказ: `sender/services/refusal.py`
— шов, зарегистрированный из `collector/api.py` (отказ — юридический контур
F21, и система 3 не имеет права знать collector). Пустое `text` (голосовое, фото)
эскалирует и помечает сообщение `[медиа]`; ответ в `exhausted` возвращает тред
в `active`; ответ в `escalated` уходит человеку в Telegram, автомата не будит.
Всё остальное — на тике: `sender/services/incoming.py` зовёт агента-продавца
(`writer/services/seller.py`, `create_agent` с единственным инструментом
`classify(return_direct=True)`) и держит четыре предохранителя, и все четыре —
`if`: один автоответ на тред (`limits.auto_replies_per_thread`),
`recursion_limit = 2` (`seller.RECURSION_LIMIT`), эскалация вместо отправки на
длинный ответ или цифру с валютой, три попытки на входящее
(`messages.handle_attempts`). Ответ уходит через тот же `outbox` и те же гейты,
но по своему окну (`[window.reply]`, круглосуточно и семидневно): лид написал
сам и ждёт сейчас. Follow-up планируется сроком (`threads.next_touch_at`, ставит
`conversation.bump_touch` той же транзакцией, что расходует касание), а текст
рождается в момент срока (`sender/services/followup.py` →
`writer/services/followup.py`): текст, сгенерированный заранее, пролежал бы в
очереди десять дней и ушёл устаревшим. Каденция `[3, 7]` и `max_touches` живут
только в `sender/config.toml`. Ответ лиду пишет один автор — seller: старый
одноходовый `agent.REPLY` удалён, ход `kind='reply'` в инбоксе зовёт того же
агента, что отвечает автоматически (промпт два раза на одну ситуацию дал бы
две калибровки).
Уникальность в `outbox` — по живым строкам, а не по всем: кончившуюся в
`failed`/`stuck` строку оператор имеет право поставить заново, иначе транспортная
авария хоронила бы лида навсегда. Статус
переписки с лидом в базу
collector'а не возвращается — переписку ведёт система 2 в той же `state.db`;
обратно сюда приходит только отказ (suppression). Прогресс шага — `ctx.progress(current,
total, label)` из операций, а не парсинг вывода.

## Архитектура writer/ (система 2)

Тот же venv, что у collector'а (`backend/pyproject.toml`), но свой пакет и
своя база: `writer.*` не импортирует `collector.*` напрямую, только через шов
в `collector/api.py`. Читает
`collector/data/derived.db` и **никогда** в неё не пишет — отбор кандидатов (F19,
F21) воспроизведён запросом в `leads_source.py`, а не импортом `report.py`,
чтобы система 2 не падала от чужих зависимостей. Цена — вторая копия правил,
её держит честным раздел `leads` в `writer/tests/`.

```bash
cd backend
uv run pytest writer/tests/    # schema, threads, leads, prompt, operations
```

«Черновики топ-N» — не скрипт, а операция очереди `writer.outreach`
(пайплайн `write`): кнопка «Черновики топ-N» на странице «Холодные» (`/cold`)
или `POST /api/pipeline/write`. Она всегда обрабатывает N **новых**
компаний: у кого тред уже есть, того пропускает и идёт дальше по списку.

**`state.threads`/`state.messages` — невосстановимый слой**, как `data/raw/` у
системы 1: схема создаётся через `CREATE TABLE IF NOT EXISTS`, `DROP` запрещён,
в git не лежит. Переписка живёт в той же `state.db`, что отказы и ответы модели.
Этап переписки живёт в `threads.stage`, а вариант оффера — в `messages.offer_variant`.

**Черновик ≠ отправленное.** Ответ модели ложится в `messages.draft_text`, а в
историю треда попадает только `sent_text` — то, что оператор реально отправил
после правки. Между ними ещё `messages.queued_text`: что подтвердил оператор
нажатием «Поставить в очередь», но что транспорт ещё не подтвердил. Историей
агента считается ровно `WHERE sent_text IS NOT NULL`:
иначе следующий ход строился бы на сообщении, которого лид не получал. Разница
draft/sent — единственная бесплатная разметка для калибровки промпта.

**Промпт хранится, а не реконструируется.** `agent.draft()` возвращает
`Attempt(draft, prompt, model)`, и полный запрос ложится в `messages.prompt`
(json пар `[["system", …], ["human", …]]`) рядом с ответом. Пересобрать его
задним числом нельзя: `seed` и история к тому времени другие, а текст писался
по тогдашним — это был бы не аудит, а догадка. Отдаётся отдельной ручкой
`GET /api/threads/{company_id}/messages/{message_id}/prompt`: промпт весит
2–4 КБ, и в каждой строке инбокса он не нужен. Черновики старше этой правки
промпта не имеют, и интерфейс говорит об этом прямо, а не рисует прочерк.
Автоответ продавца собирает `create_agent`, его промпт остаётся в Langfuse.

**Ни langgraph, ни чекпойнтера.** Инструментов у агента нет, на ход нужен один
вызов со structured output — `agent.py` это три функции и `with_structured_output`,
как в `analyze.py`. Чекпойнтер дописывал бы неподтверждённый черновик в
состояние сам и дал бы второй источник правды рядом с `messages`.

**Запрет пустых follow-up — в схеме, а не в промпте** (`schemas/outreach.py`):
`with_structured_output` повторит вызов на невалидном ответе, а инструкция в
промпте была бы просьбой, которую модель нарушает именно там, где важно.

**Веб — та же консоль.** `collector/api.py` монтирует роутер `writer/routes/threads.py`
(`/api/threads/*`: инбокс `GET /api/threads` одной сводкой + карточка, ходы
`draft/queue/incoming`, плюс `GET /api/threads/drafts` — очередь проверки
первых писем), фронтенд получает две страницы: `/cold` — конвейер холодных
черновиков, `/threads` — инбокс диалогов. Карточка лида композера не несёт:
переписка живёт на своих страницах, а не второй копией внутри неё. Эндпоинт `/draft` ходит в сеть, поэтому ключ
должен быть в `backend/.env` (см. `config.py` выше):

```bash
cd backend && uv run python main.py
```

Без ключа `/draft` отвечает 503 с инструкцией задать его в `backend/.env`, а
не трассировкой. Отказ
по-прежнему оформляется эндпоинтом collector'а: вторая точка входа в
юридический контур — второй шанс разойтись с `state.suppression`.

## Архитектура frontend/ (дашборд)

Продуктовый дашборд: семь страниц по сущностям, а не по границам систем.
`/` — счётчики и последние запуски; `/leads` — выдача, клик ведёт на
`/leads/[id]` (карточка: разбор скора, досье модели, сырьё, ответы модели);
`/cold` — конвейер проверки первых писем с полным промптом; `/threads` —
инбокс диалогов с видом касания; `/analytics` — воронка холодной переписки и
три разреза; `/sender` — пул номеров, очередь и kill switch; `/activity` —
«Процессы»: демоны и живой журнал фоновой работы.
Содержимое целиком приезжает с бэкенда, не хардкодом: пороги молчания демонов
(`silent_after_seconds`), порядок инбокса и календарь прогрева считает бэкенд.

**Одна SSE-подписка на всё дерево.** `components/live.tsx` (LiveProvider)
держит EventSource, кладёт в контекст снапшот, апсерт джоб, хвост лога,
`activityTail` (живая лента журнала) и `refreshTick` (сигнал спискам перечитать
данные после пересборки базы). Страницы не открывают своих стримов.

**SSE ходит на API-оригин напрямую, минуя rewrite next** (`.env.local`:
`NEXT_PUBLIC_API_ORIGIN`): dev-прокси Next отдаёт заголовки стрима, но
буферизует тело бесконечного ответа — события до страницы не доезжают.
JSON-эндпоинтам прокси не мешает. Сломается снова — первым делом проверь,
куда смотрит EventSource.

**Активная джоба видна отовсюду.** `components/RunBar.tsx` — полоска в
`layout.tsx` над всем деревом: заголовок, шаг, ссылка на «Процессы», «прервать».
Возвращает `null`, когда очередь свободна.

**Дизайн-система** (`globals.css`, токены в `:root`): белый канвас, чёрный
CTA, Inter + JetBrains Mono (`next/font`), hairline-границы, тёмные блоки
только у кода и лога. Тема светлая и залочена — инверсий секций нет. Иконки —
`@phosphor-icons/react`, свои SVG не рисуем.

## Слои данных

- **`data/raw/`** — невосстановимое сырьё: `<sha1(url)>.html.gz` + сайдкар
  `<sha1(url)>.json` (`url`, `final_url`, `status`, `fetched_at`). Не в git
  (гигабайты), бэкапится отдельно. `final_url` — единственный честный признак
  того, что источник молча подменил страницу.
- **`fixtures/`** — по одной эталонной странице на каждый разбор в
  `services/sources.py`, снята из `raw/`, лежит в git. Числа в `test_parsers`
  — свойства именно этих файлов; расхождение значит, что поехал разбор, а не
  что источник поменял вёрстку.
- **`data/derived.db`** — пересобираемое: `runs`, `current_run`, `*_all` + view
  (`db/schema.sql`, DERIVED-часть). Собирается прогонами `rebuild.run`, в git
  не лежит. View создаются через `CREATE VIEW IF NOT EXISTS`: любой DDL внутри
  `connect()` забирал бы блокировку записи у идущей пересборки. Цена — правка
  определения не доедет до созданной базы сама; расхождение ловит
  `test_views_match_schema_file`, чинится `DROP VIEW`.
- **`data/state.db`** — невосстановимое: отказы, джобы, переписка системы 2
  (включая `messages.prompt`/`model` — полный запрос, ушедший в модель),
  ответы модели (`db/schema.sql`, STATE-часть) и журнал фоновой работы
  (`activity`). Схема через `CREATE TABLE IF NOT EXISTS`, `DROP` запрещён, в
  git не лежит. Открывается collector'ом через `store.connect()` (ATTACH),
  writer'ом — напрямую, журналом — своим коротким соединением на запись.
  Владелец таблицы доливает свои колонки сам (`thread_store` — `prompt`/`model`,
  `sender/db/migrate.py` — состояние треда): полагаться на то, что до базы
  добежит чужая миграция, значит падать везде, где та система не стартовала.
  Обратное тоже верно — чужую колонку не доливают, а терпят её отсутствие
  (`inbox` без `threads.status`, `history` без `outbox`): колонка, созданная
  раньше владельца, украла бы у его миграции разметку старых строк.
- **`backend/sender/node/sessions/`** — невосстановимый слой: ключи сессий
  WhatsApp (сохранение сессии — «детка», потерянная ключ — переподключение
  телефоном). Не в git, в тот же бэкап, что `state.db`.
- **`data/leads.csv`** — производная от базы, печатает `export.run`.

## Песочница (`backend/sandbox/`)

Стенд, на котором аутрич проходится целиком без единого сообщения в WhatsApp.
Включается флагом `SANDBOX=1` в `backend/.env` вместе с
`SENDER_NODE_URL=http://127.0.0.1:8787/api/sandbox/node`; без флага роутеров
песочницы в приложении нет вовсе, а с флагом и боевым адресом транспорта
приложение не стартует — тестовый прогон, ушедший живым людям, дороже падения.

**Прогон — отдельный файл базы** (`data/sandbox/<run>.db`): `thread_id` — это
номер телефона и первичный ключ `threads`, поэтому второй прогон по тому же
лиду упёрся бы в занятый ключ, а «чистый контекст» пришлось бы изображать
фильтром в пяти местах. Схему наполняют настоящие владельцы таблиц, `derived.db`
остаётся боевой и только на чтение: лид в прогоне — настоящая компания.

**`backend/paths.py`** — единственный владелец пути к невосстановимому слою на
три системы (раньше его называли пять мест). **`backend/clock.py`** — «сейчас»
продукта; шесть мест зовут его вместо `datetime.now`. Смещение принадлежит
прогону и лежит в его базе: иначе прогон, начатый со сдвигом «+3 дня», после
перезапуска бэкенда откатился бы назад во времени. В бою смещение нулевое.

**Подменный Node** (`sandbox/node.py`) отдаёт те же пять ручек, что
`sender/node/index.js`, и события шлёт в свой же `POST /api/sender/webhook` —
тем же HTTP, с тем же секретом, через тот же дедуп. Статус доставки уезжает
фоном с повтором, пока вебхук не подтвердит: без сети он обогнал бы запись
`provider_id` в `outbox`. Четыре тумблера аварий (`sandbox/faults.py`) живут в
памяти процесса — авария, пережившая перезапуск, объяснялась бы потом полдня.

Песочница знает про все три системы; её не импортирует никто, кроме сборщика
`collector/api.py` — сторожит `sandbox/tests/test_import_graph.py`.

## Документация

`docs/BRD.md`, `docs/PRD.md`, `docs/TRD.md`, `docs/SPEC.md`,
`docs/ARCHITECTURE.md`, `docs/ARCHITECTURE_v2.md` — бизнес-контекст, ICP,
полная спецификация и обоснование схемы (`ARCHITECTURE_v2.md` §4 — источник
для `db/schema.sql`). Комментарии в коде вида «Ф3», «Ф9», «F19» — ссылки на
фазы и требования из этих документов.

---

## Code Style & Architecture

**Naming**: intention-revealing names for files, modules, functions, vars, classes — name must match what the thing _actually does_, not what it was originally meant to do (`getUserOrders` not `getData`, `isEmailVerified` not `flag`; rename `processing.py` → `stripe_api_helpers.py` if that's what it wraps). If a function's behavior drifts from its name during a refactor, rename it — don't leave the name stale.

**Functions**: SRP, ≤20-30 lines, 0-2 params (3+ → DTO/options object), no boolean flags (split into separate functions), no hidden side effects, guard clauses over nesting. A function/module should have exactly one reason to change — if a module handles two domains (e.g. payments + accounting), split it; cross-domain imports between two "single-purpose" modules is a smell that they aren't actually separated.

**Classes**: small, high cohesion, SRP. Objects = behavior + hidden state; DTOs = pure data, zero behavior. Wrap third-party APIs/DBs/HTTP clients behind adapters — never let vendor types leak into domain layer. Inject dependencies via constructor, don't instantiate internally.

**Errors**: throw exceptions, not error codes/flags/`Optional` return-and-check. A function should either always return a valid value or raise — never return `T | None` to push the null-check onto every caller; that cascades into `Optional` chains up the call stack. No silent null return/pass — use guard clauses, empty collections, or explicit handling. Keep try/catch out of happy-path logic.

**Comments**: explain _why_, never _what_ (e.g. "need this timestamp to filter recent intents" not "compute timestamp"); if code needs a what-comment, rewrite the code. Allowed only for legal notices, non-obvious algorithms, TODO/FIXME. Zero dead code — delete unused/commented-out code immediately.

**Law of Demeter**: don't drill into a nested object's internals across module boundaries to extract one field (e.g. reaching into a `financial_statement` response 3 levels deep for a `mutation_id`). Instead, have the owning function return only what the caller needs; pull cross-cutting data (e.g. `application_fee`) into the primary domain object itself (as a field/custom attribute) rather than passing two parallel objects (`invoice` + `invoice_data`) everywhere just so callers can dig through both.

**Domain modeling**: before splitting a "god function," understand the domain well enough to see which data naturally belongs on which entity — the right refactor is usually a data-model fix (add a field to the entity) not just a function split. Bring outside/fresh technical judgment on top of domain knowledge — don't just implement what a domain expert dictates without a critical technical pass.

**File structure**: newspaper metaphor (high-level → low-level top to bottom), 100-500 lines/file max, follow linter/formatter.

**Testing**: FIRST principles (Fast, Independent, Repeatable, Self-validating, Timely), same quality bar as prod code.

**Refactoring**: Boy Scout Rule — leave code cleaner than found. Make it work → make it clean, in small incremental steps, not big-bang rewrites.

## Python-Specific

- Type-hint every function signature: params and return type (`def get_user(user_id: int) -> User | None`). No untyped `def`.
- Use `dataclass` or `pydantic.BaseModel` for DTOs, never raw dicts/tuples passed across boundaries.
- Prefer `Enum`/`Literal` over raw strings for fixed value sets.
- Use `pathlib.Path`, not string paths.
- No mutable default args (`def f(items: list = [])` → use `None` + guard, or `field(default_factory=list)`).
- Prefer f-strings over `%`/`.format()`.
- Use `@property` for computed attributes, not getter methods.
- Raise custom exception classes (`class OrderNotFoundError(Exception)`), not bare `Exception`/`ValueError` for domain errors.
- Use context managers (`with`) for resources (files, connections, locks) — never manual open/close.
- Prefer list/dict comprehensions over manual loops for simple transforms; drop back to a loop once logic needs branching/side effects.
- Use `is None`/`is not None`, never ` == None`.
- Avoid `*args`/`**kwargs` in public APIs unless genuinely variadic — hides the contract.
- One class = one file for domain entities; group small related DTOs together.
- Run `mypy`/`ruff` (or project equivalent) — treat type/lint errors as build failures, not warnings.
- Docstrings only for public functions/classes with non-obvious behavior (Google or NumPy style) — describe _why_/_contract_, not restate the type hints.

## Debugging Protocol

- State root cause in one line before touching code.
- Fix at source-of-truth/owner layer, never patch symptoms in child layers (no fallbacks/duplicated logic there).
- Before fixing: trace top-down (route→page→container→state) and bottom-up (function→hook→service→API→DB).
- Diagnose in order: data/contracts → business logic → async/timing → UI state → integration → architecture.
- When changing a mechanic, update all coupled layers together: contracts, handlers, queries, cache, serializers, loading/error states.
- Justify why other layers are unaffected before a one-file fix.
- If not reliably reproducible, write a failing test first, then fix.
- Re-architecture requires explicit scope/risk/compatibility/rollout plan.
- Two tools must never share a side-effect surface — split the lighter tool's concern off so the heavy one stays one-per-lifecycle.

## Self-Improvement Protocol

- After a correction/successful fix: add one imperative-sentence rule about the _error class_, not the incident, to the right section.
- Search existing rules first; refine instead of duplicating.
- Contradicting rules → flag and ask which to keep.
- Keep this file under 2500 tokens; merge/delete overlapping or stale rules when exceeded.
- Weekly: audit which rules never fired or overlap, report findings.

## File Hygiene

- One dense sentence per rule, grouped by section (Architecture, Style, Bug Fix, Self-Improvement, Project-specific).
- After code changes, update CLAUDE.md/BRD/PRD/TRD/SPEC docs accordingly.
