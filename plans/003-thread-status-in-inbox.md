# 003 — «Диалоги» говорят, что со тредом и с какого номера мы пишем

**Написан против коммита:** `0102248`, **освежён против `679751f`** (ветка `feat/logs`)
**Труд:** S–M · **Риск:** низкий
**Зависимости:** **001** и **002** — обе **выполнены и смержены**. `threads.status_reason`,
`frontend/app/outcomes.ts` и классы `.tone.is-*` в `globals.css` уже есть в базе,
заводить их заново не надо.
**Закрывает находки:** 3 (`status` приезжает с бэкенда, фронт его выбрасывает),
5 (`our_number` есть в базе, но не в ответах API)

---

## Зачем это нужно

Бэкенд **уже** отдаёт статус треда. `backend/writer/db/thread_store.py::inbox`
кладёт его в ответ (прочитано на `0102248`):

```python
    status = "t.status" if _has_column(db, "threads", "status") else "'queued'"
    rows = db.execute(
        f"SELECT t.thread_id, t.company_id, t.created_at, {status},"
```

и сортирует эскалированные наверх. А `frontend/app/api.ts:304` этого поля даже
не объявляет:

```ts
export type ThreadSummary = {
  thread_id: string;
  company_id: string;
  company_name: string;
  created_at: string;
  sent: number;
  replies: number;
  drafts: number;
  last_at: string | null;
  last_message: string | null;
};
```

Поле приезжает по сети и выбрасывается на входе. В `frontend/app/threads/page.tsx`
строка инбокса рисует имя, номер, время и счётчик `↩ N` — статуса нет вовсе.
Эскалированный тред отличается от живого только тем, что стоит выше в списке, и
понять это можно, лишь зная про сортировку.

Второе: с какого номера идёт чат, знает `threads.our_number` (заводит
`backend/sender/db/migrate.py:65`, ставит `conversation.assign_number` — один раз
на тред, потому что «для лида сообщение с другого номера — новый чат»). В ответах
API его нет ни в `inbox()`, ни в `state()`. На экране номер появляется ровно в
одном месте — у строки, уже стоящей в очереди (`pending.our_number`), то есть
на несколько минут в жизни треда.

После плана: в строке инбокса — цветной бейдж статуса; в карточке треда — статус,
причина и наш номер, с которого лид видит переписку.

---

## Границы

**Правим только:**

- `backend/writer/db/thread_store.py` — `inbox()` и `thread()`: две колонки, читать
  терпимо
- `backend/writer/routes/threads.py` — `state()`: три поля в ответ
- `frontend/app/api.ts` — типы `ThreadSummary` и `Conversation`
- `frontend/app/threads/page.tsx` — бейдж в строке, статус и номер в карточке
- `frontend/app/globals.css` — при необходимости один класс раскладки
- `backend/writer/tests/test_threads.py` — тесты

**Не трогать:**

- `backend/sender/**` — колонки уже есть после плана 001, миграций здесь нет.
- `frontend/app/cold/page.tsx` и `components/MessageComposer.tsx` — «Холодные»
  живут первыми касаниями, у которых статуса ещё нет; композер общий, и трогать
  его ради статуса значит менять две страницы одной правкой.
- Сортировку инбокса **не менять**: порядок задаёт бэкенд (`inbox()` и его
  докстринг), и страница его не пересортировывает. Это записано в CLAUDE.md.

**Что уже есть в базе `679751f` и переделывать не надо:**

- `frontend/app/outcomes.ts` с `threadStatus()` и `tickOutcome()` — импортируй,
  не переписывай;
- классы `.tone`, `.tone.is-stop|is-attention|is-ok|is-idle` и токен `--attention`
  в `globals.css`;
- колонка `threads.status_reason`, которую заполняет `conversation.set_status`;
- в `threads/page.tsx` строка инбокса **уже** показывает `thread_id`, а шапка
  карточки — ссылку `channelLink` на WhatsApp. Цитаты ниже сняты с этого
  состояния. Расхождение с диском — верь диску и скажи об этом в отчёте.

Номера строк в `globals.css` в шаге 4 и 5 сместились после 002 — ориентируйся на
соседние селекторы (`.inbox-count`, секция «состояния»), а не на числа.

---

## Шаги

### Шаг 1. Две колонки в `inbox()`

В `backend/writer/db/thread_store.py::inbox` (строка 130). Колонки принадлежат
системе 3, поэтому читаются **терпимо** — тем же приёмом, каким там уже читается
`status`; жёсткий `SELECT` упал бы `OperationalError` на базе, где sender ни разу
не стартовал:

```python
    status = "t.status" if _has_column(db, "threads", "status") else "'queued'"
    reason = ("t.status_reason" if _has_column(db, "threads", "status_reason")
              else "NULL")
    number = "t.our_number" if _has_column(db, "threads", "our_number") else "NULL"
```

Дальше — эти выражения в `SELECT` (после `{status}`) и два имени в кортеже
`zip(...)`. **Осторожно с номерами колонок в `ORDER BY`:** запрос сортируется по
`8 DESC NULLS LAST` — это позиционный номер `last_at`. Добавление двух колонок
**после** `{status}` сдвинет его на `10`. Либо вставляй новые колонки в самый
конец списка (тогда `8` остаётся верным), либо поправь число — но не оставляй как
есть: сортировка молча поедет по `drafts`.

Рекомендуется вставлять в конец, кортеж имён тогда такой:

```python
    return [dict(zip(
        ("thread_id", "company_id", "created_at", "status", "sent", "replies",
         "drafts", "last_at", "last_message", "status_reason", "our_number"), row,
    )) for row in rows]
```

**Проверка:**

```bash
cd backend && uv run pytest writer/tests/test_threads.py -q
cd backend && uv run python -c "
import paths
from writer.db import thread_store
db = thread_store.connect(paths.state_db())
rows = thread_store.inbox(db)
print(rows[0] if rows else 'тредов нет — проверь на тестовой базе')
"
```

Ожидается: в словаре есть ключи `status`, `status_reason`, `our_number`, а
`last_at` по-прежнему выглядит временем, а не числом черновиков.

### Шаг 2. Те же три поля в карточке треда

В `backend/writer/db/thread_store.py::thread` (строка 96) уже есть образец
терпимого чтения — добавь `status`, `status_reason` и `our_number` тем же способом
(`_has_column`, иначе литерал). Строка `FIRST_STAGE` в файле показывает, как
подставляется дефолт.

В `backend/writer/routes/threads.py::state` (строка 253) — три поля в ответ:

```python
def state(leads, threads, company_id):
    channel = channel_of(leads, company_id)
    record = thread_store.thread(threads, channel[1])
    return {
        "thread_id": channel[1],
        "channel_kind": channel[0],
        "status": record["status"] if record else None,
        "status_reason": record["status_reason"] if record else None,
        "our_number": record["our_number"] if record else None,
        "messages": thread_store.history(threads, channel[1]),
        "draft": thread_store.pending_draft(threads, channel[1]),
        "stop": False,
    }
```

`record` может быть `None`: `state()` зовётся и до открытия треда (см.
`make_draft`, где тред создаётся ниже по коду). Ронять карточку на этом нельзя.

**Проверка:**

```bash
cd backend && uv run python -m writer.routes.threads
```

Ожидается: `web demo ok — роутер собран, базы открываются`. Список путей в
`demo()` не менялся — новых ручек в этом плане нет.

### Шаг 3. Типы на фронтенде

В `frontend/app/api.ts` — `Conversation` (строка 280):

```ts
export type Conversation = {
  thread_id: string;
  channel_kind: string;
  /** Статус ведёт система 3; null — треда ещё нет, черновик будет первым. */
  status: string | null;
  /** Почему статус такой. null у тредов, чей статус ставили до того, как
   *  причину начали сохранять. */
  status_reason: string | null;
  /** Наш номер, с которого лид видит переписку. Один на тред. */
  our_number: string | null;
  messages: ThreadMessage[];
  draft: { message_id: number; draft_text: string; angle: string | null } | null;
  stop: boolean;
};
```

И `ThreadSummary` (строка 304) — те же три поля плюс `status: string`.

**Проверка:**

```bash
cd frontend && npx tsc --noEmit
```

### Шаг 4. Бейдж в строке инбокса

В `frontend/app/threads/page.tsx`. Импорт:

```tsx
import { threadStatus } from "../outcomes";
```

В строке инбокса — бейдж рядом со счётчиком `↩ N`. Счётчик **не заменять**: он
отвечает на другой вопрос («сколько ответов»), и терять его незачем.

```tsx
              <span className="inbox-marks">
                {(() => {
                  const { label, tone } = threadStatus(thread.status);
                  return <span className={`tone is-${tone}`}>{label}</span>;
                })()}
                <span
                  className={`inbox-count mono${thread.replies > 0 ? " has-replies" : ""}`}
                  title={`${thread.sent} отправлено, ${thread.replies} ответов, ${thread.drafts} черновиков`}
                >
                  {thread.replies > 0 ? `↩ ${thread.replies}` : `→ ${thread.sent}`}
                </span>
              </span>
```

Если IIFE в разметке выглядит чуждо остальному файлу — вынеси бейдж в маленький
компонент в этом же файле, ниже основного (файл собран по «газетной» метафоре:
высокоуровневое сверху). Второй файл под один `<span>` не заводить.

CSS в `frontend/app/globals.css`, рядом с `.inbox-count` (строка 1347):

```css
.inbox-marks {
  display: flex;
  align-items: center;
  gap: 6px;
}
```

**Проверка:** `cd frontend && npm run build`, затем глазами на
`http://localhost:3000/threads`: у эскалированного треда — красный бейдж «ждёт
человека», у живого — нейтральный «живой диалог».

### Шаг 5. Статус, причина и номер в карточке

В `frontend/app/threads/page.tsx`, в блоке `<header className="cold-head">` —
после ссылки «карточка лида». Наш номер и статус: это ответ на вопросы «через
какой номер мы пишем» и «что с этим тредом».

```tsx
              {conversation.our_number && (
                <span className="inbox-sub mono">
                  пишем с {conversation.our_number}
                </span>
              )}
```

И под шапкой, до `<ol className="thread-log">` — статус с причиной:

```tsx
              {conversation.status && (
                <p className="thread-verdict">
                  <span className={`tone is-${threadStatus(conversation.status).tone}`}>
                    {threadStatus(conversation.status).label}
                  </span>
                  {conversation.status_reason
                    ? <span className="inbox-sub">{conversation.status_reason}</span>
                    : <span className="inbox-sub">
                        причина не сохранилась — статус поставлен до того, как её начали писать
                      </span>}
                </p>
              )}
```

Честное «причина не сохранилась» — не заглушка, а тот же приём, что уже применён
в этом файле к промптам старых черновиков (`hasPrompt` и его комментарий): у
тредов, чей статус ставили до плана 001, `status_reason` пуст, и прочерк вместо
объяснения оператор прочитал бы как «причины не было».

CSS:

```css
.thread-verdict {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 12px;
}
```

**Проверка:** `npm run build`, затем на живой странице открыть эскалированный тред —
видно бейдж и фразу вроде «ответ длиннее лимита или содержит цену».

### Шаг 6. Тесты

В `backend/writer/tests/test_threads.py` — образец фикстур и стиля бери там же:

```python
def test_the_inbox_carries_the_status_and_the_number_we_write_from(db):
    """Статус приезжал с бэкенда и раньше, но без причины и без номера строка
    инбокса не отвечала ни на «что с ним», ни на «через какой номер»."""
    ...  # открыть тред, выставить status/status_reason/our_number прямым UPDATE
    row = thread_store.inbox(db)[0]
    assert row["status"] == "escalated"
    assert row["status_reason"] == "лид назвал цену"
    assert row["our_number"] == "+77001112233"


def test_the_inbox_survives_a_database_without_the_sender_columns(db):
    """Колонки принадлежат системе 3, и writer открывает базу там, где sender
    ни разу не стартовал. Жёсткий SELECT уронил бы инбокс целиком."""
    ...  # база, поднятая только thread_store.connect(), без migrate.apply
    row = thread_store.inbox(db)[0]
    assert row["status"] == "queued"
    assert row["status_reason"] is None
    assert row["our_number"] is None


def test_the_inbox_is_still_ordered_by_the_last_reply(db):
    """Регрессия под этот план: новые колонки в SELECT сдвигают позиционный
    номер в ORDER BY, и сортировка молча уезжает на счётчик черновиков."""
    ...  # два треда с разным last_at — свежий обязан быть первым
```

Второй тест — главный: он и есть причина, по которой колонки читаются терпимо.
Третий закрывает ровно ту ловушку, о которой предупреждает шаг 1.

**Проверка:**

```bash
cd backend && uv run pytest writer/tests/ -q
```

---

## Готово, когда

```bash
cd backend && uv run pytest -q
cd backend && uv run python -m writer.routes.threads
cd frontend && npx tsc --noEmit && npm run build
```

И на живом стенде (`cd backend && uv run python main.py` + `cd frontend && npm run dev`):

- `http://localhost:3000/threads` — у каждой строки бейдж статуса, у
  эскалированного он красный;
- открытый тред показывает «пишем с +7…» и причину статуса словами;
- тред без статуса (черновик первого письма) карточку не ломает и бейдж не рисует.

---

## Что за этим следить

- **Терпимое чтение чужих колонок — не перестраховка.** `writer` открывает
  `state.db` там, где `sender` мог не стартовать ни разу. Любой новый
  `SELECT t.<колонка системы 3>` в `thread_store` обязан идти через `_has_column`.
- **Позиционные номера в `ORDER BY`.** В `inbox()` сортировка ссылается на колонку
  числом. Следующая правка `SELECT` рискует ровно так же — тест шага 6 это ловит.
- **`our_number` ставится один раз на тред** (`conversation.assign_number` и его
  докстринг): если на экране он однажды поменяется, это не UI, а переезд треда на
  другой номер после бана (`pool.release`), и выглядеть это должно именно так.

## Стоп-условия

- `thread_store.inbox` пришлось менять сортировку по смыслу (а не поправить число)
  → останови работу: порядок инбокса — решение бэкенда, задокументированное в
  CLAUDE.md, и менять его в плане про показ статуса нельзя.
- Понадобилось добавить `status_reason` в `writer/db/thread_store.py::_ensure_columns`
  → **останови работу**. Колонку заводит план 001 в `sender/db/migrate.py`;
  почему именно там, объяснено в докстринге `_ensure_columns`.
- `frontend/app/threads/page.tsx` на диске сильно расходится с цитатами плана →
  строй на диске и скажи об этом в отчёте.
