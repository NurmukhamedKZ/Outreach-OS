# Прозрачность прогрева на странице «Отправка»

Статус: спека, реализация не начата.

**Проблема.** Пул номеров на странице «Отправка» уже показывает день, фазу,
`сегодня X/Y` и остаток лимита (`sender/routes/sender.py::card()`), но:

1. Оператор не видит **почему** цифры такие — пороги (`passive_days`,
   `cold_start_day`, рампы, `ceiling`) существуют только в `config.toml` и
   комментариях кода.
2. Сами прогревочные отправки на вебе не видны вовсе: `outbox.recent()`,
   которым питается таблица «Очередь» на той же странице, фильтрует
   `WHERE message_id IS NOT NULL` — а у прогревочной строки `message_id` всегда
   `NULL` (`sender/db/migrate.py`, комментарий к схеме `outbox`). Оператор видит
   агрегат (`сегодня 6/6`), но не может проверить, кому именно и когда номер
   писал.

**Область.** Только наблюдаемость. Календарь прогрева (`warmup.plan`,
`warmup.tick`) и сам факт, что данные приезжают с бэкенда, а не хардкодятся во
фронтенде (см. шапку `frontend/app/sender/page.tsx`), не меняются.

**Готово, когда:** на странице «Отправка» виден (а) статичный календарь фаз,
построенный из текущего `config.toml`, и (б) список последних прогревочных
отправок с адресатом и статусом — без похода в базу и без чтения кода.

---

## 1. Бэкенд

### 1.1 `sender/services/warmup.py` — `calendar()`

Чистая функция, без похода в БД — принимает уже загруженный `warmup_config`
(тот же словарь, что `plan()`), возвращает список из четырёх строк:

```python
def calendar(warmup_config: dict) -> list[dict]:
    ...
```

| `phase` | `days` | `daily_limit` |
|---|---|---|
| `socket_delay` | `"1"` | `"0"` |
| `passive` | `"2–{1+passive_days}"` | `"0 (входящие раз в {passive_interval_hours} ч)"` |
| `internal` | `"{2+passive_days}–{cold_start_day-1}"` | `"{ramp[0]} → {ramp[-1]} (рампа)"` |
| `cold` | `"{cold_start_day}+"` | `"{ramp[0]} → {ramp[-1]}, потолок {ceiling}"` |

`phase` — значения того же `Phase` enum, что уже возвращает `plan()` (`str`)
— фронтенд переиспользует существующий `PHASE_LABEL` для подписи, второй
словарь подписей не заводим. `days`/`daily_limit` — готовые строки: фронтенд
их не считает и не форматирует, только выводит (тот же принцип, что в шапке
`page.tsx` — «страница не знает ни календаря прогрева, ни порогов»).

### 1.2 `sender/db/outbox.py` — `warmup_log()`

```python
def warmup_log(db: sqlite3.Connection, limit: int) -> list[dict]:
    """Последние прогревочные отправки: кто кому и с каким исходом.
    kind='warmup' — ровно то, что recent() исключает через message_id IS NOT NULL."""
    rows = db.execute(
        "SELECT our_number, recipient, status, updated_at FROM outbox"
        " WHERE kind = 'warmup' ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]
```

### 1.3 `sender/routes/sender.py::status()`

`GET /api/sender` — тот единственный эндпоинт, которым уже питается страница
(`fetchSender`, опрашивается на каждый `refreshTick`). Новый эндпоинт не
заводим — добавляем два ключа в существующий ответ:

```python
"warmup_calendar": warmup.calendar(settings["warmup"]),
"warmup_log": outbox.warmup_log(db, RECENT_SENDS),
```

`RECENT_SENDS` — уже существующая константа (10), используется и очередью.

---

## 2. Фронтенд

### 2.1 `app/api.ts`

```ts
export type WarmupCalendarRow = {
  phase: "socket_delay" | "passive" | "internal" | "cold";
  days: string;
  daily_limit: string;
};

export type WarmupLogRow = {
  our_number: string;
  recipient: string;
  status: string;
  updated_at: string;
};
```

`SenderStatus` получает `warmup_calendar: WarmupCalendarRow[]` и
`warmup_log: WarmupLogRow[]`.

### 2.2 `app/sender/page.tsx`

Новая секция `<section className="card"><h2>Прогрев</h2>` между «Пул
номеров» и «Подключить номер»:

- Таблица-справка: Фаза (`PHASE_LABEL[row.phase]`) | Дни | Лимит в день —
  рендерит `warmup_calendar` как есть.
- Таблица лога: Когда (`WHEN.format`) | Кто → Кому | Статус — рендерит
  `warmup_log`, пусто — секция не рисует таблицу вовсе (тот же паттерн, что
  уже применён к таблице «Очередь»: `recent.length > 0 && (...)`).

Никакой новой логики опроса — обе таблицы едут тем же `fetchSender()` /
`refreshTick`, что и всё остальное на странице.

---

## 3. Тесты

`sender/tests/test_warmup.py` — `calendar()` на значениях из текущего
`config.toml` (`socket_delay_hours=24, passive_days=3, internal_ramp=[6,12,20,30,45,60],
cold_start_day=11, cold_ramp=[5,10,15,20,25,30], ceiling=30,
passive_interval_hours=2`): проверить все четыре строки, границы дней
(`"2–4"`, `"5–10"`, `"11+"`) и что `daily_limit` internal/cold показывает
именно первое→последнее значение рампы.

`warmup_log()` — не тестируется отдельным юнитом: чистый SELECT без ветвления,
покрывается существующим `test_warmup_run.py` заодно, если тот вставляет
прогревочную строку и её же проверяет; иначе — не заводим тест ради
однострочного запроса.

---

## 4. Изменения в существующем коде

| Файл | Изменение |
|---|---|
| `backend/sender/services/warmup.py` | + `calendar()` |
| `backend/sender/db/outbox.py` | + `warmup_log()` |
| `backend/sender/routes/sender.py` | `status()` возвращает `warmup_calendar`, `warmup_log` |
| `frontend/app/api.ts` | + `WarmupCalendarRow`, `WarmupLogRow`, поля в `SenderStatus` |
| `frontend/app/sender/page.tsx` | + секция «Прогрев» |
