# 001 — Решение агента перестаёт быть невидимым: причина в базе и в журнале

**Написан против коммита:** `0102248` (ветка `feat/logs`)
**Труд:** M · **Риск:** низкий · **Зависимости:** нет — этот план первый
**Закрывает находки:** 1 (вердикт и причина не сохраняются), 2 (`incoming.py` не пишет в журнал)

---

## Зачем это нужно

Оператор видит в инбоксе тред со статусом `escalated` и не может узнать, **почему**
автомат отдал его человеку. Причина существует ровно два мгновения: строкой в логе
процесса и сообщением в Telegram. В базе остаётся только слово `escalated`.

`backend/sender/services/incoming.py`, строки 104 и 118 (прочитано на коммите `0102248`):

```python
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
```

```python
async def _escalate(db, thread: dict, row: dict, now: datetime, why: str) -> str:
    """Тупик для автомата. Из escalated выходит только человек, руками."""
    with db:
        conversation.set_status(db, thread["thread_id"], "escalated")
        conversation.mark_handled(db, row["message_id"], now)
    log.warning("тред %s эскалирован: %s", thread["thread_id"], why)
    await notify.send(f"{thread['thread_id']} — разбирай руками: {why}")
    return "escalated"
```

`reply.reason` и `why` в базу не попадают. Всё, что видит UI, — `threads.status`.

Второе: `backend/sender/services/worker.py:24` объявляет словарь исходов тика

```python
TICK_OUTCOMES = ("sent", "cancelled", "rescheduled", "taken", "retry",
                 "failed", "stuck", "queued", "answered", "escalated",
                 "closed", "touched", "exhausted")
```

и `backend/sender/tests/test_worker.py:236` держит его честным. Но `activity.record`
вызывается только для `sent`, `cancelled`, `rescheduled`, `retry`, `failed`, `stuck`
и `idle`. Исходы `answered`, `escalated`, `closed` возвращаются из `incoming.handle_one`
и **не попадают в журнал ни одной строкой** — во всём модуле `incoming.py` нет даже
`import activity`. CLAUDE.md про демонов говорит прямо: «Ни один из них не работает
молча». Здесь молчит самый вероятностный из них.

После этого плана: причина решения лежит в `threads.status_reason` (переживает
перезапуск и ретенцию журнала), а сам факт решения — строкой в `state.activity`,
где его видно на «Процессах» вместе с отправками.

---

## Границы

**Правим только эти файлы:**

- `backend/sender/db/migrate.py` — одна строка в `CONVERSATION_COLUMNS`
- `backend/sender/db/conversation.py` — `FIELDS`, `set_status`
- `backend/sender/services/incoming.py` — записи в журнал + причины
- `backend/sender/routes/webhook.py`, `backend/sender/services/queue.py`,
  `backend/sender/services/pool.py`, `backend/sender/services/followup.py` —
  только новый аргумент в вызовах `set_status`, больше ничего
- `backend/sender/tests/test_incoming.py`, `backend/sender/tests/test_conversation.py` —
  новые тесты
- `backend/CLAUDE.md` нет; правится корневой `CLAUDE.md` — один абзац, шаг 8

**Не трогать:**

- `backend/writer/**` — writer владеет таблицей `threads`, но колонками состояния
  владеет sender. Доливать `status_reason` в `writer/db/thread_store.py::_ensure_columns`
  **запрещено**: там же в файле объяснено, почему (колонка, созданная раньше владельца,
  украла бы у его миграции разметку старых строк). Показ причины в UI — план 003.
- `frontend/**` — целиком. Это чисто бэкендовый план.
- `backend/sender/notify.py` — Telegram остаётся как есть.
- Логи `log.info`/`log.warning` в `incoming.py` **не удалять**: журнал и лог решают
  разные задачи, лог остаётся для отладки процесса.

---

## Шаги

### Шаг 1. Колонка `threads.status_reason`

В `backend/sender/db/migrate.py` в кортеж `CONVERSATION_COLUMNS` (сейчас начинается
на строке 63) добавить строку сразу после `("threads", "our_number", "TEXT")`:

```python
    ("threads", "status_reason", "TEXT"),
```

Ничего больше в этом файле не меняется: `_conversation_state` уже прогоняет весь
кортеж через `ensure_column`, а тот идемпотентен.

**Проверка:**

```bash
cd backend && uv run pytest sender/tests/test_migrate.py -q
```

Ожидается: все тесты проходят (миграция идемпотентна, повторный `apply` не падает).

### Шаг 2. `set_status` требует причину

В `backend/sender/db/conversation.py`:

`FIELDS` (строка 26) — добавить колонку в конец:

```python
FIELDS = ("thread_id, company_id, status, status_reason, our_number, touch_no,"
          " auto_replies, next_touch_at")
```

`set_status` (строка 43) — четвёртый аргумент **обязательный, позиционный**:

```python
def set_status(db: sqlite3.Connection, thread_id: str, status: str,
               reason: str) -> None:
    """Статус треда и почему он такой. Причина обязательна: статус без неё —
    это `escalated` в инбоксе, на который оператор смотрит и не знает, что
    делать. Слово «почему» дешевле здесь, чем в логе процесса, который к
    моменту вопроса уже уехал."""
    if status not in STATUSES:
        raise ValueError(f"неизвестный статус треда: {status}")
    db.execute("UPDATE threads SET status = ?, status_reason = ? WHERE thread_id = ?",
               (status, reason, thread_id))
```

Дефолта `None` быть не должно: это ровно тот случай, когда необязательный аргумент
через полгода означает половину тредов без причины.

Внутренний вызов в этом же файле, `bump_touch` (строка 102):

```python
            set_status(db, thread_id, "exhausted", "касания кончились, лид не отвечал")
```

**Проверка:**

```bash
cd backend && uv run pytest sender/tests/test_conversation.py -q
```

Ожидается: **падение** с `TypeError: set_status() missing 1 required positional
argument: 'reason'` в тестах, вызывающих `set_status` втроём. Это ожидаемо — тесты
правятся на шаге 5. Если падений нет вообще, значит `FIELDS`/`set_status` не
подхватились: проверь, что правил `sender/db/conversation.py`, а не одноимённую
функцию `numbers.set_status`.

### Шаг 3. Причина на каждом из десяти внешних вызовов

Ровно десять вызовов `conversation.set_status` вне тестов. Ниже — файл, строка на
коммите `0102248` и текст причины. Причина — короткая фраза на русском в том же
тоне, что существующие `log.info` рядом.

| Файл | Строка | Статус | Причина |
|---|---|---|---|
| `sender/routes/webhook.py` | 153 | `active` | `"первое сообщение доставлено"` |
| `sender/routes/webhook.py` | 217 | `closed_refused` | `f"стоп-слово в ответе: {refused}"` |
| `sender/routes/webhook.py` | 221 | `escalated` | `"пришло медиа без текста — автомат не читает"` |
| `sender/routes/webhook.py` | 225 | `active` | `"лид ответил, тред снова живой"` |
| `sender/services/queue.py` | 71 | `unreachable` | `"у номера нет WhatsApp"` |
| `sender/services/pool.py` | 95 | `escalated` | `f"номер {number} выбыл, а в треде уже шёл диалог"` |
| `sender/services/pool.py` | 98 | `blocked_channel` | `f"номер {number} выбыл, ищем замену"` |
| `sender/services/pool.py` | 106 | `queued` | `f"переехал на номер {spare}"` |
| `sender/services/pool.py` | 141 | `queued` | `f"нашёлся свободный номер {spare}"` |
| `sender/services/followup.py` | 61 | `exhausted` | `"новых поводов написать нет"` |

Каждый вызов — четвёртым позиционным аргументом, ничего вокруг не меняя. Пример
(`sender/services/followup.py:61`, было / стало):

```python
            conversation.set_status(db, thread["thread_id"], "exhausted")
```
```python
            conversation.set_status(db, thread["thread_id"], "exhausted",
                                    "новых поводов написать нет")
```

**СТОП-условие:** если `grep -rn "set_status(" backend --include="*.py" | grep -v
"numbers.set_status\|def set_status"` находит вызов вне таблицы выше и вне
`backend/sender/tests/`, **не придумывай причину сам** — останови работу и опиши,
что нашёл. Расхождение значит, что план писался против другого коммита.

**Проверка:**

```bash
cd backend && grep -rn "conversation.set_status(" --include="*.py" . | grep -v __pycache__ | grep -v "/tests/" | wc -l
```

Ожидается: `10`. И ни одного вызова с тремя аргументами:

```bash
cd backend && uv run python -c "
import ast, pathlib
bad = []
for path in pathlib.Path('sender').rglob('*.py'):
    if 'tests' in path.parts: continue
    for node in ast.walk(ast.parse(path.read_text())):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'set_status' and len(node.args) == 3):
            bad.append(f'{path}:{node.lineno}')
print(bad or 'все вызовы с причиной')
"
```

Ожидается: `все вызовы с причиной`. (Вызовы `numbers.set_status` берут три
аргумента и в выборку не попадают — у них третий это статус номера, не причина.)

### Шаг 4. Решения агента — в журнал

В `backend/sender/services/incoming.py`:

Импорт (рядом с существующими, в блоке абсолютных импортов верхнего уровня — тем
же порядком, что в `worker.py:14`):

```python
import activity
```

Актор — **`sender.tick`**, а не новый `sender.reply`. Причина не косметическая:
`collector/routes/activity.py::workers()` группирует журнал по актору и считает
молчащим всякого, кто не писал три интервала. Новый актор появлялся бы на
«Процессах» и краснел как умерший демон каждый раз, когда лиды просто не пишут, —
а ответ лиду происходит именно на тике воркера и другого расписания не имеет.

`_verdict` — после закрытия `with db:` и рядом с существующим `log.info`:

```python
    log.info("тред %s -> %s: %s", thread["thread_id"], status, reply.reason)
    activity.record("sender.tick", "closed" if status != "escalated" else "escalated",
                    subject=thread["thread_id"],
                    detail=f"{reply.status}: {reply.reason}")
```

`_escalate` — там же, рядом с `log.warning`:

```python
    log.warning("тред %s эскалирован: %s", thread["thread_id"], why)
    activity.record("sender.tick", "escalated", subject=thread["thread_id"],
                    detail=why)
```

`_answer` — после `with db:`, перед проверкой автопилота:

```python
    activity.record("sender.tick", "answered", subject=thread["thread_id"],
                    detail=text)
```

Причина `detail=text` (а не обрезки): оператору нужно видеть в журнале, **что**
именно автомат написал лиду, не открывая тред. Лимит длины уже проверен выше в
этой же функции (`config["limits"]["max_reply_chars"]`), так что бесконечной
строки здесь быть не может.

**Три требования, каждое ловится тестом на шаге 5:**

1. `activity.record` зовётся **вне** `with db:`. Внутри чужой транзакции своё
   соединение журнала ждало бы освобождения базы, которое наступит только после
   его же возврата, — и падало бы по таймауту. Это написано в докстринге
   `backend/activity.py::record` и не является перестраховкой.
2. Причины передавать в `set_status` тоже — по таблице:
   `incoming.py:115` (`_verdict`) → `reply.reason`;
   `incoming.py:131` (`_escalate`) → `why`.
3. `activity.record`, а не `record_quietly`: `handle_one` вызывается из
   `worker._guarded`, у которого свой `except` — исключение отсюда стоит одного
   пропущенного шага, а не умершей задачи. `record_quietly` предназначен для
   записи **из** `except` демона (см. его докстринг), и здесь он спрятал бы
   потерянное событие.

**Проверка:**

```bash
cd backend && uv run pytest sender/tests/test_incoming.py -q
```

Ожидается: тесты проходят (фикстура `db` в `sender/tests/conftest.py` уже зовёт
`activity.use(path)`, так что журналу есть куда писать). Если видишь
`activity.NotConfiguredError` — значит запись попала в модуль, который тест
поднимает без фикстуры `db`; проверь, что правил только `incoming.py`.

### Шаг 5. Тесты

**`backend/sender/tests/test_conversation.py`** — поправить существующие вызовы
`set_status` (добавить четвёртым аргументом любую осмысленную фразу) и добавить:

```python
def test_a_status_carries_the_reason_it_was_set_with(db):
    """Статус без причины — это `escalated` в инбоксе, на который оператор
    смотрит и не знает, что делать."""
    open_thread(db, "+77010000001")
    with db:
        conversation.set_status(db, "+77010000001", "escalated", "лид назвал цену")
    assert conversation.get(db, "+77010000001")["status_reason"] == "лид назвал цену"
```

**`backend/sender/tests/test_incoming.py`** — новые тесты. Образец стиля бери в
этом же файле: фикстура `answered`, хелпер `говорит(monkeypatch, reply)`, имена
тестов — фразой на английском, докстринг на русском объясняет *зачем*.

```python
async def test_an_escalation_says_why_in_the_thread_and_in_the_journal(answered, monkeypatch):
    """Причина эскалации живёт дольше строки в логе: оператор открывает тред
    через час, а не в ту секунду, когда лид написал."""
    говорит(monkeypatch, Reply(text="это будет 200 000 ₸", status=None, reason=None))

    assert await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW) == "escalated"

    thread = conversation.get(answered, "+77010000001")
    assert thread["status"] == "escalated"
    assert "цен" in thread["status_reason"]
    event = activity.recent(actor="sender.tick")[0]
    assert event["outcome"] == "escalated"
    assert event["subject"] == "+77010000001"
    assert "цен" in (event["detail"] or "")


async def test_a_verdict_keeps_the_agents_own_words(answered, monkeypatch):
    """Вердикт `interested` без «почему» не отличим от `interested` по ошибке
    классификатора — а разбирать их человеку."""
    говорит(monkeypatch, Reply(text=None, status="interested",
                               reason="просит прислать КП на почту"))

    assert await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW) == "escalated"

    thread = conversation.get(answered, "+77010000001")
    assert thread["status_reason"] == "просит прислать КП на почту"
    assert "просит прислать КП" in (activity.recent(actor="sender.tick")[0]["detail"] or "")


async def test_an_answer_shows_in_the_journal_what_we_wrote(answered, monkeypatch):
    """«Что мы пишем лидам» — вопрос, на который журнал обязан отвечать без
    открывания тредов по одному."""
    говорит(monkeypatch, Reply(text="Цену назовём после разговора.", status=None, reason=None))

    assert await incoming.handle_one(answered, FakeTransport(), CONFIG, NOW) == "answered"

    event = activity.recent(actor="sender.tick")[0]
    assert event["outcome"] == "answered"
    assert event["detail"] == "Цену назовём после разговора."
```

Импорты `activity` и `conversation` в `test_incoming.py`: `conversation` уже
импортирован (строка 9), `activity` добавить.

**Проверка:**

```bash
cd backend && uv run pytest sender/tests/ -q
```

### Шаг 6. Документация

В корневом `CLAUDE.md`, в секции «Веб как обвязка, не источник правды», в абзаце
про журнал `state.activity` — дописать одно предложение после фразы про схлопывание
повторов:

> Решение агента по входящему (`answered`, `escalated`, `closed`) ложится туда же
> актором `sender.tick`, а его причина — ещё и в `threads.status_reason`: журнал
> чистится через 14 дней, а вопрос «почему автомат отдал этот тред человеку»
> задаётся и через месяц.

Больше в документации ничего не меняется.

---

## Готово, когда

```bash
cd backend && uv run pytest -q                      # вся суита обеих систем зелёная
cd backend && uv run pytest sender/tests/test_incoming.py sender/tests/test_conversation.py -q
cd backend && uv run python -m activity              # demo журнала: запись, схлопывание, ретенция
```

Плюс — вручную, на живой базе (только чтение):

```bash
cd backend && uv run python -c "
import sqlite3, paths
db = sqlite3.connect(f'file:{paths.state_db()}?mode=ro', uri=True)
print([r[1] for r in db.execute('PRAGMA table_info(threads)')])
"
```

Ожидается: в списке колонок есть `status_reason`. (Колонка появится после первого
старта приложения — миграцию применяет `migrate.apply` на старте.)

---

## Что за этим следить

- **`set_status` теперь требует причину.** Любой новый переход статуса обязан
  сказать почему — это и есть то, что план защищает. Вызов с тремя аргументами не
  скомпилируется, и это сделано намеренно.
- **`writer` терпит отсутствие `status_reason`.** Писать причину в UI будет план
  003, и там она читается тем же приёмом, каким `thread_store.inbox` уже читает
  `status`: через `_has_column`. Жёсткий `SELECT status_reason` из writer'а упал бы
  на базе, где sender ни разу не стартовал.
- **Ретенция журнала — 14 дней** (`activity.RETENTION_DAYS`). Поэтому причина
  дублируется в `threads.status_reason`: только журнал её бы не сохранил.

## Стоп-условия

- Нашёлся вызов `conversation.set_status` вне таблицы шага 3 → останови работу,
  опиши находку. План писался против `0102248`.
- `sender/tests/test_import_graph.py` покраснел → `import activity` в `incoming.py`
  нарушил границу систем. Этого быть не должно (`worker.py` импортирует `activity`
  ровно так же), но если это случилось — останови работу, не правь тест графа.
- Тест из `writer/tests/` покраснел → значит правка уехала за границу sender'а.
  Откати и опиши: `writer` в этом плане не участвует.
