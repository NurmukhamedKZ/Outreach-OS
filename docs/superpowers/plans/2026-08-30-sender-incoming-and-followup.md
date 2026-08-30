# Sender, часть 3: входящие, агент-продавец и follow-up — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Замкнуть кольцо продукта: ответ лида приезжает в тред, запланированные follow-up этого треда отменяются, агент один раз отвечает своими словами или закрывает тред классификацией, а молчащий лид получает касания по каденции `[+3д, +7д]` и уходит в `exhausted` после третьего.

**Architecture:** Вебхук делает только быстрое и детерминированное (секрет, разбор адресата, дедуп, запись, отмена расписания, стоп-слова) и отвечает 200 за миллисекунды. Всё медленное и вероятностное — вызов агента-продавца и генерация follow-up — живёт в том же тике воркера, что и отправки: последовательность бесплатно снимает гонки между вебхуком, агентом и очередью. Агент — `create_agent` из langchain с единственным инструментом `classify(return_direct=True)`; поверх него четыре предохранителя, и все четыре — `if`, ни один не промпт. Ответ агента уходит через тот же `outbox` с теми же гейтами, что холодное касание, — путь отправки в системе ровно один.

**Tech Stack:** Python 3.13, FastAPI, `sqlite3`, `re`/`json`/`zoneinfo`/`datetime` (stdlib), `tomllib`, `langchain 1.3.15` + `langgraph 1.2.11` (уже в venv, `create_agent` приезжает с ними), `langchain-openrouter`, pytest + pytest-asyncio (`asyncio_mode = "auto"`). Новых зависимостей часть не добавляет — проверено импортом `from langchain.agents import create_agent`.

**Spec:** `docs/superpowers/specs/2026-08-28-sender-incoming-and-followup-design.md`
(зонтик: `docs/superpowers/specs/2026-08-23-sender-whatsapp-design.md`;
часть 1: `docs/superpowers/specs/2026-08-28-sender-transport-and-numbers-design.md`;
часть 2, обязательная предшественница: `docs/superpowers/specs/2026-08-28-sender-outbox-worker-design.md`)

## Global Constraints

- **`sender/transport.py` — единственная точка выхода в сеть системы 3.** `sender/tests/test_import_graph.py` сторожит статически: ни один модуль `sender/`, кроме `transport.py` и `notify.py`, не имеет права импортировать `httpx`, `requests`, `urllib.request`, `scrapling`. Вызовы модели идут через `writer/services/*`, которые в этот граф не входят.
- **Новых таблиц эта часть не создаёт.** `outbox` и `numbers` пришли с частью 1. Колонки — только через `sender/db/migrate.py::ensure_column()`. `DROP` в `state.db` запрещён (невосстановимый слой): таблицы и данные не пересоздаются никогда.
- **Функции `sender/db/outbox.py` и `sender/db/conversation.py` не коммитят.** Коммитит вызывающий через `with db:`. Так выполняется правило 2 зонтичной спеки: `threads.status` меняется в одной транзакции с записью в `messages`/`outbox`. (`sender/db/numbers.py` и `writer/db/thread_store.py` коммитят внутри — их конвенцию не трогаем, но в транзакциях системы 3 их писать нельзя.)
- **«Сейчас» — аргумент, не `datetime.now()` внутри.** `now: datetime` параметром у всех функций гейтов, очереди, воркера, входящих и follow-up; `now()` зовётся один раз на границе (роутер, цикл).
- **Часовой пояс.** В базе всё UTC, ISO-8601 с точностью до секунд (`timespec="seconds"`), как `outbox.stamp`. `Asia/Almaty` из конфига используется ровно в одном месте — гейте окна отправки.
- **`sender/config.toml` — единственное место конфигурации системы 3.** Значения копируются дословно: `[cadence] follow_up_days = [3, 7]`, `max_touches = 3`; `[limits] auto_replies_per_thread = 1`, `max_reply_chars = 600`, `max_handle_attempts = 3`; `[window] timezone = "Asia/Almaty"`, `hours = [10, 18]`, `weekdays = [1, 2, 3, 4, 5]`; `[window.reply] hours = [0, 24]`, `weekdays = [1, 2, 3, 4, 5, 6, 7]`; `[pace] tick_seconds = 20`.
- **`autopilot` читается каждым тиком заново.** Kill switch, который надо перезапускать процессом, — не kill switch. Режимы: `off` (черновики, ничего не уходит) | `replies` (ответы в диалоге уходят сами) | `full` (всё).
- **Из `escalated` автоматического выхода нет.** Единственный предохранитель между моделью и живым разговором о деньгах. Ни один код этой части не имеет права вернуть тред из `escalated`.
- **Статусы треда:** `queued | active | exhausted | escalated | unreachable | closed_refused | closed_junk | blocked_channel`. `AUTOMATON_STOPS` = `escalated, unreachable, exhausted, closed_refused, closed_junk`.
- **Отмена и перенос — разные вещи.** `cancelled` — «это сообщение уже не нужно», перенос `send_after` — «нужно, но не сейчас».
- **Тесты без сети и без модели.** `FakeTransport` из `sender/tests/conftest.py`, агент заглушается монкипатчем модульной функции, «сейчас» — константа `NOW` / `INSIDE`. Живой WhatsApp — ручной смоук-чеклист Task 18, не pytest.
- **Прогон тестов:** `cd backend && uv run pytest` (все три системы). Раздел одним файлом: `uv run pytest sender/tests/test_incoming.py -v`.

## Решения, принятые в brainstorming 2026-08-30

Полностью изложены в §0 спеки, здесь — сжато, потому что план на них ссылается:

1. **Подтверждения на стоп-слово нет.** Гейт треда отменил бы строку в outbox — пункт спеки был физически невыполним.
2. **Отказ пишется швом из `collector/api.py`.** Система 3 не имеет права импортировать collector.
3. **У ответа в диалоге своё окно** `[window.reply]`, по умолчанию круглосуточное и семидневное.
4. **Обработка входящего — `sender/services/incoming.py`, в том же тике воркера.**
5. **Каденция живёт только в `sender/config.toml`**; `[cadence]` из `writer/config.toml` удаляется (кодом он не читается — проверено `grep`).
6. **Seller — единственный автор ответа лиду**; `agent.REPLY` и ход `kind='reply'` старого агента удаляются.
7. **Секрет вебхука закрывается здесь же.**

## Отступления от буквы спеки — и почему

- **Вебхук пишет входящее через `conversation.add_incoming` (sender), а не `thread_store.add_incoming` (writer).** Спека называет второе, но функция writer'а коммитит внутри, а запись входящего обязана лечь в одну транзакцию с отменой расписания: падение между двумя коммитами дало бы тред, где ответ лида записан, а follow-up через три дня всё ещё запланирован — ровно то издевательство, ради предотвращения которого пункт и написан. `thread_store.add_incoming` при этом всё равно учится принимать `provider_id` (Task 5): ручной ввод оператора обязан ложиться той же формой, иначе у одной таблицы будет два формата строки.
- **Шов отказа — регистрация (`sender/services/refusal.py::use()`), а не параметр через воркер.** Отказ нужен двоим: вебхуку (стоп-слово) и тику (`classify(refusal)`). Вебхук — ручка FastAPI, ей параметр не передать; два разных механизма на один шов хуже одного модуля с явной регистрацией из `collector/api.py`. Модуль отдаёт `False` и пишет `log.error`, если его не зарегистрировали, — молчащий шов был бы потерянным отказом.
- **Предохранитель попыток считает «заходы», а не «исключения».** `messages.handle_attempts` растёт **перед** вызовом агента, а не в `except`: процесс, убитый посреди вызова, иначе не потратил бы попытку и оставил бы ту же вечную пробку. Цена — прерванный `Ctrl+C` стоит попытки, и это правильная цена.
- **`bump_touch` получает весь `config["cadence"]`, а не `max_touches`.** Ему теперь нужен и `follow_up_days`, чтобы поставить `next_touch_at` той же транзакцией, что израсходовать касание. Два аргумента вместо словаря дали бы 3 параметра — против правила 0-2 из CLAUDE.md.

---

## Task 1: Конфиг и миграция — окно ответа, стоп-слова, вид строки, счётчик попыток

**Files:**
- Modify: `backend/sender/config.toml`
- Modify: `backend/sender/db/migrate.py:64-101`
- Test: `backend/sender/tests/test_config.py`, `backend/sender/tests/test_migrate.py`

**Interfaces:**
- Consumes: `migrate.ensure_column(db, table, column, ddl) -> bool`, `migrate.apply(db)`, `config.load() -> dict` (часть 1).
- Produces: `config.load()["window"]["reply"]` (`hours`, `weekdays`), `config.load()["stopwords"]["patterns"]` (список фрагментов регулярки), `config.load()["limits"]["max_handle_attempts"]`; в `state.db` — колонки `outbox.kind` (`TEXT NOT NULL DEFAULT 'cold'`) и `messages.handle_attempts` (`INTEGER NOT NULL DEFAULT 0`).

- [ ] **Step 1: Написать падающие тесты**

Дописать в `backend/sender/tests/test_config.py`:

```python
def test_reply_window_is_round_the_clock_by_default():
    """Лид написал сам и ждёт сейчас; молчание пятнадцать часов убивает диалог.
    Окно всё же параметром, а не отсутствием проверки: сузить его потом —
    правка конфига, а не кода."""
    window = config.load()["window"]
    assert window["reply"]["hours"] == [0, 24]
    assert window["reply"]["weekdays"] == [1, 2, 3, 4, 5, 6, 7]
    # Часовой пояс у окна ответа свой не заводится: он свойство человека на том
    # конце, а не вида сообщения.
    assert "timezone" not in window["reply"]


def test_stopwords_are_configuration_not_code():
    """Список правит человек без программиста — как окна и каденцию."""
    patterns = config.load()["stopwords"]["patterns"]
    assert "отпиш" in patterns
    assert all(isinstance(pattern, str) and pattern for pattern in patterns)


def test_handle_attempts_limit_is_configured():
    assert config.load()["limits"]["max_handle_attempts"] == 3
```

Дописать в `backend/sender/tests/test_migrate.py`:

```python
def test_outbox_learns_the_kind_of_row(db):
    """Гейт выбирает окно по виду строки, а дашборд перестаёт показывать
    холодное касание и ответ в диалоге одинаковыми."""
    migrate.apply(db)
    assert "kind" in columns(db, "outbox")
    db.execute("INSERT INTO outbox (our_number, send_after, status, created_at,"
               " updated_at) VALUES ('+77001112233', ?, 'pending', ?, ?)",
               ("2026-09-02T12:00:00+00:00",) * 3)
    db.commit()
    assert db.execute("SELECT kind FROM outbox").fetchone()[0] == "cold"


def test_messages_learn_the_attempt_counter(db):
    """Входящее, на котором агент упал, иначе стало бы вечной пробкой."""
    conversation_tables(db)
    migrate.apply(db)
    assert "handle_attempts" in columns(db, "messages")
```

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `cd backend && uv run pytest sender/tests/test_config.py sender/tests/test_migrate.py -v`
Expected: FAIL — `KeyError: 'reply'`, `KeyError: 'stopwords'`, `assert 'kind' in columns(...)`.

- [ ] **Step 3: Дописать `backend/sender/config.toml`**

После секции `[window]` (строки 11–14) добавить подтаблицу:

```toml
# Окно ответа в диалоге. Круглосуточно и всю неделю: лид написал сам и ждёт
# сейчас, а ответ, пришедший через пятнадцать часов, — это уже не диалог.
# Джиттер и дневной лимит номера при этом остаются: они защищают номер, а не
# покой лида. Часовой пояс наследуется от [window].
[window.reply]
hours    = [0, 24]
weekdays = [1, 2, 3, 4, 5, 6, 7]
```

В секции `[limits]` (строки 24–26) добавить строку:

```toml
# Сколько раз тик пробует обработать одно входящее, прежде чем отдать тред
# человеку. Три, а не одна: сетевой таймаут не повод отдавать живой тред в
# ручной режим навсегда.
max_handle_attempts     = 3
```

В конец файла добавить секцию:

```toml
# Стоп-слова проверяются регуляркой ДО модели. Классификатор вероятностный, и
# его проценты ошибок приходятся ровно на раздражённые короткие сообщения — то
# есть на тех, кто и жмёт Report. Фрагменты, а не слова целиком: «отпиш»
# закрывает и «отпишите», и «отпишитесь».
[stopwords]
patterns = [
  "не пиши",
  "не писать",
  "отпиш",
  "удалите мой номер",
  "удалите номер",
  "спам",
  "жалоб",
  "отстань",
]
```

- [ ] **Step 4: Дописать `backend/sender/db/migrate.py`**

В `apply()` рядом с `ensure_column(db, "outbox", "recipient", "TEXT")` добавить:

```python
    # Вид строки: cold | followup | reply | warmup. Нужен гейту (у ответа в
    # диалоге своё окно) и дашборду, где холодное касание и ответ сейчас
    # неразличимы. DEFAULT 'cold' — то, чем были все строки до этой части.
    ensure_column(db, "outbox", "kind", "TEXT NOT NULL DEFAULT 'cold'")
```

В кортеж `CONVERSATION_COLUMNS` добавить строку:

```python
    ("messages", "handle_attempts", "INTEGER NOT NULL DEFAULT 0"),
```

- [ ] **Step 5: Прогнать тесты**

Run: `cd backend && uv run pytest sender/tests/test_config.py sender/tests/test_migrate.py -v`
Expected: PASS.

- [ ] **Step 6: Прогнать весь пакет — миграция трогает общую базу**

Run: `cd backend && uv run pytest -q`
Expected: PASS, ноль падений.

- [ ] **Step 7: Коммит**

```bash
git add backend/sender/config.toml backend/sender/db/migrate.py backend/sender/tests/test_config.py backend/sender/tests/test_migrate.py
git commit -m "feat(sender): окно ответа, стоп-слова, вид строки и счётчик попыток"
```

---

## Task 2: Гейт выбирает окно по виду строки

**Files:**
- Modify: `backend/sender/services/gates.py:35-76`
- Modify: `backend/sender/services/worker.py:83-96`
- Test: `backend/sender/tests/test_gates.py`

**Interfaces:**
- Consumes: `config.load()["window"]["reply"]` (Task 1).
- Produces: `gates.Attempt(thread_status, suppressed, number_status, capacity, last_sent_at, jitter_minutes, kind)` — седьмое поле обязательное; `gates.window_of(kind: str, config: dict) -> dict`; `gates.REPLY_KINDS = ("reply",)`.

- [ ] **Step 1: Написать падающие тесты**

Дописать в `backend/sender/tests/test_gates.py`:

```python
def test_a_reply_goes_out_at_night():
    """Лид ответил в 21:00 — ответ уходит сейчас, а не завтра в десять.
    Молчание пятнадцать часов убивает диалог."""
    night = datetime(2026, 9, 2, 20, 0, tzinfo=timezone.utc)   # 02:00 в Алматы
    decision = gates.check(attempt(kind="reply"), night, CONFIG)
    assert decision.action == gates.SEND, decision


def test_a_cold_touch_at_the_same_moment_is_postponed():
    """Тот же момент, другой вид строки — и решение обязано быть другим."""
    night = datetime(2026, 9, 2, 20, 0, tzinfo=timezone.utc)
    decision = gates.check(attempt(kind="cold"), night, CONFIG)
    assert decision.action == gates.RESCHEDULE
    assert decision.blame == gates.WINDOW


def test_a_followup_obeys_the_cold_window():
    """Follow-up будит молчащего лида — он ничего не ждёт, и ночью его не трогают."""
    night = datetime(2026, 9, 2, 20, 0, tzinfo=timezone.utc)
    assert gates.check(attempt(kind="followup"), night, CONFIG).blame == gates.WINDOW


def test_reply_window_inherits_the_timezone():
    """Своего часового пояса у окна ответа нет: он свойство человека на том конце."""
    window = gates.window_of("reply", CONFIG)
    assert window["timezone"] == CONFIG["window"]["timezone"]
    assert window["hours"] == [0, 24]


def test_a_reply_still_obeys_the_daily_limit_of_the_number():
    """Окно защищает покой лида, лимит — номер. Второе не отменяется первым."""
    night = datetime(2026, 9, 2, 20, 0, tzinfo=timezone.utc)
    decision = gates.check(attempt(kind="reply", capacity=0), night, CONFIG)
    assert decision.action == gates.RESCHEDULE
    assert decision.blame == gates.NUMBER
```

В том же файле привести фабрику `attempt(...)` к новому полю — если её ещё нет, добавить:

```python
def attempt(*, thread_status="active", suppressed=False, number_status="active",
            capacity=5, last_sent_at=None, jitter_minutes=0.0, kind="cold"):
    return gates.Attempt(thread_status=thread_status, suppressed=suppressed,
                         number_status=number_status, capacity=capacity,
                         last_sent_at=last_sent_at, jitter_minutes=jitter_minutes,
                         kind=kind)
```

и заменить в существующих тестах файла ручные конструкторы `gates.Attempt(...)` на вызовы `attempt(...)` — иначе они упадут на недостающем поле.

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `cd backend && uv run pytest sender/tests/test_gates.py -v`
Expected: FAIL — `TypeError: Attempt.__init__() got an unexpected keyword argument 'kind'`.

- [ ] **Step 3: Реализовать выбор окна в `backend/sender/services/gates.py`**

Добавить рядом с константами blame:

```python
# Виды строк, у которых своё окно. Ответ в диалоге — единственный, у кого на
# том конце кто-то ждёт прямо сейчас; холодное касание и follow-up будят
# человека сами и ночью этого не делают.
REPLY_KINDS = ("reply",)
```

В `Attempt` добавить поле последним:

```python
    kind: str
```

Добавить функцию сразу под `check`:

```python
def window_of(kind: str, config: dict) -> dict:
    """Окно, по которому живёт строка этого вида. Секция [window.reply]
    переопределяет часы и дни, но не часовой пояс: он свойство человека на том
    конце, а не вида сообщения."""
    window = config["window"]
    if kind not in REPLY_KINDS:
        return window
    return {**window, **window["reply"]}
```

В `check` заменить два обращения к `config["window"]`:

```python
    window = window_of(attempt.kind, config)
    window_opens = next_window_start(now, window)
    if window_opens > now:
        return Decision(RESCHEDULE, "вне окна отправки", window_opens, WINDOW)

    tomorrow = next_window_start(_tomorrow(now, window), window)
```

- [ ] **Step 4: Передать вид строки из воркера**

В `backend/sender/services/worker.py::_decide` добавить в конструктор `gates.Attempt`:

```python
        kind=row["kind"],
```

- [ ] **Step 5: Прогнать тесты**

Run: `cd backend && uv run pytest sender/tests/test_gates.py sender/tests/test_worker.py -v`
Expected: PASS.

- [ ] **Step 6: Коммит**

```bash
git add backend/sender/services/gates.py backend/sender/services/worker.py backend/sender/tests/test_gates.py
git commit -m "feat(sender): у ответа в диалоге своё окно отправки"
```

---

## Task 3: `outbox` — вид строки при постановке и отмена расписания

**Files:**
- Modify: `backend/sender/db/outbox.py:15-33`
- Modify: `backend/sender/services/warmup.py:128-132`
- Test: `backend/sender/tests/test_outbox.py`

**Interfaces:**
- Consumes: колонка `outbox.kind` (Task 1).
- Produces: `outbox.put(db, message_id, thread_id, our_number, now, kind="cold") -> int`; `outbox.cancel_scheduled(db, thread_id, reason, now) -> int` (сколько живых строк треда погашено); `outbox.FIELDS` включает `kind`.

- [ ] **Step 1: Написать падающие тесты**

Дописать в `backend/sender/tests/test_outbox.py`:

```python
def test_kind_defaults_to_cold_and_is_readable_back(db):
    outbox_id = outbox.put(db, 1, "+77010000001", "+77001112233", NOW)
    db.commit()
    row = outbox.due(db, NOW)
    assert row["outbox_id"] == outbox_id and row["kind"] == "cold"


def test_a_reply_row_carries_its_kind(db):
    outbox.put(db, 1, "+77010000001", "+77001112233", NOW, kind="reply")
    db.commit()
    assert outbox.due(db, NOW)["kind"] == "reply"


def test_cancel_scheduled_kills_the_live_rows_of_one_thread(db):
    """Лид ответил, и «напоминаю о своём сообщении» через три дня станет
    издевательством. Именно эта строка превращает систему в ту, на которую
    жалуются."""
    ours = outbox.put(db, 1, "+77010000001", "+77001112233", NOW, kind="followup")
    stranger = outbox.put(db, 2, "+77010000009", "+77001112233", NOW)
    db.commit()

    with db:
        killed = outbox.cancel_scheduled(db, "+77010000001", "лид ответил", NOW)

    assert killed == 1
    assert status_of(db, ours) == "cancelled"
    assert status_of(db, stranger) == "pending", "погашена чужая строка"


def test_cancel_scheduled_does_not_touch_what_already_went_out(db):
    """Отправленное отменить нельзя: лид его уже получил."""
    outbox_id = outbox.put(db, 1, "+77010000001", "+77001112233", NOW)
    outbox.claim(db, outbox_id, NOW)
    outbox.mark_sent(db, outbox_id, "3EB0", NOW)
    db.commit()

    with db:
        assert outbox.cancel_scheduled(db, "+77010000001", "лид ответил", NOW) == 0

    assert status_of(db, outbox_id) == "sent"


def status_of(db, outbox_id):
    return db.execute("SELECT status FROM outbox WHERE outbox_id = ?",
                      (outbox_id,)).fetchone()[0]
```

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `cd backend && uv run pytest sender/tests/test_outbox.py -v`
Expected: FAIL — `KeyError: 'kind'`, `AttributeError: module 'sender.db.outbox' has no attribute 'cancel_scheduled'`.

- [ ] **Step 3: Реализовать в `backend/sender/db/outbox.py`**

Дописать `kind` в `FIELDS`:

```python
FIELDS = ("outbox_id, message_id, thread_id, our_number, send_after, status,"
          " attempts, provider_id, error, kind, created_at, updated_at")
```

Заменить `put`:

```python
def put(db: sqlite3.Connection, message_id: int, thread_id: str,
        our_number: str, now: datetime, kind: str = "cold") -> int:
    moment = stamp(now)
    try:
        cursor = db.execute(
            "INSERT INTO outbox (message_id, thread_id, our_number, send_after,"
            " status, kind, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
            (message_id, thread_id, our_number, moment, kind, moment, moment))
    except sqlite3.IntegrityError as error:
        raise AlreadyQueuedError(message_id) from error
    return cursor.lastrowid
```

Добавить рядом с `cancel`:

```python
def cancel_scheduled(db: sqlite3.Connection, thread_id: str, reason: str,
                     now: datetime) -> int:
    """Все живые строки треда — в cancelled. Зовётся, когда лид ответил:
    запланированное касание после ответа станет издевательством.

    `sending` не трогаем: её судьба уже в руках транспорта, и отмена строки,
    которая, возможно, уже ушла, дала бы неверную историю треда.
    """
    cursor = db.execute(
        "UPDATE outbox SET status = 'cancelled', error = ?, updated_at = ?"
        " WHERE thread_id = ? AND status = 'pending'",
        (reason, stamp(now), thread_id))
    return cursor.rowcount
```

- [ ] **Step 4: Пометить прогревочные строки своим видом**

В `backend/sender/services/warmup.py` в `INSERT INTO outbox` добавить `kind`:

```python
    db.execute(
        "INSERT INTO outbox (our_number, recipient, send_after, status, kind,"
        " provider_id, created_at, updated_at)"
        " VALUES (?, ?, ?, 'sent', 'warmup', ?, ?, ?)",
        (sender_number, recipient, stamp, result.provider_id, stamp, stamp))
```

- [ ] **Step 5: Прогнать тесты**

Run: `cd backend && uv run pytest sender/tests/test_outbox.py sender/tests/test_warmup.py sender/tests/test_warmup_run.py -v`
Expected: PASS.

- [ ] **Step 6: Коммит**

```bash
git add backend/sender/db/outbox.py backend/sender/services/warmup.py backend/sender/tests/test_outbox.py
git commit -m "feat(sender): вид строки очереди и отмена расписания треда"
```

---

## Task 4: `conversation` — расписание касаний, входящие и счётчики

**Files:**
- Modify: `backend/sender/db/conversation.py`
- Modify: `backend/sender/services/worker.py:161`
- Test: `backend/sender/tests/test_conversation.py`

**Interfaces:**
- Consumes: `outbox.cancel_scheduled` (Task 3), колонка `messages.handle_attempts` (Task 1).
- Produces:
  - `conversation.bump_touch(db, thread_id, cadence: dict, now: datetime) -> None` — **сигнатура изменилась**, третий аргумент теперь весь `config["cadence"]`
  - `conversation.clear_schedule(db, thread_id) -> None`
  - `conversation.due_touch(db, now) -> dict | None`
  - `conversation.add_incoming(db, thread_id, text, provider_id) -> int` (бросает `DuplicateIncomingError`)
  - `conversation.add_draft(db, thread_id, text, angle) -> int`
  - `conversation.unhandled_incoming(db) -> dict | None` (`message_id`, `thread_id`, `text`, `handle_attempts`)
  - `conversation.mark_handled(db, message_id, now) -> None`
  - `conversation.count_attempt(db, message_id) -> int` (новое значение счётчика)
  - `conversation.bump_auto_replies(db, thread_id) -> None`
  - `conversation.DuplicateIncomingError`

- [ ] **Step 1: Написать падающие тесты**

Дописать в `backend/sender/tests/test_conversation.py`:

```python
CADENCE = {"follow_up_days": [3, 7], "max_touches": 3}


def test_bump_touch_schedules_the_next_one(db):
    """Срок следующего касания ставится той же транзакцией, что расход
    текущего: второй автор next_touch_at дал бы тред, у которого касание
    израсходовано, а срок не сдвинут."""
    open_thread(db)
    with db:
        conversation.bump_touch(db, "+77010000001", CADENCE, NOW)

    thread = db.execute("SELECT touch_no, next_touch_at, status FROM threads").fetchone()
    assert thread["touch_no"] == 1
    assert thread["next_touch_at"] == (NOW + timedelta(days=3)).isoformat(timespec="seconds")
    assert thread["status"] == "queued"


def test_the_second_touch_uses_the_second_step_of_the_cadence(db):
    open_thread(db)
    with db:
        conversation.bump_touch(db, "+77010000001", CADENCE, NOW)
        conversation.bump_touch(db, "+77010000001", CADENCE, NOW)

    expected = (NOW + timedelta(days=7)).isoformat(timespec="seconds")
    assert db.execute("SELECT next_touch_at FROM threads").fetchone()[0] == expected


def test_the_last_touch_exhausts_the_thread_and_clears_the_schedule(db):
    """Автомату больше нечего сказать — и будить его тику больше нечем."""
    open_thread(db)
    with db:
        for _ in range(3):
            conversation.bump_touch(db, "+77010000001", CADENCE, NOW)

    thread = db.execute("SELECT status, next_touch_at FROM threads").fetchone()
    assert thread["status"] == "exhausted"
    assert thread["next_touch_at"] is None


def test_a_thread_with_replies_is_not_exhausted(db):
    open_thread(db)
    with db:
        conversation.add_incoming(db, "+77010000001", "перезвоните", "3EB1")
        for _ in range(3):
            conversation.bump_touch(db, "+77010000001", CADENCE, NOW)

    assert conversation.get(db, "+77010000001")["status"] == "queued"


def test_due_touch_takes_only_active_threads_whose_time_has_come(db):
    open_thread(db, "+77010000001", status="active")
    open_thread(db, "+77010000002", status="escalated")
    open_thread(db, "+77010000003", status="active")
    soon = (NOW + timedelta(days=1)).isoformat(timespec="seconds")
    past = (NOW - timedelta(days=1)).isoformat(timespec="seconds")
    db.execute("UPDATE threads SET next_touch_at = ? WHERE thread_id = '+77010000001'", (past,))
    db.execute("UPDATE threads SET next_touch_at = ? WHERE thread_id = '+77010000002'", (past,))
    db.execute("UPDATE threads SET next_touch_at = ? WHERE thread_id = '+77010000003'", (soon,))
    db.commit()

    assert conversation.due_touch(db, NOW)["thread_id"] == "+77010000001"


def test_due_touch_is_none_when_nothing_matured(db):
    open_thread(db, status="active")
    assert conversation.due_touch(db, NOW) is None


def test_incoming_is_deduplicated_by_provider_id(db):
    """Транспорт повторяет событие, пока не получит 2xx. Без этого агент видел
    бы собеседника, дважды сказавшего одно и то же."""
    open_thread(db)
    with db:
        conversation.add_incoming(db, "+77010000001", "сколько стоит?", "3EB0")

    with pytest.raises(conversation.DuplicateIncomingError):
        with db:
            conversation.add_incoming(db, "+77010000001", "сколько стоит?", "3EB0")

    assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_incoming_lands_in_history_immediately(db):
    """Ответ лида правкам не подлежит: sent_text у него заполнен сразу."""
    open_thread(db)
    with db:
        conversation.add_incoming(db, "+77010000001", "сколько стоит?", "3EB0")

    row = db.execute("SELECT role, draft_text, sent_text, sent_at FROM messages").fetchone()
    assert row["role"] == "incoming" and row["draft_text"] is None
    assert row["sent_text"] == "сколько стоит?" and row["sent_at"] is not None


def test_unhandled_incoming_returns_the_oldest_and_skips_the_handled(db):
    open_thread(db)
    with db:
        first = conversation.add_incoming(db, "+77010000001", "первое", "3EB0")
        conversation.add_incoming(db, "+77010000001", "второе", "3EB1")

    assert conversation.unhandled_incoming(db)["message_id"] == first
    with db:
        conversation.mark_handled(db, first, NOW)
    assert conversation.unhandled_incoming(db)["text"] == "второе"


def test_count_attempt_returns_the_new_value(db):
    """Счётчик растёт ДО вызова агента: процесс, убитый посреди вызова, иначе
    не потратил бы попытку и оставил бы ту же вечную пробку."""
    open_thread(db)
    with db:
        message_id = conversation.add_incoming(db, "+77010000001", "первое", "3EB0")
    with db:
        assert conversation.count_attempt(db, message_id) == 1
        assert conversation.count_attempt(db, message_id) == 2


def test_clear_schedule_stops_the_cadence(db):
    open_thread(db, status="active")
    with db:
        conversation.bump_touch(db, "+77010000001", CADENCE, NOW)
        conversation.clear_schedule(db, "+77010000001")
    assert db.execute("SELECT next_touch_at FROM threads").fetchone()[0] is None


def test_auto_replies_counts_only_what_the_automaton_said_itself(db):
    open_thread(db)
    with db:
        conversation.bump_auto_replies(db, "+77010000001")
    assert db.execute("SELECT auto_replies FROM threads").fetchone()[0] == 1


def test_add_draft_does_not_commit(db):
    """Черновик и статус треда ложатся одной транзакцией: иначе падение между
    коммитами даёт тред, которому автомат «уже ответил», а лид ничего не видел."""
    open_thread(db)
    message_id = conversation.add_draft(db, "+77010000001", "Ответ агента", "answer")
    db.rollback()
    assert db.execute("SELECT count(*) FROM messages WHERE message_id = ?",
                      (message_id,)).fetchone()[0] == 0
```

В шапку файла добавить импорты: `import pytest` и `from sender.tests.conftest import NOW` (если `NOW` там ещё не импортирован).

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `cd backend && uv run pytest sender/tests/test_conversation.py -v`
Expected: FAIL — `TypeError: bump_touch() takes 3 positional arguments but 4 were given`, `AttributeError: ... has no attribute 'due_touch'`.

- [ ] **Step 3: Реализовать в `backend/sender/db/conversation.py`**

Добавить исключение рядом с `UnknownThreadError`:

```python
class DuplicateIncomingError(Exception):
    """Транспорт повторил событие: такое входящее уже записано."""
```

Заменить `bump_touch`:

```python
def bump_touch(db: sqlite3.Connection, thread_id: str, cadence: dict,
               now: datetime) -> None:
    """Касание израсходовано, срок следующего поставлен — одной транзакцией.

    Последнее касание без ответа закрывает тред в exhausted: автомату больше
    нечего сказать, и будить его тику больше нечем.
    """
    db.execute("UPDATE threads SET touch_no = touch_no + 1 WHERE thread_id = ?",
               (thread_id,))
    thread = get(db, thread_id)
    days = cadence["follow_up_days"]
    if thread["touch_no"] > len(days):
        clear_schedule(db, thread_id)
        if thread["touch_no"] >= cadence["max_touches"] and not has_replies(db, thread_id):
            set_status(db, thread_id, "exhausted")
        return
    when = now + timedelta(days=days[thread["touch_no"] - 1])
    db.execute("UPDATE threads SET next_touch_at = ? WHERE thread_id = ?",
               (when.isoformat(timespec="seconds"), thread_id))


def clear_schedule(db: sqlite3.Connection, thread_id: str) -> None:
    """Расписание погашено: лид ответил, или касания кончились."""
    db.execute("UPDATE threads SET next_touch_at = NULL WHERE thread_id = ?",
               (thread_id,))


def due_touch(db: sqlite3.Connection, now: datetime) -> dict | None:
    """Один созревший тред. Один, а не пачка: генерация текста стоит денег и
    секунд, а тик обязан оставаться коротким."""
    row = db.execute(
        f"SELECT {FIELDS} FROM threads WHERE status = 'active'"
        " AND next_touch_at IS NOT NULL AND next_touch_at <= ?"
        " ORDER BY next_touch_at LIMIT 1",
        (now.isoformat(timespec="seconds"),)).fetchone()
    return dict(row) if row else None
```

В шапку файла добавить `timedelta` к импорту: `from datetime import datetime, timedelta`.

Добавить запись сообщений и счётчики:

```python
def add_incoming(db: sqlite3.Connection, thread_id: str, text: str,
                 provider_id: str | None) -> int:
    """Ответ лида. Правкам не подлежит, поэтому draft_text пуст, а sent_text
    заполнен сразу: лид его уже отправил.

    Не коммитит намеренно — вебхук кладёт входящее и гасит расписание одной
    транзакцией, иначе падение между коммитами оставило бы follow-up
    запланированным после ответа.
    """
    stamp = now_stamp()
    try:
        cursor = db.execute(
            "INSERT INTO messages (thread_id, role, sent_text, provider_id,"
            " created_at, sent_at) VALUES (?, 'incoming', ?, ?, ?, ?)",
            (thread_id, text, provider_id, stamp, stamp))
    except sqlite3.IntegrityError as error:
        raise DuplicateIncomingError(provider_id) from error
    return cursor.lastrowid


def add_draft(db: sqlite3.Connection, thread_id: str, text: str, angle: str) -> int:
    """Черновик автомата. Копия thread_store.add_draft, отличающаяся ровно
    отсутствием commit: черновик, счётчик auto_replies и отметка обработки
    обязаны лечь одной транзакцией."""
    cursor = db.execute(
        "INSERT INTO messages (thread_id, role, draft_text, angle, created_at)"
        " VALUES (?, 'outgoing', ?, ?, ?)",
        (thread_id, text, angle, now_stamp()))
    return cursor.lastrowid


def unhandled_incoming(db: sqlite3.Connection) -> dict | None:
    """Старшее входящее, которого ещё не касался агент."""
    row = db.execute(
        "SELECT message_id, thread_id, sent_text AS text, handle_attempts"
        " FROM messages WHERE role = 'incoming' AND handled_at IS NULL"
        " ORDER BY message_id LIMIT 1").fetchone()
    return dict(row) if row else None


def mark_handled(db: sqlite3.Connection, message_id: int, now: datetime) -> None:
    db.execute("UPDATE messages SET handled_at = ? WHERE message_id = ?",
               (now.isoformat(timespec="seconds"), message_id))


def count_attempt(db: sqlite3.Connection, message_id: int) -> int:
    """Заход на обработку. Растёт ДО вызова агента: процесс, убитый посреди
    вызова, иначе не потратил бы попытку и остался бы вечной пробкой."""
    db.execute("UPDATE messages SET handle_attempts = handle_attempts + 1"
               " WHERE message_id = ?", (message_id,))
    return db.execute("SELECT handle_attempts FROM messages WHERE message_id = ?",
                      (message_id,)).fetchone()[0]


def bump_auto_replies(db: sqlite3.Connection, thread_id: str) -> None:
    """Сколько раз автомат отвечал своими словами. Предохранитель читает это."""
    db.execute("UPDATE threads SET auto_replies = auto_replies + 1"
               " WHERE thread_id = ?", (thread_id,))


def now_stamp() -> str:
    """Момент записи сообщения. Формат — тот же, что у thread_store: таблица
    одна, и две формы штампа в ней сломали бы сортировку истории."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
```

В шапку добавить `timezone`: `from datetime import datetime, timedelta, timezone`.

Расширить `FIELDS`, чтобы `due_touch` и предохранитель видели счётчик автоответов:

```python
FIELDS = "thread_id, company_id, status, our_number, touch_no, auto_replies, next_touch_at"
```

- [ ] **Step 4: Поправить вызов в воркере**

В `backend/sender/services/worker.py::_send` заменить строку 161:

```python
        conversation.bump_touch(db, row["thread_id"], config["cadence"], now)
```

- [ ] **Step 5: Прогнать тесты**

Run: `cd backend && uv run pytest sender/tests/test_conversation.py sender/tests/test_worker.py -v`
Expected: PASS.

- [ ] **Step 6: Прогнать весь пакет**

Run: `cd backend && uv run pytest -q`
Expected: PASS.

- [ ] **Step 7: Коммит**

```bash
git add backend/sender/db/conversation.py backend/sender/services/worker.py backend/sender/tests/test_conversation.py
git commit -m "feat(sender): расписание касаний, входящие и счётчики в conversation"
```

---

## Task 5: `thread_store.add_incoming` принимает `provider_id`

**Files:**
- Modify: `backend/writer/db/thread_store.py:141-150`
- Test: `backend/writer/tests/test_threads.py`

**Interfaces:**
- Consumes: колонку `messages.provider_id` (миграция части 2).
- Produces: `thread_store.add_incoming(db, thread_id, text, provider_id=None) -> None`.

**Почему при живом `conversation.add_incoming`:** ручной ввод оператора (`POST /api/threads/{company_id}/incoming`) обязан ложиться той же формой строки, что вебхук. Два формата в одной таблице — это вопрос «а почему у половины входящих пусто» через месяц.

- [ ] **Step 1: Написать падающий тест**

Дописать в `backend/writer/tests/test_threads.py`:

```python
def test_incoming_remembers_the_provider_id():
    """Ручной ввод оператора ложится той же формой, что вебхук: два формата
    строки в одной таблице — это вопрос «почему у половины входящих пусто»."""
    db = thread_store.connect(":memory:")
    db.execute("ALTER TABLE messages ADD COLUMN provider_id TEXT")
    thread_store.open_thread(db, "+77010000001", "c1", {"name": "Ромашка", "signals": []})

    thread_store.add_incoming(db, "+77010000001", "перезвоните", provider_id="3EB0")
    thread_store.add_incoming(db, "+77010000001", "введено руками")

    rows = db.execute("SELECT provider_id FROM messages ORDER BY message_id").fetchall()
    assert [row[0] for row in rows] == ["3EB0", None]
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `cd backend && uv run pytest writer/tests/test_threads.py -v`
Expected: FAIL — `TypeError: add_incoming() got an unexpected keyword argument 'provider_id'`.

- [ ] **Step 3: Реализовать**

Заменить `add_incoming` в `backend/writer/db/thread_store.py`:

```python
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
```

- [ ] **Step 4: Прогнать тесты**

Run: `cd backend && uv run pytest writer/tests/ -v`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add backend/writer/db/thread_store.py backend/writer/tests/test_threads.py
git commit -m "feat(writer): add_incoming запоминает provider_id"
```

---

## Task 6: Вебхук — секрет

**Files:**
- Modify: `backend/sender/routes/webhook.py:48-56`
- Test: `backend/sender/tests/test_webhook.py`

**Interfaces:**
- Consumes: `settings.sender_webhook_secret` (объявлен в `backend/config.py` с частью 1, пуст по умолчанию).
- Produces: ручка `POST /api/sender/webhook` требует заголовок `X-Sender-Secret`, когда секрет задан.

- [ ] **Step 1: Написать падающие тесты**

Дописать в `backend/sender/tests/test_webhook.py`:

```python
def test_a_forged_event_is_rejected(db, http, monkeypatch):
    """До части 3 поддельное событие не стоило ничего; с ней оно пишет в
    переписку и тратит деньги на модель."""
    monkeypatch.setattr(webhook.settings, "sender_webhook_secret", "s3cret")
    sent_row(db)

    response = http.post("/api/sender/webhook", json={
        "kind": "status", "number": "+77001112233",
        "provider_id": "3EB0", "status": webhook.DELIVERED})

    assert response.status_code == 401
    assert db.execute("SELECT delivered_at FROM outbox").fetchone()[0] is None


def test_the_right_secret_passes(db, http, monkeypatch):
    monkeypatch.setattr(webhook.settings, "sender_webhook_secret", "s3cret")
    sent_row(db)

    response = http.post(
        "/api/sender/webhook", headers={"X-Sender-Secret": "s3cret"},
        json={"kind": "status", "number": "+77001112233",
              "provider_id": "3EB0", "status": webhook.DELIVERED})

    assert response.status_code == 200
    assert db.execute("SELECT delivered_at FROM outbox").fetchone()[0] is not None


def test_an_empty_secret_turns_the_check_off(db, http, monkeypatch):
    """Локальная разработка на пустом .env не должна ломаться."""
    monkeypatch.setattr(webhook.settings, "sender_webhook_secret", None)
    sent_row(db)

    response = http.post("/api/sender/webhook", json={
        "kind": "status", "number": "+77001112233",
        "provider_id": "3EB0", "status": webhook.DELIVERED})

    assert response.status_code == 200
```

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `cd backend && uv run pytest sender/tests/test_webhook.py -v`
Expected: FAIL — `AttributeError: module 'sender.routes.webhook' has no attribute 'settings'` либо `assert 200 == 401`.

- [ ] **Step 3: Реализовать**

В шапку `backend/sender/routes/webhook.py` добавить импорты:

```python
from fastapi import APIRouter, Header, HTTPException

from config import settings
```

Заменить сигнатуру ручки:

```python
@router.post("/webhook")
def receive(event: Event,
            x_sender_secret: str | None = Header(default=None)) -> dict:
    require_secret(x_sender_secret)
    if event.kind != "status" or event.provider_id is None:
        log.info("событие %s пока не обрабатывается (часть 3): %s",
                 event.kind, event.provider_id)
        return {"handled": False}
    with closing(connect()) as db:
        return {"handled": _record_status(db, event, now())}


def require_secret(given: str | None) -> None:
    """Пустой секрет означает выключенную проверку: локальная разработка на
    пустом .env не должна ломаться. Подделка получает 401, а не 200 — Node
    ретраит только то, на что не пришло 2xx, и чужой запрос не превращается в
    бесконечный цикл."""
    expected = settings.sender_webhook_secret
    if expected and given != expected:
        raise HTTPException(401, "вебхук не подписан")
```

- [ ] **Step 4: Прогнать тесты**

Run: `cd backend && uv run pytest sender/tests/test_webhook.py -v`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add backend/sender/routes/webhook.py backend/sender/tests/test_webhook.py
git commit -m "feat(sender): вебхук требует секрет, когда он задан"
```

---

## Task 7: Вебхук — входящее: адресат, дедуп, запись, отмена расписания

**Files:**
- Modify: `backend/sender/routes/webhook.py`
- Test: `backend/sender/tests/test_webhook.py`

**Interfaces:**
- Consumes: `conversation.add_incoming`, `conversation.clear_schedule`, `conversation.DuplicateIncomingError` (Task 4); `outbox.cancel_scheduled` (Task 3); `numbers.normalize` (часть 1); `notify.send` (часть 1).
- Produces: `Event` получает поле `from_` (алиас `from`); `webhook.thread_of(jid: str) -> str | None`; ветка `kind == "incoming"` в `receive`.

- [ ] **Step 1: Написать падающие тесты**

Дописать в `backend/sender/tests/test_webhook.py`:

```python
def incoming(text="сколько это стоит?", provider_id="IN1", jid="77010000001@s.whatsapp.net"):
    return {"kind": "incoming", "number": "+77001112233", "from": jid,
            "provider_id": provider_id, "text": text}


def test_a_reply_lands_in_the_thread(db, http):
    open_thread(db, "+77010000001", status="active")

    assert http.post("/api/sender/webhook", json=incoming()).status_code == 200

    row = db.execute("SELECT thread_id, role, sent_text, provider_id, handled_at"
                     " FROM messages").fetchone()
    assert row["thread_id"] == "+77010000001" and row["role"] == "incoming"
    assert row["sent_text"] == "сколько это стоит?" and row["provider_id"] == "IN1"
    assert row["handled_at"] is None, "агента зовёт тик, а не ручка"


def test_a_repeated_incoming_does_not_double_the_reply(db, http):
    """Транспорт повторяет событие, пока не получит 2xx."""
    open_thread(db, "+77010000001", status="active")
    http.post("/api/sender/webhook", json=incoming())

    assert http.post("/api/sender/webhook", json=incoming()).status_code == 200

    assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_an_incoming_cancels_the_scheduled_followups(db, http):
    """«Напоминаю о своём сообщении» через три дня после ответа — издевательство.
    Забыть эту строку легко, и именно она превращает систему в ту, на которую
    жалуются."""
    open_thread(db, "+77010000001", status="active")
    message_id = add_draft(db, "+77010000001")
    with db:
        outbox_id = outbox.put(db, message_id, "+77010000001", "+77001112233",
                               NOW, kind="followup")
        db.execute("UPDATE threads SET next_touch_at = ? WHERE thread_id = ?",
                   ("2026-09-05T12:00:00+00:00", "+77010000001"))

    http.post("/api/sender/webhook", json=incoming())

    assert db.execute("SELECT status FROM outbox WHERE outbox_id = ?",
                      (outbox_id,)).fetchone()[0] == "cancelled"
    assert db.execute("SELECT next_touch_at FROM threads").fetchone()[0] is None


def test_a_message_from_a_stranger_goes_to_telegram_and_not_to_the_base(db, http, sent_to_telegram):
    """Написал тот, кому мы не писали. Автомат такое не трогает."""
    assert http.post("/api/sender/webhook", json=incoming()).status_code == 200

    assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    assert any("+77010000001" in text for text in sent_to_telegram), sent_to_telegram


def test_a_group_chat_is_out_of_scope(db, http):
    """Групповые чаты вне области v1. Молчаливое падение здесь выглядело бы
    как потерянный ответ лида."""
    response = http.post("/api/sender/webhook",
                         json=incoming(jid="12036300@g.us"))

    assert response.status_code == 200
    assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_a_jid_that_is_not_a_number_is_not_a_crash(db, http):
    response = http.post("/api/sender/webhook", json=incoming(jid="204521@lid"))
    assert response.status_code == 200
```

В шапку файла добавить импорты и фикстуру уведомлений:

```python
from sender import notify
from sender.db import outbox
from sender.tests.conftest import NOW
from sender.tests.test_conversation import add_draft, open_thread


@pytest.fixture
def sent_to_telegram(monkeypatch):
    """Telegram без сети: собираем тексты, которые ушли бы человеку."""
    sent = []

    async def fake(text, client=None):
        sent.append(text)
        return True

    monkeypatch.setattr(notify, "send", fake)
    return sent
```

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `cd backend && uv run pytest sender/tests/test_webhook.py -v`
Expected: FAIL — входящее не обрабатывается, `count(*) == 0` вместо 1.

- [ ] **Step 3: Реализовать разбор адресата и запись**

В `backend/sender/routes/webhook.py` расширить модель события (Node шлёт поле `from`, а это ключевое слово Python — берём алиасом):

```python
class Event(BaseModel):
    kind: str
    number: str | None = None
    provider_id: str | None = None
    status: int | None = None
    text: str | None = None
    sender: str | None = Field(default=None, alias="from")
```

и добавить `from pydantic import BaseModel, Field`.

Добавить константу и разбор адресата:

```python
INCOMING = "incoming"

# Чат лида в WhatsApp — это JID (`77010000001@s.whatsapp.net`), а тред живёт
# как `+77010000001`. Групповые чаты (`@g.us`) и `@lid`-формат вне области v1.
PERSONAL_JID = "@s.whatsapp.net"


def thread_of(jid: str | None) -> str | None:
    """Тред по адресу отправителя. None — адрес не личного чата или не номер."""
    if not jid or not jid.endswith(PERSONAL_JID):
        return None
    try:
        return numbers.normalize(jid.removesuffix(PERSONAL_JID))
    except numbers.InvalidNumberError:
        return None
```

и добавить `numbers` в импорт: `from sender.db import conversation, migrate, numbers, outbox`.

Заменить тело ручки:

```python
@router.post("/webhook")
async def receive(event: Event,
                  x_sender_secret: str | None = Header(default=None)) -> dict:
    require_secret(x_sender_secret)
    if event.kind == INCOMING:
        return {"handled": await _record_incoming(event, now())}
    if event.kind != "status" or event.provider_id is None:
        log.info("событие %s не обрабатывается: %s", event.kind, event.provider_id)
        return {"handled": False}
    with closing(connect()) as db:
        return {"handled": _record_status(db, event, now())}
```

Добавить обработчик входящего:

```python
async def _record_incoming(event: Event, moment: datetime) -> bool:
    """Только быстрое и детерминированное. Всё медленное и вероятностное —
    вызов агента — подхватит тик воркера по `handled_at IS NULL`."""
    thread_id = thread_of(event.sender)
    if thread_id is None:
        log.info("входящее не из личного чата, пропускаем: %s", event.sender)
        return False
    with closing(connect()) as db:
        if conversation.get(db, thread_id) is None:
            # Написал тот, кому мы не писали. Автомат такое не трогает.
            await notify.send(f"Пишет {thread_id}, треда с ним нет: {event.text!r}")
            return False
        try:
            with db:
                conversation.add_incoming(db, thread_id, event.text or "",
                                          event.provider_id)
                outbox.cancel_scheduled(db, thread_id, "лид ответил", moment)
                conversation.clear_schedule(db, thread_id)
        except conversation.DuplicateIncomingError:
            log.info("повтор события %s — уже записано", event.provider_id)
            return False
    log.info("входящее в тред %s записано, ждёт тика", thread_id)
    return True
```

и добавить импорт `from sender import notify`.

- [ ] **Step 4: Прогнать тесты**

Run: `cd backend && uv run pytest sender/tests/test_webhook.py -v`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add backend/sender/routes/webhook.py backend/sender/tests/test_webhook.py
git commit -m "feat(sender): вебхук принимает входящие и гасит расписание треда"
```

---

## Task 8: Вебхук — стоп-слова и шов отказа

**Files:**
- Create: `backend/sender/services/refusal.py`
- Modify: `backend/sender/routes/webhook.py`
- Test: `backend/sender/tests/test_refusal.py` (создать), `backend/sender/tests/test_webhook.py`

**Interfaces:**
- Consumes: `config.load()["stopwords"]["patterns"]` (Task 1); `conversation.set_status` (часть 2).
- Produces: `refusal.use(hook: Callable[[str, str], bool]) -> None`; `refusal.refuse(handle: str, reason: str) -> bool`; `webhook.stopword(text: str, config: dict) -> str | None` (сработавший фрагмент или None).

- [ ] **Step 1: Написать падающие тесты**

Создать `backend/sender/tests/test_refusal.py`:

```python
"""Шов к юридическому контуру системы 1.

Отказ — единственное, что возвращается из системы 3 в систему 1, и он не имеет
права зависеть от импорта: collector системе 3 недоступен (тест графа импортов),
а вебхук — ручка, которой параметр не передать.
"""

from sender.services import refusal


def test_a_registered_hook_gets_the_refusal(monkeypatch):
    written = []
    monkeypatch.setattr(refusal, "_hook", lambda handle, reason: written.append((handle, reason)) or True)

    assert refusal.refuse("+77010000001", "стоп-слово: отпиш") is True
    assert written == [("+77010000001", "стоп-слово: отпиш")]


def test_an_unregistered_hook_is_loud_and_does_not_crash(monkeypatch, caplog):
    """Молчащий шов — это потерянный отказ, а отказ юридический контур (F21)."""
    monkeypatch.setattr(refusal, "_hook", None)

    assert refusal.refuse("+77010000001", "стоп-слово") is False
    assert any(record.levelname == "ERROR" for record in caplog.records)


def test_use_registers_the_hook():
    calls = []
    refusal.use(lambda handle, reason: calls.append(handle) or True)
    try:
        assert refusal.refuse("+77010000002", "почему") is True
        assert calls == ["+77010000002"]
    finally:
        refusal.use(None)
```

Дописать в `backend/sender/tests/test_webhook.py`:

```python
@pytest.fixture
def refusals(monkeypatch):
    """Отказ без collector'а: собираем то, что ушло бы в state.suppression."""
    written = []
    monkeypatch.setattr(refusal, "_hook",
                        lambda handle, reason: written.append((handle, reason)) or True)
    return written


def test_a_stop_word_refuses_and_closes_the_thread(db, http, refusals):
    """Правило до модели стоит десять строк и не ошибается никогда, а проценты
    ошибок классификатора приходятся ровно на тех, кто и жмёт Report."""
    open_thread(db, "+77010000001", status="active")

    http.post("/api/sender/webhook", json=incoming(text="отпишите меня, надоели"))

    assert refusals and refusals[0][0] == "+77010000001"
    assert conversation.get(db, "+77010000001")["status"] == "closed_refused"


def test_a_stop_word_never_wakes_the_agent(db, http, refusals):
    """handled_at проставлен ручкой: тик не должен звать модель на «отпишите»."""
    open_thread(db, "+77010000001", status="active")

    http.post("/api/sender/webhook", json=incoming(text="это спам, жалоба"))

    assert db.execute("SELECT handled_at FROM messages").fetchone()[0] is not None


def test_a_refused_lead_gets_no_confirmation(db, http, refusals):
    """Гейт треда всё равно отменил бы строку подтверждения. Человек,
    попросивший не писать, получает ровно то, что попросил, — тишину."""
    open_thread(db, "+77010000001", status="active")

    http.post("/api/sender/webhook", json=incoming(text="отпишитесь от меня"))

    assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_an_ordinary_question_is_not_a_stop_word(db, http, refusals):
    open_thread(db, "+77010000001", status="active")

    http.post("/api/sender/webhook", json=incoming(text="а сколько это стоит?"))

    assert refusals == []
    assert db.execute("SELECT handled_at FROM messages").fetchone()[0] is None
```

и добавить в шапку `from sender.services import config as sender_config, refusal` и `from sender.db import conversation`.

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `cd backend && uv run pytest sender/tests/test_refusal.py sender/tests/test_webhook.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sender.services.refusal'`.

- [ ] **Step 3: Создать `backend/sender/services/refusal.py`**

```python
"""Шов к юридическому контуру системы 1: отказ пишет collector, зовёт sender.

Регистрация, а не параметр, по двум причинам. Отказ нужен двоим — вебхуку
(стоп-слово) и тику (`classify(refusal)`), — а вебхук ручка FastAPI, ей
параметр не передать. Два разных механизма на один шов хуже одного модуля с
явной регистрацией из `collector/api.py` — единственного места, где система 3
вообще узнаёт о существовании системы 1.

Читать `suppression` sender умеет сам (`worker._suppressed`): чтение — один
SELECT, а запись — правило «повтор не задваивается», и вторая его копия
разъехалась бы молча.
"""

import logging
from typing import Callable

log = logging.getLogger(__name__)

_hook: Callable[[str, str], bool] | None = None


def use(hook: Callable[[str, str], bool] | None) -> None:
    """Зовётся один раз из collector/api.py при сборке приложения."""
    global _hook
    _hook = hook


def refuse(handle: str, reason: str) -> bool:
    """False — шов не зарегистрирован. Громко: молчащий шов это потерянный
    отказ, а отказ — юридический контур (F21)."""
    if _hook is None:
        log.error("отказ %s не записан: шов к suppression не зарегистрирован", handle)
        return False
    return _hook(handle, reason)
```

- [ ] **Step 4: Добавить стоп-слова в вебхук**

В `backend/sender/routes/webhook.py` добавить импорты `import re`, `from functools import lru_cache`, `from sender.services import config, refusal` (модуль `config` уже импортирован — дописать `refusal`).

Добавить проверку:

```python
@lru_cache
def _stopwords(patterns: tuple[str, ...]) -> re.Pattern:
    """Компиляция один раз на набор: тик и ручка зовут это на каждое входящее."""
    return re.compile("|".join(patterns), re.IGNORECASE)


def stopword(text: str, settings: dict) -> str | None:
    """Сработавший фрагмент или None. Проверяется ДО модели: классификатор
    вероятностный, и его проценты ошибок приходятся ровно на раздражённые
    короткие сообщения — то есть на тех, кто и жмёт Report."""
    found = _stopwords(tuple(settings["stopwords"]["patterns"])).search(text)
    return found.group(0) if found else None
```

В `_record_incoming` вычислить стоп-слово **до** транзакции (регулярка в базу не ходит, а держать на ней открытую транзакцию незачем) и закрыть тред внутри неё. Итоговый фрагмент:

```python
    settings = config.load()
    refused = stopword(event.text or "", settings)
    with closing(connect()) as db:
        ...
        try:
            with db:
                message_id = conversation.add_incoming(db, thread_id, event.text or "",
                                                       event.provider_id)
                outbox.cancel_scheduled(db, thread_id, "лид ответил", moment)
                conversation.clear_schedule(db, thread_id)
                if refused:
                    conversation.set_status(db, thread_id, "closed_refused")
                    conversation.mark_handled(db, message_id, moment)
        except conversation.DuplicateIncomingError:
            log.info("повтор события %s — уже записано", event.provider_id)
            return False
    if refused:
        # Отказ пишется ПОСЛЕ коммита треда: шов ходит в чужую базу, и держать
        # на нём открытую транзакцию state.db значило бы блокировать очередь.
        # Подтверждения лиду нет: гейт треда всё равно отменил бы строку, а
        # человек, попросивший не писать, получает ровно то, что попросил.
        refusal.refuse(thread_id, f"стоп-слово: {refused}")
        log.warning("тред %s закрыт по стоп-слову %r", thread_id, refused)
```

- [ ] **Step 5: Прогнать тесты**

Run: `cd backend && uv run pytest sender/tests/test_refusal.py sender/tests/test_webhook.py -v`
Expected: PASS.

- [ ] **Step 6: Коммит**

```bash
git add backend/sender/services/refusal.py backend/sender/routes/webhook.py backend/sender/tests/test_refusal.py backend/sender/tests/test_webhook.py
git commit -m "feat(sender): стоп-слова до модели и шов отказа"
```

---

## Task 9: Вебхук — медиа, воскрешение из `exhausted`, тред человека

**Files:**
- Modify: `backend/sender/routes/webhook.py`
- Test: `backend/sender/tests/test_webhook.py`

**Interfaces:**
- Consumes: всё из Task 7 и 8.
- Produces: `webhook.MEDIA_MARKER = "[медиа]"`; входящее с пустым текстом эскалирует; входящее в `exhausted` возвращает тред в `active`; входящее в `escalated` уходит в Telegram и помечается обработанным.

- [ ] **Step 1: Написать падающие тесты**

Дописать в `backend/sender/tests/test_webhook.py`:

```python
def test_a_voice_message_escalates_without_the_model(db, http, sent_to_telegram):
    """Модель, которой дали пустую реплику, сочинит содержание голосового —
    ровно тот класс ошибки, против которого стоят предохранители."""
    open_thread(db, "+77010000001", status="active")

    http.post("/api/sender/webhook", json=incoming(text=""))

    assert conversation.get(db, "+77010000001")["status"] == "escalated"
    row = db.execute("SELECT sent_text, handled_at FROM messages").fetchone()
    assert row["sent_text"] == webhook.MEDIA_MARKER
    assert row["handled_at"] is not None, "тик не должен звать модель на пустоту"
    assert sent_to_telegram


def test_a_reply_after_three_silent_touches_revives_the_thread(db, http):
    """exhausted означает «нам больше нечего сказать», а не «лид закрыт»."""
    open_thread(db, "+77010000001", status="exhausted")

    http.post("/api/sender/webhook", json=incoming())

    assert conversation.get(db, "+77010000001")["status"] == "active"
    assert db.execute("SELECT handled_at FROM messages").fetchone()[0] is None


def test_a_reply_in_a_thread_the_human_took_only_notifies(db, http, sent_to_telegram):
    """Из escalated автоматического выхода нет — даже по ответу лида."""
    open_thread(db, "+77010000001", status="escalated")

    http.post("/api/sender/webhook", json=incoming(text="давайте в четверг"))

    assert conversation.get(db, "+77010000001")["status"] == "escalated"
    assert db.execute("SELECT handled_at FROM messages").fetchone()[0] is not None
    assert any("давайте в четверг" in text for text in sent_to_telegram), sent_to_telegram


def test_a_stop_word_beats_the_escalated_thread(db, http, refusals):
    """Отказ — юридический контур: он сильнее любого состояния треда."""
    open_thread(db, "+77010000001", status="escalated")

    http.post("/api/sender/webhook", json=incoming(text="удалите мой номер"))

    assert refusals and conversation.get(db, "+77010000001")["status"] == "closed_refused"
```

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `cd backend && uv run pytest sender/tests/test_webhook.py -v`
Expected: FAIL — статус остаётся `active`/`exhausted`, `handled_at` пуст.

- [ ] **Step 3: Реализовать**

Добавить константы в `backend/sender/routes/webhook.py`:

```python
# Метка вместо текста, которого не было. Голосовое, фото и стикер приезжают от
# Node с пустым `text`, и в истории треда обязана остаться строка: «лид ответил
# и мы не поняли чем» — это событие, а не его отсутствие.
MEDIA_MARKER = "[медиа]"

# Тред, который автомат бросил, доживает до ответа: exhausted означает «нам
# больше нечего сказать», а не «лид закрыт».
REVIVED_BY_A_REPLY = "exhausted"

# Тред, который ведёт человек. Автомат в него не пишет (правило 1 зонтичной
# спеки), но реплика, замеченная через сутки, стоит сделки.
HUMAN_LEADS = "escalated"
```

Переписать `_record_incoming` целиком — итоговая версия функции:

```python
async def _record_incoming(event: Event, moment: datetime) -> bool:
    """Только быстрое и детерминированное. Всё медленное и вероятностное —
    вызов агента — подхватит тик воркера по `handled_at IS NULL`.

    Порядок ветвления не косметический: стоп-слово сильнее любого состояния
    треда (юридический контур F21), медиа сильнее обычной обработки (модели
    нечего читать), и только потом решается, будить агента или нет.
    """
    thread_id = thread_of(event.sender)
    if thread_id is None:
        log.info("входящее не из личного чата, пропускаем: %s", event.sender)
        return False
    settings = config.load()
    text = event.text or ""
    refused = stopword(text, settings)
    with closing(connect()) as db:
        thread = conversation.get(db, thread_id)
        if thread is None:
            await notify.send(f"Пишет {thread_id}, треда с ним нет: {text!r}")
            return False
        try:
            with db:
                message_id = conversation.add_incoming(
                    db, thread_id, text or MEDIA_MARKER, event.provider_id)
                outbox.cancel_scheduled(db, thread_id, "лид ответил", moment)
                conversation.clear_schedule(db, thread_id)
                _settle(db, thread, message_id, moment, refused, bool(text))
        except conversation.DuplicateIncomingError:
            log.info("повтор события %s — уже записано", event.provider_id)
            return False
    if refused:
        # Отказ пишется ПОСЛЕ коммита: шов ходит в чужую базу, и держать на нём
        # открытую транзакцию state.db значило бы блокировать очередь.
        refusal.refuse(thread_id, f"стоп-слово: {refused}")
        log.warning("тред %s закрыт по стоп-слову %r", thread_id, refused)
        return True
    if not text:
        await notify.send(f"{thread_id} прислал медиа — текста нет, разбирай руками")
    elif thread["status"] == HUMAN_LEADS:
        await notify.send(f"{thread_id} (тред ведёшь ты): {text}")
    return True


def _settle(db, thread: dict, message_id: int, moment: datetime,
            refused: str | None, has_text: bool) -> None:
    """Состояние треда сразу после записи входящего — той же транзакцией.
    `handled_at` здесь означает «тику тут делать нечего»: агента не позовут."""
    thread_id = thread["thread_id"]
    if refused:
        conversation.set_status(db, thread_id, "closed_refused")
        conversation.mark_handled(db, message_id, moment)
        return
    if not has_text:
        conversation.set_status(db, thread_id, "escalated")
        conversation.mark_handled(db, message_id, moment)
        return
    if thread["status"] == HUMAN_LEADS:
        conversation.mark_handled(db, message_id, moment)
        return
    if thread["status"] == REVIVED_BY_A_REPLY:
        conversation.set_status(db, thread_id, "active")
```

- [ ] **Step 4: Прогнать тесты**

Run: `cd backend && uv run pytest sender/tests/test_webhook.py -v`
Expected: PASS.

- [ ] **Step 5: Прогнать весь пакет**

Run: `cd backend && uv run pytest -q`
Expected: PASS.

- [ ] **Step 6: Коммит**

```bash
git add backend/sender/routes/webhook.py backend/sender/tests/test_webhook.py
git commit -m "feat(sender): медиа эскалирует, ответ воскрешает exhausted, тред человека уходит в Telegram"
```

---

## Task 10: Агент-продавец `writer/services/seller.py`

**Files:**
- Create: `backend/writer/services/seller.py`
- Modify: `backend/writer/services/agent.py:65-79`
- Test: `backend/writer/tests/test_seller.py` (создать)

**Interfaces:**
- Consumes: `agent.client(config)` (новая функция, см. Step 4), `observability.langfuse_handler()`, `writer/config.toml`.
- Produces:
  - `seller.Reply(text: str | None, status: str | None, reason: str | None)` — фризнутый датакласс
  - `seller.STATUSES = ("interested", "refusal", "wrong_number", "junk")`
  - `seller.build(config: dict)` — собранный `create_agent`
  - `seller.respond(agent, seed: dict, history: list[dict], offer: str, *, session_id: str) -> Reply`
  - `agent.client(config)` — сырой `ChatOpenRouter` без structured output

- [ ] **Step 1: Написать падающие тесты**

Создать `backend/writer/tests/test_seller.py`:

```python
"""Агент-продавец: два исхода, третьего нет.

Модель заглушается целиком — вместо create_agent подставляется объект с
.invoke, отдающий заранее заданные сообщения. Проверяется разбор исхода, а не
качество текста: качество проверяет человек в Langfuse на этапе 4 выката.
"""

import json

import pytest

from writer.services import seller

SEED = {"name": "Ромашка", "city": "Алматы",
        "signals": [{"type": "crm_widget", "quote": "виджет Bitrix24"}]}
HISTORY = [{"role": "outgoing", "text": "Здравствуйте! Увидели виджет...", "angle": "crm_widget", "sent_at": "2026-09-01T10:00:00+00:00"},
           {"role": "incoming", "text": "а сколько стоит?", "angle": None, "sent_at": "2026-09-02T10:00:00+00:00"}]


class FakeMessage:
    def __init__(self, content, type_="ai"):
        self.content = content
        self.type = type_


class FakeAgent:
    """Ровно то, что отдаёт create_agent: словарь с накопленными сообщениями."""

    def __init__(self, last):
        self._last = last
        self.seen_config = None

    def invoke(self, state, config=None):
        self.seen_config = config
        return {"messages": [FakeMessage("вопрос лида"), self._last]}


def test_free_text_comes_back_as_text():
    agent = FakeAgent(FakeMessage("Цену назовём после короткого разговора."))

    reply = seller.respond(agent, SEED, HISTORY, "оффер", session_id="+77010000001")

    assert reply.text == "Цену назовём после короткого разговора."
    assert reply.status is None


@pytest.mark.parametrize("status", seller.STATUSES)
def test_every_verdict_comes_back_as_a_status(status):
    """return_direct=True короткозамыкает цикл: последнее сообщение — вывод
    инструмента, текста в этом ходе нет и быть не должно."""
    verdict = json.dumps({"status": status, "reason": "так решил агент"})
    agent = FakeAgent(FakeMessage(verdict, type_="tool"))

    reply = seller.respond(agent, SEED, HISTORY, "оффер", session_id="+77010000001")

    assert reply.status == status and reply.reason == "так решил агент"
    assert reply.text is None


def test_an_unknown_status_is_not_a_crash():
    """Модель вернула чушь в аргументе инструмента. Падать нельзя — решение
    примет предохранитель, а не traceback."""
    verdict = json.dumps({"status": "выдумал", "reason": "почему бы и нет"})
    agent = FakeAgent(FakeMessage(verdict, type_="tool"))

    reply = seller.respond(agent, SEED, HISTORY, "оффер", session_id="+77010000001")

    assert reply.status == seller.UNKNOWN


def test_broken_tool_output_is_not_a_crash():
    agent = FakeAgent(FakeMessage("не json вовсе", type_="tool"))
    assert seller.respond(agent, SEED, HISTORY, "оффер",
                          session_id="+7").status == seller.UNKNOWN


def test_the_dialogue_and_the_offer_reach_the_prompt():
    agent = FakeAgent(FakeMessage("ответ"))

    seller.respond(agent, SEED, HISTORY, "мы строим лидоген", session_id="+77010000001")

    prompt = seller.prompt(SEED, HISTORY)
    assert "Ромашка" in prompt and "а сколько стоит?" in prompt
    assert "мы строим лидоген" in seller.SELL_MANAGER.format(offer="мы строим лидоген")


def test_the_loop_is_capped_and_the_session_is_the_thread():
    """Инструмент один и терминальный — больше двух шагов там делать нечего.
    Сессия по thread_id: когда лид скажет «вы обещали X», ответ должен
    находиться за десять секунд."""
    agent = FakeAgent(FakeMessage("ответ"))

    seller.respond(agent, SEED, HISTORY, "оффер", session_id="+77010000001")

    assert agent.seen_config["recursion_limit"] == seller.RECURSION_LIMIT
    assert agent.seen_config["metadata"]["langfuse_session_id"] == "+77010000001"
    assert "sender.reply" in agent.seen_config["metadata"]["langfuse_tags"]


def test_classify_tool_returns_its_verdict_as_json():
    """Инструмент отдаёт вывод строкой — из неё respond и читает исход."""
    assert json.loads(seller.classify.invoke(
        {"status": "refusal", "reason": "не интересно"})) == {
            "status": "refusal", "reason": "не интересно"}
```

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `cd backend && uv run pytest writer/tests/test_seller.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'writer.services.seller'`.

- [ ] **Step 3: Создать `backend/writer/services/seller.py`**

```python
"""Ответ в живом диалоге: агент с единственным инструментом.

Отличие от agent.py не в модели, а в том, что здесь есть собеседник. Холодное
касание — один вызов со structured output, потому что ответ обязан лечь в
Draft; ответ в диалоге — свободный текст ИЛИ решение закрыть тред, и выбор
между ними делает тот, кто прочитал диалог, а не второй вызов модели.

Чекпойнтера нет и не будет: запрет из CLAUDE.md касался именно его — второго
источника правды рядом с messages. Историю по-прежнему собираем из messages
руками.
"""

import json
import logging
from dataclasses import dataclass

from langchain.agents import create_agent
from langchain_core.tools import tool

import observability
from writer.services import agent as writer_agent

log = logging.getLogger(__name__)

STATUSES = ("interested", "refusal", "wrong_number", "junk")

# Модель вернула статус, которого не бывает. Это не авария — это вход для
# предохранителя: решение примет `if`, а не traceback.
UNKNOWN = "unknown"

# Инструмент один и терминальный: больше двух шагов в этом цикле делать нечего.
RECURSION_LIMIT = 2

SELL_MANAGER = """Ты ведёшь переписку в WhatsApp от лица команды, которая предлагает:
{offer}

Тебе пишет живой человек, уже получивший наше первое сообщение. Ответь ему сам
ИЛИ закрой тред инструментом classify. Третьего не дано.

Правила, которые не обсуждаются:
- Цены, сроки и любые цифры бери только из оффера выше. Их там нет — значит их
  нет и в ответе: скажи, что назовём после короткого разговора. Выдуманная
  цифра — это обещание, которое даёт живому человеку компания, а не модель.
- Не выдумывай фактов о компании собеседника. Всё, чего нет в данных, не существует.
- Пиши как человек в мессенджере: 2-4 коротких предложения, на «вы», без
  списков и без «Надеюсь, у вас всё хорошо».
- Зови classify, когда отвечать больше нечего: собеседник заинтересован и
  разговор пора отдать человеку (interested), отказался (refusal), это не тот
  человек (wrong_number) или пишет бессмыслицу (junk).
- Если сомневаешься между ответом и classify(interested) — выбирай classify.
  Живой разговор о деньгах ведёт человек."""


@dataclass(frozen=True)
class Reply:
    """Исход хода. Ровно одно из двух полей заполнено."""
    text: str | None
    status: str | None
    reason: str | None


@tool(return_direct=True)
def classify(status: str, reason: str) -> str:
    """Закрыть тред и передать его дальше. Зови, когда отвечать больше нечего.

    status: interested — заинтересован, разговор пора отдать человеку;
            refusal — отказался, писать больше нельзя;
            wrong_number — это не тот человек;
            junk — бессмыслица, спам, автоответ.
    reason: одна фраза, почему именно этот статус.
    """
    return json.dumps({"status": status, "reason": reason}, ensure_ascii=False)


def build(config: dict):
    """Агент собирается один раз на процесс: create_agent компилирует граф, и
    делать это на каждое входящее незачем."""
    return create_agent(writer_agent.client(config), tools=[classify],
                        system_prompt=SELL_MANAGER.format(
                            offer=config["offer"]["text"]))


def respond(agent, seed: dict, history: list[dict], offer: str, *,
            session_id: str) -> Reply:
    """Один ход. Сессия Langfuse — тред: когда лид скажет «вы обещали X»,
    ответ должен находиться за десять секунд."""
    handler = observability.langfuse_handler()
    config = {
        "run_name": "sender.reply",
        "recursion_limit": RECURSION_LIMIT,
        "metadata": {"langfuse_session_id": session_id,
                     "langfuse_tags": ["sender.reply"]},
        "callbacks": [handler] if handler else [],
    }
    result = agent.invoke({"messages": [("human", prompt(seed, history))]},
                          config=config)
    if handler:
        writer_agent._log_trace_background(handler)
    return _outcome(result["messages"][-1])


def _outcome(last) -> Reply:
    """Свободный текст или вердикт инструмента. `return_direct=True`
    короткозамыкает цикл, поэтому вердикт всегда приходит последним сообщением."""
    if getattr(last, "type", None) != "tool":
        return Reply(text=last.content, status=None, reason=None)
    try:
        verdict = json.loads(last.content)
        status, reason = verdict["status"], verdict.get("reason", "")
    except (ValueError, KeyError, TypeError):
        log.warning("инструмент вернул неразбираемое: %r", last.content)
        return Reply(text=None, status=UNKNOWN, reason="неразбираемый вывод classify")
    if status not in STATUSES:
        log.warning("агент вернул статус, которого не бывает: %r", status)
        return Reply(text=None, status=UNKNOWN, reason=reason)
    return Reply(text=None, status=status, reason=reason)


def prompt(seed: dict, history: list[dict]) -> str:
    """Карточка компании и диалог. Холодное сообщение — просто первое outgoing
    в этом диалоге: отдельное поле для него было бы вторым представлением того
    же текста."""
    return writer_agent.prompt(
        seed, history,
        "Задача: ответить на последнюю реплику собеседника — или закрыть тред"
        " инструментом classify.")
```

- [ ] **Step 4: Вынести сырой клиент модели в `backend/writer/services/agent.py`**

Заменить `model()`:

```python
def client(config):
    """Сырой клиент модели: без structured output, для агента с инструментами.

    require_parameters ограничивает роутинг OpenRouter провайдерами, реально
    поддерживающими strict json_schema — см. collector/services/pipeline/llm.py.
    """
    return ChatOpenRouter(
        model=config["llm"]["model"],
        api_key=settings.openrouter_api_key,
        temperature=config["llm"]["temperature"],
        reasoning=REASONING,
        timeout=REQUEST_TIMEOUT_MS,
        model_kwargs={"retries": NO_SDK_RETRY},
        openrouter_provider={"require_parameters": True},
    )


def model(config):
    """Клиент для одного хода со structured output: ответ обязан лечь в Draft."""
    return client(config).with_structured_output(Draft, method="json_schema", strict=True)
```

- [ ] **Step 5: Прогнать тесты**

Run: `cd backend && uv run pytest writer/tests/ -v`
Expected: PASS.

- [ ] **Step 6: Коммит**

```bash
git add backend/writer/services/seller.py backend/writer/services/agent.py backend/writer/tests/test_seller.py
git commit -m "feat(writer): агент-продавец с инструментом classify"
```

---

## Task 11: `sender/services/incoming.py` — предохранители и переходы

**Files:**
- Create: `backend/sender/services/incoming.py`
- Test: `backend/sender/tests/test_incoming.py` (создать)

**Interfaces:**
- Consumes: `conversation.unhandled_incoming/count_attempt/mark_handled/add_draft/bump_auto_replies/set_status` (Task 4); `seller.respond`, `seller.Reply`, `seller.STATUSES`, `seller.UNKNOWN` (Task 10); `refusal.refuse` (Task 8); `queue.enqueue` (часть 2); `thread_store.thread`, `thread_store.history` (система 2).
- Produces: `incoming.handle_one(db, transport, config, now) -> str | None` (исход: `answered | escalated | closed | None`); `incoming.PRICE`; `incoming.REPLY_MODES = ("replies", "full")`; `incoming.VERDICT_STATUS` — карта вердикта в статус треда.

- [ ] **Step 1: Написать падающие тесты**

Создать `backend/sender/tests/test_incoming.py`:

```python
"""Что автомат делает с ответом лида. Модели и сети нет.

Агент подменяется целиком: тесты проверяют предохранители и переходы, то есть
всё, что решает `if`. Качество текста проверяет человек в Langfuse.
"""

import pytest

from sender.db import conversation, numbers
from sender.services import config as sender_config, incoming, refusal
from sender.tests.conftest import NOW, FakeTransport
from sender.tests.test_conversation import open_thread
from writer.services.seller import Reply

CONFIG = sender_config.load()


@pytest.fixture
def answered(db, monkeypatch):
    """Тред с непрочитанным входящим и живым номером."""
    numbers.register(db, "+77001112233", "sessions/+77001112233", NOW)
    numbers.set_status(db, "+77001112233", "active")
    open_thread(db, "+77010000001", status="active")
    with db:
        conversation.assign_number(db, "+77010000001", "+77001112233")
        conversation.add_incoming(db, "+77010000001", "а сколько стоит?", "IN1")
    db.execute("UPDATE threads SET seed = ? WHERE thread_id = '+77010000001'",
               ('{"name": "Ромашка", "city": "Алматы", "signals": []}',))
    db.commit()
    monkeypatch.setattr(sender_config, "autopilot", lambda: "replies")
    return db


def говорит(monkeypatch, reply):
    """Агент отвечает заранее заданным исходом, в сеть не ходит."""
    monkeypatch.setattr(incoming, "_seller", lambda: object())
    monkeypatch.setattr(incoming, "_ask", lambda agent, *args, **kwargs: reply)


async def test_free_text_becomes_a_draft_and_a_queued_row(answered, monkeypatch):
    говорит(monkeypatch, Reply(text="Цену назовём после разговора.", status=None, reason=None))

    assert await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW) == "answered"

    draft = answered.execute(
        "SELECT draft_text, angle FROM messages WHERE role = 'outgoing'").fetchone()
    assert draft["draft_text"] == "Цену назовём после разговора."
    assert answered.execute("SELECT kind FROM outbox").fetchone()[0] == "reply"
    thread = conversation.get(answered, "+77010000001")
    assert thread["auto_replies"] == 1 and thread["status"] == "active"
    assert answered.execute("SELECT handled_at FROM messages WHERE role = 'incoming'"
                            ).fetchone()[0] is not None


async def test_autopilot_off_writes_the_draft_but_queues_nothing(answered, monkeypatch):
    """Этап 3 выката: оператор читает каждое сообщение и правит промпт."""
    monkeypatch.setattr(sender_config, "autopilot", lambda: "off")
    говорит(monkeypatch, Reply(text="Ответ агента", status=None, reason=None))

    assert await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW) == "answered"

    assert answered.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    assert answered.execute("SELECT count(*) FROM messages WHERE role = 'outgoing'"
                            ).fetchone()[0] == 1


async def test_the_automaton_answers_in_its_own_words_only_once(answered, monkeypatch):
    """Второе входящее эскалирует независимо от того, что вернул классификатор."""
    answered.execute("UPDATE threads SET auto_replies = 1")
    answered.commit()
    говорит(monkeypatch, Reply(text="ещё один ответ", status=None, reason=None))

    assert await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW) == "escalated"

    assert conversation.get(answered, "+77010000001")["status"] == "escalated"
    assert answered.execute("SELECT count(*) FROM messages WHERE role = 'outgoing'"
                            ).fetchone()[0] == 0, "агента звали, хотя не должны были"


async def test_an_invented_price_never_reaches_the_lead(answered, monkeypatch):
    """Дешёвая сетка под самый дорогой класс ошибки. Стоп-правило выката
    написано ровно про этот случай."""
    говорит(monkeypatch, Reply(text="Обойдётся в 250 000 тенге в месяц.",
                               status=None, reason=None))

    assert await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW) == "escalated"

    assert answered.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    assert conversation.get(answered, "+77010000001")["status"] == "escalated"


async def test_a_long_answer_is_escalated(answered, monkeypatch):
    говорит(monkeypatch, Reply(text="а" * (CONFIG["limits"]["max_reply_chars"] + 1),
                               status=None, reason=None))

    assert await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW) == "escalated"
    assert answered.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


@pytest.mark.parametrize("status,expected", [
    ("interested", "escalated"),
    ("refusal", "closed_refused"),
    ("wrong_number", "closed_junk"),
    ("junk", "closed_junk"),
])
async def test_every_verdict_moves_the_thread(answered, monkeypatch, status, expected):
    говорит(monkeypatch, Reply(text=None, status=status, reason="так решил агент"))

    await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW)

    assert conversation.get(answered, "+77010000001")["status"] == expected


async def test_a_refusal_verdict_writes_the_suppression(answered, monkeypatch):
    written = []
    monkeypatch.setattr(refusal, "_hook",
                        lambda handle, reason: written.append(handle) or True)
    говорит(monkeypatch, Reply(text=None, status="refusal", reason="не интересно"))

    await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW)

    assert written == ["+77010000001"]


async def test_an_unknown_verdict_goes_to_the_human(answered, monkeypatch):
    говорит(monkeypatch, Reply(text=None, status="unknown", reason="чушь"))

    assert await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW) == "escalated"


async def test_a_failing_agent_spends_an_attempt_and_does_not_kill_the_tick(answered, monkeypatch):
    monkeypatch.setattr(incoming, "_seller", lambda: object())

    def взрывается(*args, **kwargs):
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr(incoming, "_ask", взрывается)

    assert await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW) is None

    row = answered.execute("SELECT handle_attempts, handled_at FROM messages"
                           " WHERE role = 'incoming'").fetchone()
    assert row["handle_attempts"] == 1 and row["handled_at"] is None


async def test_the_fourth_visit_escalates_instead_of_calling_the_model(answered, monkeypatch):
    """Вечная пробка: `due` детерминированно отдаёт ту же строку, и без этого
    тик долбил бы её вечно."""
    answered.execute("UPDATE messages SET handle_attempts = 3 WHERE role = 'incoming'")
    answered.commit()
    monkeypatch.setattr(incoming, "_seller",
                        lambda: pytest.fail("модель звали на исчерпанных попытках"))

    assert await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW) == "escalated"

    assert conversation.get(answered, "+77010000001")["status"] == "escalated"
    assert answered.execute("SELECT handled_at FROM messages WHERE role = 'incoming'"
                            ).fetchone()[0] is not None


async def test_nothing_unhandled_is_not_an_error(db):
    assert await incoming.handle_one(db, FakeTransport(), CONFIG, NOW) is None
```

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `cd backend && uv run pytest sender/tests/test_incoming.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sender.services.incoming'`.

- [ ] **Step 3: Создать `backend/sender/services/incoming.py`**

```python
"""Что автомат делает с ответом лида.

Вебхук уже сделал всё быстрое и детерминированное. Здесь — единственное
медленное и вероятностное место системы 3, и поэтому здесь же живут все
предохранители. Все они `if`, ни один не промпт: промпт — это просьба, которую
модель нарушает именно там, где важно.

Историю и карточку компании читаем через thread_store системы 2: своей копии
переписки у sender'а нет и быть не должно, а второе определение «что считается
историей» разъехалось бы с первым молча.
"""

import asyncio
import logging
import re
from datetime import datetime
from functools import lru_cache

from sender import notify
from sender.db import conversation
from sender.services import config as sender_config, pool, queue, refusal
from writer.db import thread_store
from writer.services import config as writer_config, seller

log = logging.getLogger(__name__)

# Режимы, в которых ответ уходит сам. В `off` остаётся черновик под кнопку —
# это и есть этап 3 выката: оператор читает каждое сообщение и правит промпт.
REPLY_MODES = ("replies", "full")

# Цифра рядом с валютой. Самый дорогой класс ошибки — придуманная цена: это
# обещание, которое даёт живому человеку компания, а не модель. Стоп-правило
# выката написано ровно про этот случай.
PRICE = re.compile(r"\d[\d\s.,]*\s*(₸|тг\b|тенге|руб|₽|\$|usd|kzt|eur|€)", re.IGNORECASE)

# Куда вердикт агента двигает тред. Все четыре терминальны для автомата.
VERDICT_STATUS = {
    "interested": "escalated",
    "refusal": "closed_refused",
    "wrong_number": "closed_junk",
    "junk": "closed_junk",
}

ANGLE = "answer"


async def handle_one(db, transport, config: dict, now: datetime) -> str | None:
    """Одно необработанное входящее за тик. None — обрабатывать нечего либо
    попытка сгорела: ни то, ни другое не повод ронять тик."""
    row = conversation.unhandled_incoming(db)
    if row is None:
        return None
    thread = conversation.get(db, row["thread_id"])

    if row["handle_attempts"] >= config["limits"]["max_handle_attempts"]:
        return await _escalate(db, thread, row, now,
                               "не смогли обработать входящее три раза")
    if thread["auto_replies"] >= config["limits"]["auto_replies_per_thread"]:
        return await _escalate(db, thread, row, now,
                               "автомат уже отвечал своими словами")

    with db:
        conversation.count_attempt(db, row["message_id"])
    try:
        reply = await asyncio.to_thread(_ask, _seller(), db, thread["thread_id"])
    except Exception:
        # Попытка потрачена, `handled_at` пуст: следующий тик попробует снова,
        # а четвёртый отдаст тред человеку.
        log.exception("агент не справился с входящим %s", row["message_id"])
        return None

    if reply.status is not None:
        return await _verdict(db, thread, row, reply, now)
    return await _answer(db, transport, thread, row, reply.text, now, config)


async def _answer(db, transport, thread: dict, row: dict, text: str,
                  now: datetime, config: dict) -> str:
    """Свободный текст агента — через те же гейты, что холодное касание."""
    if len(text) > config["limits"]["max_reply_chars"] or PRICE.search(text):
        return await _escalate(db, thread, row, now,
                               "ответ длиннее лимита или содержит цену")
    with db:
        message_id = conversation.add_draft(db, thread["thread_id"], text, ANGLE)
        conversation.bump_auto_replies(db, thread["thread_id"])
        conversation.mark_handled(db, row["message_id"], now)
    if sender_config.autopilot() not in REPLY_MODES:
        log.info("ответ в тред %s остался черновиком: автопилот выключен",
                 thread["thread_id"])
        return "answered"
    try:
        await queue.enqueue(db, transport, thread["thread_id"], now, config,
                            kind="reply")
    except (queue.ClosedThreadError, queue.NothingToQueueError,
            pool.NoNumberAvailableError) as skip:
        log.info("ответ в %s не встал в очередь: %s", thread["thread_id"], skip)
    return "answered"


async def _verdict(db, thread: dict, row: dict, reply, now: datetime) -> str:
    """Вердикт агента. Неизвестный статус — тоже вердикт, только человеку."""
    status = VERDICT_STATUS.get(reply.status)
    if status is None:
        return await _escalate(db, thread, row, now,
                               f"агент вернул статус {reply.status!r}")
    with db:
        conversation.set_status(db, thread["thread_id"], status)
        conversation.mark_handled(db, row["message_id"], now)
    log.info("тред %s -> %s: %s", thread["thread_id"], status, reply.reason)
    if status == "closed_refused":
        # После коммита: шов ходит в чужую базу, и держать на нём открытую
        # транзакцию state.db значило бы блокировать очередь.
        refusal.refuse(thread["thread_id"], f"агент: {reply.reason}")
    if status == "escalated":
        await notify.send(f"{thread['thread_id']} заинтересован: {reply.reason}")
        return "escalated"
    return "closed"


async def _escalate(db, thread: dict, row: dict, now: datetime, why: str) -> str:
    """Тупик для автомата. Из escalated выходит только человек, руками."""
    with db:
        conversation.set_status(db, thread["thread_id"], "escalated")
        conversation.mark_handled(db, row["message_id"], now)
    log.warning("тред %s эскалирован: %s", thread["thread_id"], why)
    await notify.send(f"{thread['thread_id']} — разбирай руками: {why}")
    return "escalated"


@lru_cache
def _seller():
    """Агент собирается один раз на процесс: create_agent компилирует граф."""
    return seller.build(writer_config.load())


def _ask(agent, db, thread_id: str):
    """Синхронный вызов модели — его и уносит to_thread. Отдельной функцией,
    чтобы тест подменял ровно поход в сеть, а не всю обработку."""
    settings = writer_config.load()
    thread = thread_store.thread(db, thread_id)
    return seller.respond(agent, thread["seed"],
                          thread_store.history(db, thread_id),
                          settings["offer"]["text"], session_id=thread_id)
```

- [ ] **Step 4: Разрешить `queue.enqueue` принимать вид строки**

В `backend/sender/services/queue.py` изменить сигнатуру и вызов `outbox.put`:

```python
async def enqueue(db, transport, thread_id: str, now: datetime, config: dict,
                  text: str | None = None, kind: str = "cold") -> int:
```

```python
        outbox_id = outbox.put(db, message_id, thread_id, our_number, now, kind)
```

- [ ] **Step 5: Прогнать тесты**

Run: `cd backend && uv run pytest sender/tests/test_incoming.py sender/tests/test_queue.py -v`
Expected: PASS.

- [ ] **Step 6: Коммит**

```bash
git add backend/sender/services/incoming.py backend/sender/services/queue.py backend/sender/tests/test_incoming.py
git commit -m "feat(sender): обработка входящего с четырьмя предохранителями"
```

---

## Task 12: `writer/services/followup.py` — задача follow-up из роутера

**Files:**
- Create: `backend/writer/services/followup.py`
- Modify: `backend/writer/routes/threads.py:131-139`
- Test: `backend/writer/tests/test_followup.py` (создать)

**Interfaces:**
- Consumes: `agent.followup_task`, `agent.unused_angles`, `agent.draft`, `agent.model`; `thread_store.silent_days`, `thread_store.used_angles`.
- Produces: `followup.task(threads, thread) -> str`; `followup.make(llm, threads, thread, offer) -> Draft`.

**Зачем переезд:** тик системы 3 не может дотянуться до функции внутри роутера FastAPI, а вторая копия правил каденции стоила бы ровно того же, что вторая копия отбора лидов в системе 2.

- [ ] **Step 1: Написать падающие тесты**

Создать `backend/writer/tests/test_followup.py`:

```python
"""Follow-up без нового повода запрещён — и это правило живёт в одном месте.

Тик системы 3 и кнопка оператора обязаны получать одну и ту же задачу: две
копии правил каденции разъехались бы молча.
"""

from writer.db import thread_store
from writer.services import followup

SEED = {"name": "Ромашка", "city": "Алматы", "signals": [
    {"type": "crm_widget", "quote": "виджет Bitrix24"},
    {"type": "ads_platform", "quote": "крутят Яндекс.Директ"},
]}


def store():
    db = thread_store.connect(":memory:")
    thread_store.open_thread(db, "+77010000001", "c1", SEED)
    return db


def test_the_task_names_the_unused_angle():
    db = store()
    message_id = thread_store.add_draft(db, "+77010000001", "Здравствуйте!", "crm_widget")
    db.execute("UPDATE messages SET sent_text = draft_text, sent_at = ?"
               " WHERE message_id = ?", (thread_store.now(), message_id))
    db.commit()

    task = followup.task(db, thread_store.thread(db, "+77010000001"))

    assert "ads_platform" in task, task
    assert "crm_widget" not in task, "повод, который уже использовали, предложен снова"


def test_without_a_new_angle_the_task_asks_for_stop():
    """Follow-up без нового повода — это тот же шаблон, отправленный второй раз."""
    db = store()
    for angle in ("crm_widget", "ads_platform"):
        message_id = thread_store.add_draft(db, "+77010000001", "текст", angle)
        db.execute("UPDATE messages SET sent_text = draft_text, sent_at = ?"
                   " WHERE message_id = ?", (thread_store.now(), message_id))
    db.commit()

    assert "stop=true" in followup.task(db, thread_store.thread(db, "+77010000001"))


def test_make_passes_the_task_and_the_history_to_the_model():
    db = store()

    class FakeLLM:
        def __init__(self):
            self.seen = None

        def invoke(self, messages, config=None):
            self.seen = messages
            return type("Draft", (), {"text": "новый повод", "angle": "ads_platform",
                                      "stop": False})()

    llm = FakeLLM()
    draft = followup.make(llm, db, thread_store.thread(db, "+77010000001"), "оффер")

    assert draft.angle == "ads_platform"
    assert "оффер" in llm.seen[0][1], "оффер не доехал до системного промпта"
```

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `cd backend && uv run pytest writer/tests/test_followup.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'writer.services.followup'`.

- [ ] **Step 3: Создать `backend/writer/services/followup.py`**

```python
"""Следующее касание молчащему лиду: одна задача, два потребителя.

Кнопка оператора в инбоксе и тик системы 3 обязаны получать одну и ту же
задачу — иначе автомат и человек пишут по разным правилам, и калибровать
промпт не по чему. Правило само простое: касание без НОВОГО повода не
отправляется, потому что напоминание о предыдущем сообщении поводом не
является.
"""

import logctx
from writer.db import thread_store
from writer.services import agent


def task(threads, thread) -> str:
    """Задача хода: сколько лид молчит и какой повод ещё не использован."""
    days = thread_store.silent_days(threads, thread["thread_id"], thread_store.now()) or 0
    unused = agent.unused_angles(
        thread["seed"], thread_store.used_angles(threads, thread["thread_id"]))
    return agent.followup_task(days, unused)


def make(llm, threads, thread, offer: str):
    """Черновик следующего касания. stop=true приезжает полем Draft, а не
    исключением: «писать не о чем» — это ответ модели, а не авария."""
    with logctx.entity(thread["thread_id"]):
        return agent.draft(llm, thread["seed"],
                           thread_store.history(threads, thread["thread_id"]),
                           task(threads, thread),
                           session_id=thread["thread_id"],
                           name="sender.followup", offer=offer)
```

- [ ] **Step 4: Роутер зовёт общую функцию**

В `backend/writer/routes/threads.py` заменить `task_of`:

```python
def task_of(kind, threads, thread):
    if kind == "first":
        return agent.FIRST
    return followup.task(threads, thread)
```

и добавить `followup` в импорт: `from writer.services import agent, config, followup`.

- [ ] **Step 5: Прогнать тесты**

Run: `cd backend && uv run pytest writer/tests/ -v`
Expected: PASS.

- [ ] **Step 6: Коммит**

```bash
git add backend/writer/services/followup.py backend/writer/routes/threads.py backend/writer/tests/test_followup.py
git commit -m "feat(writer): задача follow-up переезжает из роутера в сервис"
```

---

## Task 13: `sender/services/followup.py` — созревшие касания

**Files:**
- Create: `backend/sender/services/followup.py`
- Test: `backend/sender/tests/test_followup.py` (создать)

**Interfaces:**
- Consumes: `conversation.due_touch/add_draft/set_status/clear_schedule` (Task 4); `writer.services.followup.make` (Task 12); `queue.enqueue(..., kind=...)` (Task 11).
- Produces: `sender_followup.touch_one(db, transport, config, now) -> str | None` (исход `touched | exhausted | None`); `sender_followup.FULL_MODES = ("full",)`.

- [ ] **Step 1: Написать падающие тесты**

Создать `backend/sender/tests/test_followup.py`:

```python
"""Касание молчащему лиду: срок планируется заранее, текст рождается в срок.

Текст, сгенерированный заранее, пролежит в очереди десять дней и уйдёт
устаревшим — за это время поправится промпт или сменится оффер.
"""

from datetime import timedelta

import pytest

from sender.db import conversation, numbers
from sender.services import config as sender_config, followup
from sender.tests.conftest import NOW, FakeTransport
from sender.tests.test_conversation import open_thread

CONFIG = sender_config.load()
SEED = '{"name": "Ромашка", "city": "Алматы", "signals": [{"type": "ads_platform", "quote": "Директ"}]}'


class FakeDraft:
    def __init__(self, text="Новый повод: у вас Директ", angle="ads_platform", stop=False):
        self.text, self.angle, self.stop = text, angle, stop


@pytest.fixture
def matured(db, monkeypatch):
    numbers.register(db, "+77001112233", "sessions/+77001112233", NOW)
    numbers.set_status(db, "+77001112233", "active")
    open_thread(db, "+77010000001", status="active")
    db.execute("UPDATE threads SET seed = ?, our_number = ?, touch_no = 1,"
               " next_touch_at = ? WHERE thread_id = '+77010000001'",
               (SEED, "+77001112233",
                (NOW - timedelta(hours=1)).isoformat(timespec="seconds")))
    db.commit()
    monkeypatch.setattr(sender_config, "autopilot", lambda: "full")
    return db


def пишет(monkeypatch, draft):
    monkeypatch.setattr(followup, "_llm", lambda: object())
    monkeypatch.setattr(followup, "_write", lambda llm, db, thread, offer: draft)


async def test_a_matured_thread_gets_a_draft_and_a_queued_row(matured, monkeypatch):
    пишет(monkeypatch, FakeDraft())

    assert await followup.touch_one(matured, FakeTransport(), CONFIG, NOW) == "touched"

    draft = matured.execute("SELECT draft_text, angle FROM messages").fetchone()
    assert draft["draft_text"] == "Новый повод: у вас Директ"
    assert matured.execute("SELECT kind FROM outbox").fetchone()[0] == "followup"


async def test_autopilot_off_leaves_the_draft_for_the_button(matured, monkeypatch):
    monkeypatch.setattr(sender_config, "autopilot", lambda: "replies")
    пишет(monkeypatch, FakeDraft())

    assert await followup.touch_one(matured, FakeTransport(), CONFIG, NOW) == "touched"

    assert matured.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    assert matured.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


async def test_a_thread_without_new_angles_is_exhausted(matured, monkeypatch):
    """Поводов больше нет, а напоминание о себе поводом не является."""
    пишет(monkeypatch, FakeDraft(text="", stop=True))

    assert await followup.touch_one(matured, FakeTransport(), CONFIG, NOW) == "exhausted"

    thread = matured.execute("SELECT status, next_touch_at FROM threads").fetchone()
    assert thread["status"] == "exhausted" and thread["next_touch_at"] is None
    assert matured.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


async def test_nothing_matured_is_not_an_error(db):
    assert await followup.touch_one(db, FakeTransport(), CONFIG, NOW) is None


async def test_a_failing_model_clears_nothing_and_does_not_kill_the_tick(matured, monkeypatch):
    """Срок остаётся на месте: следующий тик попробует снова, а лид не теряет
    касание из-за одного таймаута."""
    monkeypatch.setattr(followup, "_llm", lambda: object())

    def взрывается(*args, **kwargs):
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr(followup, "_write", взрывается)

    assert await followup.touch_one(matured, FakeTransport(), CONFIG, NOW) is None

    assert matured.execute("SELECT next_touch_at FROM threads").fetchone()[0] is not None
    assert conversation.get(matured, "+77010000001")["status"] == "active"
```

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `cd backend && uv run pytest sender/tests/test_followup.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sender.services.followup'`.

- [ ] **Step 3: Создать `backend/sender/services/followup.py`**

```python
"""Касание молчащему лиду: срок планируется заранее, текст рождается в срок.

Против «сгенерировать сразу три»: текст пролежит в очереди десять дней, за это
время поправится промпт или сменится оффер — а уйдёт старьё. Против «пусть
решает агент»: агента будит входящее, и молчащего лида будить некому.

Срок ставит `conversation.bump_touch` той же транзакцией, что расходует
касание. Здесь только «срок настал» — и что с этим делать.
"""

import asyncio
import logging
from datetime import datetime
from functools import lru_cache

from sender.db import conversation
from sender.services import config as sender_config, pool, queue
from writer.db import thread_store
from writer.services import agent, config as writer_config, followup as writer_followup

log = logging.getLogger(__name__)

# Follow-up будит человека, который нам не отвечал: он уходит сам только на
# полном автопилоте. В `replies` остаётся черновиком под кнопку.
FULL_MODES = ("full",)

KIND = "followup"


async def touch_one(db, transport, config: dict, now: datetime) -> str | None:
    """Один созревший тред за тик. None — будить некого либо модель не
    ответила: ни то, ни другое не повод ронять тик."""
    thread = conversation.due_touch(db, now)
    if thread is None:
        return None
    try:
        draft = await asyncio.to_thread(_write, _llm(), db, thread,
                                        writer_config.load()["offer"]["text"])
    except Exception:
        # Срок остаётся на месте: следующий тик попробует снова, и лид не
        # теряет касание из-за одного таймаута.
        log.exception("не смогли написать касание в тред %s", thread["thread_id"])
        return None

    if draft.stop:
        with db:
            conversation.clear_schedule(db, thread["thread_id"])
            conversation.set_status(db, thread["thread_id"], "exhausted")
        log.info("тред %s исчерпан: новых поводов нет", thread["thread_id"])
        return "exhausted"

    with db:
        conversation.add_draft(db, thread["thread_id"], draft.text, draft.angle)
        conversation.clear_schedule(db, thread["thread_id"])
    if sender_config.autopilot() not in FULL_MODES:
        log.info("касание в тред %s осталось черновиком: автопилот не полный",
                 thread["thread_id"])
        return "touched"
    try:
        await queue.enqueue(db, transport, thread["thread_id"], now, config, kind=KIND)
    except (queue.ClosedThreadError, queue.NothingToQueueError,
            pool.NoNumberAvailableError) as skip:
        log.info("касание в %s не встало в очередь: %s", thread["thread_id"], skip)
    return "touched"


@lru_cache
def _llm():
    """Клиент модели один на процесс — как у операции writer.outreach."""
    return agent.model(writer_config.load())


def _write(llm, db, thread: dict, offer: str):
    """Синхронный вызов модели — его и уносит to_thread. Отдельной функцией,
    чтобы тест подменял ровно поход в сеть."""
    return writer_followup.make(llm, db, thread_store.thread(db, thread["thread_id"]),
                                offer)
```

**Важно:** `clear_schedule` зовётся сразу после `add_draft`, а не оставляет срок на месте: следующий `next_touch_at` поставит `bump_touch` в момент подтверждённой отправки. Иначе тик писал бы второй черновик тому же треду каждые двадцать секунд, пока очередь не дойдёт до первого.

- [ ] **Step 4: Прогнать тесты**

Run: `cd backend && uv run pytest sender/tests/test_followup.py -v`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add backend/sender/services/followup.py backend/sender/tests/test_followup.py
git commit -m "feat(sender): созревшее касание рождает текст в момент срока"
```

---

## Task 14: Воркер — два новых шага тика

**Files:**
- Modify: `backend/sender/services/worker.py:21-64`
- Test: `backend/sender/tests/test_worker.py`

**Interfaces:**
- Consumes: `incoming.handle_one` (Task 11), `followup.touch_one` (Task 13).
- Produces: `worker.tick` дополнительно возвращает `answered | escalated | closed | touched | exhausted`; `worker.TICK_OUTCOMES` расширен.

- [ ] **Step 1: Написать падающие тесты**

Дописать в `backend/sender/tests/test_worker.py`:

```python
async def test_the_tick_handles_an_incoming_before_it_sends(db, monkeypatch):
    """Ответ лида — единственное событие, у которого есть собеседник, ждущий
    сейчас."""
    order = []

    async def fake_incoming(db_, transport_, config_, now_):
        order.append("incoming")
        return "answered"

    async def fake_followup(db_, transport_, config_, now_):
        order.append("followup")
        return None

    monkeypatch.setattr(worker.incoming, "handle_one", fake_incoming)
    monkeypatch.setattr(worker.followup, "touch_one", fake_followup)

    assert await worker.tick(db, FakeTransport(), CONFIG, INSIDE) == "answered"
    assert order == ["incoming", "followup"]


async def test_a_send_still_wins_the_outcome(db, monkeypatch):
    """Отправка — самое значимое, что случилось за тик: по ней обновляется экран."""
    ready(db)

    async def nothing(db_, transport_, config_, now_):
        return None

    monkeypatch.setattr(worker.incoming, "handle_one", nothing)
    monkeypatch.setattr(worker.followup, "touch_one", nothing)

    assert await worker.tick(db, FakeTransport(), CONFIG, INSIDE) == "sent"


async def test_an_exploding_incoming_does_not_kill_the_tick(db, monkeypatch):
    """Упавшая asyncio-задача исчезает без строки в логе, и ноль отправок
    обнаружился бы через сутки."""
    ready(db)

    async def взрывается(db_, transport_, config_, now_):
        raise RuntimeError("агент лёг")

    async def nothing(db_, transport_, config_, now_):
        return None

    monkeypatch.setattr(worker.incoming, "handle_one", взрывается)
    monkeypatch.setattr(worker.followup, "touch_one", nothing)

    assert await worker.tick(db, FakeTransport(), CONFIG, INSIDE) == "sent"


def test_every_outcome_is_declared():
    """TICK_OUTCOMES читает фронтенд: исход, которого нет в списке, приедет на
    экран строкой, которую никто не ждал."""
    assert {"answered", "escalated", "closed", "touched", "exhausted"} \
        <= set(worker.TICK_OUTCOMES)
```

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `cd backend && uv run pytest sender/tests/test_worker.py -v`
Expected: FAIL — `AttributeError: module 'sender.services.worker' has no attribute 'incoming'`.

- [ ] **Step 3: Реализовать**

В `backend/sender/services/worker.py` добавить импорт:

```python
from sender.services import (config as sender_config, followup, gates, incoming,
                             pool, queue)
```

Расширить константу:

```python
TICK_OUTCOMES = ("sent", "cancelled", "rescheduled", "taken", "retry",
                 "failed", "stuck", "queued", "answered", "escalated",
                 "closed", "touched", "exhausted")
```

Заменить тело `tick`:

```python
async def tick(db, transport, config: dict, now: datetime) -> str | None:
    """Исход тика или None, если делать было нечего.

    Порядок шагов не косметический: ответ лида — единственное событие, у
    которого есть собеседник, ждущий сейчас. Каждый шаг в своём try: агент,
    легший на одном треде, не имеет права остановить очередь.
    """
    for outbox_id in sweep_stuck(db, config, now):
        await notify.send(f"Отправка {outbox_id} висит в sending дольше "
                          f"{config['retry']['stuck_after_minutes']} минут. "
                          "Проверь в телефоне, ушло или нет")
    handled = await _guarded(incoming.handle_one(db, transport, config, now),
                             "входящее")
    matured = await _guarded(followup.touch_one(db, transport, config, now),
                             "касание")
    queued = (sender_config.autopilot() in COLD_MODES
              and await _queue_cold_touch(db, transport, config, now))
    row = outbox.due(db, now)
    if row is None:
        return handled or matured or ("queued" if queued else None)
    try:
        return await _process(db, transport, row, now, config) or handled or matured
    except Exception:
        # `due` детерминированно отдаёт одну и ту же старшую строку, поэтому
        # исключение на ней — вечная пробка: цикл его проглотит, overdue будет
        # расти, а heartbeat останется бодрым. Гасим строку, чтобы очередь
        # двинулась, и разбираемся по логу.
        log.exception("строка %s не обрабатывается — гасим, чтобы очередь шла",
                      row["outbox_id"])
        with db:
            outbox.fail(db, row["outbox_id"], "необрабатываемая строка, см. лог", now)
        return "failed"


async def _guarded(work, what: str) -> str | None:
    """Шаг тика, который ходит в модель. Своё try у каждого: агент, легший на
    одном треде, не имеет права остановить отправки."""
    try:
        return await work
    except Exception:
        log.exception("шаг тика «%s» упал", what)
        return None
```

- [ ] **Step 4: Прогнать тесты**

Run: `cd backend && uv run pytest sender/tests/ -v`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add backend/sender/services/worker.py backend/sender/tests/test_worker.py
git commit -m "feat(sender): тик обрабатывает входящее и созревшее касание"
```

---

## Task 15: Seller становится единственным автором ответа

**Files:**
- Modify: `backend/writer/routes/threads.py:63-93,131-139`
- Modify: `backend/writer/services/agent.py:125-128`
- Modify: `backend/writer/config.toml:16-20`
- Test: `backend/writer/tests/test_prompt.py`, `backend/writer/tests/test_threads.py`

**Interfaces:**
- Consumes: `seller.build`, `seller.respond`, `seller.Reply` (Task 10).
- Produces: `POST /api/threads/{company_id}/draft {"kind": "reply"}` отдаёт черновик seller'а; `agent.REPLY` больше нет; `[cadence]` в `writer/config.toml` больше нет.

**Почему:** оператор на этапе 3 выката обязан видеть ровно тот текст, который ушёл бы автоматически, — ради этого ручное подтверждение и заведено. Два промпта на одну ситуацию дали бы две калибровки и две точки в Langfuse.

- [ ] **Step 1: Написать падающие тесты**

В `backend/writer/tests/test_prompt.py` заменить три обращения к `agent.REPLY` (строки 25, 29, 51) на строковую константу самого теста — задача хода перестала быть свойством `agent`:

```python
TASK = "Задача: ответить на последнюю реплику лида."
```

и подставить `TASK` вместо `agent.REPLY`; в строке 52 заменить `name="writer.reply"` на `name="sender.followup"`, а проверку `run_name` — на то же значение.

Дописать в `backend/writer/tests/test_threads.py`:

```python
def test_the_reply_move_goes_through_the_seller(monkeypatch):
    """У ответа лиду один автор. Второй промпт на ту же ситуацию дал бы вторую
    калибровку и вопрос «а что именно мы правим»."""
    from writer.routes import threads as route
    from writer.services import agent, seller

    assert not hasattr(agent, "REPLY"), "старый одноходовый ответ остался в коде"

    asked = []
    monkeypatch.setattr(route, "seller_agent", lambda: object())
    monkeypatch.setattr(
        seller, "respond",
        lambda agent_, seed, history, offer, *, session_id:
            asked.append(session_id) or seller.Reply(
                text="Ответ продавца", status=None, reason=None))

    thread = {"thread_id": "+77010000001", "seed": {"name": "Ромашка", "signals": []}}
    move = route._seller_move(None, thread, [])

    assert move.text == "Ответ продавца" and move.angle == "answer" and not move.stop
    assert asked == ["+77010000001"], "сессия Langfuse не равна треду"


def test_a_verdict_leaves_no_draft(monkeypatch):
    """Агент решил закрыть тред — черновика в этом ходе нет, и это правильно."""
    from writer.routes import threads as route
    from writer.services import seller

    monkeypatch.setattr(route, "seller_agent", lambda: object())
    monkeypatch.setattr(
        seller, "respond",
        lambda agent_, seed, history, offer, *, session_id:
            seller.Reply(text=None, status="refusal", reason="не интересно"))

    thread = {"thread_id": "+77010000001", "seed": {"name": "Ромашка", "signals": []}}
    assert route._seller_move(None, thread, []) is None
```

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `cd backend && uv run pytest writer/tests/ -v`
Expected: FAIL — `AttributeError: module 'writer.services.agent' has no attribute 'REPLY'` в `threads.py`, либо `assert not hasattr(...)`.

- [ ] **Step 3: Удалить старый ход из `backend/writer/services/agent.py`**

Убрать блок `REPLY = (...)` целиком (строки 125–128). `FIRST` и `followup_task` остаются: у холодного касания и follow-up автор прежний.

- [ ] **Step 4: Развести два вида хода в `backend/writer/routes/threads.py`**

Добавить импорт `from writer.services import agent, config, followup, seller` и функцию сборки агента:

```python
@lru_cache
def seller_agent():
    """Агент собирается один раз на процесс — как и клиент модели."""
    return seller.build(CONFIG)
```

(добавить `from functools import lru_cache` в шапку)

В `make_draft` заменить блок генерации на ветвление по виду хода:

```python
            history = thread_store.history(threads, channel[1])
            proposal = (_seller_move(threads, thread, history)
                        if request.kind == "reply"
                        else _writer_move(request.kind, threads, thread, history))
            if proposal is None:
                raise HTTPException(409, "агент закрыл тред — ответа не будет")
            if not proposal.stop:
                thread_store.add_draft(threads, channel[1], proposal.text, proposal.angle)
            return {**state(leads, threads, company_id), "stop": proposal.stop}
```

и добавить обе функции хода:

```python
def _writer_move(kind, threads, thread, history):
    """Холодное касание и follow-up: один вызов со structured output."""
    task = agent.FIRST if kind == "first" else followup.task(threads, thread)
    return agent.draft(agent.model(CONFIG), thread["seed"], history, task,
                       session_id=thread["thread_id"], name=f"writer.{kind}",
                       offer=CONFIG["offer"]["text"])


def _seller_move(threads, thread, history):
    """Ответ в диалоге — тот же агент, что отвечает автоматически. None, если
    он решил закрыть тред: черновика в этом ходе нет, и это правильно."""
    reply = seller.respond(seller_agent(), thread["seed"], history,
                           CONFIG["offer"]["text"],
                           session_id=thread["thread_id"])
    if reply.status is not None:
        return None
    return Move(text=reply.text, angle="answer", stop=False)
```

и объявить общий вид хода рядом с `KINDS` — `Draft` сюда не годится: его
валидатор режет текст по 700 символов и по списку шаблонных фраз, а это правила
холодного касания, не ответа в живом диалоге:

```python
@dataclass(frozen=True)
class Move:
    """Ход агента глазами ручки: что писать, чем цепляем, писать ли вообще."""
    text: str
    angle: str
    stop: bool
```

с импортом `from dataclasses import dataclass`.

Удалить функцию `task_of` целиком — её работу делят `_writer_move` и `followup.task`.

- [ ] **Step 5: Удалить `[cadence]` из `backend/writer/config.toml`**

Убрать три строки секции (комментарий, заголовок `[cadence]`, `followup_days`). Каденция живёт в `sender/config.toml`; кодом эта секция не читалась — проверено `grep -rn "cadence" --include="*.py"`.

- [ ] **Step 6: Прогнать тесты**

Run: `cd backend && uv run pytest -q`
Expected: PASS.

- [ ] **Step 7: Коммит**

```bash
git add backend/writer/routes/threads.py backend/writer/services/agent.py backend/writer/config.toml backend/writer/tests/
git commit -m "refactor(writer): один автор ответа лиду — seller; каденция только в sender"
```

---

## Task 16: Шов отказа и секрет в Node

**Files:**
- Modify: `backend/collector/api.py:24-40`
- Modify: `backend/sender/node/index.js:13-14,121-142`
- Modify: `backend/.env.example`
- Test: `backend/collector/tests/test_web.py`

**Interfaces:**
- Consumes: `refusal.use` (Task 8); `collector.services.suppression.refuse` (существует).
- Produces: при импорте `collector.api` шов зарегистрирован — `refusal.refuse(handle, reason)` пишет в `state.suppression`; Node шлёт `X-Sender-Secret` из `SENDER_WEBHOOK_SECRET`.

- [ ] **Step 1: Написать падающий тест**

Дописать в `backend/collector/tests/test_web.py`:

```python
def test_the_refusal_seam_is_registered_on_import():
    """Единственное место, где система 3 узнаёт о системе 1. Незарегистрированный
    шов — это потерянный отказ, а отказ юридический контур (F21)."""
    import collector.api  # noqa: F401 — импорт и есть регистрация
    from sender.services import refusal

    assert refusal._hook is not None
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run: `cd backend && uv run pytest collector/tests/test_web.py -v`
Expected: FAIL — `assert None is not None`.

- [ ] **Step 3: Зарегистрировать шов в `backend/collector/api.py`**

Добавить импорты:

```python
from collector.services import suppression as suppression_service
from collector.db import lead as lead_store
from sender.services import refusal as sender_refusal
```

и рядом с регистрацией операции writer'а:

```python
def _write_refusal(handle: str, reason: str) -> bool:
    """Шов к юридическому контуру: отказ, найденный системой 3, пишет система 1.

    Здесь же, а не импортом из sender'а: collector системе 3 недоступен (тест
    графа импортов части 1), и api.py — единственное место, где обе системы
    вообще видят друг друга.
    """
    db = lead_store.connect()
    try:
        return suppression_service.refuse(db, handle, reason)
    finally:
        db.close()


sender_refusal.use(_write_refusal)
```

- [ ] **Step 4: Слать секрет из Node**

В `backend/sender/node/index.js` добавить константу рядом с `PYTHON_URL`:

```javascript
const WEBHOOK_SECRET = process.env.SENDER_WEBHOOK_SECRET ?? "";
```

и заголовок в `notify`:

```javascript
    const response = await fetch(`${PYTHON_URL}/api/sender/webhook`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...(WEBHOOK_SECRET ? { "x-sender-secret": WEBHOOK_SECRET } : {}),
      },
      body: JSON.stringify(payload),
    });
```

- [ ] **Step 5: Дописать `backend/.env.example`**

```
# Общий секрет вебхука: Python сверяет заголовок X-Sender-Secret, Node шлёт его
# из той же переменной. Пустое значение выключает проверку — так работает
# локальная разработка, где Node и Python говорят через 127.0.0.1.
SENDER_WEBHOOK_SECRET=
```

- [ ] **Step 6: Прогнать тесты**

Run: `cd backend && uv run pytest -q`
Expected: PASS.

- [ ] **Step 7: Проверить, что Node запускается с новой переменной**

Run: `cd backend/sender/node && node --check index.js`
Expected: без вывода (синтаксис корректен).

- [ ] **Step 8: Коммит**

```bash
git add backend/collector/api.py backend/sender/node/index.js backend/.env.example backend/collector/tests/test_web.py
git commit -m "feat: шов отказа из системы 3 в систему 1 и подпись вебхука"
```

---

## Task 17: Счётчики и страница «Отправка»

**Files:**
- Modify: `backend/collector/services/metrics.py:63-88`
- Modify: `backend/sender/db/conversation.py`
- Modify: `frontend/app/api.ts`
- Modify: `frontend/app/sender/page.tsx:72-100`
- Test: `backend/collector/tests/test_web.py`, `backend/sender/tests/test_conversation.py`

**Interfaces:**
- Consumes: `conversation` (Task 4).
- Produces: `conversation.counters(db) -> dict` с ключами `waiting` и `escalated`; `/api/stats` отдаёт их в `sender.threads`; `GET /api/sender` отдаёт то же поле `threads`.

- [ ] **Step 1: Написать падающие тесты**

Дописать в `backend/sender/tests/test_conversation.py`:

```python
def test_counters_show_what_waits_for_a_human(db):
    """«Ждут ответа» и «эскалировано» — два числа, по которым видно, что
    автомат встал, а люди ждут."""
    open_thread(db, "+77010000001", status="active")
    open_thread(db, "+77010000002", status="escalated")
    open_thread(db, "+77010000003", status="active")
    with db:
        conversation.add_incoming(db, "+77010000001", "сколько стоит?", "IN1")
        handled = conversation.add_incoming(db, "+77010000003", "ок", "IN2")
        conversation.mark_handled(db, handled, NOW)

    assert conversation.counters(db) == {"waiting": 1, "escalated": 1}
```

Дописать в `backend/collector/tests/test_web.py`:

```python
def test_stats_report_what_waits_for_a_human():
    from collector.services import metrics

    sender = metrics.sender_stats()
    assert set(sender["threads"]) == {"waiting", "escalated"}
```

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run: `cd backend && uv run pytest sender/tests/test_conversation.py collector/tests/test_web.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'counters'`, `KeyError: 'threads'`.

- [ ] **Step 3: Реализовать счётчики в `backend/sender/db/conversation.py`**

```python
def counters(db: sqlite3.Connection) -> dict:
    """Что ждёт человека. `waiting` — входящее, на которое ещё не ответили:
    единственное число, по которому одинаково видно и вставший тик, и агента,
    который молча ничего не делает."""
    return {
        "waiting": db.execute(
            "SELECT count(DISTINCT thread_id) FROM messages"
            " WHERE role = 'incoming' AND handled_at IS NULL").fetchone()[0],
        "escalated": db.execute(
            "SELECT count(*) FROM threads WHERE status = 'escalated'").fetchone()[0],
    }
```

- [ ] **Step 4: Отдать их в `/api/stats`**

В `backend/collector/services/metrics.py::sender_stats` добавить импорт `conversation` и поле:

```python
    from sender.db import conversation, outbox
```

```python
        try:
            rows = db.execute(
                "SELECT status, count(*) FROM numbers GROUP BY status").fetchall()
            queue = outbox.counters(db, datetime.now(timezone.utc))
            threads = conversation.counters(db)
        except sqlite3.OperationalError:
            return {"status": "live", "numbers": {}, "queue": {},
                    "threads": {"waiting": 0, "escalated": 0}, "heartbeat": None}
    return {"status": "live", "numbers": dict(rows), "queue": queue,
            "threads": threads, "heartbeat": worker.heartbeat()}
```

Ветку «базы ещё нет» в начале функции привести к тому же виду:

```python
    if not db_path.exists():
        return {"status": "live", "numbers": {}, "queue": {},
                "threads": {"waiting": 0, "escalated": 0}, "heartbeat": None}
```

- [ ] **Step 5: Отдать их в `GET /api/sender`**

В `backend/sender/routes/sender.py::status` добавить строку в словарь ответа:

```python
            "threads": conversation.counters(db),
```

- [ ] **Step 6: Показать на странице**

В `frontend/app/api.ts` дополнить тип `SenderStatus`:

```typescript
  threads: { waiting: number; escalated: number };
```

В `frontend/app/sender/page.tsx` в блок `.counters` добавить два счётчика после «созрели, но стоят»:

```tsx
          <span className={status && status.threads.waiting > 0 ? "has-replies" : ""}>
            ждут ответа <b>{status?.threads.waiting ?? 0}</b>
          </span>
          <span className={status && status.threads.escalated > 0 ? "has-replies" : ""}>
            эскалировано <b>{status?.threads.escalated ?? 0}</b>
          </span>
```

и в таблицу очереди — колонку вида строки: в `<thead>` добавить `<th>Вид</th>` после `<th>Сообщение</th>`, в `<tbody>` — `<td>{row.kind}</td>` на той же позиции; в `QueueRow` в `api.ts` добавить `kind: string;`.

- [ ] **Step 7: Прогнать тесты и сборку фронтенда**

Run: `cd backend && uv run pytest -q`
Expected: PASS.

Run: `cd frontend && npx tsc --noEmit`
Expected: без ошибок.

- [ ] **Step 8: Коммит**

```bash
git add backend/sender/db/conversation.py backend/collector/services/metrics.py backend/sender/routes/sender.py frontend/app/api.ts frontend/app/sender/page.tsx backend/sender/tests/test_conversation.py backend/collector/tests/test_web.py
git commit -m "feat: «ждут ответа» и «эскалировано» в счётчиках и на странице отправки"
```

---

## Task 18: Документация и ручной смоук на живом WhatsApp

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/PRD.md`
- Modify: `docs/superpowers/specs/2026-08-28-sender-incoming-and-followup-design.md` (шапка: статус)

**Interfaces:**
- Consumes: всё предыдущее.
- Produces: описание системы 3 в `CLAUDE.md` включает входящие, агента-продавца и каденцию; спека помечена реализованной.

Автотестами это не покрывается: живой WhatsApp — единственная часть системы, которую нельзя проверить без телефона.

- [ ] **Step 1: Прогнать весь пакет и убедиться, что всё зелёное**

Run: `cd backend && uv run pytest -q`
Expected: PASS, ноль падений.

- [ ] **Step 2: Поднять три процесса**

```bash
cd backend/sender/node && npm start          # порт 8788
cd backend && uv run python main.py          # порт 8787
cd frontend && npm run dev                   # http://localhost:3000
```

- [ ] **Step 3: Пройти чеклист руками**

Автопилот держать в `off` весь чеклист, кроме шага 6.

1. Написать с личного телефона на рабочий номер, треда с которым нет → приходит уведомление в Telegram, в `messages` пусто.
2. Открыть тред кнопкой в инбоксе, отправить первое касание, ответить с телефона → в карточке треда появляется входящее, в логе строка «входящее в тред … записано, ждёт тика».
3. Через двадцать секунд в карточке появляется черновик ответа агента; в Langfuse — трейс с тегом `sender.reply` в сессии, равной номеру.
4. Ответить с телефона «отпишите меня» → тред уходит в `closed_refused`, номер появляется в `GET /api/suppression`, ответа лиду не приходит.
5. Прислать с телефона голосовое → тред уходит в `escalated`, приходит Telegram, модель не вызывается (в Langfuse нового трейса нет).
6. Переключить автопилот в `replies`, ответить с телефона вопросом → ответ уходит сам, в `outbox` строка с `kind = 'reply'`, и уходит она **вне** окна 10:00–18:00, если проверять вечером.
7. Вернуть автопилот в `off`.

- [ ] **Step 4: Переписать секцию системы 3 в `CLAUDE.md`**

В абзаце про систему 3 (после описания воркера очереди) дописать:

```
Входящее приходит вебхуком `POST /api/sender/webhook` (`kind: "incoming"`,
подпись заголовком `X-Sender-Secret`): ручка делает только быстрое —
канонизует JID в `thread_id`, дедуплицирует по `provider_id`, пишет сообщение
и одной транзакцией гасит расписание треда (`outbox -> cancelled`,
`next_touch_at = NULL`), проверяет стоп-слова регуляркой из `config.toml` и
отдаёт 200. Всё вероятностное — на тике: `sender/services/incoming.py` зовёт
агента-продавца (`writer/services/seller.py`, `create_agent` с единственным
инструментом `classify(return_direct=True)`) и держит четыре предохранителя, и
все четыре — `if`: один автоответ на тред (`limits.auto_replies_per_thread`),
`recursion_limit = 2`, эскалация вместо отправки на длинный ответ или цифру с
валютой, три попытки на входящее. Ответ уходит через тот же outbox и те же
гейты, но по своему окну (`[window.reply]`, круглосуточно): лид написал сам и
ждёт сейчас. Follow-up планируется сроком (`threads.next_touch_at`, ставит
`conversation.bump_touch` той же транзакцией, что расходует касание), а текст
рождается в момент срока (`sender/services/followup.py` →
`writer/services/followup.py`): текст, сгенерированный заранее, пролежит в
очереди десять дней и уйдёт устаревшим. Каденция `[3, 7]` и `max_touches`
живут только в `sender/config.toml`. Отказ пишет система 1 через
`sender/services/refusal.py`, зарегистрированный из `collector/api.py`, — тот
же шов, что монтирует роутеры. Ответ лиду пишет один автор — seller: старый
одноходовый `agent.REPLY` удалён, ход `kind='reply'` в инбоксе зовёт того же
агента, что отвечает автоматически.
```

- [ ] **Step 5: Отметить требования в `docs/PRD.md`**

Требования системы 3, касающиеся входящих, автоответов и каденции, перевести из «заявлено» в реализованные — той же пометкой, какой отмечены требования частей 1 и 2.

- [ ] **Step 6: Пометить спеку реализованной**

В шапке `docs/superpowers/specs/2026-08-28-sender-incoming-and-followup-design.md` заменить строку статуса:

```
Статус: реализовано 2026-08-30, план — `docs/superpowers/plans/2026-08-30-sender-incoming-and-followup.md`.
```

- [ ] **Step 7: Коммит**

```bash
git add CLAUDE.md docs/PRD.md docs/superpowers/specs/2026-08-28-sender-incoming-and-followup-design.md
git commit -m "docs: система 3 замкнута — входящие, агент-продавец и каденция"
```
