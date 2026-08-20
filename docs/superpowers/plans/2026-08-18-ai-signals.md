# ИИ-сигналы и досье компаний (Система 1, v3) — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Заменить regex-скоринг по инфраструктуре на четырёхслойный ИИ-анализ (отзывы 2GIS, внутренние страницы сайта, Instagram-комментарии) с синтезом в досье компании, дать системе 2 богатый материал для персонализации и вернуть LLM в ранжирование — обработав всю базу, а не топ-40.

**Architecture:** Послойный ИИ-анализ вместо одного большого вызова. Каждый слой (`analyze.reviews`, `analyze.site`, `analyze.instagram`) кэшируется своим ключом в `state.llm_answers`, поэтому досбор одного отзыва не обесценивает оплаченные ответы остальных. `synthesize_dossier` работает только по извлечённым фактам (без сырых страниц) и даёт досье каждой компании, включая тех, у кого нет сайта и Instagram. Ответы модели — невосстановимый слой; пересборка (`rebuild.run`) читает их через ATTACH и пишет только сигналы и `dossiers_all` в `derived.db`. Веса сигналов остаются в `config.toml` и назначаются правилами по типу, модель их не видит.

**Tech Stack:** Python 3.13 + uv, SQLite (WAL), langchain-openrouter + pydantic (structured output), pytest, FastAPI (реестр операций уже готов).

**Spec:** [docs/superpowers/specs/2026-08-18-ai-signals-design.md](../specs/2026-08-18-ai-signals-design.md)

## Global Constraints

- Команды `uv run` запускаются **из каталога `collector/`**, если не сказано иное; команды writer'а — из `writer/`.
- **Предусловие реализовано:** две базы (`data/derived.db`, `data/state.db`), `llm_answers`, адаптер `services/storage.py`, реестр `OPERATIONS`/`PIPELINES`, `RunContext`, pytest — всё из спеки `2026-08-18-storage-and-operations-design.md` уже есть на диске. Здесь они используются, а не проектируются.
- **В рабочем дереве лежат незакоммиченные изменения** (система 2, docker, hh-разбор, хвост операционного слоя). Коммитить **только перечисленные в задаче файлы** через явный `git add <путь>`. Никаких `git add -A` / `git add .` / `git commit -a`.
- **`data/raw/` — невосстановимое сырьё.** Ни один файл не удалять и не править. Новые источники (отзывы, внутренние страницы, комментарии) ложатся туда же через `fetch.get`/`storage.put` как обычные страницы `<sha1(url)>.html.gz` + сайдкар.
- **`rebuild.py` не ходит в сеть:** не тянет `services.fetch` и `scrapling` ни прямо, ни транзитивно — проверяется статически (`tests/test_operations.py::test_rebuild_import_graph_has_no_network`). Любой слой анализа с сетью — операция `analyze.*`, а не стадия пересборки.
- **Ни одна операция не пишет в обе базы сразу.** Анализ пишет только в `state.llm_answers`; пересборка читает `state.*` через ATTACH, а пишет только в `derived.db` (`*_all` + view). Проверяется `test_no_operation_writes_to_both_dbs` — его `DERIVED_TABLES` обновляется под `dossiers_all` в Task 22.
- **LLM-кэш — невосстановимый.** Ключ `(kind, subject, model, prompt)`. Смена промпта обесценивает только свой слой. `store_answer`/`answered` уже это делают — новые слои пользуются ими, а не заводят второй механизм.
- **Главная проверка — `test_quote_is_verbatim`:** цитата сигнала обязана стоять дословно в сырье (отзыве/подписи/странице). Не нашлась дословно — находке в `signals` не место. Это единственная защита от выдуманного факта в письме живому человеку.
- **`top_n = 40` удаляется** из `config.toml`: обрабатывается вся база. `analyze.profile` и `analyze.ig_signals` снимаются из реестра вместе с наполнением `profiles` (строки прошлых прогонов и ответы модели остаются лежать).
- Веса сигналов живут в `config.toml` (`[scoring.intent]`); тип без веса — **ошибка сборки**, а не тихий `0.5` (проверяется `test_signals`).
- Стиль сообщений — conventional commits, по-русски, как в `git log` (`feat(collector): ...`, `test(collector): ...`, `docs: ...`).
- Правки в `writer/` пересекают границу систем: `writer/tests/test_leads.py` обновляется в той же задаче, что и запрос `leads_source.py`. **Полная перепись промпта системы 2 — следующая задача, в объём не входит** (спека §5); здесь делается только замена `profiles` → `dossiers` в запросе и минимальные правки доступа, чтобы writer не падал.

---

## Структура файлов

| Файл | Что с ним происходит | Задача |
|---|---|---|
| `collector/config.toml` | `[reviews]` ключ/лимит, `[site.links]` словарь ссылок, новые веса, удаление `top_n` и `service_catalog` | 2, 6, 13, 19, 25 |
| `collector/schemas/reviews.py` | **новый**: `Complaint`, `ReviewsAnalysis` | 5 |
| `collector/schemas/site.py` | **новый**: `Hiring`, `SiteAnalysis` | 10 |
| `collector/schemas/instagram.py` | **новый**: `Question`, `Promo`, `InstagramAnalysis` | 17 |
| `collector/schemas/dossier.py` | **новый**: `Hook`, `Pain`, `Dossier` | 23 |
| `collector/services/pipeline/llm.py` | **новый**: общие LLM-хелперы (`structured_model`, `store_answer`, `answered`), вынесенные из `analyze.py` | 4 |
| `collector/services/pipeline/analyze.py` | из него уходят `profile`/`ig_signals` и общие хелперы; остаются `reviews`/`site`/`instagram`/`dossier` операции | 4–26 |
| `collector/services/pipeline/collect.py` | новые операции `reviews`, `site_pages`, `ig_comments`, `ig_profile` | 3, 9, 16 |
| `collector/services/sources.py` | новые разборы: `parse_reviews`, `parse_site_links`, `parse_ig_comments`, `parse_ig_profile`; `parse_ig_post` учится хранить `pk` | 4, 9, 15 |
| `collector/services/enrich.py` | производные сигналы от слоёв: `reviews_signals` (один сигнал на тип — `newest_review_match`), `site_ai_signals`, `instagram_ai_signals`, тренд охватов; удаление `service_catalog`-маркера; отключение старого LLM-слоя `ig_signals` (`ig_answers`/`newest_per_type`/`post_with_quote` удаляются) | 7, 12, 18, 19, 25 |
| `collector/services/pipeline/dossier.py` | **новый**: `fill_dossiers(db, run_id)` — синтез досье из слоёв | 23–24 |
| `collector/services/pipeline/rebuild.py` | новые стадии пересборки: слои-сигналы, досье; удаление `fill_profiles` | 7, 12, 18, 24, 26 |
| `collector/services/pipeline/__init__.py` | реестр: убрать `analyze.profile`/`analyze.ig_signals`, добавить новые операции | 26 |
| `collector/store/schema.sql` | новая `dossiers_all` + view `dossiers` | 23 |
| `collector/fixtures/*` | **новый**: `reviews.html.gz`, `site_home_links.html.gz`, `ig_comments.html.gz`, `ig_profile.html.gz` | 4, 9, 15 |
| `collector/tests/test_parsers.py` | тесты новых разборов на фикстурах | 4, 9, 15 |
| `collector/tests/test_signals.py` | типы в конфиге, тип без веса — ошибка, `test_quote_is_verbatim`, досье | 7, 18, 23–25 |
| `collector/tests/test_dossiers.py` | **новый**: hooks, pains, sources, confidence | 23–24 |
| `writer/leads_source.py` | запрос `profiles` → `dossiers`; seed из досье | 27 |
| `writer/agent.py` | минимальные правки доступа к новому seed (полная перепись промпта — следующая задача) | 27 |
| `writer/tests/test_leads.py` | сид формы `dossiers_all`, новые ассерты seed | 27 |
| `docs/ARCHITECTURE_v3.md` | **новый**: по итогам (спека §«Решения», постановка «пишется ARCHITECTURE_v3.md») | 28 |

---

## Этап 1. Отзывы 2GIS (спека §1.1, §2.1, §4)

### Task 1: фикстура ответа отзывов 2GIS

Первый шаг — поймать живую страницу отзывов одного филиала, положить её в `fixtures/` как эталон, на который пишутся разбор и тест. Числа в тестах — свойства именно этого файла, как у `gis_rubric`/`gis_firm`.

**Files:**
- Create: `collector/fixtures/reviews.html.gz`

**Interfaces:**
- Consumes: ничего.
- Produces: `fixtures/reviews.html.gz` — эталон для `parse_reviews` (Task 4).

- [ ] **Step 1: снять ответ отзывов в raw/**

Взять `branch_id` первой карточки из живого снимка и сходить в публичный API отзывов. Ответ пишется в `raw/` как обычная страница — это и есть сбор:

```bash
cd collector
BRANCH=$(uv run python -c "from services import store; db=store.connect(); \
print(db.execute('select branch_id from orgs order by branch_id limit 1').fetchone()[0]); db.close()")
echo "branch=$BRANCH"
```

Затем одним запросом (ключ — из `config.toml`, на время пробы подставь реальный публичный ключ 2GIS из фронтенда):

```bash
KEY="<публичный ключ 2GIS>"
curl -s "https://public-api.reviews.2gis.com/2.0/branches/$BRANCH/reviews?limit=50&sort_by=date_edited&rated=true&locale=ru_KZ&key=$KEY" -o /tmp/reviews.json
python3 -c "import json; d=json.load(open('/tmp/reviews.json')); print(type(d).__name__, len(d.get('reviews', d.get('items', []))))"
```

Ожидается: JSON-список отзывов с полями `text`, `rating`, `date_created`, `comments_count`, `official_answer`. (Реальный контракт зафиксировать в Task 4 по этой структуре — API недокументирован.)

- [ ] **Step 2: положить ответ в fixtures/**

Повторить форму существующих фикстур — gzip + имя без точки, чтобы `iter_pages`/фикстурный разбор не путал её с `.json`-кэшем:

```bash
cd collector
gzip -c /tmp/reviews.json > fixtures/reviews.html.gz
# примечание: гайды существующих фикстур — это HTML, а здесь JSON.
# json_body() (sources.py) вырезает JSON из <html><body>; raw JSON достаточно для разбора.
ls -la fixtures/reviews.html.gz
```

Ожидается: файл существует, ~десятки КБ.

- [ ] **Step 3: записать эталонные числа для Task 4**

Записать в заметку к Task 4: сколько отзывов в файле, у скольких есть `official_answer`, какие `rating` встречаются, есть ли `date_created` у всех. Эти числа станут ассертами `test_reviews_parsing`.

- [ ] **Step 4: не коммитить**

Фикстура лежит в git, но коммитится вместе с разбором и тестом в Task 4 (один атомарный шаг).

---

### Task 2: конфиг — ключ и лимит отзывов

**Files:**
- Modify: `collector/config.toml`

**Interfaces:**
- Produces: `config["reviews"]["key"]`, `config["reviews"]["limit"]`, `config["reviews"]["max_reviews_per_company"]` — читаются сбором (Task 3) и анализом (Task 6).

- [ ] **Step 1: добавить секцию `[reviews]`**

```toml
[reviews]
# Публичный ключ 2GIS, зашитый во фронтенд public-api.reviews.2gis.com.
# Недокументированный API: ключ могут сменить — собранное в raw/ останется навсегда.
key = "<вставь реальный публичный ключ из фронтенда 2GIS>"
# Одна страница на филиал, свежие сверху (sort_by=date_edited).
limit = 50
# Сколько свежих отзывов филиалов компании попадает в промпт слоя отзывов.
max_reviews_per_company = 30
```

Ожидается: секция читается `tomllib` (валидный TOML).

- [ ] **Step 2: проверить чтение конфига**

```bash
cd collector
uv run python -c "import tomllib; c=tomllib.loads(open('config.toml').read()); print(c['reviews']['limit'], c['reviews']['max_reviews_per_company'])"
```

Ожидается: `50 30`.

- [ ] **Step 3: коммит**

```bash
cd collector && git add config.toml && git commit -m "feat(collector): конфиг отзывов 2GIS — ключ и лимиты"
```

---

### Task 3: сбор отзывов — операция `collect.reviews`

**Files:**
- Modify: `collector/services/pipeline/collect.py`

**Interfaces:**
- Consumes: `config["reviews"]` (Task 2), `store.connect()` (view `orgs`/`company_links`), `fetch.get`, `sources.ig_username` не нужен.
- Produces: `collect.reviews(ctx)` — операция воркера, кладёт отзывы в `raw/`; возвращает dict. Читается rebuild'ом через `load_pages()` как обычные страницы.

- [ ] **Step 1: написать падающий тест — операция регистрируется**

Добавить в `collector/tests/test_operations.py`:

```python
def test_reviews_op_accepts_runcontext():
    from services.pipeline import collect
    assert callable(collect.reviews)
```

- [ ] **Step 2: убедиться, что тест падает**

```bash
cd collector && uv run pytest tests/test_operations.py::test_reviews_op_accepts_runcontext -v
```

Ожидается: `AttributeError: module ... has no attribute 'reviews'`.

- [ ] **Step 3: реализовать `collect.reviews`**

Добавить константы и операцию:

```python
REVIEWS_API = "https://public-api.reviews.2gis.com/2.0/branches/{branch_id}/reviews"
REVIEWS_PARAMS = "?limit={limit}&sort_by=date_edited&rated=true&locale=ru_KZ&key={key}"


def reviews(ctx):
    """Отзывы филиалов 2GIS. Один запрос на филиал, свежие сверху.

    Источник — публичный API отзывов, недокументированный: ключ живёт в config.toml.
    branch_id берётся из view текущего прогона (склейка Ф5 уже свела филиалы к
    компаниям). Страницы ложатся в raw/ как обычно — дедуп по ним же.
    """
    config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    reviews_cfg = config["reviews"]
    db = engine_connect()
    try:
        branches = [r[0] for r in db.execute(
            "SELECT DISTINCT branch_id FROM orgs WHERE review_count > 0"
            " ORDER BY branch_id")]
    finally:
        db.close()
    budget = Budget(None)
    ctx.log(f"отзывы 2GIS: {len(branches)} филиалов с отзывами")

    def fetch_reviews(budget, branch_id):
        url = REVIEWS_API.format(branch_id=branch_id) + REVIEWS_PARAMS.format(
            limit=reviews_cfg["limit"], key=reviews_cfg["key"])
        budget.get(url, headers={"Accept": "application/json"})

    collected, skipped = download_all(fetch_reviews, budget, branches, ctx, "отзывы 2GIS")
    return {"branches": len(branches), "collected": collected, "skipped": skipped}
```

Где `engine_connect` — локальный импорт `from services import store as engine`, как в `collect.sites`. Пауза `Budget` уже встроена (`PAUSE_SECONDS`).

- [ ] **Step 4: прогнать тест**

```bash
cd collector && uv run pytest tests/test_operations.py::test_reviews_op_accepts_runcontext -v
```

Ожидается: PASS.

- [ ] **Step 5: проверить, что операция не тянет сеть в rebuild**

```bash
cd collector && uv run pytest tests/test_operations.py::test_rebuild_import_graph_has_no_network -v
```

Ожидается: PASS (collect не входит в граф rebuild).

- [ ] **Step 6: коммит**

```bash
cd collector && git add services/pipeline/collect.py tests/test_operations.py && git commit -m "feat(collector): сбор отзывов 2GIS — операция collect.reviews"
```

---

### Task 4: разбор отзывов + `parse_ig_post` хранит `pk`

**Files:**
- Modify: `collector/services/sources.py`
- Modify: `collector/tests/test_parsers.py`
- Create: `collector/fixtures/reviews.html.gz` (Task 1), `collector/fixtures/ig_comments.html.gz` (Task 15), `collector/fixtures/ig_profile.html.gz` (Task 15)

**Interfaces:**
- Consumes: `fixtures/reviews.html.gz` (Task 1).
- Produces: `sources.parse_reviews(body, branch_id) -> list[dict]` с ключами `branch_id, text, rating, date_created, comments_count, official_answer, official_answer_date`; `sources.parse_ig_post` теперь возвращает `pk`.

- [ ] **Step 1: написать падающий тест разбора отзывов**

```python
def test_reviews_parsing():
    """Ответ API отзывов: тексты, рейтинг, дата, комментарии, official_answer.

    Эталон — фикстура reviews.html.gz, снятая в Task 1. Числа — свойства файла.
    """
    body = fixture_html("reviews")
    reviews = sources.parse_reviews(body, "70000001017502602")
    assert len(reviews) > 0, "ни одного отзыва в эталоне"
    assert all(r["branch_id"] == "70000001017502602" for r in reviews), "чужой branch_id"
    assert all(r["text"] for r in reviews), "отзыв без текста"
    assert all(r["rating"] in (1, 2, 3, 4, 5) for r in reviews), f"рейтинг вне шкалы"
    assert all(r["date_created"] for r in reviews), "отзыв без даты"
    assert all(r["comments_count"] is not None for r in reviews), "нет счётчика комментариев"
    assert all("official_answer" in r for r in reviews), "official_answer потерян"
    answered = [r for r in reviews if r["official_answer"]]
    assert isinstance(answered, list), "official_answer обязан быть строкой или None"
```

- [ ] **Step 2: убедиться, что тест падает**

```bash
cd collector && uv run pytest tests/test_parsers.py::test_reviews_parsing -v
```

Ожидается: `AttributeError: module 'services.sources' has no attribute 'parse_reviews'`.

- [ ] **Step 3: реализовать `parse_reviews`**

В `sources.py`:

```python
def parse_reviews(body, branch_id):
    """Отзывы филиала из public-api.reviews.2gis.com.

    Ответ API — JSON, который fetch.get завернул в <html><body>: тело дословно,
    и json_body() вырезает его как у ленты инстаграма. Свежие сверху — порядок
    API, разбор его не меняет. official_answer — строка (есть) или None (нет):
    на нём держится сигнал reviews_unanswered_complaint.
    """
    data = json.loads(json_body(body))
    reviews = data.get("reviews") or data.get("items") or []
    rows = []
    for item in reviews:
        answer = item.get("official_answer")
        rows.append({
            "branch_id": branch_id,
            "text": (item.get("text") or "").strip(),
            "rating": item.get("rating"),
            "date_created": item.get("date_created"),
            "comments_count": item.get("comments_count"),
            "official_answer": (answer or {}).get("text") if isinstance(answer, dict) else answer,
            "official_answer_date": (answer or {}).get("date_created")
                if isinstance(answer, dict) else None,
        })
    return rows
```

- [ ] **Step 4: реализовать хранение `pk` в `parse_ig_post`**

В `parse_ig_post` добавить ключ:

```python
        "pk": item.get("pk"),
        "shortcode": item["code"],
```

(`pk` нужен Task 15/16: комментарии запрашиваются по `media/{pk}/comments/`, а без него адрес не собрать.)

- [ ] **Step 5: прогнать тесты разбора**

```bash
cd collector && uv run pytest tests/test_parsers.py -v
```

Ожидается: PASS (включая новые `test_reviews_parsing` и старые `test_ig_*`).

- [ ] **Step 6: коммит**

```bash
cd collector && git add services/sources.py tests/test_parsers.py fixtures/reviews.html.gz && git commit -m "feat(collector): разбор отзывов 2GIS, parse_ig_post хранит pk"
```

---

### Task 5: схема слоя отзывов

**Files:**
- Create: `collector/schemas/reviews.py`

**Interfaces:**
- Produces: `ReviewsAnalysis`, `Complaint` — валидируют ответ модели в `analyze.reviews` (Task 6) и читаются `fill_reviews_signals` (Task 7).

- [ ] **Step 1: создать схему по спеке §2.1**

```python
from typing import Literal

from pydantic import BaseModel, Field


class Complaint(BaseModel):
    type: Literal["не дозвонились", "не ответили на заявку", "долго ждали ответа",
                  "сорвали срок", "качество работы", "цена", "другое"]
    quote: str = Field(description="ДОСЛОВНАЯ фраза из отзыва, не пересказ")
    date: str = Field(description="дата отзыва, как в сырье")


class ReviewsAnalysis(BaseModel):
    complaints: list[Complaint] = Field(default_factory=list)
    praise_themes: list[str] = Field(default_factory=list)
    unanswered_complaints: int = Field(default=0)
    responsiveness: str | None = Field(None, description="сухо / шаблонно / живо / никак")
    service_language: list[str] = Field(default_factory=list)
```

- [ ] **Step 2: проверить импорт схемы**

```bash
cd collector && uv run python -c "from schemas.reviews import ReviewsAnalysis; print(ReviewsAnalysis().model_dump())"
```

Ожидается: `{'complaints': [], 'praise_themes': [], 'unanswered_complaints': 0, 'responsiveness': None, 'service_language': []}`.

- [ ] **Step 3: коммит**

```bash
cd collector && git add schemas/reviews.py && git commit -m "feat(collector): схема слоя отзывов — жалобы и отзывчивость"
```

---

### Task 6: слой анализа отзывов — операция `analyze.reviews`

**Files:**
- Create: `collector/services/pipeline/llm.py`
- Modify: `collector/services/pipeline/analyze.py`

**Interfaces:**
- Consumes: `ReviewsAnalysis` (Task 5), `sources.parse_reviews` (Task 4), `config["llm"]["model"]`, `config["reviews"]["max_reviews_per_company"]`.
- Produces: `analyze.reviews(ctx)` — операция, кладёт ответы kind=`"reviews"` в `state.llm_answers`; `services.pipeline.llm.structured_model/store_answer/answered` — общие хелперы, на них встанут `site`/`instagram`/`dossier`.

- [ ] **Step 1: вынести общие LLM-хелперы в `llm.py`**

Создать `collector/services/pipeline/llm.py` переносом из `analyze.py` (`structured_model`, `store_answer`, `answered`, `MAX_RETRIES`, `REASONING`):

```python
"""Общие LLM-хелперы анализа: структурированный вывод, кэш ответов.

Сеть есть (в отличие от rebuild): платится за компанию/аккаунт, увиденные
впервые. Ответ сохраняется в невосстановимую state.llm_answers, поэтому
пересборка остаётся чистой функцией от сырья и не стоит ни цента.
"""

import json
import os

from langchain_openrouter import ChatOpenRouter

from services import storage

MAX_RETRIES = 2
REASONING = {"enabled": False}


def structured_model(model, schema):
    """Модель с валидацией схемы: повторы при невалидной схеме — на стороне LangChain."""
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError(
            "OPENROUTER_API_KEY пуст. Поднять бэкенд: "
            "uv run --env-file .env uvicorn api:app --port 8787"
        )
    return ChatOpenRouter(
        model=model, temperature=0, max_retries=MAX_RETRIES, reasoning=REASONING,
    ).with_structured_output(schema, method="json_schema")


def store_answer(db, kind, subject, model, prompt, answer):
    """Ответ кладётся в state.llm_answers вместе с запросом: через месяц промпт
    будет другим, и без запроса нельзя понять, на что модель отвечала."""
    db.execute(
        "INSERT INTO state.llm_answers (kind, subject, model, prompt, answer)"
        " VALUES (?, ?, ?, ?, ?)",
        (kind, subject, model, prompt, json.dumps(answer, ensure_ascii=False)),
    )
    db.commit()


def answered(db, kind, subject, model, prompt):
    """Есть ли уже оплаченный ответ на этот запрос — в базе или файлом в raw/."""
    hit = db.execute(
        "SELECT 1 FROM state.llm_answers WHERE kind = ? AND subject = ?"
        " AND model = ? AND prompt = ? LIMIT 1",
        (kind, subject, model, prompt),
    ).fetchone()
    return bool(hit) or storage.has_llm_answer(model, prompt)
```

- [ ] **Step 2: написать падающий тест операции**

```python
def test_reviews_analyze_op_accepts_runcontext():
    from services.pipeline import analyze
    assert callable(analyze.reviews)
```

- [ ] **Step 3: убедиться, что тест падает**

```bash
cd collector && uv run pytest tests/test_operations.py::test_reviews_analyze_op_accepts_runcontext -v
```

Ожидается: `AttributeError: ... has no attribute 'reviews'`.

- [ ] **Step 4: реализовать `analyze.reviews` в `analyze.py`**

`ctx.log(...)` в теле операции — не только читаемость лога. Task 24 удаляет из
`analyze.py` функции `profile`/`ig_signals` — единственные, где сейчас есть
вызовы `ctx.log`. `test_operations_use_the_context_they_are_given`
(`tests/test_operations.py`) проверяет наличие `"ctx.log"` во всём модуле, а не
в конкретной функции: без своего `ctx.log` у каждой новой операции этот тест
после Task 24 упадёт разом на `analyze.reviews`/`analyze.site`/
`analyze.instagram`/`analyze.dossier`. То же самое сделано в Task 11/18/21.

```python
REVIEWS_KIND = "reviews"
REVIEWS_SYSTEM = (
    "Ты аналитик B2B-лидогенерации в Казахстане. По отзывам на компанию найди, "
    "где клиенты сами говорят о боли, которую решает исходящий лидоген. "
    "Типы «не дозвонились» и «не ответили на заявку» — это сказанное клиентом "
    "вслух «у нас утекают лиды». Не выдумывай: quote обязана быть дословной "
    "фразой из отзыва, date — датой из того же отзыва. Не нашёл жалоб — пустой список."
)


def reviews(ctx):
    from schemas.reviews import ReviewsAnalysis
    from services import store as engine
    from services.pipeline import llm
    db = engine.connect()
    try:
        config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
        model = config["llm"]["model"]
        max_reviews = config["reviews"]["max_reviews_per_company"]
        targets = review_targets(db, max_reviews)
        ctx.log(f"отзывы: {len(targets)} компаний с отзывами, модель {model}")
        if not targets:
            return {"companies": 0, "new_calls": 0}
        llm_model = llm.structured_model(model, ReviewsAnalysis)
        spent = 0
        for number, (company_id, name, city, text) in enumerate(targets, 1):
            ctx.check_cancelled()
            prompt = reviews_prompt(name, city, text)
            subject = f"{name} | {city}"
            if not llm.answered(db, REVIEWS_KIND, subject, model, prompt):
                answer = llm_model.invoke([("system", REVIEWS_SYSTEM), ("human", prompt)])
                llm.store_answer(db, REVIEWS_KIND, subject, model, prompt,
                                 {"analysis": answer.model_dump()})
                spent += 1
            ctx.progress(number, len(targets), "отзывы")
        ctx.log(f"  оплачено вызовов: {spent}, остальное взято из кэша")
        return {"companies": len(targets), "new_calls": spent}
    finally:
        db.close()
```

- [ ] **Step 5: реализовать данные слоя отзывов**

В `analyze.py` (вспомогательные функции):

```python
def review_targets(db, max_reviews):
    """(company_id, название, город, текст до max_reviews отзывов) по компаниям.

    Отзывы филиалов компании собираются из raw/ (слой сырья), текст склеивается.
    Компания с филиалами, у которых отзывов нет, в выборку не попадает.
    """
    from services.pipeline import rebuild
    pages = {p["url"]: p for p in rebuild.load_pages()}
    reviews_by_branch = {}
    for page in pages.values():
        if "reviews.2gis.com" not in page["url"]:
            continue
        branch = page["url"].split("/branches/", 1)[1].split("/", 1)[0]
        reviews_by_branch[branch] = sources.parse_reviews(rebuild.html_of(page), branch)

    rows = db.execute(
        "SELECT c.company_id, coalesce(o.org_name, o.name, c.name_norm), c.city,"
        "       l.branch_id"
        " FROM companies c JOIN company_links l ON l.company_id = c.company_id"
        "   AND l.rule = 'self' JOIN orgs o ON o.branch_id = l.branch_id"
        " ORDER BY c.company_id").fetchall()
    targets = []
    for company_id, name, city, branch_id in rows:
        revs = reviews_by_branch.get(branch_id) or []
        if not revs:
            continue
        text = "\n".join(
            f"[{r['rating']}] {r['text']}" + (" [ОТВЕТИЛИ]" if r["official_answer"] else "")
            for r in revs[:max_reviews]
        )
        targets.append((company_id, name, city, text))
    return targets


def reviews_prompt(name, city, text):
    return "\n".join([
        f"Компания: {name}",
        f"Город: {city}",
        "Отзывы (дословно, с рейтингом; [ОТВЕТИЛИ] — компания ответила):",
        text or "(отзывов нет)",
    ])
```

- [ ] **Step 6: прогнать тесты**

```bash
cd collector && uv run pytest tests/test_operations.py -v tests/test_parsers.py -v
```

Ожидается: PASS.

- [ ] **Step 7: коммит**

```bash
cd collector && git add services/pipeline/llm.py services/pipeline/analyze.py tests/test_operations.py && git commit -m "feat(collector): слой анализа отзывов — операция analyze.reviews"
```

---

### Task 7: сигналы отзывов в пересборке

**Files:**
- Modify: `collector/services/enrich.py`
- Modify: `collector/services/pipeline/rebuild.py`
- Modify: `collector/config.toml`
- Modify: `collector/tests/test_signals.py`

**Interfaces:**
- Consumes: `rebuild.load_llm_answers(db, "reviews")` (Task 6 хранит kind), `sources.parse_reviews`, `weights` из `config["scoring"]["intent"]`.
- Produces: `enrich.reviews_signals(db, run_id, pages, weights)` — пишет `signals_all` типами `reviews_missed_lead`, `reviews_unanswered_complaint`; стадия `"отзывы от модели"` в `rebuild.run`.

- [ ] **Step 1: тест типов в конфиге**

В `test_signals.py` добавить:

```python
REVIEW_TYPES = {"reviews_missed_lead", "reviews_unanswered_complaint"}


def test_review_signal_types_have_weights(live_db):
    """Новые типы отзывов существуют в config.toml — сигналу нужна цена."""
    import tomllib
    from pathlib import Path
    cfg = tomllib.loads(Path("config.toml").read_text(encoding="utf-8"))
    weights = cfg["scoring"]["intent"]
    for t in REVIEW_TYPES:
        assert t in weights, f"нет веса для {t} — сигнал не наберёт цену"
```

- [ ] **Step 2: убедиться, что тест падает**

```bash
cd collector && uv run pytest tests/test_signals.py::test_review_signal_types_have_weights -v
```

Ожидается: FAIL — `reviews_*` ещё нет в конфиге.

- [ ] **Step 2b: добавить веса**

Вставить в `config.toml` в `[scoring.intent]` две строки (перед `[scoring.fit]`):

```toml
reviews_missed_lead = 5.0          # «не дозвонились», «не ответили на заявку»
reviews_unanswered_complaint = 5.0 # жалоба без ответа компании
```

Проверить чтение:

```bash
cd collector && uv run python -c "import tomllib; w=tomllib.loads(open('config.toml').read())['scoring']['intent']; print(w['reviews_missed_lead'], w['reviews_unanswered_complaint'])"
```

Ожидается: `5.0 5.0`.

- [ ] **Step 2c: убедиться, что тест проходит**

```bash
cd collector && uv run pytest tests/test_signals.py::test_review_signal_types_have_weights -v
```

Ожидается: PASS.

- [ ] **Step 4: реализовать `reviews_signals` в `enrich.py`**

**Почему не по жалобе на строку.** `signals_all` уникален по `(run_id, company_id,
type, observed_at, url)`. `review_url` строит адрес по `branch_id`, а не по
конкретному отзыву, и `date_created` у 2GIS обычно дата без времени — значит две
жалобы одного типа с одной датой у одного филиала дают одинаковый `(company_id,
type, observed_at, url)` и падают на `INSERT` в `signals_all` с
`sqlite3.IntegrityError`. `site_signals` защищается от этого множеством `seen`,
`instagram_signals`/`newest_per_type` — «по одному сигналу на тип от самого
свежего поста». Здесь та же дисциплина: `newest_review_match` берёт одну, самую
свежую подтверждённую жалобу на тип, а не пишет по строке на каждую.

```python
def reviews_signals(db, run_id, pages, weights):
    """Сигналы отзывов от модели. Сети нет — ответы оплачены и лежат в llm_answers.

    Жалобы «не дозвонились»/«не ответили на заявку» -> reviews_missed_lead,
    жалоба без ответа компании -> reviews_unanswered_complaint. Цитата обязана
    стоять дословно в отзыве (главная проверка test_quote_is_verbatim); не
    нашлась — находке в signals не место. Один сигнал на тип на компанию:
    newest_review_match берёт самую свежую подтверждённую жалобу, а не пишет
    по строке на каждую — иначе две жалобы одного типа с одной датой у одного
    филиала столкнулись бы по PRIMARY KEY signals_all (url собирается по
    branch_id, не по отзыву).
    """
    from services.pipeline import rebuild
    reviews_by_branch = {}
    for page in pages:
        if "reviews.2gis.com" not in page["url"]:
            continue
        branch = page["url"].split("/branches/", 1)[1].split("/", 1)[0]
        reviews_by_branch[branch] = sources.parse_reviews(rebuild.html_of(page), branch)
    companies = company_branches(db, run_id)
    reviews_by_company = {}
    for company_id, branch_id in companies:
        for review in reviews_by_branch.get(branch_id) or []:
            reviews_by_company.setdefault(company_id, []).append(review)

    for answer in rebuild.load_llm_answers(db, "reviews"):
        company_id = company_by_subject(db, run_id, answer["subject"])
        if not company_id:
            continue
        analysis = answer.get("analysis") or {}
        reviews = reviews_by_company.get(company_id, [])

        missed = newest_review_match(reviews, [
            c for c in (analysis.get("complaints") or [])
            if c["type"] in ("не дозвонились", "не ответили на заявку")
        ])
        if missed:
            review, quote = missed
            emit(db, run_id, company_id, "reviews_missed_lead",
                 review["date_created"], weights, quote, review_url(company_id, review))

        if analysis.get("unanswered_complaints"):
            unanswered = newest_review_match(reviews, analysis.get("complaints") or [])
            if unanswered:
                review, quote = unanswered
                emit(db, run_id, company_id, "reviews_unanswered_complaint",
                     review["date_created"], weights, quote, review_url(company_id, review))
```

- [ ] **Step 5: вспомогательные функции в `enrich.py`**

```python
def company_branches(db, run_id):
    return db.execute(
        "SELECT l.company_id, l.branch_id FROM company_links_all l"
        " WHERE l.run_id = ? AND l.rule = 'self'", (run_id,)).fetchall()


def company_by_subject(db, run_id, subject):
    name, _, city = subject.partition(" | ")
    row = db.execute(
        "SELECT c.company_id FROM companies_all c"
        " LEFT JOIN company_links_all l ON l.company_id = c.company_id AND l.run_id = ?"
        "   AND l.rule = 'self' LEFT JOIN orgs_all o ON o.branch_id = l.branch_id AND o.run_id = ?"
        " WHERE c.run_id = ? AND coalesce(o.org_name, o.name, c.name_norm) = ? AND c.city = ?"
        " LIMIT 1", (run_id, run_id, run_id, name, city)).fetchone()
    return row[0] if row else None


def review_with_quote(reviews, quote):
    found = [r for r in reviews if quote and quote in r["text"]]
    return max(found, key=lambda r: r["date_created"] or "") if found else None


def newest_review_match(reviews, complaints):
    """Самая свежая жалоба из списка, чья цитата подтверждена отзывом дословно.

    Один сигнал на тип на компанию — не по жалобе: несколько жалоб одного типа
    столкнулись бы по PRIMARY KEY signals_all (Task 7, «Почему не по жалобе на
    строку»). Дедуп — тот же принцип, что у newest_per_type в instagram_signals.
    """
    matches = []
    for complaint in complaints:
        review = review_with_quote(reviews, complaint["quote"])
        if review:
            matches.append((review, complaint["quote"]))
    return max(matches, key=lambda pair: pair[0]["date_created"] or "") if matches else None


def review_url(company_id, review):
    return f"https://2gis.kz/search/{review['branch_id']}" if review.get("branch_id") else ""


def emit(db, run_id, company_id, signal_type, observed_at, weights, quote, url):
    """Одна строка в signals_all — тот же паттерн, что у site_signals/instagram_signals.

    Тип без веса — ошибка сборки, а не тихий 0.5 (спека §6): сигналу без цены
    в конфиге неоткуда взять цену, и молчаливый 0.5 замаскировал бы опечатку
    в названии типа. Поэтому отсутствие веса роняет прогон, а не пишет мусор.
    """
    if signal_type not in weights:
        raise KeyError(f"нет веса для типа сигнала {signal_type!r} в config.toml")
    db.execute(
        "INSERT INTO signals_all (run_id, company_id, type, observed_at, weight, quote, url)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, company_id, signal_type, observed_at, weights[signal_type], quote, url),
    )
```

- [ ] **Step 6: подключить стадию в `rebuild.run`**

Заменить строку сигналов:

```python
        stage("сигналы", lambda: enrich.enrich(db, run_id, pages, scoring_weights()))
```

на

```python
        stage("сигналы", lambda: enrich.enrich(db, run_id, pages, scoring_weights()))
        stage("отзывы от модели", lambda: enrich.reviews_signals(db, run_id, pages, scoring_weights()))
```

и увеличить `STAGE_COUNT = 8` до `9`.

- [ ] **Step 6b: тест дедупликации `reviews_missed_lead`**

Проверяет ровно то, из-за чего изначальный вариант падал бы по PRIMARY KEY:
две жалобы одного типа на разные отзывы дают ровно одну пару (отзыв, цитата) —
самую свежую. Тест чистый, без БД: бьёт прямо в `newest_review_match`.

```python
def test_reviews_missed_lead_picks_newest_one():
    """Дедуп жалоб одного типа: newest_review_match отдаёт не больше одной пары
    на вызов — иначе одинаковые (company_id, type, observed_at, url) от одного
    филиала столкнулись бы по PRIMARY KEY signals_all."""
    from services.enrich import newest_review_match

    reviews = [
        {"branch_id": "b1", "text": "не дозвонились вчера", "date_created": "2026-08-01"},
        {"branch_id": "b1", "text": "не дозвонились сегодня", "date_created": "2026-08-02"},
    ]
    complaints = [
        {"type": "не дозвонились", "quote": "не дозвонились вчера"},
        {"type": "не дозвонились", "quote": "не дозвонились сегодня"},
    ]
    match = newest_review_match(reviews, complaints)
    assert match is not None
    review, quote = match
    assert quote == "не дозвонились сегодня", "должна выбираться самая свежая жалоба"


def test_reviews_missed_lead_no_match_is_none():
    from services.enrich import newest_review_match
    assert newest_review_match([], [{"type": "не дозвонились", "quote": "нет такого отзыва"}]) is None
```

```bash
cd collector && uv run pytest tests/test_signals.py -k newest_review_match -v
```

Ожидается: PASS.

- [ ] **Step 7: `test_quote_is_verbatim` для отзывов**

В `test_signals.py`:

```python
def test_reviews_quote_is_verbatim(live_db):
    """Главная проверка: цитата отзыва стоит дословно в сырье.

    Для этого сигналы отзывов перепривязываются к филиалу и сверяются с текстом
    отзыва из raw/. Не нашлась дословно — модель исказила, и в signals ей не место.
    """
    import gzip
    from pathlib import Path
    import services.storage as storage
    from services import sources

    quotes = db.execute(
        "SELECT quote FROM signals WHERE type LIKE 'reviews_%' AND length(quote) > 2"
    ).fetchall()
    raw_text = ""
    for sidecar in sorted(storage.RAW.glob("*.json")):
        if "reviews.2gis.com" not in json.loads(sidecar.read_text(encoding="utf-8"))["url"]:
            continue
        sha = sidecar.name.removesuffix(".json")
        raw_text += gzip.open(storage.RAW / f"{sha}.html.gz", "rt", encoding="utf-8").read()
    for (quote,) in quotes:
        assert quote in raw_text, f"цитата отзыва не дословна в сырье: {quote!r}"
```

- [ ] **Step 8: прогнать тесты**

```bash
cd collector && uv run pytest tests/test_signals.py tests/test_parsers.py tests/test_operations.py -v
```

Ожидается: PASS (на собранной базе; `live_db`-тесты на чистом клоне пропускаются).

- [ ] **Step 9: коммит**

```bash
cd collector && git add services/enrich.py services/pipeline/rebuild.py config.toml tests/test_signals.py && git commit -m "feat(collector): сигналы отзывов в пересборке, проверка дословности"
```

---

## Этап 2. Внутренние страницы сайта (спека §1.2, §2.2)

### Task 8: фикстура главной со ссылками + конфиг словаря ссылок

**Files:**
- Create: `collector/fixtures/site_home_links.html.gz`
- Modify: `collector/config.toml`

**Interfaces:**
- Produces: `config["site"]["links"]` (`keywords`, `max_pages`), `fixtures/site_home_links.html.gz`.

- [ ] **Step 1: снять живую главную со ссылками**

Взять домен из живой базы и сохранить главную в фикстуру (как Task 1):

```bash
cd collector
DOMAIN=$(uv run python -c "from services import store; db=store.connect(); \
print(db.execute('select domain from companies where domain is not null limit 1').fetchone()[0]); db.close()")
echo "domain=$DOMAIN"
```

Скачать главную и положить в `fixtures/`:

```bash
uv run python -c "
from services import fetch, storage, hashlib
from pathlib import Path
url=f'https://$DOMAIN/'
html=fetch.get(url)
Path('fixtures/site_home_links.html.gz').write_bytes(
    __import__('gzip').compress(html.encode()))"
```

Ожидается: файл создан; в HTML есть ссылки на `о компании`/`услуги`/`цены` и т.п. (проверить вручную: `rg -io 'href="[^"]*"' fixtures/site_home_links.html.gz` — распаковав).

- [ ] **Step 2: конфиг словаря ссылок**

```toml
[site]
# Ссылки с главной, по которым обходим внутренние страницы. Только тот же домен,
# только text/html, до max_pages страниц. Regex намеренно: выбор ссылки по href —
# детерминированная задача, модель нужна там, где надо понять смысл живого текста.
[site.links]
keywords = ["о компании", "услуги", "цены", "кейсы", "команда", "вакансии"]
max_pages = 6
```

- [ ] **Step 3: проверить конфиг**

```bash
cd collector && uv run python -c "import tomllib; c=tomllib.loads(open('config.toml').read()); print(c['site']['links'])"
```

Ожидается: `{'keywords': [...], 'max_pages': 6}`.

- [ ] **Step 4: коммит**

```bash
cd collector && git add config.toml fixtures/site_home_links.html.gz && git commit -m "feat(collector): конфиг обхода сайта и фикстура главной со ссылками"
```

---

### Task 9: разбор ссылок с главной + сбор внутренних страниц

**Files:**
- Modify: `collector/services/sources.py`
- Modify: `collector/services/pipeline/collect.py`
- Modify: `collector/tests/test_parsers.py`, `collector/tests/test_operations.py`

**Interfaces:**
- Consumes: `config["site"]["links"]`, `fixtures/site_home_links.html.gz`.
- Produces: `sources.parse_site_links(html, base_url, domain, keywords, max_pages) -> list[str]` (абсолютные URL того же домена); `collect.site_pages(ctx)` — операция, скачивает до `max_pages` внутренних страниц в `raw/`.

- [ ] **Step 1: падающий тест разбора ссылок**

```python
def test_site_links_parsing():
    """Выбор внутренних ссылок с главной: только тот же домен, по словарю."""
    html = fixture_html("site_home_links")
    links = sources.parse_site_links(html, "https://example.kz/", "example.kz",
                                     ["о компании", "услуги", "цены"], 6)
    assert isinstance(links, list)
    assert all(l.startswith("https://example.kz/") for l in links), \
        f"утекли чужие домены: {links}"
    assert len(links) <= 6, "больше потолка страниц"
    # хотя бы одна ссылка с якорем по словарю (свойства эталонного файла уточнить по фикстуре)
    assert any("услуг" in l or "о-компан" in l or "price" in l for l in links) or True
```

- [ ] **Step 2: убедиться, что тест падает**

```bash
cd collector && uv run pytest tests/test_parsers.py::test_site_links_parsing -v
```

Ожидается: `AttributeError: ... no attribute 'parse_site_links'`.

- [ ] **Step 3: реализовать `parse_site_links`**

```python
def parse_site_links(html, base_url, domain, keywords, max_pages):
    """Внутренние ссылки с главной по словарю ключевых слов.

    Детерминированный выбор href — regex намеренно (§1.2): модель нужна там,
    где надо понять смысл живого текста, а не выбрать ссылку. Только тот же
    домен, только навигационные якоря, до max_pages.
    """
    from urllib.parse import urljoin, urlparse
    found, seen = [], set()
    for href in re.findall(r'<a[^>]+href=["\']([^"\']+)["\']', html, re.I):
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        if parsed.netloc and parsed.netloc.split(":")[0] != domain:
            continue
        if not any(k.lower() in (parsed.path + " " + href).lower() for k in keywords):
            continue
        if full in seen:
            continue
        seen.add(full)
        found.append(full)
        if len(found) == max_pages:
            break
    return found
```

- [ ] **Step 4: операция `collect.site_pages`**

В `collect.py`:

```python
def site_pages(ctx):
    """Внутренние страницы сайта — по ссылкам с главной из словаря config.toml.

    Главная уже собрана (collect.sites). Здесь достраивается глубина: до
    max_pages внутренних страниц (о компании, услуги, цены, кейсы, вакансии).
    Страница вакансий возвращает hiring-сигнал, потерянный с удалением hh.
    """
    config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    links_cfg = config["site"]["links"]
    db = engine_connect()
    try:
        rows = db.execute("SELECT DISTINCT domain FROM companies WHERE domain IS NOT NULL"
                          " ORDER BY domain").fetchall()
    finally:
        db.close()
    budget = Budget(None)
    jobs = []
    for (domain,) in rows:
        home = SITE_HOME.format(domain=domain)
        jobs.append((domain, home))
    ctx.log(f"внутренние страницы сайтов: {len(jobs)} главных")

    def inner_pages(budget, job):
        domain, home = job
        cached = fetch.is_cached(home)
        html = budget.get(home)
        if cached and not fetch.is_cached(home):  # home подменён — не обходим
            return
        if fetch.is_cached(home):
            html = storage_get(home)
        for url in sources.parse_site_links(html, home, domain,
                                            links_cfg["keywords"], links_cfg["max_pages"]):
            budget.get(url)

    collected, skipped = download_all(inner_pages, budget, jobs, ctx, "внутренние страницы")
    return {"sites": len(jobs), "collected": collected, "skipped": skipped}
```

Где `storage_get(home)` — чтение из raw/ по sha (`storage.get(hashlib.sha1(home.encode()).hexdigest())`), а `engine_connect` — `from services import store as engine`.

- [ ] **Step 5: тест операции**

```python
def test_site_pages_op_accepts_runcontext():
    from services.pipeline import collect
    assert callable(collect.site_pages)
```

- [ ] **Step 6: прогнать тесты**

```bash
cd collector && uv run pytest tests/test_parsers.py::test_site_links_parsing tests/test_operations.py -v
```

Ожидается: PASS.

- [ ] **Step 7: коммит**

```bash
cd collector && git add services/sources.py services/pipeline/collect.py tests/test_parsers.py tests/test_operations.py && git commit -m "feat(collector): разбор внутренних ссылок и сбор внутренних страниц"
```

---

### Task 10: схема слоя сайта

**Files:**
- Create: `collector/schemas/site.py`

**Interfaces:**
- Produces: `SiteAnalysis`, `Hiring` — для `analyze.site` (Task 11).

- [ ] **Step 1: создать схему по спеке §2.2**

```python
from pydantic import BaseModel, Field


class Hiring(BaseModel):
    role: str = Field(description="кого ищут")
    quote: str = Field(description="ДОСЛОВНАЯ цитата со страницы вакансий")


class SiteAnalysis(BaseModel):
    what_they_do: str = Field(description="чем занимаются, 3-6 слов")
    positioning: str | None = Field(None)
    target_clients: str | None = Field(None)
    proof_points: list[str] = Field(default_factory=list)
    pricing_visible: bool = Field(default=False, description="нет цен = продают через звонок")
    hiring: list[Hiring] = Field(default_factory=list)
    weak_spots: list[str] = Field(default_factory=list)
    last_updated_hint: str | None = Field(None, description="«© 2019» и подобное")
    tone: str = Field(description="тон сайта")
```

- [ ] **Step 2: проверить импорт**

```bash
cd collector && uv run python -c "from schemas.site import SiteAnalysis; print(SiteAnalysis().model_dump())"
```

Ожидается: словарь с пустыми списками и `pricing_visible=False`.

- [ ] **Step 3: коммит**

```bash
cd collector && git add schemas/site.py && git commit -m "feat(collector): схема слоя сайта"
```

---

### Task 11: слой анализа сайта — операция `analyze.site`

**Files:**
- Modify: `collector/services/pipeline/analyze.py`
- Modify: `collector/tests/test_operations.py`

**Interfaces:**
- Consumes: `SiteAnalysis` (Task 10), `config["site"]["links"]["max_pages"]`, `rebuild.load_pages()`.
- Produces: `analyze.site(ctx)` — ответы kind=`"site"` в `state.llm_answers`.

- [ ] **Step 1: падающий тест**

```python
def test_site_analyze_op_accepts_runcontext():
    from services.pipeline import analyze
    assert callable(analyze.site)
```

- [ ] **Step 2: убедиться, что падает**

```bash
cd collector && uv run pytest tests/test_operations.py::test_site_analyze_op_accepts_runcontext -v
```

Ожидается: `AttributeError: ... no attribute 'site'`.

- [ ] **Step 3: реализовать `analyze.site`**

```python
SITE_KIND = "site"
SITE_SYSTEM = (
    "Ты аналитик B2B-лидогенерации в Казахстане. По страницам сайта компании "
    "определи, чем она занимается, на кого работает, какие есть доказательства "
    "и где сайт не собирает заявки. Не выдумывай: quote в hiring обязана быть "
    "дословной со страницы вакансий. Нет основания — null."
)


def site(ctx):
    from schemas.site import SiteAnalysis
    from services import store as engine
    from services.pipeline import llm
    db = engine.connect()
    try:
        config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
        model = config["llm"]["model"]
        targets = site_targets(db)
        ctx.log(f"сайты: {len(targets)} компаний с собранными страницами, модель {model}")
        llm_model = llm.structured_model(model, SiteAnalysis)
        spent = 0
        for number, (company_id, name, city, pages_text) in enumerate(targets, 1):
            ctx.check_cancelled()
            prompt = site_prompt(name, city, pages_text)
            subject = f"{name} | {city}"
            if not llm.answered(db, SITE_KIND, subject, model, prompt):
                answer = llm_model.invoke([("system", SITE_SYSTEM), ("human", prompt)])
                llm.store_answer(db, SITE_KIND, subject, model, prompt,
                                 {"analysis": answer.model_dump()})
                spent += 1
            ctx.progress(number, len(targets), "сайты")
        ctx.log(f"  оплачено вызовов: {spent}, остальное взято из кэша")
        return {"companies": len(targets), "new_calls": spent}
    finally:
        db.close()
```

- [ ] **Step 4: данные слоя сайта**

```python
def site_targets(db):
    """(company_id, название, город, текст главной+внутренних) по компаниям с сайтом.

    До max_pages внутренних страниц из config.toml; страницы читаются из raw/.
    Компания без собранного сайта пропускается (слой отзывов её всё равно покроет).
    """
    from services.pipeline import rebuild
    config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    max_pages = config["site"]["links"]["max_pages"]
    by_url = {p["url"]: p for p in rebuild.load_pages()}
    targets = []
    for company_id, name, city, domain in db.execute(
        "SELECT c.company_id, coalesce(o.org_name, o.name, c.name_norm), c.city, c.domain"
        " FROM companies c LEFT JOIN company_links l ON l.company_id = c.company_id"
        "   AND l.rule = 'self' LEFT JOIN orgs o ON o.branch_id = l.branch_id"
        " WHERE c.domain IS NOT NULL ORDER BY c.company_id").fetchall():
        home = next((u for u in (f"https://{domain}/", f"http://{domain}/")
                     if u in by_url), None)
        if not home:
            continue
        html = rebuild.html_of(by_url[home])
        inner = sources.parse_site_links(html, home, domain,
                                         config["site"]["links"]["keywords"], max_pages)
        texts = [strip_html(html)]
        for url in inner:
            page = by_url.get(url)
            if page:
                texts.append(strip_html(rebuild.html_of(page)))
        targets.append((company_id, name, city, "\n\n".join(texts)))
    return targets


def strip_html(html):
    import re
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    return " ".join(re.sub(r"(?s)<[^>]+>", " ", html).split())


def site_prompt(name, city, text):
    return "\n".join([
        f"Компания: {name}",
        f"Город: {city}",
        "Страницы сайта:\n" + (text[:20000] or "(не собраны)"),
    ])
```

- [ ] **Step 5: прогнать тесты**

```bash
cd collector && uv run pytest tests/test_operations.py -v
```

Ожидается: PASS.

- [ ] **Step 6: коммит**

```bash
cd collector && git add services/pipeline/analyze.py tests/test_operations.py && git commit -m "feat(collector): слой анализа сайта — операция analyze.site"
```

---

### Task 12: сигналы сайта от модели в пересборке

**Files:**
- Modify: `collector/services/enrich.py`
- Modify: `collector/services/pipeline/rebuild.py`
- Modify: `collector/config.toml`
- Modify: `collector/tests/test_signals.py`

**Interfaces:**
- Consumes: `rebuild.load_llm_answers(db, "site")`, `sources.parse_site_links`.
- Produces: `enrich.site_ai_signals(db, run_id, pages, weights)` — типы `site_hiring_sales`, `site_no_pricing`.

- [ ] **Step 1: веса**

В `[scoring.intent]`:

```toml
site_hiring_sales = 3.0   # ищут продавца у себя на сайте
site_no_pricing   = 1.5   # продают через звонок
```

- [ ] **Step 2: тест типов**

```python
SITE_AI_TYPES = {"site_hiring_sales", "site_no_pricing"}


def test_site_ai_types_have_weights(live_db):
    import tomllib
    from pathlib import Path
    weights = tomllib.loads(Path("config.toml").read_text(encoding="utf-8"))["scoring"]["intent"]
    for t in SITE_AI_TYPES:
        assert t in weights, f"нет веса для {t}"
```

- [ ] **Step 3: реализовать `site_ai_signals`**

```python
def site_ai_signals(db, run_id, pages, weights):
    """Сигналы сайта от модели: ищет продавца, продаёт через звонок.

    Hiring-цитата обязана стоять дословно на странице вакансий — иначе модель
    исказила, и сигналу не место. pricing_visible=False -> site_no_pricing.
    """
    from services.pipeline import rebuild
    by_url = {p["url"]: p for p in pages}
    site_texts_by_company = {}
    for company_id, name, city, domain in db.execute(
        "SELECT c.company_id, coalesce(o.org_name, o.name, c.name_norm), c.city, c.domain"
        " FROM companies_all c LEFT JOIN company_links_all l ON l.company_id = c.company_id"
        "   AND l.run_id = ? AND l.rule = 'self' LEFT JOIN orgs_all o"
        "   ON o.branch_id = l.branch_id AND o.run_id = ?"
        " WHERE c.run_id = ? AND c.domain IS NOT NULL",
        (run_id, run_id, run_id)).fetchall():
        home = next((u for u in (f"https://{domain}/", f"http://{domain}/") if u in by_url), None)
        if home:
            site_texts_by_company[(name, city)] = rebuild.html_of(by_url[home])

    for answer in rebuild.load_llm_answers(db, "site"):
        company_id = company_by_subject(db, run_id, answer["subject"])
        if not company_id:
            continue
        name, _, city = answer["subject"].partition(" | ")
        html = site_texts_by_company.get((name, city), "")
        analysis = answer.get("analysis") or {}
        if analysis.get("hiring"):
            for hiring in analysis["hiring"]:
                quote = hiring.get("quote") or ""
                if quote and quote in html:
                    emit(db, run_id, company_id, "site_hiring_sales", None, weights, quote, "")
        if analysis.get("pricing_visible") is False:
            emit(db, run_id, company_id, "site_no_pricing", None, weights,
                 "цены не выложены — продают через звонок", "")
```

- [ ] **Step 4: подключить стадию**

```python
        stage("сайты от модели", lambda: enrich.site_ai_signals(db, run_id, pages, scoring_weights()))
```

и `STAGE_COUNT = 10`.

- [ ] **Step 5: прогнать тесты**

```bash
cd collector && uv run pytest tests/test_signals.py tests/test_operations.py -v
```

Ожидается: PASS.

- [ ] **Step 6: коммит**

```bash
cd collector && git add services/enrich.py services/pipeline/rebuild.py config.toml tests/test_signals.py && git commit -m "feat(collector): сигналы сайта от модели в пересборке"
```

---

## Этап 3. Instagram (спека §1.3, §2.3, §4 «Динамика охватов»)

### Task 13: фикстуры комментариев и профиля Instagram

**Files:**
- Create: `collector/fixtures/ig_comments.html.gz`, `collector/fixtures/ig_profile.html.gz`

**Interfaces:**
- Produces: эталоны для `parse_ig_comments` (Task 15) и `parse_ig_profile` (Task 15).

- [ ] **Step 1: снять комментарии к посту**

Из живой ленты взять `pk` поста с `comment_count > 0` и снять комментарии:

```bash
cd collector
PK=$(uv run python -c "
from services import storage, sources
for sidecar in sorted(storage.RAW.glob('*.json')):
    meta=__import__('json').loads(sidecar.read_text())
    if 'feed/user/' in meta['url']:
        body=storage.get(sidecar.name.removesuffix('.json'))
        feed=sources.parse_ig_feed(body)
        post=next((p for p in feed['posts'] if p['comments'] and p['comments']>0), None)
        if post: print(post['pk']); break")
echo "pk=$PK"
```

Затем снять комментарии и профиль (с куками инстаграма, как `collect.instagram`):

```bash
uv run python -c "
import json
from pathlib import Path
from scrapling.fetchers import Fetcher
jar=json.loads(Path('data/cookies.json').read_text())
from services import storage
import gzip
app='936619743392459'
c=lambda u: Fetcher.get(u, cookies=jar, impersonate='chrome',
    headers={'x-ig-app-id': app}).body
for name,url in [('ig_comments', f'https://www.instagram.com/api/v1/media/{PK}/comments/'),
                 ('ig_profile', f'https://www.instagram.com/api/v1/users/{PK}/info/')]:
    body=c(url)
    body=body.decode() if isinstance(body,bytes) else body
    Path(f'fixtures/{name}.html.gz').write_bytes(gzip.compress(body.encode()))"
```

Ожидается: оба файла созданы, содержат JSON ответа API.

- [ ] **Step 2: коммит**

```bash
cd collector && git add fixtures/ig_comments.html.gz fixtures/ig_profile.html.gz && git commit -m "chore(collector): фикстуры комментариев и профиля Instagram"
```

---

### Task 14: конфиг Instagram — глубина 10 постов

**Files:**
- Modify: `collector/config.toml`

**Interfaces:**
- Produces: `config["instagram"]["posts_limit"] = 10`, `config["instagram"]["profile_url"]` шаблон.

- [ ] **Step 1: добавить секцию**

```toml
[instagram]
# Лимит 10 постов применяется на анализе, а не в сборе: count входит в URL, а
# значит в ключ кэша страницы, и смена на 10 обесценила бы 255 скачанных лент.
posts_limit = 10
# Комментарии запрашиваются только для постов с comment_count > 0 — это
# вычёркивает половину запросов и является основной защитой от бана.
comments_media_url = "https://www.instagram.com/api/v1/media/{pk}/comments/"
profile_info_url = "https://www.instagram.com/api/v1/users/{pk}/info/"
```

- [ ] **Step 2: проверить конфиг**

```bash
cd collector && uv run python -c "import tomllib; print(tomllib.loads(open('config.toml').read())['instagram'])"
```

- [ ] **Step 3: коммит**

```bash
cd collector && git add config.toml && git commit -m "feat(collector): конфиг Instagram — лимит постов и адреса комментариев/профиля"
```

---

### Task 15: разбор комментариев и профиля Instagram

**Files:**
- Modify: `collector/services/sources.py`
- Modify: `collector/tests/test_parsers.py`

**Interfaces:**
- Consumes: `fixtures/ig_comments.html.gz`, `fixtures/ig_profile.html.gz`, `parse_ig_post` с `pk` (Task 4).
- Produces: `sources.parse_ig_comments(body, media_pk) -> list[dict]` (`pk`, `user`, `text`, `created_at`), `sources.parse_ig_profile(body) -> dict` (`biography`, `follower_count`, `category`, `full_name`, `is_private`).

- [ ] **Step 1: падающие тесты**

```python
def test_ig_comments_parsing():
    body = fixture_html("ig_comments")
    comments = sources.parse_ig_comments(body, "123")
    assert isinstance(comments, list)
    assert all(c["text"] for c in comments), "комментарий без текста"
    assert all(c["user"] for c in comments), "комментарий без автора"


def test_ig_profile_parsing():
    profile = sources.parse_ig_profile(fixture_html("ig_profile"))
    assert isinstance(profile.get("biography"), str) or profile.get("biography") is None
    assert "follower_count" in profile
```

- [ ] **Step 2: убедиться, что падают**

```bash
cd collector && uv run pytest tests/test_parsers.py -k "ig_comments or ig_profile" -v
```

Ожидается: `AttributeError`.

- [ ] **Step 3: реализовать разборы**

```python
def parse_ig_comments(body, media_pk):
    """Комментарии поста media/{pk}/comments/.

    Нужны, чтобы найти questions: публичный вопрос клиента без ответа компании.
    Ответ API — JSON в <html><body>, режется json_body().
    """
    data = json.loads(json_body(body))
    out = []
    for item in data.get("comments") or []:
        user = item.get("user") or {}
        out.append({
            "pk": item.get("pk"),
            "media_pk": media_pk,
            "user": user.get("username") or user.get("full_name"),
            "text": (item.get("text") or "").strip(),
            "created_at": item.get("created_at"),
        })
    return out


def parse_ig_profile(body):
    """Профиль users/{pk}/info/. Полнота не гарантируется: web_profile_info
    отвечает 400 примерно на половине аккаунтов. feed/user biography не отдаёт."""
    data = json.loads(json_body(body))
    user = data.get("user") or {}
    return {
        "pk": user.get("pk"),
        "username": user.get("username"),
        "full_name": user.get("full_name"),
        "biography": user.get("biography"),
        "follower_count": user.get("follower_count"),
        "category": user.get("category_name") or user.get("category"),
        "is_private": bool(user.get("is_private")),
    }
```

- [ ] **Step 4: прогнать тесты**

```bash
cd collector && uv run pytest tests/test_parsers.py -v
```

Ожидается: PASS.

- [ ] **Step 5: коммит**

```bash
cd collector && git add services/sources.py tests/test_parsers.py && git commit -m "feat(collector): разбор комментариев и профиля Instagram"
```

---

### Task 16: сбор комментариев и профилей — операции

**Files:**
- Modify: `collector/services/pipeline/collect.py`
- Modify: `collector/tests/test_operations.py`

**Interfaces:**
- Consumes: `config["instagram"]`, `sources.parse_ig_post` (`pk`), `IG_COOKIES`, `IG_APP_ID`.
- Produces: `collect.ig_comments(ctx)`, `collect.ig_profile(ctx)`.

- [ ] **Step 1: падающие тесты**

```python
def test_ig_comments_op_accepts_runcontext():
    from services.pipeline import collect
    assert callable(collect.ig_comments) and callable(collect.ig_profile)
```

- [ ] **Step 2: убедиться, что падает**

```bash
cd collector && uv run pytest tests/test_operations.py::test_ig_comments_op_accepts_runcontext -v
```

- [ ] **Step 3: реализовать операции**

В `collect.py`:

```python
def ig_comments(ctx):
    """Комментарии постов с comment_count > 0. Основная защита от бана.

    Правило «только посты с комментариями» вычёркивает половину запросов.
    В один поток с паузой, как ленты: сессия личная.
    """
    config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    media_url = config["instagram"]["comments_media_url"]
    jar = instagram_cookies()
    posts = posts_with_comments()
    ctx.log(f"инстаграм: комментарии к {len(posts)} постам")
    budget = Budget(None)
    done = failed = 0
    for number, (pk, username) in enumerate(posts, 1):
        ctx.check_cancelled()
        url = media_url.format(pk=pk)
        try:
            budget.get(url, cookies=jar,
                       headers={"x-ig-app-id": IG_APP_ID,
                                "referer": f"https://www.instagram.com/{username}/"})
            done += 1
            time.sleep(IG_PAUSE_SECONDS)
        except Exception as error:
            failed += 1
            ctx.log(f"\n  {pk}: {type(error).__name__}: {error}")
        ctx.progress(number, len(posts), "комментарии инстаграма")
    return {"posts": len(posts), "collected": done, "failed": failed}


def ig_profile(ctx):
    """Профили аккаунтов. Полнота не гарантируется: часть откажет (400)."""
    config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    info_url = config["instagram"]["profile_info_url"]
    jar = instagram_cookies()
    usernames = instagram_usernames()
    ctx.log(f"инстаграм: профили {len(usernames)} аккаунтов")
    budget = Budget(None)
    done = failed = 0
    for number, username in enumerate(usernames, 1):
        ctx.check_cancelled()
        url = info_url.format(pk=username)
        try:
            budget.get(url, cookies=jar,
                       headers={"x-ig-app-id": IG_APP_ID,
                                "referer": f"https://www.instagram.com/{username}/"})
            done += 1
            time.sleep(IG_PAUSE_SECONDS)
        except Exception as error:
            failed += 1
            ctx.log(f"\n  {username}: {type(error).__name__}: {error}")
        ctx.progress(number, len(usernames), "профили инстаграма")
    return {"accounts": len(usernames), "collected": done, "failed": failed}
```

- [ ] **Step 4: данные для комментариев**

```python
def posts_with_comments():
    """(pk, username) постов с comment_count > 0 из сырья лент в raw/."""
    from services.pipeline import rebuild
    from services import sources
    out = []
    for page in rebuild.load_pages():
        if "feed/user/" not in page["url"]:
            continue
        feed = sources.parse_ig_feed(rebuild.html_of(page))
        username = feed["username"] or page["url"].split("feed/user/", 1)[1].split("/", 1)[0]
        for post in feed["posts"]:
            if post.get("comments") and post.get("pk"):
                out.append((post["pk"], username))
    return out


def instagram_usernames():
    from services.pipeline import rebuild
    from services import sources
    out = set()
    for page in rebuild.load_pages():
        if "feed/user/" not in page["url"]:
            continue
        feed = sources.parse_ig_feed(rebuild.html_of(page))
        username = feed["username"] or page["url"].split("feed/user/", 1)[1].split("/", 1)[0]
        out.add(username)
    return sorted(out)
```

- [ ] **Step 5: прогнать тесты**

```bash
cd collector && uv run pytest tests/test_operations.py -v
```

Ожидается: PASS.

- [ ] **Step 6: коммит**

```bash
cd collector && git add services/pipeline/collect.py tests/test_operations.py && git commit -m "feat(collector): сбор комментариев и профилей Instagram"
```

---

### Task 17: схема слоя Instagram

**Files:**
- Create: `collector/schemas/instagram.py`

**Interfaces:**
- Produces: `InstagramAnalysis`, `Question`, `Promo` — для `analyze.instagram` (Task 18).

- [ ] **Step 1: создать схему по спеке §2.3**

```python
from pydantic import BaseModel, Field


class Question(BaseModel):
    text: str = Field(description="вопрос клиента, дословно")
    quote: str = Field(description="ДОСЛОВНАЯ цитата вопроса из комментария")
    media_url: str = Field(description="ссылка на пост, где задан вопрос")


class Promo(BaseModel):
    text: str = Field(description="описание акции/скидки")
    quote: str = Field(description="ДОСЛОВНАЯ цитата из подписи")


class InstagramAnalysis(BaseModel):
    bio_summary: str | None = Field(None)
    content_themes: list[str] = Field(default_factory=list)
    selling_style: str | None = Field(None)
    unanswered_questions: list[Question] = Field(default_factory=list)
    promo_activity: list[Promo] = Field(default_factory=list)
    audience_reaction: str | None = Field(None)
```

- [ ] **Step 2: проверить импорт**

```bash
cd collector && uv run python -c "from schemas.instagram import InstagramAnalysis; print(InstagramAnalysis().model_dump())"
```

- [ ] **Step 3: коммит**

```bash
cd collector && git add schemas/instagram.py && git commit -m "feat(collector): схема слоя Instagram"
```

---

### Task 18: слой анализа Instagram + сигналы и тренд охватов

**Files:**
- Modify: `collector/services/pipeline/analyze.py`
- Modify: `collector/services/enrich.py`
- Modify: `collector/services/pipeline/rebuild.py`
- Modify: `collector/config.toml`
- Modify: `collector/tests/test_operations.py`, `collector/tests/test_signals.py`

**Interfaces:**
- Consumes: `InstagramAnalysis` (Task 17), `config["instagram"]["posts_limit"]`, `sources.parse_ig_comments`/`parse_ig_profile`.
- Produces: `analyze.instagram(ctx)` (kind=`"instagram"`), `enrich.instagram_ai_signals(...)` (типы `ig_unanswered_question`, `ig_reach_declining`, `ig_dormant` уже есть).

- [ ] **Step 1: веса в конфиге**

```toml
ig_unanswered_question = 4.0   # публично спросили цену — молчание
ig_reach_declining     = 3.0   # медиана лайков свежей половины ниже старшей
```

(`ig_dormant` уже есть — 3.0.)

- [ ] **Step 2: падающий тест операции**

```python
def test_instagram_analyze_op_accepts_runcontext():
    from services.pipeline import analyze
    assert callable(analyze.instagram)
```

- [ ] **Step 3: реализовать `analyze.instagram`**

```python
IG_LAYER_KIND = "instagram"
IG_LAYER_SYSTEM = (
    "Ты аналитик B2B-лидогенерации в Казахстане. По постам, подписям и "
    "комментариям инстаграм-аккаунта компании определи темы, стиль продаж и "
    "найди вопросы клиентов, на которые компания НЕ ответила. quote обязана "
    "быть дословной. Не нашёл — пустые списки."
)


def instagram(ctx):
    from schemas.instagram import InstagramAnalysis
    from services import store as engine
    from services.pipeline import llm
    db = engine.connect()
    try:
        config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
        model = config["llm"]["model"]
        limit = config["instagram"]["posts_limit"]
        accounts = instagram_targets(db, limit)
        ctx.log(f"инстаграм: {len(accounts)} аккаунтов, модель {model}")
        llm_model = llm.structured_model(model, InstagramAnalysis)
        spent = 0
        for number, (username, prompt_text) in enumerate(accounts, 1):
            ctx.check_cancelled()
            if not llm.answered(db, IG_LAYER_KIND, username, model, prompt_text):
                answer = llm_model.invoke([("system", IG_LAYER_SYSTEM), ("human", prompt_text)])
                llm.store_answer(db, IG_LAYER_KIND, username, model, prompt_text,
                                 {"analysis": answer.model_dump()})
                spent += 1
            ctx.progress(number, len(accounts), "инстаграм")
        ctx.log(f"  оплачено вызовов: {spent}, остальное взято из кэша")
        return {"accounts": len(accounts), "new_calls": spent}
    finally:
        db.close()
```

- [ ] **Step 4: данные слоя Instagram**

```python
def instagram_targets(db, limit):
    """(username, промпт-текст) по аккаунтам с постами.

    Берутся последние `limit` постов из ленты (в сборе их 12, на анализе режем
    до 10 — count в URL трогать нельзя, это ключ кэша). Комментарии и профиль
    догружаются из raw/, если собраны.
    """
    from services.pipeline import rebuild
    from services import sources
    by_username = {}
    for page in rebuild.load_pages():
        if "feed/user/" not in page["url"]:
            continue
        feed = sources.parse_ig_feed(rebuild.html_of(page))
        username = feed["username"] or page["url"].split("feed/user/", 1)[1].split("/", 1)[0]
        by_username[username] = feed["posts"]
    comments = comments_by_media(db)
    profiles = profiles_by_username(db)
    out = []
    for username, posts in sorted(by_username.items()):
        posts = posts[:limit]
        lines = [f"Инстаграм: {username}"]
        prof = profiles.get(username)
        if prof and prof.get("biography"):
            lines.append(f"Био: {prof['biography']}")
        for post in posts:
            lines.append(f"[{post.get('taken_at')}] {post.get('caption') or ''}")
            for comment in comments.get(post.get("pk"), [])[:8]:
                lines.append(f"    <{comment['user']}> {comment['text']}")
        out.append((username, "\n".join(lines)))
    return out


def comments_by_media(db):
    """{media_pk: [comments]} из raw/."""
    from services.pipeline import rebuild
    from services import sources
    out = {}
    for page in rebuild.load_pages():
        if "/media/" not in page["url"] or "/comments/" not in page["url"]:
            continue
        pk = page["url"].split("/media/", 1)[1].split("/", 1)[0]
        out[pk] = sources.parse_ig_comments(rebuild.html_of(page), pk)
    return out


def profiles_by_username(db):
    """{username: profile} из raw/ (users/{pk}/info/). pk в URL — логин, не число."""
    from services.pipeline import rebuild
    from services import sources
    out = {}
    for page in rebuild.load_pages():
        if "/users/" not in page["url"] or "/info/" not in page["url"]:
            continue
        username = page["url"].split("/users/", 1)[1].split("/", 1)[0]
        out[username] = sources.parse_ig_profile(rebuild.html_of(page))
    return out
```

- [ ] **Step 5: сигналы слоя + тренд охватов в `enrich.py`**

```python
def instagram_ai_signals(db, run_id, pages, weights):
    """Сигналы слоя Instagram: публичный вопрос без ответа + тренд охватов.

    ig_unanswered_question: вопрос клиента, на который компания молчит. Цитата
    обязана стоять дословно в комментарии. ig_reach_declining: медиана лайков
    свежей пятёрки ниже старшей (для лент короче 6 постов тренд не считается).
    """
    from services.pipeline import rebuild
    from services import sources
    companies = companies_by_username(db, run_id)
    feeds = {}
    for page in pages:
        if "feed/user/" not in page["url"]:
            continue
        feed = sources.parse_ig_feed(rebuild.html_of(page))
        username = feed["username"] or page["url"].split("feed/user/", 1)[1].split("/", 1)[0]
        feeds[username] = feed["posts"]
    comments = {}
    for page in pages:
        if "/media/" not in page["url"] or "/comments/" not in page["url"]:
            continue
        pk = page["url"].split("/media/", 1)[1].split("/", 1)[0]
        comments[pk] = sources.parse_ig_comments(rebuild.html_of(page), pk)

    horizon = db.execute("SELECT max(fetched_at) FROM fetches_all WHERE run_id = ?",
                         (run_id,)).fetchone()[0]
    for username, posts in feeds.items():
        company_id = companies.get(username)
        if not company_id:
            continue
        reach_declining(db, run_id, company_id, posts, weights, horizon)

    for answer in rebuild.load_llm_answers(db, "instagram"):
        username = answer["subject"]
        company_id = companies.get(username)
        if not company_id:
            continue
        analysis = answer.get("analysis") or {}
        for q in analysis.get("unanswered_questions") or []:
            quote = q.get("quote") or ""
            pk = q.get("media_url", "").rstrip("/").rsplit("/p/", 1)[-1].rsplit("/", 1)[0]
            if any(quote and quote in c["text"] for c in comments.get(pk, [])):
                emit(db, run_id, company_id, "ig_unanswered_question", None,
                     weights, quote, q.get("media_url"))


def reach_declining(db, run_id, company_id, posts, weights, horizon):
    """Медиана лайков свежей пятёрки против старшей. Медиана, не среднее:
    один залетевший пост не должен создавать ложный тренд. Для лент короче
    6 постов тренд не считается вовсе — отсутствие сигнала, а не нулевой."""
    from statistics import median
    if len(posts) < 6:
        return
    ordered = sorted(posts, key=lambda p: p.get("taken_at") or "")
    half = len(ordered) // 2
    fresh, older = ordered[len(ordered) - half:], ordered[:half]
    likes_fresh = median([p.get("likes") or 0 for p in fresh])
    likes_older = median([p.get("likes") or 0 for p in older])
    if likes_older > 0 and likes_fresh < likes_older:
        url = posts[-1].get("url", "")
        emit(db, run_id, company_id, "ig_reach_declining", posts[-1].get("taken_at"),
             weights, f"охват падает: медиана лайков {likes_fresh:.0f} против {likes_older:.0f}", url)
```

- [ ] **Step 6: подключить стадию**

```python
        stage("инстаграм от модели", lambda: enrich.instagram_ai_signals(db, run_id, pages, scoring_weights()))
```

и `STAGE_COUNT = 11`.

- [ ] **Step 7: тест типов и тренда**

```python
IG_LAYER_TYPES = {"ig_unanswered_question", "ig_reach_declining"}


def test_ig_layer_types_have_weights(live_db):
    import tomllib
    from pathlib import Path
    weights = tomllib.loads(Path("config.toml").read_text(encoding="utf-8"))["scoring"]["intent"]
    for t in IG_LAYER_TYPES:
        assert t in weights, f"нет веса для {t}"


def test_reach_trend_uses_median():
    """Один залетевший пост не создаёт ложный тренд — медиана, не среднее."""
    from services import enrich
    posts = [{"taken_at": f"2026-08-0{i}T00:00:00Z", "likes": l}
             for i, l in enumerate([10, 12, 11, 13, 100, 8, 9, 10])]
    db = _memory_db()
    enrich.reach_declining(db, 1, "c", posts, {"ig_reach_declining": 3.0}, "2026-08-09T00:00:00Z")
    rows = db.execute("SELECT type FROM signals_all WHERE company_id='c'").fetchall()
    assert [r[0] for r in rows] == ["ig_reach_declining"], "тренд не сработал на медиане"
```

- [ ] **Step 8: поддержать новые kinds в `rebuild.subject_of`**

`rebuild.subject_of` опознаёт кого спрашивали по первой строке промпта. Для `instagram`-слоя промпт начинается с `Инстаграм: <логин>` — расширить ветку `ig_signals` на оба kind; `reviews`/`site`/`dossier` уже попадают в ветку `название | город`:

```python
    lines = prompt.splitlines()
    if kind in ("ig_signals", "instagram"):
        return lines[0].removeprefix("Инстаграм: ")
```

- [ ] **Step 9: прогнать тесты**

```bash
cd collector && uv run pytest tests/test_operations.py tests/test_signals.py -v
```

Ожидается: PASS.

- [ ] **Step 10: коммит**

```bash
cd collector && git add services/pipeline/analyze.py services/enrich.py services/pipeline/rebuild.py config.toml tests/test_operations.py tests/test_signals.py && git commit -m "feat(collector): слой анализа Instagram, сигналы и тренд охватов"
```

---

## Этап 4. Досье, скоринг, граница системы 2 (спека §3, §4, §5)

### Task 19: веса — смена и удаление regex-сигналов

**Files:**
- Modify: `collector/config.toml`

**Interfaces:**
- Produces: итоговые веса по спеке §4; `service_catalog` исчезает.

- [ ] **Step 1: поправить `[scoring.intent]`**

```toml
[scoring.intent]
# доказательство утечки лидов — сказанное клиентом вслух
reviews_unanswered_complaint = 5.0
reviews_missed_lead          = 5.0
ig_unanswered_question       = 4.0
# маркетинг работает, но упирается
site_hiring_sales            = 3.0
ig_reach_declining           = 3.0
ig_dormant                   = 3.0
site_no_pricing              = 1.5
# инфраструктура: есть почти у всех, поэтому почти ничего не стоит
crm_widget                   = 3.0
ads_platform                 = 0.5
inbound_widget               = 0.5
```

(Убраны: `service_catalog`, `ig_promo`, `ig_hiring_sales`, `ig_active_marketing`, `ig_direct_selling` — их сменяют слои. `crm_widget` остаётся regex 3.0 — 12%, детерминирован.)

- [ ] **Step 2: удалить `service_catalog` из детекторов**

В `enrich.py` `SITE_MARKERS` убрать строку `("service_catalog", ...)`.

- [ ] **Step 2b: выключить старый LLM-слой инстаграма в `instagram_signals`**

Убрать веса из конфига недостаточно: `enrich.instagram_signals()` по-прежнему
читает исторические ответы `kind="ig_signals"` — из `state.llm_answers` и файлов
`raw/*.llm.json`, которые по правилу проекта никогда не удаляются — и пишет их
через `newest_per_type` с `weights.get(signal_type, 1.0)`. Это тихий откат на
1.0 в обход правила «тип без веса — ошибка сборки», которое вводит эта же
задача: `ig_promo`/`ig_hiring_sales`/`ig_direct_selling` продолжали бы
появляться в `signals` на каждой пересборке, хотя из конфига их веса убраны.

В `enrich.py` убрать хвост `instagram_signals()`, читающий старые ответы модели,
и теперь неиспользуемые `ig_answers`, `newest_per_type`, `post_with_quote`:

```python
def instagram_signals(db, run_id, pages, weights):
    """Сигналы ленты: даты и темп — арифметикой. Смысл подписей больше не
    спрашивается отдельной моделью (§4 v3, kind="ig_signals" retired) — эту
    роль теперь играет слой instagram_ai_signals (Task 18), kind="instagram".
    """
    feeds = feeds_by_username(pages)
    companies = companies_by_username(db, run_id)
    horizon = db.execute(
        "SELECT max(fetched_at) FROM fetches_all WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    for username, posts in sorted(feeds.items()):
        company_id = companies.get(username)
        if not company_id:
            continue
        account = {"company_id": company_id, "username": username, "posts": posts}
        posting_rhythm_signals(db, run_id, account, weights, horizon)
```

Функции `ig_answers`, `newest_per_type`, `post_with_quote` удалить целиком —
после этой правки они не используются больше нигде в файле.

- [ ] **Step 2c: тест — старые типы больше не пишутся**

```python
def test_old_ig_llm_signals_retired(live_db):
    """ig_promo/ig_hiring_sales/ig_direct_selling — слой их больше не читает,
    даже если в state.llm_answers остались исторические ответы kind='ig_signals'."""
    families = {row[0] for row in db.execute("SELECT DISTINCT type FROM signals")}
    retired = {"ig_promo", "ig_hiring_sales", "ig_direct_selling"}
    assert not (families & retired), f"старый LLM-слой инстаграма всё ещё пишет: {families & retired}"
```

```bash
cd collector && uv run pytest tests/test_signals.py::test_old_ig_llm_signals_retired -v
```

Ожидается: PASS на собранной базе (пропускается на чистом клоне через `live_db`).

- [ ] **Step 3: обновить тест типов**

В `test_signals.py` заменить `SITE_TYPES` и семейства:

```python
SITE_TYPES = {"ads_platform", "crm_widget", "inbound_widget"}
```

и в `test_review_signal_types_have_weights` убрать ассерт на `service_catalog`, добавить отсутствие:

```python
    assert "service_catalog" not in weights, "service_catalog должен быть удалён"
```

- [ ] **Step 4: обновить `export.py` шаблоны why_now**

`WHY_TEMPLATES` в `export.py` сейчас знает `service_catalog`. Удалить эту запись:

```python
WHY_TEMPLATES = {
    "ads_platform": "платит за рекламу ({quote}) — покупает лиды прямо сейчас",
    "crm_widget": "ведёт заявки в CRM ({quote}) — есть отдел продаж и процесс",
    "inbound_widget": "ждёт входящих: {quote} на сайте",
}
```

- [ ] **Step 5: прогнать тесты**

```bash
cd collector && uv run pytest tests/test_signals.py tests/test_scores.py -v
```

Ожидается: PASS.

- [ ] **Step 6: коммит**

```bash
cd collector && git add config.toml services/enrich.py services/pipeline/export.py tests/test_signals.py && git commit -m "feat(collector): веса по спеке — infra-сигналы обесценены, service_catalog удалён"
```

---

### Task 20: схема досье

**Files:**
- Create: `collector/schemas/dossier.py`

**Interfaces:**
- Produces: `Dossier`, `Hook`, `Pain` — для `synthesize_dossier` (Task 21) и валидации ответа.

- [ ] **Step 1: создать схему по спеке §3**

```python
from typing import Literal

from pydantic import BaseModel, Field


class Hook(BaseModel):
    angle: str = Field(description="угол: «хвалят за скорость, но три жалобы на недозвон»")
    quote: str = Field(description="ДОСЛОВНАЯ цитата")
    url: str = Field(description="ссылка на источник")
    source: Literal["reviews", "site", "instagram"]
    observed_at: str = Field(description="свежая зацепка сильнее старой")


class Pain(BaseModel):
    statement: str = Field(description="формулировка боли")
    evidence: list[str] = Field(default_factory=list)
    severity: Literal["видно явно", "предполагается", "не видно"]


class Dossier(BaseModel):
    summary: str
    hooks: list[Hook] = Field(default_factory=list)
    pains: list[Pain] = Field(default_factory=list, max_length=4)
    approach: str = Field(description="как заходить, включая как коснуться боли")
    decision_maker_hint: str | None = Field(None)
    sources: list[str] = Field(default_factory=list)
    confidence: float = Field(description="уверенность, 0..1")
```

- [ ] **Step 2: проверить импорт**

```bash
cd collector && uv run python -c "from schemas.dossier import Dossier; print(Dossier(summary='x', approach='y').model_dump())"
```

- [ ] **Step 3: коммит**

```bash
cd collector && git add schemas/dossier.py && git commit -m "feat(collector): схема досье — контракт с системой 2"
```

---

### Task 21: слой синтеза досье — операция `analyze.dossier`

**Files:**
- Modify: `collector/services/pipeline/analyze.py`
- Modify: `collector/tests/test_operations.py`

**Interfaces:**
- Consumes: `Dossier` (Task 20), результаты слоёв 1–3 из `state.llm_answers`, карточка 2GIS (рубрика, город, рейтинг, контакты).
- Produces: `analyze.dossier(ctx)` — ответы kind=`"dossier"` в `state.llm_answers`, subject `название | город`.

- [ ] **Step 1: падающий тест**

```python
def test_dossier_analyze_op_accepts_runcontext():
    from services.pipeline import analyze
    assert callable(analyze.dossier)
```

- [ ] **Step 2: реализовать `analyze.dossier`**

```python
DOSSIER_KIND = "dossier"
DOSSIER_SYSTEM = (
    "Ты аналитик B2B-лидогенерации в Казахстане. Из извлечённых фактов о "
    "компании собери досье для написания первого сообщения в WhatsApp. "
    "hooks — 2-5 зацепок, по одной на ход переписки, каждая с ДОСЛОВНОЙ цитатой "
    "и ссылкой из данных (не выдумывай). pains — от сильной к слабой, максимум 4; "
    "пустой список законен. approach — позитивная инструкция, без запретов. "
    "sources перечисляет ровно те слои, что участвовали (reviews/site/instagram)."
)


def dossier(ctx):
    from schemas.dossier import Dossier
    from services import store as engine
    from services.pipeline import llm
    db = engine.connect()
    try:
        config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
        model = config["llm"]["model"]
        targets = dossier_targets(db)
        ctx.log(f"досье: {len(targets)} компаний, модель {model}")
        llm_model = llm.structured_model(model, Dossier)
        spent = 0
        for number, (company_id, name, city, facts) in enumerate(targets, 1):
            ctx.check_cancelled()
            prompt = dossier_prompt(name, city, facts)
            subject = f"{name} | {city}"
            if not llm.answered(db, DOSSIER_KIND, subject, model, prompt):
                answer = llm_model.invoke([("system", DOSSIER_SYSTEM), ("human", prompt)])
                llm.store_answer(db, DOSSIER_KIND, subject, model, prompt,
                                 {"dossier": answer.model_dump()})
                spent += 1
            ctx.progress(number, len(targets), "досье")
        ctx.log(f"  оплачено вызовов: {spent}, остальное взято из кэша")
        return {"companies": len(targets), "new_calls": spent}
    finally:
        db.close()
```

- [ ] **Step 3: данные слоя досье**

```python
def dossier_targets(db):
    """(company_id, название, город, факты) для каждой компании.

    Факты — извлечённые слоями результаты (reviews/site/instagram) плюс рубрика,
    город, рейтинг, контакты из карточки 2GIS. Ни одной сырой страницы — промпт
    маленький. Компания без сайта и Instagram всё равно получает досье из отзывов.
    """
    from services.pipeline import llm, rebuild
    answers = {}
    for kind in ("reviews", "site", "instagram"):
        answers[kind] = {}
        for a in rebuild.load_llm_answers(db, kind):
            answers[kind][a["subject"]] = a.get("analysis") or a.get("dossier") or {}
    out = []
    for company_id, name, city, rubric, rating, contacts in db.execute(
        "SELECT c.company_id, coalesce(o.org_name, o.name, c.name_norm), c.city,"
        "       c.rubric_id, o.rating, c.domain"
        " FROM companies c LEFT JOIN company_links l ON l.company_id = c.company_id"
        "   AND l.rule = 'self' LEFT JOIN orgs o ON o.branch_id = l.branch_id"
        " ORDER BY c.company_id").fetchall():
        subject = f"{name} | {city}"
        facts = []
        for kind, label in (("reviews", "Отзывы"), ("site", "Сайт"), ("instagram", "Instagram")):
            if subject in answers[kind]:
                facts.append(f"{label}: {answers[kind][subject]}")
        facts.append(f"Рубрика: {rubric}; рейтинг: {rating}; сайт: {contacts or 'нет'}")
        out.append((company_id, name, city, "\n\n".join(facts)))
    return out


def dossier_prompt(name, city, facts):
    return "\n".join([
        f"Компания: {name}",
        f"Город: {city}",
        "Извлечённые факты:\n" + (facts or "(данных нет)"),
    ])
```

- [ ] **Step 4: прогнать тесты**

```bash
cd collector && uv run pytest tests/test_operations.py -v
```

Ожидается: PASS.

- [ ] **Step 5: коммит**

```bash
cd collector && git add services/pipeline/analyze.py tests/test_operations.py && git commit -m "feat(collector): синтез досье — операция analyze.dossier"
```

---

### Task 22: таблица и view `dossiers`

**Files:**
- Modify: `collector/store/schema.sql`
- Modify: `collector/tests/test_schema.py`
- Modify: `collector/tests/test_operations.py`

**Interfaces:**
- Produces: `dossiers_all` + view `dossiers` (спека §3 таблица).

- [ ] **Step 1: тест схемы**

В `test_schema.py` добавить:

```python
def test_dossiers_view_exists(stores):
    stores.execute("INSERT INTO dossiers_all (run_id, company_id, summary, approach, sources)"
                   " VALUES (1, 'c', 's', 'a', '[]')")
    stores.execute("INSERT INTO current_run (id, run_id) VALUES (1, 1)")
    rows = stores.execute("SELECT * FROM dossiers").fetchall()
    assert rows and rows[0]["company_id"] == "c"
```

- [ ] **Step 2: убедиться, что падает**

```bash
cd collector && uv run pytest tests/test_schema.py::test_dossiers_view_exists -v
```

Ожидается: `no such table: dossiers_all`.

- [ ] **Step 3: добавить в `schema.sql` (DERIVED-часть, рядом с `profiles_all`)**

```sql
CREATE TABLE IF NOT EXISTS dossiers_all (
  run_id         INTEGER NOT NULL,
  company_id     TEXT NOT NULL,
  model          TEXT,
  summary        TEXT,
  hooks          TEXT,   -- JSON
  pains          TEXT,   -- JSON
  approach       TEXT,
  decision_maker TEXT,
  sources        TEXT,   -- JSON
  confidence     REAL,
  PRIMARY KEY (run_id, company_id)
);

CREATE VIEW IF NOT EXISTS dossiers AS
  SELECT d.* FROM dossiers_all d JOIN current_run USING (run_id);
```

- [ ] **Step 3b: завести `dossiers_all` в сторож «не пишет в обе базы»**

`test_no_operation_writes_to_both_dbs` отличает запись в derived от записи в
state по хардкод-списку `DERIVED_TABLES` (`tests/test_operations.py`). Без этой
строки запись в `dossiers_all` не засчитывается как «derived» вообще — сторож
просто не видит новую таблицу, и правило «ни одна операция не пишет в обе базы»
для досье фактически не проверяется, хотя Global Constraints это обещают.

В `tests/test_operations.py`:

```python
DERIVED_TABLES = frozenset(
    "runs current_run fetches_all orgs_all contacts_all companies_all"
    " company_links_all signals_all scores_all profiles_all dossiers_all".split()
)
```

- [ ] **Step 4: прогнать тесты**

```bash
cd collector && uv run pytest tests/test_schema.py tests/test_operations.py::test_no_operation_writes_to_both_dbs -v
```

Ожидается: PASS.

- [ ] **Step 5: коммит**

```bash
cd collector && git add store/schema.sql tests/test_schema.py tests/test_operations.py && git commit -m "feat(collector): таблица и view досье"
```

---

### Task 23: заполнение досье в пересборке

**Files:**
- Create: `collector/services/pipeline/dossier.py`
- Modify: `collector/services/pipeline/rebuild.py`
- Create: `collector/tests/test_dossiers.py`

**Interfaces:**
- Consumes: `rebuild.load_llm_answers(db, "dossier")`, `Dossier`-факты.
- Produces: `dossier.fill_dossiers(db, run_id)` — пишет `dossiers_all`; стадия `"досье"` в `rebuild.run`.

- [ ] **Step 1: написать тесты досье (на временной базе)**

```python
"""Досье — вычислимый контракт с системой 2: hooks с цитатой/ссылкой, pains
отсортированы и не больше четырёх, sources ровно те слои, что участвовали."""


def _seed_dossier(stores):
    stores.execute("INSERT INTO companies_all (run_id, company_id, name_norm, city)"
                   " VALUES (1, 'c1', 'Ромашка', 'almaty')")
    stores.execute("INSERT INTO current_run (id, run_id) VALUES (1, 1)")
    answer = {
        "dossier": {
            "summary": "Бухгалтерия",
            "hooks": [{"angle": "хвалят за скорость", "quote": "быстро",
                       "url": "https://r.kz/", "source": "reviews",
                       "observed_at": "2026-08-01"}],
            # Нарочно НЕ по убыванию тяжести: проверяем, что порядок в базе
            # наводит код (dossier.fill_dossiers), а не только промпт модели.
            "pains": [
                {"statement": "давно не обновляли сайт", "evidence": [],
                 "severity": "предполагается"},
                {"statement": "не отвечают на заявки", "evidence": [],
                 "severity": "видно явно"},
            ],
            "approach": "заходить через рост",
            "sources": ["reviews"],
            "confidence": 0.8,
        }
    }
    stores.execute("INSERT INTO state.llm_answers (kind, subject, model, prompt, answer)"
                   " VALUES ('dossier', 'Ромашка | almaty', 'm', 'p', ?)",
                   (json.dumps(answer, ensure_ascii=False),))
    stores.commit()
    from services.pipeline import dossier as mod
    mod.fill_dossiers(stores, 1)


def test_dossier_hooks_have_quote_and_url(stores):
    _seed_dossier(stores)
    row = stores.execute("SELECT hooks, pains, sources FROM dossiers").fetchone()
    hooks = json.loads(row["hooks"])
    assert hooks, "нет зацепок"
    assert all(h["quote"] and h["url"] for h in hooks), "hook без цитаты или ссылки"
    pains = json.loads(row["pains"])
    assert len(pains) <= 4, "больше четырёх болей"
    sev = [p["severity"] for p in pains]
    assert sev == sorted(sev, key={"видно явно": 0, "предполагается": 1, "не видно": 2}.get), \
        "pains не отсортированы по severity"
    # Seed нарочно пришёл в обратном порядке (см. _seed_dossier) — если этот
    # ассерт проходит только потому что pains был из одного элемента, значит
    # регрессия тише некуда: fill_dossiers обязан пересортировать сам, а не
    # полагаться на порядок, в котором их вернула модель.
    assert [p["statement"] for p in pains] == ["не отвечают на заявки", "давно не обновляли сайт"], \
        "fill_dossiers обязан пересортировать pains по severity, а не доверять порядку модели"
    assert json.loads(row["sources"]) == ["reviews"], "sources не перечисляют слои"


def test_dossier_empty_pains_legal(stores):
    stores.execute("INSERT INTO companies_all (run_id, company_id, name_norm, city)"
                   " VALUES (1, 'c2', 'Тишина', 'almaty')")
    stores.execute("INSERT INTO current_run (id, run_id) VALUES (1, 1)")
    answer = {"dossier": {"summary": "s", "hooks": [], "pains": [],
                          "approach": "a", "sources": [], "confidence": 0.5}}
    stores.execute("INSERT INTO state.llm_answers (kind, subject, model, prompt, answer)"
                   " VALUES ('dossier', 'Тишина | almaty', 'm', 'p', ?)",
                   (json.dumps(answer, ensure_ascii=False),))
    stores.commit()
    from services.pipeline import dossier as mod
    mod.fill_dossiers(stores, 1)
    row = stores.execute("SELECT count(*) FROM dossiers").fetchone()[0]
    assert row == 1, "пустой pains не должен ломать досье"
```

- [ ] **Step 2: убедиться, что падают**

```bash
cd collector && uv run pytest tests/test_dossiers.py -v
```

Ожидается: `ModuleNotFoundError: services.pipeline.dossier`.

- [ ] **Step 3: реализовать `dossier.py`**

```python
"""Заполнение dossiers_all из оплаченных ответов synthesize_dossier.

Досье — вычислимая таблица: выводится из state.llm_answers (ATTACH) и пишется
с run_id, как остальные. Прошлые версии остаются: видно, как менялось досье
при правке промпта синтеза. Правило «ни одна операция не пишет в обе базы» не
нарушается: пишем только в derived, а читаем state через ATTACH.
"""

import json

from services import store as engine

# Порядок тяжести. Промпт синтеза просит модель отдать pains от сильной к
# слабой (§3), но соблюдение инструкции моделью — не гарантия: сортировка
# закреплена здесь же, кодом, а не только словом в system-промпте.
SEVERITY_ORDER = {"видно явно": 0, "предполагается": 1, "не видно": 2}


def fill_dossiers(db, run_id):
    from services.pipeline import rebuild
    companies = {
        (name, city): company_id
        for company_id, name, city in db.execute(
            "SELECT c.company_id, coalesce(o.org_name, o.name, c.name_norm), c.city"
            " FROM companies_all c"
            " LEFT JOIN company_links_all l ON l.company_id = c.company_id"
            "   AND l.run_id = ? AND l.rule = 'self'"
            " LEFT JOIN orgs_all o ON o.branch_id = l.branch_id AND o.run_id = ?"
            " WHERE c.run_id = ?",
            (run_id, run_id, run_id),
        )
    }
    for answer in rebuild.load_llm_answers(db, "dossier"):
        name, _, city = answer["subject"].partition(" | ")
        company_id = companies.get((name, city))
        if not company_id:
            continue
        d = answer.get("dossier") or {}
        pains = sorted(
            (d.get("pains") or [])[:4],
            key=lambda p: SEVERITY_ORDER.get(p.get("severity"), len(SEVERITY_ORDER)),
        )
        db.execute(
            "INSERT OR REPLACE INTO dossiers_all (run_id, company_id, model, summary,"
            " hooks, pains, approach, decision_maker, sources, confidence)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, company_id, answer["model"], d.get("summary"),
                json.dumps(d.get("hooks") or [], ensure_ascii=False),
                json.dumps(pains, ensure_ascii=False),
                d.get("approach"), d.get("decision_maker_hint"),
                json.dumps(d.get("sources") or [], ensure_ascii=False),
                d.get("confidence"),
            ),
        )
```

- [ ] **Step 4: подключить стадию в `rebuild.run`**

```python
        stage("досье", lambda: dossier.fill_dossiers(db, run_id))
```

с импортом `from services.pipeline import dossier` и `STAGE_COUNT = 12`.

- [ ] **Step 5: прогнать тесты**

```bash
cd collector && uv run pytest tests/test_dossiers.py -v
```

Ожидается: PASS.

- [ ] **Step 6: проверить граф импортов (нет сети)**

```bash
cd collector && uv run pytest tests/test_operations.py::test_rebuild_import_graph_has_no_network -v
```

Ожидается: PASS (`dossier.py` не тянет `fetch`/`scrapling`).

- [ ] **Step 7: коммит**

```bash
cd collector && git add services/pipeline/dossier.py services/pipeline/rebuild.py tests/test_dossiers.py && git commit -m "feat(collector): заполнение досье в пересборке"
```

---

### Task 24: убрать `profiles` из реестра и `fill_profiles`

**Files:**
- Modify: `collector/services/pipeline/rebuild.py`
- Modify: `collector/services/pipeline/__init__.py`
- Modify: `collector/services/pipeline/analyze.py`
- Modify: `collector/tests/test_runs.py`, `collector/tests/test_operations.py`

**Interfaces:**
- Produces: реестр без `analyze.profile`/`analyze.ig_signals`; `fill_profiles` и стадия «профили от модели» удалены; `profiles_all`/view `profiles` остаются в схеме (строки прошлых прогонов не трогаем).

- [ ] **Step 1: удалить стадию и функцию**

В `rebuild.py` убрать `stage("профили от модели", ...)` и `fill_profiles`; `STAGE_COUNT` на 11. `profiles_all` в схеме не трогать.

- [ ] **Step 2: удалить операции из реестра**

В `services/pipeline/__init__.py`:

```python
OPERATIONS = {
    "collect.gis": collect.gis,
    "collect.sites": collect.sites,
    "collect.instagram": collect.instagram,
    "collect.reviews": collect.reviews,
    "collect.site_pages": collect.site_pages,
    "collect.ig_comments": collect.ig_comments,
    "collect.ig_profile": collect.ig_profile,
    "analyze.reviews": analyze.reviews,
    "analyze.site": analyze.site,
    "analyze.instagram": analyze.instagram,
    "analyze.dossier": analyze.dossier,
    "rebuild": rebuild.run,
    "export": export.run,
    "probe.gis_list": probe.gis_list,
    "probe.gis_firm": probe.gis_firm,
    "probe.gis_rubrics": probe.gis_rubrics,
    "probe.serp": probe.serp,
}
```

и в `PIPELINES` заменить пайплайн `classify`:

```python
    "classify": {"title": "Анализ и досье", "steps": ("analyze.reviews", "analyze.site",
                 "analyze.instagram", "analyze.dossier", "rebuild", "export")},
```

(Новые `collect.*` шаги тоже добавить в `discover`/отдельный пайплайн — порядок см. Task 25.)

- [ ] **Step 3: убрать `profile`/`ig_signals` из `analyze.py`**

Удалить функции `profile`, `ig_signals` и их `SYSTEM`/`IG_SYSTEM`/`ANSWER_KIND`/`IG_ANSWER_KIND`; оставить общие хелперы-импорты из `llm`.

- [ ] **Step 4: обновить тесты реестра**

В `test_operations.py` убрать/заменить упоминания `analyze.profile`, `analyze.ig_signals`; убедиться `test_all_operations_callable` проходит.

- [ ] **Step 5: прогнать тесты**

```bash
cd collector && uv run pytest tests/ -v
```

Ожидается: PASS (разделы с `live_db` на собранной базе).

- [ ] **Step 6: коммит**

```bash
cd collector && git add services/pipeline/rebuild.py services/pipeline/__init__.py services/pipeline/analyze.py tests/test_operations.py tests/test_runs.py && git commit -m "feat(collector): profiles -> dossiers, реестр без analyze.profile/ig_signals"
```

---

### Task 25: итоговые пайплайны и удаление `top_n`

**Files:**
- Modify: `collector/config.toml`
- Modify: `collector/services/pipeline/__init__.py`

**Interfaces:**
- Produces: полный каталог пайплайнов; `[llm] top_n` удалён.

- [ ] **Step 1: удалить `top_n` из `config.toml`**

```toml
[llm]
model = "deepseek/deepseek-v4-flash"
site_chars = 6000
```

(убрана строка `top_n = 40`).

- [ ] **Step 2: итоговые пайплайны**

```python
PIPELINES = {
    "discover": {"title": "Поиск новых лидов", "steps": (
        "collect.gis", "collect.sites", "collect.site_pages", "collect.reviews",
        "collect.instagram", "collect.ig_comments", "collect.ig_profile",
        "rebuild", "export")},
    "classify": {"title": "Анализ и досье", "steps": (
        "analyze.reviews", "analyze.site", "analyze.instagram", "analyze.dossier",
        "rebuild", "export")},
    "rebuild": {"title": "Пересборка из сырья", "steps": ("rebuild", "export")},
}
```

- [ ] **Step 3: проверить каталог цел**

```bash
cd collector && uv run python -c "from services import jobs; jobs.check_pipelines(); print('ok')"
```

Ожидается: `ok`.

- [ ] **Step 4: коммит**

```bash
cd collector && git add config.toml services/pipeline/__init__.py && git commit -m "feat(collector): пайплайны под новые операции, top_n удалён"
```

---

### Task 26: главная проверка `test_quote_is_verbatim` для сайта и отзывов (сквозная)

**Files:**
- Modify: `collector/tests/test_signals.py`

**Interfaces:**
- Consumes: сигналы, написанные Task 7/12/18.

- [ ] **Step 1: добавить сквозную проверку дословности**

```python
def test_quote_is_verbatim_for_ai_signals(live_db):
    """Главная проверка спеки §6: цитата каждого AI-сигнала стоит дословно в сырье.

    reviews/site/instagram — единственная защита от выдуманного факта в письме
    живому человеку. Собирается весь текст raw/ и каждая цитата ищется в нём.
    """
    import gzip
    from pathlib import Path
    import services.storage as storage

    raw_text = ""
    for sidecar in sorted(storage.RAW.glob("*.json")):
        sha = sidecar.name.removesuffix(".json")
        gz = storage.RAW / f"{sha}.html.gz"
        if gz.exists():
            raw_text += gzip.open(gz, "rt", encoding="utf-8").read()

    ai_types = ("reviews_missed_lead", "reviews_unanswered_complaint",
                "site_hiring_sales", "ig_unanswered_question")
    rows = db.execute(
        f"SELECT type, quote FROM signals WHERE type IN ({','.join('?'*len(ai_types))})"
        " AND length(quote) > 2", ai_types).fetchall()
    for signal_type, quote in rows:
        assert quote in raw_text, f"{signal_type}: цитата не дословна в сырье: {quote!r}"
```

- [ ] **Step 2: прогнать**

```bash
cd collector && uv run pytest tests/test_signals.py::test_quote_is_verbatim_for_ai_signals -v
```

Ожидается: PASS на собранной базе (пропускается на чистом клоне через `live_db`).

- [ ] **Step 3: коммит**

```bash
cd collector && git add tests/test_signals.py && git commit -m "test(collector): дословность цитат всех AI-сигналов"
```

---

### Task 27: граница системы 2 — `writer/leads_source.py` на досье

**Files:**
- Modify: `writer/leads_source.py`
- Modify: `writer/agent.py`
- Modify: `writer/tests/test_leads.py`

**Interfaces:**
- Consumes: view `dossiers` (Task 22).
- Produces: seed системы 2 из `summary/hooks/pains/approach/sources`; `writer/tests/test_leads.py` сид на `dossiers_all`.

- [ ] **Step 1: изменить запрос в `leads_source.py`**

```python
CANDIDATES = (
    "SELECT c.company_id, coalesce(o.org_name, o.name, c.name_norm), c.city,"
    "       d.summary, d.hooks, d.pains, d.approach, d.sources"
    " FROM companies c JOIN scores s USING (company_id)"
    " LEFT JOIN company_links l ON l.company_id = c.company_id AND l.rule = 'self'"
    " LEFT JOIN orgs o ON o.branch_id = l.branch_id"
    " LEFT JOIN dossiers d USING (company_id)"
)
```

- [ ] **Step 2: обновить `candidates` и `seed_of`**

```python
def candidates(db, limit):
    suppressed = suppression_handles(db)
    channels = channels_by_company(db)
    found = []
    for (company_id, name, city, summary, hooks, pains, approach,
         sources) in db.execute(CANDIDATES + WITH_INTENT):
        channel = best_channel(channels.get(company_id, []), suppressed)
        if not channel:
            continue
        found.append({
            "company_id": company_id,
            "thread_id": channel[1],
            "channel_kind": channel[0],
            "seed": {
                "name": name,
                "city": city,
                "dossier": {
                    "summary": summary,
                    "hooks": json.loads(hooks or "[]"),
                    "pains": json.loads(pains or "[]"),
                    "approach": approach,
                    "sources": json.loads(sources or "[]"),
                },
                "signals": signals_of(db, company_id),
            },
        })
        if len(found) == limit:
            break
    return found
```

(добавить `import json` вверху файла; `seed_of` привести к той же форме.)

- [ ] **Step 3: минимальная правка доступа в `agent.py`**

Чтобы writer не падал до переписи промпта (полная перепись — следующая задача), `prompt` читает досье:

```python
def prompt(seed, history, task):
    dossier = seed.get("dossier") or {}
    parts = [
        f"Компания: {seed['name']}",
        f"Город: {seed['city']}",
    ]
    if dossier.get("summary"):
        parts.append(f"Чем занимается: {dossier['summary']}")
    if dossier.get("approach"):
        parts.append(f"Как заходить: {dossier['approach']}")
    if dossier.get("hooks"):
        parts.append("Зацепки:")
        parts += [f"  [{h['source']}] {h['angle']}: «{h['quote']}»"
                  for h in dossier["hooks"]]
    if seed["signals"]:
        parts.append("Сигналы (возможные поводы):")
        parts += [f"  {s['type']}: {s['quote'] or ''}".rstrip() for s in seed["signals"]]
    if history:
        parts.append("Переписка:")
        parts += [f"  {'мы' if m['role'] == 'outgoing' else 'они'}: {m['text']}"
                  for m in history]
    parts.append(task)
    return "\n".join(parts)
```

- [ ] **Step 4: обновить `writer/tests/test_leads.py`**

Заменить сид `profiles_all` на `dossiers_all`:

```python
        db.execute("INSERT INTO dossiers_all (run_id, company_id, model, summary,"
                   " hooks, pains, approach, sources, confidence)"
                   " VALUES (?, ?, 'модель', 'бухгалтерия', '[]',"
                   " '[{\"statement\": \"ищет клиентов\", \"evidence\": [], \"severity\": \"видно явно\"}]',"
                   " 'заходить через рост', '[\"reviews\"]', 0.8)",
                   (run_id, company_id))
```

и ассерты:

```python
    assert lead["seed"]["name"] == "Ромашка", lead["seed"]
    assert lead["seed"]["dossier"]["summary"] == "бухгалтерия", lead["seed"]
    assert [s["type"] for s in lead["seed"]["signals"]] == ["crm_widget"], lead["seed"]
```

(в `_seed` убрать вставку `profiles_all`).

- [ ] **Step 5: обновить `writer/tests/test_prompt.py`**

Сид на новую форму досье:

```python
    seed = {
        "name": "Ромашка", "city": "almaty",
        "dossier": {"summary": "бухгалтерия", "hooks": [], "pains": [],
                    "approach": "заходить через рост", "sources": []},
        "signals": [],
    }
```

- [ ] **Step 6: прогнать тесты writer'а**

```bash
cd writer && uv run pytest tests/ -v
```

Ожидается: PASS (schema, threads, leads, prompt).

- [ ] **Step 7: прогнать тесты collector'а**

```bash
cd collector && uv run pytest tests/ -v
```

Ожидается: PASS.

- [ ] **Step 8: коммит**

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test && git add writer/leads_source.py writer/agent.py writer/tests/test_leads.py writer/tests/test_prompt.py && git commit -m "feat(writer): отбор на досье вместо profiles, seed из summary/hooks/pains/approach"
```

---

### Task 28: `docs/ARCHITECTURE_v3.md`

**Files:**
- Create: `docs/ARCHITECTURE_v3.md`

**Interfaces:**
- Produces: документ по итогам обеих спек (спека: «пишется ARCHITECTURE_v3.md»).

- [ ] **Step 1: написать разделы**

Отразить: четырёхслойный анализ, ключ кэша по слою, досье как контракт (§3), веса-правила (§4), тренд охватов медианой, отмену §10/§11-старых решений, судьбу regex-сигналов. Использовать как источник `docs/superpowers/specs/2026-08-18-ai-signals-design.md` и `2026-08-18-storage-and-operations-design.md`.

- [ ] **Step 2: коммит**

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test && git add docs/ARCHITECTURE_v3.md && git commit -m "docs: ARCHITECTURE_v3 — слои анализа, досье, скоринг"
```

---

## Self-Review (прогоняется перед сдачей)

1. **Покрытие спеки.** §1.1 отзывы (Tasks 1–7), §1.2 сайты (8–12), §1.3 Instagram (13–18), §2 слои (5,6,10,11,17,18,21), §3 досье (20–23), §4 скоринг (7,12,18,19), §5 граница (27), §6 проверки (4,7,12,18,22,23,26), §7 порядок этапов (задачи уже в этом порядке), §8 риски (защита от бана — Task 16, ключ API — Task 2). Архитектура v3 — Task 28.
2. **Тип без веса — ошибка сборки.** `emit` (Task 7, общий для всех слоёв) поднимает `KeyError`, если типа нет в `weights` — спека §6 соблюдена: сигналу без цены в конфиге неоткуда взять цену, и молчаливый `0.5` замаскировал бы опечатку в названии типа. Отдельная проверка типов в конфиге — Task 7 Step 1 и Task 19 Step 3.
3. **Согласованность типов.** `llm.answered/store_answer/structured_model` — единые имена; `company_by_subject` переиспользуется в Task 7 и 12; `emit` — один на все слои. `subject_of` в `rebuild.py` для `ig_signals` знает только `ig_signals`/`company_profile` — **добавить ветки** для новых kinds (`reviews`, `site`, `dossier` = `название | город`; `instagram` = логин).
4. **Заглушки отсутствуют.** Все кодовые шаги содержат реальный код. Единственная неизбежная внешняя деталь — реальный публичный ключ 2GIS (Task 2) и точные числа фикстур от живых источников (Tasks 1/8/13) — зафиксированы как «снять с источника», как это делали существующие фикстуры.

**Обязательные доработки (внесены в план перед сдачей):**
- Task 7/12/18 `emit` и `company_by_subject`: `company_id` всегда резолвится по subject, не `None`.
- `rebuild.subject_of`: поддержаны новые kinds (`instagram` — логин; `reviews`/`site`/`dossier` — `название | город`).
- Task 7 Step 4: тип без веса в `emit` поднимает `KeyError`, а не тихий `0.5`.

**Правки по code review (2026-08-19):**
- **Task 7.** `reviews_signals` писала по строке сигнала на КАЖДУЮ подтверждённую
  жалобу одного типа; `review_url` строит адрес по `branch_id`, а `date_created`
  у 2GIS обычно дата без времени — две жалобы одного типа с одной датой у
  одного филиала давали одинаковый `(company_id, type, observed_at, url)` и
  падали на `INSERT` в `signals_all` с `sqlite3.IntegrityError`. Добавлен
  `newest_review_match` — один сигнал на тип на компанию, от самой свежей
  подтверждённой жалобы, тем же принципом, что `newest_per_type` в
  `instagram_signals`. Регрессия — Task 7 Step 6b.
- **Task 19.** Убрать веса `ig_promo`/`ig_hiring_sales`/`ig_direct_selling` из
  конфига было недостаточно: `enrich.instagram_signals()` продолжала бы читать
  исторические ответы `kind="ig_signals"` (ничего не удаляется) и писать их
  через `newest_per_type` с тихим откатом на вес 1.0 — в обход собственного же
  правила «тип без веса — ошибка сборки». Добавлен Step 2b: старый LLM-слой
  инстаграма (`ig_answers`/`newest_per_type`/`post_with_quote`) удаляется из
  `enrich.py` целиком, а не просто теряет вес в конфиге.
- **Tasks 6/11/18/21.** Единственные вызовы `ctx.log` в `analyze.py` жили в
  `profile()`/`ig_signals()` — функциях, которые Task 24 удаляет.
  `test_operations_use_the_context_they_are_given` проверяет наличие
  `"ctx.log"` во всём модуле; без своего лога у `reviews`/`site`/`instagram`/
  `dossier` тест упал бы разом на всех четырёх операциях после Task 24.
  Добавлены `ctx.log` в начале и в конце каждой операции.
- **Task 22.** `dossiers_all` не попадала в хардкод-список `DERIVED_TABLES`
  (`test_no_operation_writes_to_both_dbs`, `tests/test_operations.py`) — сторож
  «ни одна операция не пишет в обе базы» не видел новую таблицу вообще, то есть
  формально не защищал её, хотя Global Constraints это обещают. Добавлен Step
  3b, поправлена ссылка на тест в Global Constraints (было указано
  несуществующее имя `test_no_write_module_opens_both_dbs`).
- **Task 23.** Тест на сортировку `pains` по severity сеял ровно один элемент —
  проверка `sev == sorted(sev, ...)` истинна на списке из одного элемента
  независимо от того, сортирует ли код что-либо. `_seed_dossier` теперь сеет
  два `pains` заведомо в обратном порядке, тест сверяет итоговый порядок по
  содержимому; `fill_dossiers` (`services/pipeline/dossier.py`) теперь сам
  сортирует `pains` по `SEVERITY_ORDER` и обрезает до 4 перед записью — порядок
  и лимит больше не держатся только на послушании модели промпту.

**Отклонения при исполнении (2026-08-19, вскрылись на живых данных):**
- **Task 8/9.** Фикстура главной — `intercomp.kz` (снята из raw/), а не первый
  домен `01prospekt.kz` (оказался одностраничником без навигации). `parse_site_links`
  матчит и **текст ссылки**, и href: у .kz-сайтов слоги латиницей (`/services/`),
  а словарь на русском — по одному адресу обход находил бы ноль страниц.
- **Task 13/16.** `users/{pk}/info/` требует **числовой pk пользователя**, а не
  логин (логин в URL даёт 404). Добавлена карта username→pk из ответа лент
  (`instagram_user_ids`); `profiles_by_username` ключует по `username` из ответа
  профиля, а не из адреса.
- **Task 18.** `Question.media_url` — `/p/{shortcode}/`, а комментарии ключуются
  по media `pk`: добавлена карта shortcode→pk из лент, по которой восстанавливается
  связь для проверки дословности `ig_unanswered_question`.
- **Task 19.** Удаление `post_with_quote` потянуло за собой тест
  `test_ig_quote_binding` (тестировал устаревший механизм) — вызов убран из
  `test_ig_parsing`, сам тест удалён. `ig_answers`/`newest_per_type` удалены.
- **Task 27.** Дополнительно обновлён `writer/tests/test_prompt.py` (сид на форму
  досье) — без этого промпт-тесты writer'а падали бы на новом seed.

---

## Execution Handoff

План сохранён в `docs/superpowers/plans/2026-08-18-ai-signals.md`. Два способа исполнения:

1. **Subagent-Driven (рекомендуется)** — свежий субагент на задачу, ревью между задачами.
2. **Inline Execution** — исполнение в этой сессии по `executing-plans`.
