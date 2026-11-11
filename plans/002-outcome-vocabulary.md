# 002 — Один словарь исходов: подпись и цвет в одном месте

**Написан против коммита:** `0102248` (ветка `feat/logs`)
**Труд:** S · **Риск:** низкий · **Зависимости:** нет (но выполнять после 001 —
он добавляет причину, которую подписи объясняют)
**Закрывает находку:** 6 (словаря исходов нет, цвета придётся заводить в трёх местах)

---

## Зачем это нужно

Статусов треда восемь (`backend/sender/db/conversation.py:18`):

```python
STATUSES = ("queued", "active", "exhausted", "escalated", "unreachable",
            "closed_refused", "closed_junk", "blocked_channel")
```

Исходов тика тринадцать (`backend/sender/services/worker.py:24`). Их скоро надо
показать на трёх страницах — «Диалоги» (план 003), «Сегодня» (план 004) и
«Процессы» (уже сейчас показывает сырые английские слова). Если каждая страница
завёдет свой `Record<string, string>` с подписями и свой цвет, через месяц
`escalated` будет трёх разных цветов, а новый статус приедет на экран сырым.

Такой словарь в проекте уже есть, и лежит он **на фронтенде** — это осознанное
решение, а не недоделка. `frontend/app/activity/page.tsx:36`:

```tsx
// Имена акторов — словарь бэкенда (activity.record), подписи — чистая
// презентация; порог молчания приезжает с бэкенда и здесь не повторяется.
const ACTOR_LABELS: Record<string, string> = {
  "sender.tick": "Тик воркера",
  ...
```

И контракт между списками уже держится тестом на бэкенде —
`backend/sender/tests/test_worker.py:231`:

```python
def test_every_outcome_is_declared():
    """TICK_OUTCOMES читает фронтенд: исход, которого нет в списке, приедет на
    экран строкой, которую никто не ждал."""
```

Этот план продолжает ровно тот же приём: словарь значений — на бэкенде, подписи и
тон — одним модулем на фронтенде, расхождение ловит тест на бэкенде. **Новой ручки
API здесь нет и не надо**: подпись и цвет — чистая презентация, а не вычисленный
факт вроде порога молчания демона или календаря прогрева.

---

## Границы

**Правим только:**

- `frontend/app/outcomes.ts` — **новый файл**, единственный владелец подписей и тонов
- `frontend/app/globals.css` — четыре класса тона и один токен цвета
- `backend/sender/tests/test_worker.py` — тест, что словари не разъехались

**Не трогать:**

- `frontend/app/activity/page.tsx` — переезд `ACTOR_LABELS` в новый модуль **не
  входит в этот план**: акторы это не исходы, и смешивать их в одном словаре значит
  строить god-object из двух разных вопросов («кто» и «что вышло»).
- Никакие страницы этот модуль пока не импортируют. Его потребители — планы 003 и
  004. Файл без импортёров на этом шаге — норма, а не забытая работа.
- `backend/sender/db/conversation.py`, `backend/sender/services/worker.py` — словари
  значений остаются как есть, они и есть источник правды.

---

## Шаги

### Шаг 1. Модуль подписей и тонов

Создать `frontend/app/outcomes.ts`. Стиль файла — как у `frontend/app/api.ts`:
докстринг сверху объясняет *зачем*, экспортируемые типы над функциями.

```ts
/** Подписи и цвет на каждый исход. Единственное место, где статус превращается
 *  в слово и в тон.
 *
 *  Значения — словарь бэкенда (`conversation.STATUSES`, `worker.TICK_OUTCOMES`),
 *  подписи и тон — чистая презентация, ровно как ACTOR_LABELS на «Процессах».
 *  Расхождение списков ловит бэкенд: sender/tests/test_worker.py.
 *
 *  Тонов четыре, и это не палитра, а смысл: `stop` — нужен человек прямо сейчас,
 *  `attention` — само не разрешится, `ok` — идёт как задумано, `idle` — кончилось
 *  и делать нечего. Пятый тон означал бы, что оператору снова надо думать, какой
 *  цвет что значит.
 */

export type Tone = "stop" | "attention" | "ok" | "idle";

export type Outcome = { label: string; tone: Tone };

/** Статус треда: `threads.status`, владелец — система 3. */
const THREAD_STATUS: Record<string, Outcome> = {
  escalated: { label: "ждёт человека", tone: "stop" },
  closed_refused: { label: "отказался", tone: "idle" },
  closed_junk: { label: "не тот человек", tone: "idle" },
  unreachable: { label: "нет WhatsApp", tone: "idle" },
  exhausted: { label: "касания кончились", tone: "attention" },
  blocked_channel: { label: "ищем номер", tone: "attention" },
  active: { label: "живой диалог", tone: "ok" },
  queued: { label: "ждёт отправки", tone: "ok" },
};

/** Исход тика воркера: `worker.TICK_OUTCOMES`, он же outcome в журнале. */
const TICK_OUTCOME: Record<string, Outcome> = {
  escalated: { label: "отдал человеку", tone: "stop" },
  failed: { label: "не отправилось", tone: "stop" },
  stuck: { label: "судьба неизвестна", tone: "stop" },
  retry: { label: "повтор", tone: "attention" },
  rescheduled: { label: "перенесено", tone: "attention" },
  cancelled: { label: "отменено", tone: "attention" },
  crashed: { label: "авария демона", tone: "stop" },
  sent: { label: "отправлено", tone: "ok" },
  answered: { label: "ответил сам", tone: "ok" },
  touched: { label: "касание", tone: "ok" },
  queued: { label: "в очередь", tone: "ok" },
  closed: { label: "тред закрыт", tone: "idle" },
  exhausted: { label: "касания кончились", tone: "idle" },
  taken: { label: "занято другим тиком", tone: "idle" },
  idle: { label: "нечего делать", tone: "idle" },
  incoming: { label: "лид написал", tone: "ok" },
  duplicate: { label: "повтор от транспорта", tone: "idle" },
  refused: { label: "стоп-слово", tone: "idle" },
  healthy: { label: "номера в порядке", tone: "ok" },
  transport_down: { label: "транспорт не отвечает", tone: "stop" },
  started: { label: "запущено", tone: "ok" },
  finished: { label: "готово", tone: "ok" },
};

/** Неизвестное значение показываем как есть и нейтральным тоном: выдуманная
 *  подпись врёт, а сырое слово честно говорит «этого мы не ждали». */
function unknown(value: string): Outcome {
  return { label: value, tone: "idle" };
}

export function threadStatus(status: string | null | undefined): Outcome {
  if (!status) return unknown("статус неизвестен");
  return THREAD_STATUS[status] ?? unknown(status);
}

export function tickOutcome(outcome: string): Outcome {
  return TICK_OUTCOME[outcome] ?? unknown(outcome);
}
```

Заметь: `TICK_OUTCOME` покрывает не только `worker.TICK_OUTCOMES`, но и исходы
остальных акторов журнала (`incoming`, `duplicate`, `refused` от вебхука;
`healthy`, `transport_down` от монитора; `started`, `finished` от воркера джоб;
`crashed` от любого). Они приходят в ту же колонку `activity.outcome`, и второй
словарь на них был бы тем самым расхождением, от которого этот файл и заводится.

**Проверка:**

```bash
cd frontend && npx tsc --noEmit
```

Ожидается: без ошибок.

### Шаг 2. Четыре класса тона в CSS

В `frontend/app/globals.css`. Токен — в `:root` (сейчас блок начинается на строке 4),
после `--error-soft`:

```css
  --attention: #ab6400;
```

Значение из `docs/DESIGN.md` (`accent-warning`) — своих цветов не придумываем.

Классы — в секцию «состояния» (сейчас начинается комментарием на строке 1360),
рядом с существующими `.status.is-*`:

```css
/* Тон исхода: один класс — один смысл. Подпись и тон приходят из
   app/outcomes.ts, здесь только цвет. */
.tone {
  font-size: 12px;
  font-weight: 500;
  padding: 3px 9px;
  border-radius: 999px;
  white-space: nowrap;
}

.tone.is-stop {
  background: var(--error-soft);
  color: var(--error);
}

.tone.is-attention {
  background: #fdf6e7;
  color: var(--attention);
}

.tone.is-ok {
  background: var(--surface-strong);
  color: var(--ink);
}

.tone.is-idle {
  background: transparent;
  color: var(--muted);
}
```

`.status.is-*` (строка 624) **не удалять и не переписывать**: это статусы джоб на
«Обзоре» и «Процессах», другая сущность и другой словарь.

**Проверка:**

```bash
cd frontend && npm run build
```

Ожидается: сборка проходит.

### Шаг 3. Тест, что словари не разъехались

В `backend/sender/tests/test_worker.py`, рядом с существующим
`test_every_outcome_is_declared` (строка 231) — тот же приём, только теперь список
читается из настоящего файла фронтенда, а не дублируется в тесте:

```python
def test_the_frontend_has_a_label_for_every_tick_outcome():
    """Исход без подписи приедет на экран сырым английским словом. Список
    подписей лежит во фронтенде (чистая презентация), но полнота его —
    свойство бэкенда: словарь значений здесь."""
    vocabulary = (Path(__file__).parents[3] / "frontend/app/outcomes.ts").read_text()
    missing = [outcome for outcome in worker.TICK_OUTCOMES
               if f"\n  {outcome}: {{" not in vocabulary]
    assert not missing, f"нет подписи для исходов: {missing}"


def test_the_frontend_has_a_label_for_every_thread_status():
    """То же для статусов треда: `escalated` без подписи — это слово
    «escalated» в инбоксе оператора."""
    vocabulary = (Path(__file__).parents[3] / "frontend/app/outcomes.ts").read_text()
    missing = [status for status in conversation.STATUSES
               if f"\n  {status}: {{" not in vocabulary]
    assert not missing, f"нет подписи для статусов: {missing}"
```

Импорты в начале файла: `from pathlib import Path` и `from sender.db import
conversation` (проверь, нет ли их уже — `conversation` в этом файле, скорее всего,
уже импортирован).

**Путь к фронтенду проверь до того, как писать тест:**

```bash
cd backend && uv run python -c "
from pathlib import Path
print((Path('sender/tests/test_worker.py').resolve().parents[3] / 'frontend/app/outcomes.ts'))
"
```

Ожидается путь, оканчивающийся на `Scrapling-Test/frontend/app/outcomes.ts`. Если
уровней получилось не столько — **поправь число в `parents[N]`, а не путь целиком**,
и не подставляй абсолютный путь: тест обязан работать из любого клона.

**СТОП-условие:** если оба теста красные из-за того, что `outcomes.ts` найти не
удалось (`FileNotFoundError`), а путь напечатался верный — останови работу и опиши.
Тест, молча пропускающий проверку, когда файла нет, хуже отсутствующего теста.

**Проверка:**

```bash
cd backend && uv run pytest sender/tests/test_worker.py -q
```

---

## Готово, когда

```bash
cd frontend && npx tsc --noEmit && npm run build
cd backend && uv run pytest sender/tests/test_worker.py -q
cd backend && uv run pytest -q
```

Плюс: оба списка покрыты полностью — проверяется теми же двумя тестами, красный
тест называет пропущенные значения по именам.

---

## Что за этим следить

- **Новый статус треда или новый исход тика теперь ломает тест на бэкенде**, а не
  приезжает сырым словом на экран. Это и есть смысл плана: `conversation.STATUSES`
  и `worker.TICK_OUTCOMES` остаются источником правды, а `outcomes.ts` обязан за
  ними успевать.
- **Тонов четыре и пятого быть не должно.** Пятый цвет означает, что оператор
  снова читает легенду вместо экрана. Новый исход выбирает один из четырёх.
- **Исходы чужих акторов** (`webhook`, `sender.monitor`, `jobs`) тестом не покрыты:
  их словарь не объявлен на бэкенде кортежом, и объявлять его ради теста —
  отдельная работа, которая в этот план не входит. `unknown()` показывает такое
  значение сырым и нейтральным, то есть честно.

## Стоп-условия

- `npm run build` падает не на `outcomes.ts` → останови работу: этот план CSS и
  один новый файл, ломать сборку ему нечем.
- Захотелось импортировать `outcomes.ts` в какую-нибудь страницу «чтобы файл не
  лежал зря» → **не надо**, это планы 003 и 004. Файл без импортёров здесь ожидаем.
