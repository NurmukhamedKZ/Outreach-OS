# Ускорение сбора и анализа — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сократить холодный `discover` с 4 ч 36 мин до ~3 часов, а холодный `classify` — с ~6 часов до ~2,5–3 часов, не меняя ни модель, ни объём собираемых данных.

**Architecture:** Четыре независимые правки, каждая своим коммитом. (1) Пул потоков анализа поднимается до измеренного потолка провайдера. (2) Пауза между сетевыми запросами переезжает с воркера на хост, что разблокирует поднятие пула сбора. (3) Комментарии Instagram запрашиваются только к тем постам, которые попадут в промпт. (4) Шаги сбора получают стадии и дорожки, и ветка Instagram идёт параллельно ветке 2GIS/сайтов.

**Tech Stack:** Python 3.12+, `uv`, pytest, SQLite (json1), asyncio, FastAPI, Next.js 15 + TypeScript.

**Spec:** `docs/superpowers/specs/2026-09-03-collect-speedup-design.md`

**Проверено до написания плана.** Код `Pacer`, `plan_steps`, `_set_step` и
исполнитель стадий собраны в песочнице и прогнаны ровно теми тестами, что
записаны ниже: 16 из 16 зелёные. Проверены отдельно `json_set` по индексу шага
(кириллица в подписи прогресса цела), разбор новой секции `[pacing]` в
`config.toml` (соседние секции не задеты), раскладка `discover` по стадиям и
дорожкам и тест задачи 3 на настоящей фикстуре `ig_feed.html.gz` — он падает
до правки и проходит после. Один тест ревью забраковало и заменило: см.
задачу 4, `test_next_step_of_a_lane_does_not_start_after_a_sibling_failed`.

## Global Constraints

- Все команды бэкенда запускаются из `backend/`: `uv run pytest`, `uv run python …`.
- Тесты не ходят в сеть. Ни один шаг этого плана не добавляет сетевого вызова в тесты.
- Единственное место конфигурации системы 1 — `backend/collector/config.toml`. Ни один темп, вес или лимит не хардкодится в коде, кроме паузы Instagram (см. ниже).
- `IG_PAUSE_SECONDS = 6` в `collector/services/pipeline/collect.py` **не трогается и не переезжает в конфиг**: это единственный темп, который намеренно нельзя ускорить правкой файла. Цена ошибки — аккаунт живого человека.
- `IG_POST_COUNT = 12` в `collect.py` **не трогается**: `count` входит в адрес запроса, а значит в ключ кэша страницы, и смена обесценила бы 291 скачанную ленту.
- Модель в `config.toml` (`[llm].model = "deepseek/deepseek-v4-flash"`) **не меняется**: имя модели входит в ключ кэша `state.llm_answers`, и смена переоплатила бы 4254 ответа.
- `analyze.site_prompt` и `[llm].site_chars` **не трогаются**: рез с 20000 до 6000 обесценил бы 664 из 1001 оплаченного ответа. Это отдельное решение, в план не входит.
- `data/raw/`, `data/state.db` — невосстановимые слои. Ни один шаг плана ничего в них не удаляет.
- Комментарии в коде объясняют «почему», а не «что»; сообщения коммитов — на русском.

---

### Task 1: Пул анализа — 16 потоков вместо восьми

**Files:**
- Modify: `backend/collector/services/pipeline/llm.py:57-60`

**Interfaces:**
- Consumes: ничего.
- Produces: `llm.MAX_WORKERS = 16` — размер пула, который `llm.run_concurrent` создаёт на каждый слой анализа.

Теста здесь нет намеренно: меняется одна константа, и любой тест на её значение проверял бы, что 16 равно 16. Настоящая проверка — замер, он уже сделан и записан в спеку (§4.1). Задача существует отдельным коммитом ради отдельной строки в истории: если провайдер однажды начнёт отдавать 429, откат должен быть одним `git revert`, а не археологией внутри большого коммита.

- [ ] **Step 1: Убедиться, что тесты анализа зелёные до правки**

Run: `cd backend && uv run pytest collector/tests/test_llm.py collector/tests/test_analyze.py -q`
Expected: PASS (сеть не нужна — там заглушки `FakeLLM`/`FlakyLLM`).

- [ ] **Step 2: Поднять константу и заменить комментарий на замер**

В `backend/collector/services/pipeline/llm.py` заменить блок

```python
# Провайдеров со structured_outputs для модели из config.toml — около десятка;
# больше потоков чаще ловит 429, не даёт кэшу состязаться быстрее.
MAX_WORKERS = 8
```

на

```python
# Замер на 16 одинаковых промптах досье, менялось только число потоков:
#   4 потока — 176 с; 8 — 220 и 149 с; 16 — 87, 80 и 78,5 с; 24 — 281 с.
# Шестнадцать воспроизводимо быстрее восьми примерно вдвое. Двадцать четыре
# хуже восьми: провайдер не отказывает, а ставит в очередь, и это видно по
# двум вызовам, шедшим 209 и 281 секунду вместо обычных двадцати. Потолок,
# за которым троттлинг съедает выигрыш, лежит между 16 и 24 — отсюда 16, а
# не «побольше». Прежнее значение 8 стояло с гипотезой про 429, которую этот
# замер не подтвердил.
MAX_WORKERS = 16
```

- [ ] **Step 3: Прогнать тесты целиком**

Run: `cd backend && uv run pytest -q`
Expected: PASS, столько же тестов, сколько до правки.

- [ ] **Step 4: Коммит**

```bash
cd backend && git add collector/services/pipeline/llm.py
git commit -m "perf(analyze): пул 16 потоков вместо восьми — по замеру, а не по гипотезе

Восемь стояли с гипотезой «больше потоков чаще ловит 429». Замер на 16
одинаковых промптах: 8 потоков — 220 и 149 с, 16 — 87, 80 и 78,5 с, 24 —
281 с с хвостами по 209 и 281 с. Холодный classify ~6 ч -> ~2,5-3 ч."
```

---

### Task 2: Пауза принадлежит хосту, а не воркеру

**Files:**
- Modify: `backend/collector/services/pipeline/collect.py` (импорты; константы `PAUSE_SECONDS`/`MAX_WORKERS`; класс `Budget`; новый класс `Pacer` и функция `default_pacer`)
- Modify: `backend/collector/config.toml` (новая секция `[pacing]`)
- Test: `backend/collector/tests/test_collect.py`

**Interfaces:**
- Consumes: `collect.Budget(cap)` — уже существующий конструктор, вызывается семь раз из операций `collect.*` с единственным аргументом.
- Produces:
  - `collect.Pacer(intervals: dict[str, float], default: float)` с методами `wait(url: str) -> None` и `interval_for(host: str) -> float`, атрибут `default: float`;
  - `collect.default_pacer() -> Pacer` — читает `[pacing]` из `config.toml`;
  - `collect.Budget(cap, pacer=None)` — второй аргумент необязателен, по умолчанию `default_pacer()`;
  - `collect.MAX_WORKERS = 16`;
  - константы `PAUSE_SECONDS` больше нет.

- [ ] **Step 1: Написать падающие тесты**

Дописать в конец `backend/collector/tests/test_collect.py`:

```python
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace


def test_pacer_spaces_repeat_requests_to_one_host():
    """Второй запрос к тому же хосту ждёт интервал, первый — не ждёт ничего."""
    pacer = collect.Pacer({}, 0.05)
    started = time.monotonic()
    for _ in range(3):
        pacer.wait("https://example.kz/page")
    assert time.monotonic() - started >= 0.10, "интервал между запросами к одному хосту не выдержан"


def test_pacer_does_not_space_different_hosts():
    """Три разных домена не ждут друг друга — ради этого пауза и переезжает
    с воркера на хост: 2599 сайтов компаний живут на 2599 доменах."""
    pacer = collect.Pacer({}, 0.05)
    started = time.monotonic()
    for host in ("a.kz", "b.kz", "c.kz"):
        pacer.wait(f"https://{host}/")
    assert time.monotonic() - started < 0.05, "разные хосты заставили друг друга ждать"


def test_pacer_longest_match_wins():
    """Точное имя сильнее суффикса, длинный суффикс — короткого: иначе
    поддомен нельзя было бы выделить, не переписывая общее правило."""
    pacer = collect.Pacer({"2gis.kz": 1.0, "catalog.2gis.kz": 2.0}, 0.5)
    assert pacer.interval_for("catalog.2gis.kz") == 2.0
    assert pacer.interval_for("sub.catalog.2gis.kz") == 2.0
    assert pacer.interval_for("www.2gis.kz") == 1.0
    assert pacer.interval_for("example.kz") == 0.5


def test_pacer_holds_one_host_across_threads():
    """Слот резервируется под локом: двадцать потоков в один хост дают
    девятнадцать интервалов, а не двадцать одновременных запросов."""
    pacer = collect.Pacer({}, 0.02)
    started = time.monotonic()
    with ThreadPoolExecutor(20) as pool:
        list(pool.map(lambda _: pacer.wait("https://one.kz/"), range(20)))
    assert time.monotonic() - started >= 0.02 * 19


def test_pacer_lets_other_hosts_through_while_one_waits():
    """Ожидание очереди к 2GIS не держит поток, идущий на чужой домен —
    сон обязан быть вне общего лока, иначе правка бессмысленна."""
    pacer = collect.Pacer({"slow.kz": 0.4}, 0.0)
    pacer.wait("https://slow.kz/")            # занять слот
    started = time.monotonic()

    with ThreadPoolExecutor(2) as pool:
        waiting = pool.submit(pacer.wait, "https://slow.kz/")   # будет спать 0.4
        time.sleep(0.05)
        quick_started = time.monotonic()
        pool.submit(pacer.wait, "https://fast.kz/").result()
        quick = time.monotonic() - quick_started
        waiting.result()

    assert quick < 0.1, f"запрос к чужому хосту прождал {quick:.2f} с — лок держится во время сна"
    assert time.monotonic() - started >= 0.3


def test_budget_paces_only_network_requests(monkeypatch):
    """Страница из raw/ бесплатна и очереди к хосту не ждёт: на повторном
    прогоне сбор не делает ни одного запроса и не должен ничего проспать."""
    asked = []
    pacer = SimpleNamespace(wait=asked.append)
    monkeypatch.setattr(collect.fetch, "is_cached", lambda url: url.endswith("cached"))
    monkeypatch.setattr(collect.fetch, "get", lambda url, **kw: "<html></html>")

    budget = collect.Budget(None, pacer)
    budget.get("https://example.kz/cached")
    budget.get("https://example.kz/fresh")

    assert asked == ["https://example.kz/fresh"]


def test_pacing_config_keeps_2gis_faster_than_unknown_hosts():
    """Калибровка из config.toml: 2GIS — один хост на тысячи запросов, и его
    интервал короче, чем у незнакомого домена, которому достанется пара
    страниц. Ровно эта пропорция и была потеряна, пока пауза жила на воркере.
    """
    pacer = collect.default_pacer()
    assert pacer.interval_for("2gis.kz") < pacer.default
    assert pacer.interval_for("public-api.reviews.2gis.com") < pacer.default
```

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `cd backend && uv run pytest collector/tests/test_collect.py -q`
Expected: FAIL — `AttributeError: module 'collector.services.pipeline.collect' has no attribute 'Pacer'`.

- [ ] **Step 3: Добавить секцию `[pacing]` в конфиг**

В `backend/collector/config.toml`, после секции `[site.links]` и перед `[scoring]`:

```toml
[pacing]
# Интервал между запросами К ОДНОМУ хосту, в секундах. Раньше пауза была одна
# на все источники (секунда на воркер, восемь воркеров), и 2599 сайтов
# компаний на 2599 разных доменах платили вежливость, адресованную 2GIS.
#
# 0.15 для 2GIS — это 6,7 запроса в секунду, внутри коридора 4–8, который
# держали восемь воркеров с секундной паузой и который прошёл боевой сбор.
# Значения короче умолчания не опечатка: 2GIS — один хост, принимающий
# тысячи запросов, а незнакомому домену достаётся от одной до семи страниц.
# Ответила капча — поднимать здесь, код не трогать.
#
# Instagram сюда не входит: его шесть секунд живут константой в collect.py,
# и это единственный темп, который нельзя ускорить правкой конфига.
default = 0.5
"2gis.kz" = 0.15
"public-api.reviews.2gis.com" = 0.15
```

- [ ] **Step 4: Реализовать `Pacer` и подключить его к `Budget`**

В `backend/collector/services/pipeline/collect.py` добавить в импорты:

```python
from urllib.parse import urlsplit
```

Заменить константы

```python
# Пауза на поток: 8 потоков без паузы — это 40 запросов в секунду, и 2GIS на такой
# скорости отвечает капчей. Секунда на поток держит темп в пределах 4–8 запросов.
PAUSE_SECONDS = 1.0
MAX_WORKERS = 8
```

на

```python
# Шестнадцать, а не восемь: 6188 запросов боевого сбора шли 75 минут — это
# 1,36 запроса в секунду при потолке восемь, то есть воркеры семь восьмых
# времени ждут ответа сети. Темп источника держит не число воркеров, а
# Pacer ниже, поэтому пул можно поднимать, не приближая капчу.
MAX_WORKERS = 16
```

Заменить класс `Budget` целиком и добавить перед ним `Pacer`:

```python
class Pacer:
    """Минимальный интервал между запросами к одному хосту.

    Пауза принадлежит хосту, а не воркеру: капчей отвечает источник, а не наш
    пул. Пока в работе одни страницы 2GIS, разницы нет, но в фазе сайтов те же
    воркеры идут на 2599 разных доменов, и каждый платил секунду вежливости
    хосту, который об этом никогда не узнает.

    Слот резервируется под общим локом, а сон идёт вне его: иначе поток,
    ждущий очереди к 2GIS, держал бы за собой всех, кто идёт на чужие домены,
    — то есть ровно ту беду, ради которой пауза сюда и переехала.
    """

    def __init__(self, intervals, default):
        self.intervals = intervals
        self.default = default
        self.free_at = {}
        self.lock = Lock()

    def interval_for(self, host):
        """Самое длинное совпадение: точное имя, иначе самый длинный подходящий
        суффикс, иначе умолчание. Длинное выигрывает у короткого, чтобы
        поддомен можно было выделить, не переписывая общее правило."""
        if host in self.intervals:
            return self.intervals[host]
        matches = [(len(name), value) for name, value in self.intervals.items()
                   if host.endswith(f".{name}")]
        return max(matches)[1] if matches else self.default

    def wait(self, url):
        host = urlsplit(url).hostname or ""
        interval = self.interval_for(host)
        with self.lock:
            start = max(time.monotonic(), self.free_at.get(host, 0.0))
            self.free_at[host] = start + interval
        delay = start - time.monotonic()
        if delay > 0:
            time.sleep(delay)


def default_pacer():
    """Темпы из config.toml. Значения — калибровочная ручка: источник ответил
    капчей — поднимают их, а не правят код.

    default вынимается отдельной строкой: «default» — не имя домена, и в карте
    хостов ему места нет.
    """
    pacing = dict(tomllib.loads(CONFIG.read_text(encoding="utf-8"))["pacing"])
    default = pacing.pop("default")
    return Pacer(pacing, default)


class Budget:
    """Потолок сетевых запросов. Страница из raw/ бесплатна: она не входит ни
    в потолок, ни в очередь к хосту."""

    def __init__(self, cap, pacer=None):
        self.cap = cap
        self.spent = 0
        self.lock = Lock()
        self.pacer = pacer or default_pacer()

    def get(self, url, **kw):
        if fetch.is_cached(url):
            return fetch.get(url, **kw)
        with self.lock:
            if self.cap is not None and self.spent >= self.cap:
                raise BudgetSpent(f"потолок {self.cap} сетевых запросов исчерпан")
            self.spent += 1
        self.pacer.wait(url)
        return fetch.get(url, **kw)
```

Внимание: `dict(...)` перед `pop` обязателен — без копии `pop` правил бы словарь, разобранный из toml, и следующий читатель конфига не нашёл бы `default`.

Не сворачивать `default_pacer` в одну строку `Pacer(pacing, pacing.pop("default"))`: она работает — Python вычисляет аргументы слева направо, и `Pacer` получает уже изменённый словарь по ссылке, — но держится на порядке вычисления и алиасинге разом. Проверено, работает, и всё равно не стоит того: это ровно та строка, которую разбирают в три часа ночи.

- [ ] **Step 5: Прогнать тесты сбора**

Run: `cd backend && uv run pytest collector/tests/test_collect.py -q`
Expected: PASS, все семь новых тестов зелёные.

- [ ] **Step 6: Убедиться, что `PAUSE_SECONDS` больше нигде не упоминается**

Run: `cd backend && grep -rn "PAUSE_SECONDS" --include="*.py" . | grep -v IG_PAUSE_SECONDS`
Expected: пусто. `IG_PAUSE_SECONDS` остаётся — это другая константа и она не трогается.

- [ ] **Step 7: Прогнать все тесты**

Run: `cd backend && uv run pytest -q`
Expected: PASS.

- [ ] **Step 8: Коммит**

```bash
cd backend && git add collector/services/pipeline/collect.py collector/config.toml collector/tests/test_collect.py
git commit -m "perf(collect): интервал на хост вместо паузы на воркер, пул 16

Паузу платил воркер, а капчей отвечает хост: в фазе сайтов восемь воркеров
платили секунду вежливости 2599 разным доменам. Темп переехал в Pacer с
интервалами в config.toml, и это разблокировало пул: 6188 запросов шли 75
минут при потолке восемь запросов в секунду — то есть в ожидании сети.
Не-Instagram сбор 75 -> ~40 минут."
```

---

### Task 3: Комментарии Instagram — только к постам из среза

**Files:**
- Modify: `backend/collector/services/pipeline/collect.py:329-343` (`posts_with_comments`)
- Test: `backend/collector/tests/test_collect.py`

**Interfaces:**
- Consumes: `collect.posts_with_comments() -> list[tuple[str, str]]` — пары `(media_pk, username)`; сигнатура не меняется.
- Produces: та же сигнатура, но лента режется до `[instagram].posts_limit` постов.

Фикстура `collector/fixtures/ig_feed.html.gz` для этого идеальна: в ней 12 постов, и единственный пост с комментариями стоит одиннадцатым (индекс 10). То есть на ней сегодняшний код делает ровно один запрос, и ровно этот запрос никто никогда не прочитает.

- [ ] **Step 1: Написать падающий тест**

Дописать в конец `backend/collector/tests/test_collect.py`:

```python
def test_comments_are_asked_only_for_posts_the_model_will_see(tmp_path, monkeypatch):
    """Комментарии запрашиваются к тем же постам, что уйдут в промпт.

    В эталонной ленте 12 постов, и единственный пост с комментариями —
    одиннадцатый. Анализ берёт первые posts_limit (10) и до него не доходит,
    значит запрос за его комментариями оплачивается риском бана и
    выбрасывается. По живому raw/ таких запросов 221 из 1441.
    """
    import gzip
    import hashlib
    import json

    url = collect.IG_FEED.format(username="adalservice__", count=collect.IG_POST_COUNT)
    sha = hashlib.sha1(url.encode()).hexdigest()
    raw = tmp_path / "raw"
    raw.mkdir()
    with gzip.open(FIXTURES / "ig_feed.html.gz", "rb") as src, \
         gzip.open(raw / f"{sha}.html.gz", "wb") as dst:
        dst.write(src.read())
    (raw / f"{sha}.json").write_text(json.dumps(
        {"url": url, "final_url": url, "status": 200,
         "fetched_at": "2026-08-20T09:00:00Z"}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(storage, "RAW", raw)

    assert collect.posts_with_comments() == []
```

В шапке файла добавить импорт фикстур рядом с существующими:

```python
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"   # collector/fixtures
```

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `cd backend && uv run pytest collector/tests/test_collect.py::test_comments_are_asked_only_for_posts_the_model_will_see -q`
Expected: FAIL — вернулась одна пара `[('3921395397561545652', 'adalservice__')]` вместо пустого списка.

- [ ] **Step 3: Резать ленту тем же срезом, что и анализ**

В `backend/collector/services/pipeline/collect.py` заменить `posts_with_comments`:

```python
def posts_with_comments():
    """(media_pk, username) постов с comment_count > 0 из сырья лент в raw/.

    Срез тот же, что берёт analyze.instagram_targets: в ленте 12 постов
    (IG_POST_COUNT входит в адрес, а значит в ключ кэша страницы, и менять
    его нельзя), а в промпт уходят первые posts_limit. Комментарии к
    остальным не читает никто — на живом raw/ это 221 запрос из 1441,
    по шесть секунд каждый.
    """
    from collector.services.pipeline import rebuild
    limit = tomllib.loads(CONFIG.read_text(encoding="utf-8"))["instagram"]["posts_limit"]
    out = []
    for page in rebuild.load_pages():
        if "feed/user/" not in page["url"]:
            continue
        feed = sources.parse_ig_feed(rebuild.html_of(page))
        username = feed["username"] or page["url"].split("feed/user/", 1)[1].split("/", 1)[0]
        for post in feed["posts"][:limit]:
            if post.get("comments") and post.get("pk"):
                out.append((post["pk"], username))
    return out
```

- [ ] **Step 4: Прогнать тест**

Run: `cd backend && uv run pytest collector/tests/test_collect.py -q`
Expected: PASS.

- [ ] **Step 5: Прогнать все тесты**

Run: `cd backend && uv run pytest -q`
Expected: PASS.

- [ ] **Step 6: Коммит**

```bash
cd backend && git add collector/services/pipeline/collect.py collector/tests/test_collect.py
git commit -m "perf(collect): комментарии только к постам, которые увидит модель

В ленте 12 постов, в промпт уходят первые posts_limit (10), а комментарии
запрашивались ко всем. На живом raw/ это 221 запрос из 1441 — 22 минуты
риска бана за данные, которые никто не читает. Срез теперь тот же, что в
analyze.instagram_targets, и берётся из того же ключа конфига."
```

---

### Task 4: Стадии и дорожки в джобах (бэкенд)

**Files:**
- Modify: `backend/collector/services/jobs.py` (`enqueue_steps`, `make_context`, `as_job`, `_execute`, `cancel`, `cancel_current`, `check_pipelines`, `_finish`; новые `plan_steps`, `as_step`, `_set_step`, `_run_lane`, реестры `_current_ctxs`/`_cancelled`)
- Modify: `backend/collector/services/pipeline/__init__.py` (объявление `PIPELINES["discover"]`)
- Modify: `backend/collector/routes/pipeline.py:23-26` (каталог отдаёт плоские имена)
- Test: `backend/collector/tests/test_jobs.py`

**Interfaces:**
- Consumes: `OPERATIONS: dict[str, Callable]`, `PIPELINES: dict[str, dict]` из `collector.services.pipeline`.
- Produces:
  - `jobs.plan_steps(declaration) -> list[dict]` — объявление шагов в плоский список `{"name": str, "stage": int, "lane": int, "status": str, "progress": dict | None}`; `status` принимает `pending | running | done | failed | cancelled`;
  - `jobs.as_step(entry) -> dict` — шаг для фронта, добавляет `"command"`; строку из старых джоб разворачивает в `{"name", "command", "stage": 0, "lane": 0, "status": "done", "progress": None}`;
  - `jobs.make_context(job_id, step_index) -> (ctx, state)` — второй аргумент обязателен;
  - `as_job(row)["step"]` — теперь **число завершённых шагов**, а не индекс текущего;
  - `as_job(row)["steps"][i]` — словарь с полями выше.

Колонка `state.jobs.step` перестаёт заполняться и остаётся в схеме мёртвой — как `exit_code`, оставшийся от эпохи subprocess. Схема не меняется: `steps` и так `TEXT` с json.

- [ ] **Step 1: Написать падающие тесты**

Дописать в конец `backend/collector/tests/test_jobs.py`:

```python
def test_plan_steps_lays_out_stages_and_lanes():
    """Строка — стадия из одной дорожки; кортеж кортежей — одна стадия, где
    дорожки идут рядом. Порядок списка — (стадия, дорожка), как в объявлении."""
    steps = jobs.plan_steps(["one", (("a", "b"), ("c",)), "last"])
    assert [(s["name"], s["stage"], s["lane"]) for s in steps] == [
        ("one", 0, 0),
        ("a", 1, 0), ("b", 1, 0),
        ("c", 1, 1),
        ("last", 2, 0),
    ]
    assert all(s["status"] == "pending" and s["progress"] is None for s in steps)


def test_lanes_of_one_stage_run_side_by_side(stores):
    """Две дорожки одной стадии идут одновременно: каждая ждёт события соседа.
    При последовательном исполнении обе не дождутся и тест упадёт."""
    import threading
    from collector.services.pipeline import OPERATIONS

    left_started, right_started = threading.Event(), threading.Event()

    def left(ctx):
        left_started.set()
        assert right_started.wait(timeout=5), "правая дорожка не стартовала"
        return {}

    def right(ctx):
        right_started.set()
        assert left_started.wait(timeout=5), "левая дорожка не стартовала"
        return {}

    OPERATIONS["_left_test"], OPERATIONS["_right_test"] = left, right
    try:
        job_id = jobs.enqueue_steps("custom", "Две дорожки",
                                    [(("_left_test",), ("_right_test",))])
        asyncio.run(jobs.run_pending())
        record = jobs.job(job_id)
        assert record["status"] == "done", record["error"]
        assert {s["status"] for s in record["steps"]} == {"done"}
    finally:
        OPERATIONS.pop("_left_test", None)
        OPERATIONS.pop("_right_test", None)


def test_steps_of_one_lane_run_in_order(stores):
    """Внутри дорожки — строго по очереди: ig_comments читает то, что положила
    в raw/ collect.instagram, и обогнать её не имеет права."""
    from collector.services.pipeline import OPERATIONS

    order = []
    OPERATIONS["_first_test"] = lambda ctx: order.append("first") or {}
    OPERATIONS["_second_test"] = lambda ctx: order.append("second") or {}
    try:
        job_id = jobs.enqueue_steps("custom", "Одна дорожка",
                                    [(("_first_test", "_second_test"),)])
        asyncio.run(jobs.run_pending())
        assert jobs.job(job_id)["status"] == "done"
        assert order == ["first", "second"]
    finally:
        OPERATIONS.pop("_first_test", None)
        OPERATIONS.pop("_second_test", None)


def test_failed_lane_stops_its_neighbour_before_the_job_finishes(stores):
    """asyncio.to_thread не прерывается извне, поэтому упавшая дорожка не имеет
    права уронить джобу, пока соседка ещё в сети: осиротевший поток продолжал
    бы качать страницы и писать лог в уже завершённую джобу."""
    import time
    from collector.services.pipeline import OPERATIONS

    def boom(ctx):
        time.sleep(0.1)          # дать соседке начать
        raise ValueError("ветка упала")

    def patient(ctx):
        while True:
            ctx.check_cancelled()
            time.sleep(0.02)

    OPERATIONS["_boom_lane_test"], OPERATIONS["_patient_lane_test"] = boom, patient
    try:
        job_id = jobs.enqueue_steps("custom", "Падение ветки",
                                    [(("_boom_lane_test",), ("_patient_lane_test",))])
        asyncio.run(asyncio.wait_for(jobs.run_pending(), timeout=10))
        record = jobs.job(job_id)
        assert record["status"] == "failed"
        assert "ветка упала" in record["error"], record["error"]
        statuses = {s["name"]: s["status"] for s in record["steps"]}
        assert statuses["_boom_lane_test"] == "failed", statuses
        assert statuses["_patient_lane_test"] == "cancelled", \
            "соседняя ветка осталась бегущей после того, как джоба помечена упавшей"
    finally:
        OPERATIONS.pop("_boom_lane_test", None)
        OPERATIONS.pop("_patient_lane_test", None)


def test_next_step_of_a_lane_does_not_start_after_a_sibling_failed(stores):
    """Отмена — флаг на джобе, а не только на уже созданных контекстах.

    Сторожить надо именно СЛЕДУЮЩИЙ шаг дорожки: первые шаги обеих дорожек
    стартуют одновременно, это замысел, и помешать соседке начать нельзя.
    А вот collect.ig_comments не имеет права уйти в сеть после того, как
    ветка сайтов уже упала, — иначе падение стоило бы лишних минут запросов.
    """
    import threading
    import time
    from collector.services.pipeline import OPERATIONS

    ran = []
    started = threading.Event()

    def slow_then_ok(ctx):
        started.set()
        time.sleep(0.2)
        return {}

    def boom(ctx):
        assert started.wait(timeout=5)
        raise ValueError("сосед упал")

    OPERATIONS["_slow_ok_test"] = slow_then_ok
    OPERATIONS["_must_not_run_test"] = lambda ctx: ran.append("second") or {}
    OPERATIONS["_boom_neighbour_test"] = boom
    try:
        job_id = jobs.enqueue_steps(
            "custom", "Поздний шаг",
            [(("_slow_ok_test", "_must_not_run_test"), ("_boom_neighbour_test",))])
        asyncio.run(asyncio.wait_for(jobs.run_pending(), timeout=10))

        assert jobs.job(job_id)["status"] == "failed"
        assert ran == [], "второй шаг дорожки стартовал уже после падения соседки"
        statuses = {s["name"]: s["status"] for s in jobs.job(job_id)["steps"]}
        assert statuses["_slow_ok_test"] == "done", statuses
        assert statuses["_must_not_run_test"] == "pending", statuses
    finally:
        for name in ("_slow_ok_test", "_must_not_run_test", "_boom_neighbour_test"):
            OPERATIONS.pop(name, None)


def test_old_jobs_with_plain_string_steps_still_render(stores):
    """В истории лежат джобы, чьи steps — массив строк. Показать их надо, а не
    уронить страницу «Процессы» на первой же старой записи."""
    job_id = jobs.enqueue_steps("custom", "Старая", ["export"])
    jobs._update(job_id, steps=json.dumps(["export", "rebuild"], ensure_ascii=False))
    record = jobs.job(job_id)
    assert [s["name"] for s in record["steps"]] == ["export", "rebuild"]
    assert {s["status"] for s in record["steps"]} == {"done"}
    assert record["step_count"] == 2


def test_step_progress_lands_in_its_own_step(stores):
    """Прогресс пишется в свой шаг: две ветки пишут в одну json-колонку
    одновременно, и цикл «прочитать-склеить-записать» терял бы правку соседа."""
    from collector.services.pipeline import OPERATIONS

    def reporting(ctx):
        ctx.progress(3, 7, "рубрики 2GIS")
        return {}

    OPERATIONS["_progress_test"] = reporting
    try:
        job_id = jobs.enqueue_steps("custom", "Прогресс", ["_progress_test"])
        asyncio.run(jobs.run_pending())
        step = jobs.job(job_id)["steps"][0]
        assert step["progress"] == {"current": 3, "total": 7, "label": "рубрики 2GIS"}
    finally:
        OPERATIONS.pop("_progress_test", None)


def test_discover_runs_instagram_beside_the_rest():
    """Объявление discover: Instagram идёт своей дорожкой рядом с 2GIS и
    сайтами, а rebuild — отдельной стадией после всего сбора."""
    steps = jobs.plan_steps(jobs.PIPELINES["discover"]["steps"])
    by_name = {s["name"]: s for s in steps}

    assert by_name["collect.instagram"]["stage"] == by_name["collect.sites"]["stage"]
    assert by_name["collect.instagram"]["lane"] != by_name["collect.sites"]["lane"]
    assert by_name["collect.ig_comments"]["lane"] == by_name["collect.instagram"]["lane"]
    assert by_name["collect.site_pages"]["lane"] == by_name["collect.sites"]["lane"]

    collecting = max(s["stage"] for s in steps if s["name"].startswith("collect."))
    assert by_name["rebuild"]["stage"] > collecting, \
        "rebuild обязан идти один и после всего сбора — он пересобирает derived прогоном"
```

В шапке `test_jobs.py` добавить `import json` к существующим импортам.

- [ ] **Step 2: Прогнать и убедиться, что падает**

Run: `cd backend && uv run pytest collector/tests/test_jobs.py -q`
Expected: FAIL — `AttributeError: module 'collector.services.jobs' has no attribute 'plan_steps'`.

- [ ] **Step 3: Разложить объявление шагов на стадии и дорожки**

В `backend/collector/services/jobs.py` заменить `enqueue_steps` и добавить рядом `plan_steps`:

```python
def plan_steps(declaration):
    """Объявление шагов -> плоский список {name, stage, lane, status, progress}.

    Строка — стадия из одной дорожки; кортеж — одна стадия, где каждый элемент
    идёт своей дорожкой параллельно соседям, а строки внутри дорожки — по
    очереди. Плоский список, а не дерево, ровно по одной причине: его читает
    фронтенд и рисует списком, и вложенность стоила бы ему рекурсивного
    компонента ради двух веток. Порядок — (стадия, дорожка), как в объявлении.
    """
    steps = []
    for stage, entry in enumerate(declaration):
        lanes = entry if isinstance(entry, (tuple, list)) else (entry,)
        for lane, chain in enumerate(lanes):
            for name in ((chain,) if isinstance(chain, str) else chain):
                steps.append({"name": name, "stage": stage, "lane": lane,
                              "status": "pending", "progress": None})
    return steps


def enqueue_steps(kind, title, names):
    """Примитив очереди: принимает объявление шагов. Каталожные пайплайны и
    одиночные операции сходятся здесь — логика постановки одна."""
    with closing(connect()) as db:
        cursor = db.execute(
            "INSERT INTO state.jobs (kind, title, steps, status, created_at)"
            " VALUES (?, ?, ?, 'queued', ?)",
            (kind, title, json.dumps(plan_steps(names), ensure_ascii=False), now()),
        )
        db.commit()
        job_id = cursor.lastrowid
    publish_job(job_id)
    return job_id
```

- [ ] **Step 4: Хранить статус шага, а не вычислять его индексом**

Заменить `as_job` и добавить `as_step` и `_set_step`:

```python
def as_job(row):
    """Строка state.jobs -> то, что видит фронт. Порядок колонок берётся у самой
    строки (row_factory=Row), а не из копии списка рядом со схемой.

    step — число завершённых шагов, а не индекс текущего: в стадии с двумя
    дорожками текущих шагов два, и одним индексом это не выражается. Колонка
    state.jobs.step больше не заполняется и осталась в схеме мёртвой, как
    exit_code от эпохи subprocess.
    """
    record = dict(row)
    steps = [as_step(entry) for entry in json.loads(record.pop("steps") or "[]")]
    log = record.pop("log") or ""
    return {
        **record,
        "steps": steps,
        "step": sum(1 for step in steps if step["status"] == "done"),
        "step_count": len(steps),
        "log_lines": log.count("\n") + 1 if log else 0,
        "progress": json.loads(record["progress"]) if record["progress"] else None,
    }


def as_step(entry):
    """Шаг для фронта. Строка — формат до стадий и дорожек: такие джобы лежат
    в истории, и показать их надо, а не уронить страницу на первой же."""
    if isinstance(entry, str):
        return {"name": entry, "command": entry, "stage": 0, "lane": 0,
                "status": "done", "progress": None}
    return {**entry, "command": entry["name"]}


def _set_step(job_id, index, **fields):
    """Правит один шаг в json-колонке steps.

    Правит SQLite (json_set), а не Python: две дорожки пишут в одну колонку
    одновременно, и цикл «прочитать-склеить-записать» терял бы правку соседа.
    """
    assignments = ", ".join(f"'$[{index}].{name}', json(?)" for name in fields)
    values = [json.dumps(value, ensure_ascii=False) for value in fields.values()]
    with closing(connect()) as db:
        db.execute(
            f"UPDATE state.jobs SET steps = json_set(steps, {assignments}) WHERE id = ?",
            (*values, job_id),
        )
        db.commit()
    publish_job(job_id)
```

- [ ] **Step 5: Контекст знает свой шаг, отмена знает всю джобу**

Заменить глобальную `_current_ctx` на два реестра и переписать `make_context`:

```python
_current_ctxs = {}    # job_id -> контексты бегущих шагов: дорожек может быть несколько
_cancelled = set()    # job_id, которым уже пришла отмена


def make_context(job_id, step_index):
    """Контекст одного шага. Отмена читается и из своего состояния, и из
    множества отменённых джоб: дорожка, стартовавшая через миллисекунду после
    падения соседки, обязана увидеть отмену, которой при её создании ещё не
    было ни в одном контексте."""
    state = {"cancelled": False}

    def check_cancelled():
        if state["cancelled"] or job_id in _cancelled:
            raise _Cancelled()

    def progress(current, total, label):
        payload = {"current": current, "total": total, "label": label}
        _set_step(job_id, step_index, progress=payload)
        # Колонка джобы держится ради фронтенда, который пока читает одну
        # полосу на джобу. Уходит вместе с ним — задача 5.
        _update(job_id, progress=json.dumps(payload, ensure_ascii=False))

    def log(message):
        _append_log(job_id, [message])
        events.publish({"type": "log", "job_id": job_id, "lines": [message]})

    return SimpleNamespace(
        check_cancelled=check_cancelled, progress=progress, log=log,
        cancel=lambda: state.update(cancelled=True),
        job_id=job_id,
    ), state
```

- [ ] **Step 6: Исполнять стадии по очереди, дорожки — рядом**

Заменить `_execute` и добавить `_run_lane`:

```python
async def _execute(job_id):
    steps = json.loads(_raw_steps(job_id))
    with logctx.job(job_id):
        for stage in sorted({step["stage"] for step in steps}):
            lanes = {}
            for index, step in enumerate(steps):
                if step["stage"] == stage:
                    lanes.setdefault(step["lane"], []).append(index)

            tasks = [asyncio.ensure_future(_run_lane(job_id, steps, indexes))
                     for indexes in lanes.values()]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            failure = next((task.exception() for task in done if task.exception()), None)
            if pending:
                # to_thread не прерывается извне: соседкам ставится флаг, и мы
                # ждём, пока они выйдут сами. Пометить джобу раньше — значит
                # оставить поток, пишущий страницы и лог в завершённую джобу.
                _cancelled.add(job_id)
                await asyncio.wait(pending)
                failure = failure or next(
                    (task.exception() for task in pending if task.exception()), None)
            if failure is None:
                continue
            _cancelled.discard(job_id)
            if isinstance(failure, _Cancelled):
                _finish(job_id, "cancelled")
            else:
                _finish(job_id, "failed", error=f"{type(failure).__name__}: {failure}")
            return
    _finish(job_id, "done")


async def _run_lane(job_id, steps, indexes):
    """Шаги одной дорожки — строго по очереди. Первая же ошибка выходит наружу:
    её ловит стадия и решает судьбу соседних дорожек."""
    for index in indexes:
        name = steps[index]["name"]
        if job_id in _cancelled:
            raise _Cancelled()
        if name not in OPERATIONS:
            raise KeyError(f"нет операции {name}")
        ctx, _state = make_context(job_id, index)
        _current_ctxs.setdefault(job_id, []).append(ctx)
        _set_step(job_id, index, status="running")
        try:
            result = await asyncio.to_thread(OPERATIONS[name], ctx)
        except _Cancelled:
            _set_step(job_id, index, status="cancelled")
            raise
        except Exception:
            # Полный traceback пишется здесь, а не в стадии: имя упавшего шага
            # известно только тут, а state.jobs.error хранит короткую строку
            # для фронтенда. Без этой записи причину провала можно гадать
            # только по типу и сообщению.
            logger.exception(f"джоба {job_id}, шаг {name} упала")
            _set_step(job_id, index, status="failed")
            raise
        finally:
            _current_ctxs.get(job_id, []).remove(ctx)
        _set_step(job_id, index, status="done")
        _update(job_id, result=json.dumps(result, ensure_ascii=False))
```

Три места, где легко ошибиться:

- `_update(job_id, result=...)` в конце шага пишет в одну колонку из обеих
  дорожек: у джобы с ветками `result` — исход той дорожки, что финишировала
  последней. Раньше это был исход последнего шага, то есть тоже не сумма.
  Колонки нет ни в типе `Job` фронтенда, ни на одной странице, поэтому
  сводить исходы веток в список сейчас незачем — но если она однажды
  понадобится, начинать надо отсюда.

- `remove(ctx)` в `finally` безопасен только потому, что список чистит
  единственный владелец — сама дорожка, и `_finish` до её выхода не зовётся.
  Если порядок когда-нибудь поменяется, `remove` бросит `ValueError` прямо в
  `finally` и съест настоящее исключение шага.
- `except Exception`, а не `except BaseException`: `_Cancelled` перехвачен
  веткой выше, а `asyncio.CancelledError` наследуется от `BaseException` и
  проходить через журнал ошибок не должен.

- [ ] **Step 7: Отмена достаёт до всех дорожек**

Заменить хвост `cancel` и тело `cancel_current`, а в `_finish` вычищать реестры:

```python
def cancel(job_id):
    """Снимает queued мгновенно; running — кооперативно, между единицами работы."""
    record = job(job_id)
    if not record:
        raise KeyError(job_id)
    if record["status"] == "queued":
        _finish(job_id, "cancelled")
        return job(job_id)
    if record["status"] == "running":
        _cancelled.add(job_id)                       # увидят и те дорожки, что ещё не стартовали
        for ctx in list(_current_ctxs.get(job_id, ())):
            ctx.cancel()
    return job(job_id)


def cancel_current():
    """Кооперативно останавливает текущую running-джобу — зовётся при остановке
    бэкенда: без этого фоновый поток операции (asyncio.to_thread) продолжает
    молотить весь оставшийся список доменов, а Python не отпускает процесс,
    пока этот поток не завершится сам."""
    for job_id, contexts in list(_current_ctxs.items()):
        _cancelled.add(job_id)
        for ctx in list(contexts):
            ctx.cancel()


def _finish(job_id, status, error=None):
    """Кода возврата у операции нет: она возвращает dict или бросает исключение.
    Колонка exit_code осталась от эпохи subprocess и больше не заполняется."""
    _cancelled.discard(job_id)
    _current_ctxs.pop(job_id, None)
    _update(job_id, status=status, error=error, finished_at=now())
    events.publish({"type": "refresh"})
```

- [ ] **Step 8: Каталог и проверка каталога понимают вложенность**

В `backend/collector/services/jobs.py`:

```python
def check_pipelines():
    """Каталог пайплайнов цел: каждый шаг — существующая операция."""
    for kind, pipeline in PIPELINES.items():
        assert pipeline["title"], f"{kind}: нет названия"
        for step in plan_steps(pipeline["steps"]):
            assert step["name"] in OPERATIONS, \
                f"{kind}: шаг {step['name']} не в реестре операций"
```

В `backend/collector/routes/pipeline.py` заменить сборку каталога:

```python
        "pipelines": [{"kind": k, "title": p["title"],
                       "steps": [s["name"] for s in jobs.plan_steps(p["steps"])]}
                      for k, p in jobs.PIPELINES.items()],
```

- [ ] **Step 9: Объявить две дорожки в `discover`**

В `backend/collector/services/pipeline/__init__.py` заменить объявление:

```python
PIPELINES = {
    # Две дорожки сбора идут рядом: они не делят ни одного хоста и ни одного
    # файла — сбор кладёт страницы в raw/ (имя файла sha1 адреса) и читает
    # выдачу прошлого прогона только на чтение. Instagram платит шесть секунд
    # за запрос и тянется почти три часа; 2GIS с сайтами укладываются в сорок
    # минут и раньше просто ждали своей очереди. Инвариант «один воркер»
    # касается rebuild, который пересобирает derived прогоном, — он и остаётся
    # отдельной стадией после всего сбора.
    "discover": {"title": "Поиск новых лидов", "steps": (
        "collect.gis",
        (("collect.reviews", "collect.sites", "collect.site_pages"),
         ("collect.instagram", "collect.ig_comments", "collect.ig_profile")),
        "rebuild", "export")},
    "classify": {"title": "Анализ и досье", "steps": (
        "analyze.reviews", "analyze.site", "analyze.instagram", "analyze.dossier",
        "rebuild", "export")},
    "rebuild": {"title": "Пересборка из сырья", "steps": ("rebuild", "export")},
}
```

- [ ] **Step 10: Починить тест, зовущий `make_context` напрямую**

В `backend/collector/tests/test_jobs.py` заменить в `test_context_carries_job_id`:

```python
    ctx, _state = jobs.make_context(job_id, 0)
```

- [ ] **Step 11: Прогнать тесты джоб**

Run: `cd backend && uv run pytest collector/tests/test_jobs.py -q`
Expected: PASS, включая девять новых тестов.

- [ ] **Step 12: Прогнать все тесты**

Run: `cd backend && uv run pytest -q`
Expected: PASS.

- [ ] **Step 13: Проверить вживую, что фронт не сломался**

Run: `cd backend && uv run python main.py` (в соседнем терминале `cd frontend && npm run dev`), открыть `http://localhost:3000/activity`, нажать «Пересборка из сырья».
Expected: шаги подсвечиваются, полоса прогресса идёт, «прервать» останавливает джобу. Страница показывает и старые джобы из истории.

- [ ] **Step 14: Коммит**

```bash
cd backend && git add collector/services/jobs.py collector/services/pipeline/__init__.py \
  collector/routes/pipeline.py collector/tests/test_jobs.py
git commit -m "feat(jobs): стадии и дорожки — Instagram собирается рядом с 2GIS

Семь шагов сбора шли подряд, хотя связей между ними две: site_pages после
sites и ig_* после instagram. Ветки не делят ни хоста, ни файла, поэтому
идут рядом; rebuild остаётся отдельной стадией после всего сбора.
Статус шага теперь хранится, а не вычисляется индексом: в стадии с двумя
дорожками текущих шагов два. discover 3 ч 38 мин -> ~3 часа."
```

---

### Task 5: Фронтенд читает статус шага, а не арифметику

**Files:**
- Modify: `frontend/app/api.ts:142` (тип `JobStep`)
- Modify: `frontend/components/JobMonitor.tsx` (список шагов, полосы прогресса)
- Modify: `frontend/components/RunBar.tsx:24-26` (подпись состояния)
- Modify: `frontend/app/globals.css` (стиль упавшего шага)
- Modify: `backend/collector/services/jobs.py` (`make_context` перестаёт дублировать прогресс в колонку джобы)

**Interfaces:**
- Consumes: `Job.steps[i]` из задачи 4 — `{name, command, stage, lane, status, progress}`; `Job.step` — число завершённых.
- Produces: ничего для бэкенда.

- [ ] **Step 1: Расширить тип шага**

В `frontend/app/api.ts` заменить строку 142:

```ts
export type JobStep = {
  name: string;
  command: string;
  stage: number;
  lane: number;
  status: "pending" | "running" | "done" | "failed" | "cancelled";
  progress: { label?: string; current?: number; total?: number } | null;
};
```

- [ ] **Step 2: Красить шаги по статусу и рисовать полосу у каждого бегущего**

В `frontend/components/JobMonitor.tsx` заменить блок `<ol className="jobSteps">` вместе со следующим за ним блоком `{active.progress && …}`:

```tsx
      <ol className="jobSteps">
        {active.steps.map((step) => (
          <li key={`${step.stage}-${step.lane}-${step.name}`} className={stepClass(step)}>
            {step.status === "done" ? (
              <CheckCircleIcon size={14} weight="fill" />
            ) : (
              <span className="step-index" />
            )}
            {step.name}
          </li>
        ))}
      </ol>

      {active.steps
        .filter((step) => step.status === "running" && step.progress?.total)
        .map((step) => (
          <div className="jobProgress" key={`${step.stage}-${step.lane}-${step.name}`}>
            <div className="jobProgress-track">
              <div
                className="jobProgress-fill"
                style={{
                  width: `${Math.round(((step.progress?.current ?? 0) / (step.progress?.total ?? 1)) * 100)}%`,
                }}
              />
            </div>
            <span className="mono">
              {step.progress?.current ?? 0} / {step.progress?.total}
              {step.progress?.label ? ` · ${step.progress.label}` : ""}
            </span>
          </div>
        ))}
```

Ниже, рядом с существующей `statusLabel`, добавить и удалить лишнее:

```tsx
function stepClass(step: JobStep) {
  if (step.status === "done") return "is-done";
  if (step.status === "running") return "is-current";
  if (step.status === "failed" || step.status === "cancelled") return "is-failed";
  return "";
}
```

Строку 10 `frontend/components/JobMonitor.tsx` заменить на:

```tsx
import { cancelJob, startPipeline, type JobStep, type Pipeline } from "@/app/api";
```

Функцию `finished` удалить — её звал только вычисленный индекс, которого больше нет; проверить, что других её вызовов в файле не осталось.

- [ ] **Step 3: Подпись состояния — «готово N из M»**

В `frontend/components/RunBar.tsx` заменить строку 25:

```tsx
        {active.status === "running" ? `готово ${active.step} из ${active.step_count}` : active.status}
```

И в `frontend/components/JobMonitor.tsx` — функцию `statusLabel`:

```tsx
function statusLabel(job: { status: string; step: number; step_count: number }) {
  if (job.status === "running") return `готово ${job.step} из ${job.step_count}`;
```

(остаток функции не трогать)

- [ ] **Step 4: Стиль упавшего шага**

В `frontend/app/globals.css` после блока `.jobSteps .is-current` (строки 533–536) добавить:

```css
.jobSteps .is-failed {
  color: var(--error);
}

.is-failed .step-index {
  background: var(--error);
}
```

Токен именно `--error` (`:root`, строка 19: `#b42318`) — им же покрашен `.form-error`. Токена `--danger` в дизайн-системе нет.

- [ ] **Step 5: Убрать дублирующую запись прогресса в колонку джобы**

Фронт больше не читает `job.progress` — вторая запись на каждый тик прогресса перестала быть нужной. В `backend/collector/services/jobs.py::make_context` заменить `progress`:

```python
    def progress(current, total, label):
        _set_step(job_id, step_index,
                  progress={"current": current, "total": total, "label": label})
```

- [ ] **Step 6: Прогнать тесты бэкенда**

Run: `cd backend && uv run pytest -q`
Expected: PASS. Тест `test_step_progress_lands_in_its_own_step` из задачи 4 продолжает проверять прогресс — он читает шаг, а не колонку.

- [ ] **Step 7: Собрать фронтенд**

Run: `cd frontend && npx tsc --noEmit && npm run build`
Expected: обе команды без ошибок.

- [ ] **Step 8: Проверить вживую параллельную стадию**

Run: `cd backend && uv run python main.py`, в соседнем терминале `cd frontend && npm run dev`, открыть `http://localhost:3000/activity`, нажать «Поиск новых лидов».
Expected: два шага одновременно подсвечены как текущие, под списком две полосы прогресса с разными подписями («карточки 2GIS» и «ленты инстаграма»), «прервать» останавливает обе ветки.

- [ ] **Step 9: Коммит**

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test
git add frontend/app/api.ts frontend/components/JobMonitor.tsx frontend/components/RunBar.tsx \
  frontend/app/globals.css backend/collector/services/jobs.py
git commit -m "feat(web): шаг красится по своему статусу, полоса — у каждой ветки

Текущий шаг вычислялся сравнением индексов, и в стадии с двумя дорожками
это выражалось одним числом неправильно. Статус приезжает с бэкенда, полос
прогресса теперь столько, сколько шагов идёт. Колонка progress у джобы
больше не пишется — её читал только старый одинокий бар."
```

---

## Проверка результата

После всех пяти задач:

- [ ] `cd backend && uv run pytest -q` — зелёный.
- [ ] `POST /api/pipeline/discover` на живых данных: в логе видно, что `collect.instagram` и `collect.sites` идут одновременно; общее время джобы — около трёх часов вместо 4 ч 36 мин.
- [ ] Ни один источник не ответил капчей: в логе джобы нет `BotCheck`, а в `data/raw/` не появилось сайдкаров с `captcha` в `final_url`.
- [ ] `POST /api/pipeline/classify` на непустом наборе новых компаний: время слоя примерно вдвое меньше прежнего.
- [ ] Обновить `CLAUDE.md`: секция про `collect` — темп теперь в `[pacing]` и на хост, а не на воркер; секция про джобы — у шага есть стадия, дорожка и статус, `discover` собирает Instagram параллельно.
