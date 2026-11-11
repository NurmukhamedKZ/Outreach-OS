# 004 — «Обзор» становится «Сегодня»: список задач оператора и бейдж в сайдбаре

**Написан против коммита:** `0102248`, **освежён против `a15e703`** (ветка `feat/logs`)
**Труд:** M–L · **Риск:** средний (переписывается главная страница)
**Зависимости:** **001**, **002**, **003** — все три **выполнены и смержены**.
**Закрывает находку:** 4 («Обзор» — счётчики и гайд, ни одной ссылки в задачу) +
запрос на уведомления бейджами в UI

---

## Зачем это нужно

`frontend/app/page.tsx` — это три карточки счётчиков и раскрывающийся гайд.
Счётчики отвечают на «сколько всего», но не на «что от меня требуется сейчас».
Оператор, у которого три лида ответили «интересно», видит на главной
`тредов открыто 42` и идёт искать их руками в «Диалогах».

При этом всё нужное уже посчитано. `backend/collector/services/metrics.py::sender_stats`
кладёт в снапшот `threads: {"waiting": …, "escalated": …}` и `queue`, а
`writer_stats` — `drafts`. Снапшот приезжает по SSE в `LiveProvider` и лежит в
контексте на каждой странице. Не хватает ровно двух вещей: **списка задач с
именами и причинами** (счётчик `escalated: 3` не говорит, кто эти трое) и
**ссылки, ведущей в диалог**.

`frontend/app/api.ts:200` при этом объявляет тип снапшота беднее, чем он есть:

```ts
  sender: { status: string; numbers: Record<string, number> };
```

`queue` и `threads` бэкенд отдаёт, а тип их не знает — то же самое, что было со
`status` в плане 003.

После плана: `/` — список задач на сегодня, каждая строка ведёт в диалог
(оттуда уже есть ссылка на карточку лида), счётчики остаются ниже, а в сайдбаре
у «Сегодня» и «Диалогов» — бейдж с числом задач.

---

## Границы

**Правим только:**

- `backend/tasks.py` — **новый** модуль верхнего уровня, только чтение
- `backend/collector/routes/tasks.py` — **новая** ручка `GET /api/tasks`
- `backend/collector/api.py` — шов `tasks.use(...)` и `include_router`
- `backend/tests/` или `backend/collector/tests/` — тесты (см. шаг 6)
- `frontend/app/api.ts` — тип `Task`, `fetchTasks`, честный тип `DashboardStats.sender`
- `frontend/app/page.tsx` — список задач
- `frontend/components/Sidebar.tsx` — подпись и бейдж
- `frontend/app/threads/page.tsx` — открыть тред по `?company=`
- `frontend/app/globals.css` — классы списка задач и бейджа
- корневой `CLAUDE.md` — секции про фронтенд и про веб-обвязку

**Не трогать:**

- `backend/collector/services/metrics.py` — снапшот остаётся как есть, задачи это
  отдельный вопрос и отдельная ручка. Тащить список задач в снапшот, который
  улетает по SSE каждому подписчику на каждом событии, — это имена и причины в
  каждом кадре.
- `backend/analytics.py` — воронка не про задачи.
- «Гайд» на `/` (`function Guide`) — **сохранить целиком**. Это единственная
  инструкция в продукте, и удалять её ради красивой главной нельзя.
- Счётчики трёх систем на `/` — **сохранить**, просто ниже задач.

**Что уже есть в базе `a15e703` и переделывать не надо:**

- `frontend/app/outcomes.ts` (`threadStatus`, `tickOutcome`) и классы `.tone.is-*`;
- в `frontend/app/api.ts` типы `Conversation` и `ThreadSummary` уже несут
  `status`, `status_reason`, `our_number` — трогать их в этом плане не нужно,
  правится только `DashboardStats.sender` и добавляется тип `Task`;
- `frontend/app/threads/page.tsx` уже импортирует `threadStatus` и рисует бейдж
  статуса; этому плану остаётся **только** приём `?company=` в шаге 5;
- колонки `threads.status_reason` и `threads.our_number` заполняются системой 3.

**Перед фронтенд-кодом прочитай `frontend/AGENTS.md`.** В нём сказано, что этот
Next.js (16.3.1) расходится с тем, что модели помнят, и что документацию надо
брать из `node_modules/next/dist/docs/`. Для шага 5 относящийся файл —
`01-app/01-getting-started/03-layouts-and-pages.md`, секция «What to use and
when»: `useSearchParams` — правильный выбор, когда параметр нужен только на
клиенте, что здесь и есть.

**Что сознательно не делаем** (и почему — чтобы это не выглядело забытым):

- `queue.overdue` («созрели, но стоят») в список задач **не попадает**: это симптом
  аварии транспорта, а не задача про конкретного лида, и он уже показан на
  `/sender`. Задача, которую нельзя закрыть кликом, в списке задач учит список
  игнорировать.
- Браузерных push-уведомлений здесь нет: выбраны бейджи в UI. `sender/notify.py`
  (Telegram) остаётся единственным каналом, который догоняет оператора вне вкладки.

---

## Шаги

### Шаг 1. `backend/tasks.py` — что требуется от человека

Новый модуль верхнего уровня. **Образец — `backend/analytics.py`**: тот же шов
`use()`, то же чтение обеих баз в режиме `ro`, тот же приём с отсутствующим
файлом базы. Прочитай его целиком перед тем, как писать этот — повторять его
структуру важнее, чем изобретать свою.

Почему верхний уровень, а не `collector/services/`: данные лежат в трёх системах
(`threads`/`messages` — система 2, `outbox` — система 3, имя компании — derived
системы 1), и ни одна из них не имеет права знать две другие. Это дословно та же
причина, что записана в докстринге `analytics.py`.

Контракт:

```python
"""Что требуется от человека прямо сейчас. Одна строка — одна задача.

Верхний уровень backend/, рядом с analytics.py и activity.py, по той же причине:
задача собирается из трёх систем (тред и черновик — система 2, очередь — система
3, имя компании — derived системы 1), и ни одна из них не имеет права знать две
другие.

Счётчик отвечает на «сколько», задача — на «кто и почему». `escalated: 3` не
говорит, кто эти трое, и оператор идёт искать их руками.

Только чтение: обе базы открываются в режиме ro.
"""

KINDS = ("escalated", "draft", "stuck")
```

DTO — `dataclass`, а не сырой dict через границу (правило из CLAUDE.md):

```python
@dataclass(frozen=True)
class Task:
    """Задача оператора. `href` не здесь: куда ведёт строка — вопрос фронтенда."""
    kind: str            # escalated | draft | stuck
    company_id: str
    company_name: str
    thread_id: str
    our_number: str | None
    why: str             # причина словами, готовая к показу
    at: str | None       # когда стало задачей, ISO
```

`tasks(limit: int = 50) -> list[dict]` собирает три запроса и отдаёт их одним
списком в порядке срочности: `escalated` → `stuck` → `draft`.

1. **`escalated`** — `SELECT thread_id, company_id, status_reason, our_number FROM
   threads WHERE status = 'escalated'`. `why` = `status_reason`, а при `NULL` —
   `"причина не сохранилась"` (у тредов старше плана 001). `at` — `handled_at`
   последнего входящего этого треда.
2. **`stuck`** — строки `outbox` со `status = 'stuck'`: судьба сообщения
   неизвестна, и решает её человек, глядя в телефон (см. докстринг
   `worker._after_a_broken_tick`). `why` = `"не знаем, ушло ли — проверь в телефоне"`,
   `at` = `updated_at`.
3. **`draft`** — первые касания, ждущие проверки. Запрос **не переписывать**:
   определение «черновик первого касания» уже есть в
   `writer/db/thread_store.py::cold_drafts` со всеми оговорками (последний
   черновик треда, а не все). Импортировать `writer` из `tasks.py` нельзя, поэтому
   запрос воспроизводится — ровно как `writer/db/leads_source.py` воспроизводит
   правила отбора F19/F21 вместо импорта `export.py`. **Скопируй SQL из
   `cold_drafts` дословно** и оставь комментарий, откуда он и почему копия.
   `why` = `"черновик первого письма ждёт проверки"`.

Имена компаний — вторым запросом в `derived.db` (тем же приёмом, что
`analytics.py::_segments`), одним `WHERE company_id IN (...)` на весь список, а не
запросом на строку. Нет имени — показываем `company_id`, а не «—».

Терпимость к отсутствию: файла `state.db` нет, или в нём нет таблицы `threads`
(sender/writer не стартовали) → пустой список, не исключение. `analytics.py`
делает это через `_empty(days)` и `_has_table` — повтори.

**Проверка:**

```bash
cd backend && uv run python -c "
import paths, tasks
from collector.services import store
tasks.use(paths.state_db(), store.DERIVED)
for row in tasks.tasks():
    print(row['kind'], row['company_name'], '—', row['why'])
"
```

Ожидается: список задач или пусто. Исключений нет ни при пустой базе, ни при
отсутствующем файле.

### Шаг 2. Ручка `GET /api/tasks`

`backend/collector/routes/tasks.py` — по образцу `collector/routes/analytics.py`
(он на 18 строк, и этот должен быть такого же размера):

```python
"""Задачи оператора для страницы «Сегодня».

Порядок срочности считает бэкенд, а не страница: «эскалированный тред важнее
непроверенного черновика» — правило предметной области, и второй его копией во
фронтенде мы получили бы два разных представления о том, что делать первым.
"""

from fastapi import APIRouter

import tasks

router = APIRouter(prefix="/api/tasks")

MAX_LIMIT = 200


@router.get("")
def today(limit: int = 50) -> dict:
    return {"tasks": tasks.tasks(limit=min(max(limit, 1), MAX_LIMIT))}
```

В `backend/collector/api.py`:

- импорт `import tasks` рядом с `import analytics` (строка 24);
- шов рядом с `analytics.use(...)` (строка 67):
  `tasks.use(paths.state_db(), store.DERIVED)`;
- роутер рядом с `analytics_routes` (строка 115):
  `app.include_router(tasks_routes.router)`, импорт добавить в существующий
  групповой `from collector.routes import (...)`.

**Проверка:**

```bash
cd backend && uv run python main.py &
sleep 3 && curl -s localhost:8787/api/tasks | head -c 400; kill %1
```

Ожидается: JSON с ключом `tasks`.

### Шаг 3. Типы и запрос на фронтенде

В `frontend/app/api.ts`:

```ts
/** Что требуется от человека сейчас. Порядок задаёт бэкенд — по срочности. */
export type Task = {
  kind: "escalated" | "draft" | "stuck";
  company_id: string;
  company_name: string;
  thread_id: string;
  our_number: string | null;
  why: string;
  at: string | null;
};

export function fetchTasks(limit = 50) {
  return json<{ tasks: Task[] }>(`/api/tasks?limit=${limit}`);
}
```

И честный тип снапшота (сейчас строка 200) — бэкенд эти поля уже отдаёт:

```ts
  sender: {
    status: string;
    numbers: Record<string, number>;
    queue: { queued?: number; sent_today?: number; overdue?: number };
    threads: { waiting: number; escalated: number };
    heartbeat: string | null;
  };
```

Поля `queue` необязательные не для красоты: `metrics.sender_stats` возвращает
`"queue": {}` на базе без таблиц системы 3.

**Проверка:** `cd frontend && npx tsc --noEmit`

### Шаг 4. Страница «Сегодня»

`frontend/app/page.tsx`. Порядок сверху вниз: **задачи → счётчики → история
запусков → гайд** (газетная метафора: важное первым).

Задачи тянутся запросом при монтировании и перечитываются на `refreshTick` из
`useLive()` — тем же приёмом, что `threads/page.tsx` перечитывает инбокс.

Каркас:

```tsx
      <header className="page-head">
        <h1>Сегодня</h1>
        <span className="page-sub">
          {tasks === null
            ? "смотрим, что требуется…"
            : tasks.length === 0
              ? "задач нет — автомат справляется сам"
              : `${tasks.length} ${plural(tasks.length)} требуют вас`}
        </span>
      </header>

      <section className="card">
        {tasks?.map((task) => (
          <Link
            key={`${task.kind}:${task.thread_id}`}
            href={hrefOf(task)}
            className="row task-row"
          >
            <span className="row-text">
              <span className="inbox-name">{task.company_name}</span>
              <span className="inbox-sub">{task.why}</span>
              <span className="inbox-sub mono">
                {task.thread_id}
                {task.our_number ? ` · пишем с ${task.our_number}` : ""}
              </span>
            </span>
            <span className={`tone is-${TASK_TONE[task.kind]}`}>{TASK_LABEL[task.kind]}</span>
          </Link>
        ))}
        {tasks !== null && tasks.length === 0 && (
          <p className="placeholder">
            Ничего не ждёт. Новые задачи появятся сами: лид ответит, автомат
            отдаст тред человеку или черновик встанет на проверку.
          </p>
        )}
      </section>
```

Ссылки:

```tsx
/** Куда ведёт задача. Черновик проверяют на «Холодных», диалог — в «Диалогах». */
function hrefOf(task: Task) {
  return task.kind === "draft"
    ? "/cold"
    : `/threads?company=${encodeURIComponent(task.company_id)}`;
}
```

`plural(n)` в подзаголовке — маленькая функция, которую надо написать здесь же,
рядом с `hrefOf`: русское склонение по числу («1 задача», «3 задачи»,
«5 задач»). Правило обычное — по последним двум цифрам; готовой такой функции в
проекте нет, проверял.

Подписи и тон задач — три строки рядом с `hrefOf`, **не в `outcomes.ts`**: `kind`
задачи это не статус треда и не исход тика, и складывать их в один словарь значит
делать из него god-object.

```tsx
const TASK_LABEL: Record<Task["kind"], string> = {
  escalated: "ждёт вас",
  stuck: "проверить в телефоне",
  draft: "проверить черновик",
};

const TASK_TONE: Record<Task["kind"], "stop" | "attention"> = {
  escalated: "stop",
  stuck: "stop",
  draft: "attention",
};
```

Счётчики (`<div className="metrics">…`), «История запусков» и `<Guide />`
переезжают **ниже без изменений разметки**. Скелетоны счётчиков сохранить.

CSS в `globals.css`:

```css
.task-row {
  grid-template-columns: 1fr auto;
  align-items: center;
  text-decoration: none;
  color: inherit;
}
```

**Проверка:** `npm run build`, затем на `http://localhost:3000/` — задачи сверху,
клик по строке ведёт в диалог, счётчики и гайд на месте.

### Шаг 5. Открыть тред по ссылке + бейдж в сайдбаре

**`frontend/app/threads/page.tsx`** — принять `?company=`:

```tsx
import { useSearchParams } from "next/navigation";
...
  const params = useSearchParams();
  const [selected, setSelected] = useState<string | null>(params.get("company"));
```

`useState(params.get("company"))` — начальное значение, а не `useEffect`: ссылка с
«Сегодня» открывает страницу заново, и переход внутри страницы (клик по строке
инбокса) не должен спорить с параметром URL. Если в дереве появится
`useSearchParams`, Next может потребовать `Suspense` вокруг клиентского
компонента — на `npm run build` это видно сразу, и лечится обёрткой в
`app/threads/page.tsx`, а не отключением проверки.

**`frontend/components/Sidebar.tsx`:**

- подпись `{ href: "/", label: "Обзор", … }` → `label: "Сегодня"`. Маршрут `/`
  **оставить**: ссылки, закладки и `RunBar` на него уже указывают.
- бейдж с числом задач у «Сегодня» и «Диалогов» — из снапшота, который в контексте
  уже есть; новых запросов не заводить:

```tsx
  // Задачи считаются из снапшота, а не запросом: он и так приезжает по SSE на
  // каждое событие, и второй источник числа разошёлся бы с первым.
  const waiting = (stats?.sender.threads.escalated ?? 0) + (stats?.writer.drafts ?? 0);
```

Рисуется `<span className="nav-badge">{waiting}</span>` при `waiting > 0` у
`/` и `/threads`. Существующий `nav-live` (точка активной джобы) **не трогать**:
он про другое — про идущую джобу.

```css
.nav-badge {
  margin-left: auto;
  min-width: 18px;
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--error);
  color: #fff;
  font-size: 11px;
  font-weight: 600;
  text-align: center;
  font-variant-numeric: tabular-nums;
}
```

`nav-live` тоже использует `margin-left: auto` — проверь, что вместе они не
разъезжаются, и при конфликте оставь `auto` бейджу, а точке дай фиксированный
отступ.

**Проверка:** `npm run build`; на живом стенде — бейдж появляется, клик с «Сегодня»
открывает нужный тред сразу.

### Шаг 6. Тесты

Тестов на `backend/tasks.py` в проекте пока негде положить по образцу
`analytics.py` — проверь `ls backend/collector/tests/ | grep analytics` и положи
рядом с ним, тем же именем (`test_tasks.py`), с теми же фикстурами. Если тестов
аналитики нет вовсе — заведи `backend/collector/tests/test_tasks.py`, фикстуру
базы бери из `sender/tests/conftest.py` (там `state.db` поднимается настоящими
владельцами таблиц).

Обязательные тесты:

```python
def test_an_escalated_thread_becomes_a_task_with_its_reason():
    """Счётчик `escalated: 3` не говорит, кто эти трое, — за этим план и писался."""

def test_a_thread_without_a_saved_reason_says_so_instead_of_guessing():
    """Треды старше плана 001 причины не имеют, и придуманная фраза врала бы."""

def test_tasks_are_ordered_by_urgency():
    """Порядок — правило предметной области, и он принадлежит бэкенду."""

def test_no_state_database_means_no_tasks_and_no_exception():
    """На чистой установке страница обязана показать «задач нет», а не 500."""

def test_a_draft_that_was_regenerated_appears_once():
    """Копия SQL из cold_drafts обязана сохранить оговорку про последний
    черновик треда: «Перегенерировать» оставляет предыдущий вариант в таблице."""
```

Последний — главный: он и есть цена копии запроса.

**Проверка:**

```bash
cd backend && uv run pytest -q
```

### Шаг 7. Документация

В корневом `CLAUDE.md`:

- секция «Архитектура frontend/»: «семь страниц» → восемь? **Нет** — страниц
  столько же, `/` переименована. Поправь описание: `/` — «Сегодня»: задачи
  оператора, ниже счётчики и последние запуски.
- секция «Веб как обвязка»: дописать `GET /api/tasks` в перечисление ручек рядом с
  `/api/stats`, `/api/activity`, `/api/analytics` — одной фразой, с указанием, что
  порядок срочности считает бэкенд.
- секция «Песочница»: `backend/tasks.py` в перечислении модулей верхнего уровня
  (`paths.py`, `clock.py`, `activity.py`, `analytics.py`).

---

## Готово, когда

```bash
cd backend && uv run pytest -q
cd backend && uv run python -c "
import paths, tasks; from collector.services import store
tasks.use(paths.state_db(), store.DERIVED); print(len(tasks.tasks()), 'задач')
"
cd frontend && npx tsc --noEmit && npm run build
```

И на живом стенде:

- `/` открывается списком задач; при пустом списке — честная фраза, не пустая карточка;
- клик по задаче-эскалации открывает **тот самый** тред в «Диалогах»;
- клик по задаче-черновику ведёт на «Холодные»;
- бейдж в сайдбаре совпадает с числом задач `escalated` + `draft`;
- счётчики, «История запусков» и гайд на месте, ниже задач.

---

## Что за этим следить

- **`tasks.py` держит копию запроса `cold_drafts`.** Это осознанная цена границы
  систем — та же, что у `leads_source.py` с правилами F19/F21. Правка `cold_drafts`
  обязана доехать сюда; честным это держит тест про перегенерированный черновик.
- **Бейдж считается из снапшота, а список — из ручки.** Числа могут на секунду
  разойтись (снапшот приезжает по событию, список — по `refreshTick`). Это
  приемлемо; чинить рассинхрон вторым источником числа — нет.
- **Задача обязана быть закрываемой кликом.** Первый же «системный» пункт в этом
  списке (упавший демон, вставшая очередь) научит оператора его пролистывать.
  Аварии живут на «Процессах» и в Telegram.
- **`/api/tasks` читает базы в `ro`.** Ручка задач не имеет права писать: она
  зовётся на каждом открытии главной.

## Стоп-условия

- Захотелось положить список задач в снапшот `/api/stats` «чтобы не делать ручку»
  → **останови работу**: снапшот улетает по SSE на каждое событие каждому
  подписчику, и имена с причинами в каждом кадре — это не оптимизация.
- `tasks.py` понадобилось импортировать `writer.*` или `sender.*` → останови
  работу. Модуль верхнего уровня ходит в базы своим SQL, как `analytics.py`;
  граница систем не обсуждается.
- `npm run build` требует `Suspense` вокруг `useSearchParams` → оберни компонент,
  не отключай проверку и не переходи на `window.location`.
- Гайд или счётчики пришлось удалить, чтобы страница «выглядела чище» → верни.
  Гайд — единственная инструкция в продукте.
