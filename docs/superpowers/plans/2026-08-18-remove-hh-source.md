# Удаление источника hh.kz — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Убрать hh.kz из системы 1 целиком — сбор, разбор, таблицу, сигналы, веса, проверки, фикстуры и упоминания в документации — не изменив ни одной строки выдачи.

**Architecture:** Удаление идёт против потока данных: сначала потребители сигналов (веса, детекторы, шаблоны), потом склейка и таблица, потом сбор. На каждом шаге конвейер остаётся рабочим и проходит собственный набор проверок. Схема пересобирается через `DROP`, миграций нет.

**Tech Stack:** Python 3.13 + uv, SQLite, Next.js (фронтенд). Тестовый набор проекта — `collector/scripts/check.py`, pytest в проекте нет.

**Spec:** [docs/superpowers/specs/2026-08-18-remove-hh-source-design.md](../specs/2026-08-18-remove-hh-source-design.md)

## Global Constraints

- Все команды `uv run` запускаются **из каталога `collector/`**, если не сказано иное.
- **`data/raw/` не трогать.** Ни одного файла не удалять и не править. 458 hh-страниц остаются на диске намеренно (спека §4.1).
- **Инвариант выдачи:** после каждой задачи `data/leads.csv` обязан совпадать с эталоном побайтово, а `count(*) FROM signals` = 1876. Расхождение — условие остановки, а не повод обновить эталон.
- Эталон, снятый 18 августа 2026:
  - `sha256(data/leads.csv)` = `fa96f54ede4cbe07faa06f62275fb7171268ba22932955600cb7486fe115f093`
  - `count(*) FROM signals` = 1876
  - `ls data/raw | wc -l` = 7834
- **Единственный `hh` в коде, который обязан выжить:** `collector/scripts/check.py`, условие размера страницы в `check_raw` (`if "2gis.kz" in meta["url"] or "hh.kz" in meta["url"]:`). Страницы физически лежат в `raw/`, и их размер по-прежнему проверяется.
- `NAME_SIMILARITY` в `services/resolve.py` **не удалять** — её использует склейка филиалов 2GIS.
- В рабочем дереве лежат незакоммиченные изменения по системе 2 и docker. Коммитить **только перечисленные в задаче файлы**, через явный `git add <путь>`. Никаких `git add -A`, `git add .`, `git commit -a`.
- Стиль сообщений — conventional commits, по-русски, как в `git log`.

---

## Структура файлов

| Файл | Что с ним происходит | Задача |
|---|---|---|
| `collector/config.toml` | удаляются веса `vacancy_*`, `stale_vacancy_days`, затем секция `[hh]` | 1, 3 |
| `collector/services/enrich.py` | удаляются детекторы вакансий, `quote_around`, параметр `stale_vacancy_days` | 1 |
| `collector/build.py` | `scoring_config` теряет второй элемент; удаляются `fill_vacancies`, `vacancy_origins`, регулярки hh | 1, 2 |
| `collector/report.py` | удаляются два шаблона `WHY_TEMPLATES` | 1 |
| `collector/services/resolve.py` | удаляется `link_employers` | 2 |
| `collector/services/sources.py` | удаляются `parse_vacancy_ids`, `parse_job_posting`, `JSON_LD`, `VACANCY_ID` | 2 |
| `collector/services/probes/hh_vacancies.py` | удаляется файл | 2 |
| `collector/db/schema.sql` | удаляется таблица `vacancies` | 2 |
| `collector/fixtures/hh_list.html.gz`, `hh_vacancy.html.gz` | удаляются файлы | 2 |
| `collector/scripts/check.py` | удаляются 4 проверки | 1, 2, 3 |
| `collector/scripts/collect.py` | удаляется ветка сбора вакансий | 3 |
| `collector/services/fetch.py` | адреса в `demo()` меняются на 2GIS | 4 |
| `frontend/app/api.ts` | удаляются две подписи | 4 |
| `writer/agent.py`, `writer/schemas/outreach.py`, `writer/scripts/check.py` | `vacancy_sales` → `crm_widget` | 4 |
| `CLAUDE.md`, `docs/TRD.md` и др. | актуализация, секция «Отклонённые источники» | 5 |

---

## Task 1: сигналы, веса и шаблоны обоснований

Удаляются потребители вакансий на стороне скоринга. Таблица `vacancies` при этом
продолжает наполняться — конвейер остаётся рабочим, и проверки, которые её читают,
пока не трогаются.

**Files:**
- Modify: `collector/config.toml:115` (`stale_vacancy_days`), `:120-122` (три веса)
- Modify: `collector/services/enrich.py` — шапка модуля, `VACANCY_MARKERS`, `enrich()`, `vacancy_signals()`, `stale_vacancies()`, `quote_around()`
- Modify: `collector/build.py:76-79` (`scoring_config` — единственный вызов на строке 65), `:65` (вызов `enrich.enrich`)
- Modify: `collector/report.py:181-183` (`WHY_TEMPLATES`)
- Test: `collector/scripts/check.py` (запуск, без правок в этой задаче)

**Interfaces:**
- Produces: `build.scoring_config()` возвращает **один** объект — словарь весов `config["scoring"]["intent"]`, а не кортеж из двух. Задача 2 на это не опирается, но сигнатуру менять больше нельзя.
- Produces: `enrich.enrich(db, pages, weights)` — три параметра вместо четырёх.

- [ ] **Step 1: Снять эталон и убедиться, что набор зелёный**

```bash
cd collector
shasum -a 256 data/leads.csv
sqlite3 db/leads.db "select count(*) from signals;"
ls data/raw | wc -l
uv run -m scripts.check
```

Ожидается: `fa96f54ede4cbe07faa06f62275fb7171268ba22932955600cb7486fe115f093`, `1876`, `7834`, и `check ok: parsers, raw, build, collect, resolve, signals, scores, leads, web, jobs` с кодом выхода 0.

Если хоть одно число не совпало — **остановиться и отчитаться**. План написан против этого состояния.

- [ ] **Step 2: Удалить веса из `config.toml`**

Удалить строку 115 вместе с комментарием над ней:

```toml
# Вакансия считается зависшей, если висит дольше этого срока.
stale_vacancy_days = 60
```

И три строки из `[scoring.intent]`:

```toml
vacancy_sales = 3.0                # вакансия в продажах (по тексту, не по заголовку)
vacancy_stale = 3.0                # вакансия висит дольше stale_vacancy_days
vacancy_growth = 3.0               # рост числа вакансий за квартал
```

`vacancy_growth` удаляется вместе с остальными: производителя у этого веса нет ни одного (проверено `grep -rn vacancy_growth --include='*.py'` — пусто).

Комментарий над блоком `[scoring.intent]` про «нет сигнала не значит отрицательный вес» остаётся.

- [ ] **Step 3: Убрать чтение `stale_vacancy_days` в `build.py`**

Было (`build.py:76-79`):

```python
def scoring_config():
    """Веса и пороги из config.toml: в коде их держать нельзя, они калибруются."""
    config = tomllib.loads(Path("config.toml").read_text(encoding="utf-8"))["scoring"]
    return config["intent"], config["stale_vacancy_days"]
```

Стало:

```python
def scoring_weights():
    """Веса сигналов из config.toml: в коде их держать нельзя, они калибруются."""
    config = tomllib.loads(Path("config.toml").read_text(encoding="utf-8"))["scoring"]
    return config["intent"]
```

Было (`build.py:66`):

```python
    enrich.enrich(db, pages, *scoring_config())
```

Стало:

```python
    enrich.enrich(db, pages, scoring_weights())
```

- [ ] **Step 4: Убрать детекторы вакансий из `enrich.py`**

Удалить целиком: константу `VACANCY_MARKERS`, функции `vacancy_signals`, `stale_vacancies`, `quote_around`.

`quote_around` удаляется потому, что её единственный вызов стоял в `vacancy_signals` (проверено `grep -rn quote_around --include='*.py'` — два совпадения: определение и этот вызов).

Было:

```python
def enrich(db, pages, weights, stale_vacancy_days):
    """Наполнить signals. Веса приходят из config.toml, а не зашиты здесь.

    pages передаётся снаружи, а не читается с диска заново: сборка обязана быть
    функцией ОДНОГО снимка raw/. Повторное чтение подхватывало бы страницы,
    появившиеся за время сборки, и они не попадали бы в fetches.
    """
    site_signals(db, pages, weights)
    vacancy_signals(db, weights, stale_vacancy_days)
    instagram_signals(db, pages, weights)
```

Стало:

```python
def enrich(db, pages, weights):
    """Наполнить signals. Веса приходят из config.toml, а не зашиты здесь.

    pages передаётся снаружи, а не читается с диска заново: сборка обязана быть
    функцией ОДНОГО снимка raw/. Повторное чтение подхватывало бы страницы,
    появившиеся за время сборки, и они не попадали бы в fetches.
    """
    site_signals(db, pages, weights)
    instagram_signals(db, pages, weights)
```

- [ ] **Step 5: Переписать шапку `enrich.py` — источников два, а не три**

В докстринге модуля блок «Три источника» описывает сайт, вакансию hh и ленту
инстаграма. Заменить его на:

```
Два источника:

  сайт компании   CRM, пиксели, реклама, формы — regex по сырому HTML
  лента инстаграма  даты и темп постинга — арифметикой здесь, смысл подписей —
                  моделью в scripts/classify_ig.py, готовые ответы читаются с диска
```

Абзац про «Колонка has_hiring_signal boolean через месяц становится незаметной
ложью: вакансия вчерашняя и полугодовой давности — разные лиды» **оставить**: он
объясняет, почему `signals` — события с датой, и это правило пережило hh.

Абзац «Почему у инстаграма смысл отдан модели, а у сайта нет» и абзац про
обязательные `quote` и `url` оставить без изменений.

- [ ] **Step 6: Удалить шаблоны в `report.py`**

Из словаря `WHY_TEMPLATES` (`report.py:176-184`) удалить две строки:

```python
    "vacancy_sales": "ищет людей в продажи: «{quote}»",
    "vacancy_stale": "{quote} — наймом закрыть не вышло",
```

Остальные четыре шаблона и комментарий над словарём не трогать.

- [ ] **Step 7: Пересобрать базу и проверить инвариант**

```bash
cd collector
uv run build.py
uv run report.py 30
shasum -a 256 data/leads.csv
sqlite3 db/leads.db "select count(*) from signals;"
```

Ожидается: сборка завершается строкой `db/leads.db готова за N с`, `sha256` совпадает с эталоном из Global Constraints, `signals` = 1876.

Если `sha256` разошёлся — **остановиться**. Сигналов `vacancy_*` в базе нет, выдача меняться не может; расхождение значит, что задет чужой код.

- [ ] **Step 8: Прогнать весь набор проверок**

```bash
cd collector
uv run -m scripts.check
```

Ожидается: код выхода 0. Раздел `build` по-прежнему включает `check_vacancies` (таблица ещё наполняется), раздел `resolve` — блок `LINKED_VACANCIES_FLOOR`; оба обязаны пройти.

- [ ] **Step 9: Коммит**

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test
git add collector/config.toml collector/services/enrich.py collector/build.py collector/report.py
git commit -m "refactor(collector): убрать сигналы и веса вакансий hh"
```

---

## Task 2: склейка, разбор и таблица

Удаляется всё, что превращает hh-страницы из `raw/` в строки базы. После задачи
таблицы `vacancies` в схеме нет.

**Files:**
- Modify: `collector/services/resolve.py:46-51` (`resolve`), `:219-247` (`link_employers`)
- Modify: `collector/build.py:42-48` (регулярки), `:60` (вызов), `:242-289` (`fill_vacancies`, `vacancy_origins`), `:300-303` (`report`)
- Modify: `collector/services/sources.py:16-18`, `:109-123`
- Modify: `collector/db/schema.sql` — таблица `vacancies`
- Modify: `collector/scripts/check.py:59` (вызов), `:127-146` (`check_hh_parsing`), `:263` (вызов), `:290-310` (`check_vacancies`), `:512-526` (блок `LINKED_VACANCIES_FLOOR`)
- Delete: `collector/services/probes/hh_vacancies.py`, `collector/fixtures/hh_list.html.gz`, `collector/fixtures/hh_vacancy.html.gz`

**Interfaces:**
- Consumes: `build.scoring_weights()` и `enrich.enrich(db, pages, weights)` из задачи 1.
- Produces: в `leads.db` восемь таблиц минус `vacancies` — семь: `fetches`, `orgs`, `contacts`, `companies`, `company_links`, `signals`, `scores`, `profiles`, `suppression`. Задача 4 (`writer/`) опирается на то, что `vacancies` больше нет.

- [ ] **Step 1: Удалить `link_employers` из `resolve.py`**

Было (`resolve.py:46-51`):

```python
def resolve(db):
    """Наполнить companies и company_links, проставить vacancies.company_id."""
    branches = load_branches(db)
    groups = group_branches(branches)
    write_companies(db, branches, groups)
    link_employers(db)
```

Стало:

```python
def resolve(db):
    """Наполнить companies и company_links."""
    branches = load_branches(db)
    groups = group_branches(branches)
    write_companies(db, branches, groups)
```

Затем удалить функцию `link_employers` целиком (строки 219-247, включая её
докстринг про открытый вопрос №1 TRD) и заголовок-комментарий над ней, если он
относится только к ней.

**`NAME_SIMILARITY` (строка 30) не трогать** — её использует `join_similar_names`
на строке 145.

- [ ] **Step 2: Удалить наполнение таблицы в `build.py`**

Удалить функции `fill_vacancies` и `vacancy_origins` целиком, вызов
`fill_vacancies(db, pages)` в `main()`, регулярки `HH_LIST_URL` и `HH_VACANCY_URL`
и константу `VACANCY_URL` вместе с её комментарием.

В `report()` было:

```python
        for table in ("fetches", "orgs", "contacts", "vacancies")
```

Стало:

```python
        for table in ("fetches", "orgs", "contacts")
```

В шапке модуля фраза «страница удаляется, вакансия закрывается» **остаётся**: она
про природу `raw/`, а не про hh, и hh-страницы в `raw/` лежат по-прежнему.

`pages_matching` **не удалять** — её используют `fill_orgs` и `fill_contacts`.

- [ ] **Step 3: Удалить разбор в `sources.py`**

Удалить константы:

```python
JSON_LD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
VACANCY_ID = re.compile(r"/vacancy/(\d{6,})")
```

и функции `parse_vacancy_ids`, `parse_job_posting` целиком.

`INITIAL_STATE` и остальные функции не трогать. Импорт `re` остаётся — его
используют другие регулярки модуля.

- [ ] **Step 4: Удалить разведчик**

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test
git rm collector/services/probes/hh_vacancies.py
```

Это единственный внешний потребитель удалённых функций (проверено
`grep -rn "parse_job_posting\|parse_vacancy_ids" --include='*.py'`).

- [ ] **Step 5: Удалить таблицу из схемы**

Из `collector/db/schema.sql` удалить блок целиком — от комментария
`-- hh.kz: полный текст вакансии — главный intent-сигнал проекта.` до закрывающей
скобки `CREATE TABLE vacancies (...);` включительно, вместе с комментарием про
`company_id` и `match_confidence`.

В шапке файла было:

```sql
-- На Ф3 наполняются четыре таблицы: fetches, orgs, contacts, vacancies.
```

Стало:

```sql
-- На Ф3 наполняются три таблицы: fetches, orgs, contacts.
```

Строку `-- Схема leads.db — восемь таблиц из ARCHITECTURE_v2 §4.` заменить на
`-- Схема leads.db — семь таблиц из ARCHITECTURE_v2 §4.`

- [ ] **Step 6: Удалить фикстуры и проверку разбора hh**

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test
git rm collector/fixtures/hh_list.html.gz collector/fixtures/hh_vacancy.html.gz
```

В `collector/scripts/check.py` удалить вызов `check_hh_parsing()` из
`check_parsers()` (строка 59) и саму функцию `check_hh_parsing` целиком
(строки 127-146).

- [ ] **Step 7: Удалить проверку таблицы и метрику привязки**

В `check.py` удалить вызов `check_vacancies(db)` из `check_build()` (строка 263)
и функцию `check_vacancies` целиком (строки 290-310).

В `check_resolve()` удалить блок целиком — комментарий про самое слабое место
конвейера, `LINKED_VACANCIES_FLOOR = 4`, запросы `fuzzy` и `total`, ассерт и
последний `print`:

```python
    # Привязка работодателя hh к компании 2GIS — самое слабое место конвейера:
    # из 384 вакансий привязаны 4. Число зафиксировано, чтобы падение до нуля
    # не проходило зелёным. Ветку чинит отдельный план; когда починят — поднять
    # порог здесь тем же коммитом, иначе ассерт перестанет что-либо значить.
    LINKED_VACANCIES_FLOOR = 4
    fuzzy = db.execute(
        "SELECT count(*) FROM vacancies WHERE company_id IS NOT NULL"
    ).fetchone()[0]
    total = db.execute("SELECT count(DISTINCT employer) FROM vacancies").fetchone()[0]
    assert fuzzy >= LINKED_VACANCIES_FLOOR, (
        f"вакансий привязано к компаниям {fuzzy}, было {LINKED_VACANCIES_FLOOR} — "
        "склейка работодателей стала хуже, сигналы вакансий исчезнут из скоринга"
    )
```

и строку:

```python
    print(f"  вакансий привязано к компаниям {fuzzy}, работодателей всего {total}")
```

Строку `print(f"  компаний {companies} из {branches} филиалов, крупнейшая из {multi[1]}")`
и `db.close()` **оставить**.

- [ ] **Step 8: Пересобрать и проверить инвариант**

```bash
cd collector
uv run build.py
uv run report.py 30
shasum -a 256 data/leads.csv
sqlite3 db/leads.db "select count(*) from signals;"
sqlite3 db/leads.db "select name from sqlite_master where name='vacancies';"
```

Ожидается: строка `собрано из N страниц raw/: fetches …, orgs …, contacts …` (без `vacancies`), `sha256` совпадает с эталоном, `signals` = 1876, последний запрос — пустой вывод.

- [ ] **Step 9: Прогнать проверки**

```bash
cd collector
uv run -m scripts.check
```

Ожидается: код выхода 0 на всех десяти разделах.

Отдельно убедиться, что раздел `parsers` работает без базы и без `raw/` — это его
главное свойство, и удаление фикстур могло его сломать:

```bash
cd collector
uv run -m scripts.check parsers
```

Ожидается: код выхода 0, в выводе три разбора (2GIS рубрика, 2GIS карточка, лента инстаграма), строки `hh:` больше нет.

- [ ] **Step 10: Коммит**

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test
git add collector/services/resolve.py collector/build.py collector/services/sources.py collector/db/schema.sql collector/scripts/check.py
git commit -m "refactor(collector): убрать разбор вакансий hh и таблицу vacancies"
```

`git rm` из шагов 4 и 6 уже проиндексировал удаления — отдельно добавлять их не нужно.

---

## Task 3: сбор

Удаляется сетевая ветка. После задачи `scripts.collect` перестаёт ходить на hh
и делает на ~440 запросов меньше за полный прогон.

**Files:**
- Modify: `collector/scripts/collect.py:35-36` (адреса), `:42` (`HH_HEADERS`), `:78-105` (`main`), `:109-123` (`parse_args`), `:125-136` (`load_plan`), `:224-264` (`collect_vacancies`, `vacancy_list`, `vacancy_page`)
- Modify: `collector/config.toml:64-105` (секция `[hh]`)
- Modify: `collector/scripts/check.py:400` (вызов), `:433-445` (`check_slug_guard`)

**Interfaces:**
- Produces: `load_plan(args)` возвращает словарь с ключами `cities` и `rubrics`. Ключа `slugs` больше нет.
- Produces: `main()` больше не возвращает и не печатает список подменённых slug'ов; `sys.exit` в конце `main` исчезает.

- [ ] **Step 1: Удалить ветку сбора в `collect.py`**

Удалить функции `collect_vacancies`, `vacancy_list`, `vacancy_page` целиком,
константы `VACANCY_LIST`, `VACANCY`, `HH_HEADERS`.

Было (`main`):

```python
    if args.instagram:
        collect_instagram(budget)
        substituted = []
    elif args.sites:
        collect_sites(budget, args.workers)
        substituted = []
    else:
        branches = collect_org_lists(budget, plan, args.workers)
        collect_firm_cards(budget, branches, args.workers)
        substituted = collect_vacancies(budget, plan, args.workers)

    report(budget, raw_before, time.time() - started)
    if substituted:
        sys.exit(
            f"\nОТКАЗ: {len(substituted)} slug'ов подменены общим списком города: "
            f"{', '.join(substituted)}\n"
            "Их вакансии в сбор не взяты. Убери slug из config.toml [hh].slugs "
            "или замени на существующую SEO-страницу hh."
        )
```

Стало:

```python
    if args.instagram:
        collect_instagram(budget)
    elif args.sites:
        collect_sites(budget, args.workers)
    else:
        branches = collect_org_lists(budget, plan, args.workers)
        collect_firm_cards(budget, branches, args.workers)

    report(budget, raw_before, time.time() - started)
```

**`import sys` не трогать** — он нужен ещё в четырёх местах модуля (`sys.exit`
при отсутствии базы и куков в ветках `--sites` и `--instagram`).

**Класс `Substituted` не удалять.** Его поднимает проверка пагинации 2GIS
(`первая страница отдала страницу {current}`) и проверка сессии инстаграма. Из
удаляемого кода уходит только третье его использование — в `vacancy_list`.

- [ ] **Step 2: Удалить аргумент и ключ плана**

В `parse_args` удалить строку:

```python
    parser.add_argument("--slugs", type=int, help="сколько первых slug'ов hh взять")
```

В `load_plan` было:

```python
    return {
        "cities": args.cities or config["cities"],
        "rubrics": config["rubrics"]["include"][: args.rubrics],
        "slugs": config["hh"]["slugs"][: args.slugs],
    }
```

Стало:

```python
    return {
        "cities": args.cities or config["cities"],
        "rubrics": config["rubrics"]["include"][: args.rubrics],
    }
```

- [ ] **Step 3: Удалить секцию `[hh]` из `config.toml`**

Удалить всю секцию — от строки `[hh]` с комментарием о транслитерации до
закрывающей `]` списка `slugs` включительно, вместе с блоком комментария «У hh нет
SEO-страницы под эти фразы — проверено, повторно не пробовать».

Следующая секция `[scoring]` и всё после неё остаются.

- [ ] **Step 4: Удалить проверку подмены slug'а**

В `check.py` удалить вызов `check_slug_guard(db, config)` из `check_collect()` и
функцию `check_slug_guard` целиком (строки 433-445).

`check_pagination_guard(db)` и `check_plan_coverage(db, config)` **остаются** — обе
про 2GIS. Переменная `config` в `check_collect` по-прежнему нужна: её читает
`check_plan_coverage`.

- [ ] **Step 5: Проверить, что сбор запускается и hh в нём нет**

```bash
cd collector
uv run -m scripts.collect --help
```

Ожидается: код выхода 0, в списке аргументов нет `--slugs`.

Сеть в проверке не нужна — достаточно убедиться, что конфиг и модуль сошлись:

```bash
cd collector
uv run python -c "
import tomllib, pathlib
config = tomllib.loads(pathlib.Path('config.toml').read_text(encoding='utf-8'))
assert 'hh' not in config, 'секция [hh] осталась в config.toml'
import scripts.collect as collect
assert not hasattr(collect, 'VACANCY_LIST'), 'адрес списка вакансий остался'
assert not hasattr(collect, 'collect_vacancies'), 'сбор вакансий остался'
print('collect.py и config.toml: ветки hh нет')
"
```

Ожидается: код выхода 0 и строка `collect.py и config.toml: ветки hh нет`.

- [ ] **Step 6: Прогнать проверки и инвариант**

```bash
cd collector
uv run -m scripts.check
uv run build.py && uv run report.py 30
shasum -a 256 data/leads.csv
```

Ожидается: код выхода 0, `sha256` совпадает с эталоном.

- [ ] **Step 7: Финальный grep по коду**

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test
grep -rn -e vacanc -e 'hh\.kz' collector/ --include='*.py'
```

Ожидается **ровно одно** совпадение — условие размера страницы в `check_raw`
(`collector/scripts/check.py`). Любое другое совпадение значит, что задача не
доделана.

- [ ] **Step 8: Коммит**

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test
git add collector/scripts/collect.py collector/config.toml collector/scripts/check.py
git commit -m "refactor(collector): убрать сбор вакансий hh"
```

---

## Task 4: фронтенд, система 2 и самопроверка `fetch.py`

Три потребителя типа `vacancy_sales` за пределами конвейера сбора.

**Files:**
- Modify: `frontend/app/api.ts:239-247` (`SIGNAL_LABELS`)
- Modify: `writer/agent.py:36`
- Modify: `writer/schemas/outreach.py:32`
- Modify: `writer/scripts/check.py` — строки 41, 79-80, 104, 110, 129, 143-145, 155, 170, 174, 186-187
- Modify: `collector/services/fetch.py:145-163` (`demo`)

**Interfaces:**
- Consumes: из задачи 2 — таблицы `vacancies` в `leads.db` больше нет. `writer/leads_source.py` её не читает, менять его не нужно.

- [ ] **Step 1: Удалить подписи во фронтенде**

Было (`frontend/app/api.ts`):

```ts
export const SIGNAL_LABELS: Record<string, string> = {
  ads_platform: "Платит за рекламу",
  crm_widget: "CRM на сайте",
  inbound_widget: "Виджет входящих",
  service_catalog: "Каталог услуг с ценами",
  vacancy_sales: "Ищет продажников",
  vacancy_stale: "Вакансия висит давно",
};
```

Стало:

```ts
export const SIGNAL_LABELS: Record<string, string> = {
  ads_platform: "Платит за рекламу",
  crm_widget: "CRM на сайте",
  inbound_widget: "Виджет входящих",
  service_catalog: "Каталог услуг с ценами",
};
```

- [ ] **Step 2: Проверить сборку фронтенда**

```bash
cd frontend
npm run build
```

Ожидается: сборка успешна. Если `SIGNAL_LABELS` используется с обязательным
индексом по неизвестному ключу — TypeScript укажет место; `Record<string, string>`
такого не требует, и правка должна пройти без последствий.

- [ ] **Step 3: Заменить пример угла в промпте системы 2**

Было (`writer/agent.py:36`):

```
- angle — короткий машинный тип (vacancy_sales, ads_platform, answer), а не
  описание сообщения фразой.
```

Стало:

```
- angle — короткий машинный тип (crm_widget, ads_platform, answer), а не
  описание сообщения фразой.
```

Было (`writer/schemas/outreach.py:32`):

```python
        "чем цепляем — тип сигнала из данных (vacancy_sales, ads_platform, ig_promo)"
```

Стало:

```python
        "чем цепляем — тип сигнала из данных (crm_widget, ads_platform, ig_promo)"
```

- [ ] **Step 4: Заменить тип в проверках системы 2**

В `writer/scripts/check.py` заменить `vacancy_sales` на `crm_widget` во всех
местах. Вместе с типом меняются цитата и ссылка — иначе фикстура будет описывать
вакансию, называя её CRM-виджетом.

Строка 41:

```python
    assert [s["type"] for s in lead["seed"]["signals"]] == ["crm_widget"], lead["seed"]
```

Строки 79-80:

```python
        db.execute("INSERT INTO signals VALUES (?, 'crm_widget', '2026-08-01', 3.0, ?, ?)",
                   (company_id, "виджет Bitrix24", "https://romashka.kz/"))
```

Строка 104:

```python
    seed = {"name": "Ромашка", "signals": [{"type": "crm_widget", "quote": "виджет Bitrix24"}]}
```

Строка 110:

```python
    message_id = thread_store.add_draft(db, "+77010000001", "Здравствуйте, ...", "crm_widget")
```

Строка 129:

```python
    assert thread_store.used_angles(db, "+77010000001") == ["crm_widget"], \
```

Строки 143-145:

```python
    good = Draft(text="Здравствуйте! Увидел на сайте виджет Bitrix24...",
                 angle="crm_widget", stop=False)
    assert good.angle == "crm_widget"
```

Строка 155:

```python
        Draft(text="а" * (MAX_CHARS + 1), angle="crm_widget", stop=False)
```

Строки 170-171 (порядок сигналов в `seed` важен — от него зависит порядок
`unused_angles` на строках 186-187):

```python
        "signals": [{"type": "crm_widget", "quote": "виджет Bitrix24"},
                    {"type": "ads_platform", "quote": "Google Ads"}],
```

Строка 174:

```python
        {"role": "outgoing", "text": "Первое сообщение", "angle": "crm_widget"},
```

Строки 186-187:

```python
    assert agent.unused_angles(seed, ["crm_widget"]) == ["ads_platform"]
    assert agent.unused_angles(seed, ["crm_widget", "ads_platform"]) == []
```

Отдельно: ассерт `check_prompt` на строке 181 проверяет, что цитата сигнала попала
в промпт:

```python
    assert "нужен менеджер по продажам" in text, "цитата сигнала потеряна"
```

Заменить на новую цитату:

```python
    assert "виджет Bitrix24" in text, "цитата сигнала потеряна"
```

- [ ] **Step 5: Прогнать проверки системы 2**

```bash
cd writer
uv run -m scripts.check
```

Ожидается: код выхода 0 на всех разделах (`schema`, `threads`, `leads`, `prompt`).

Раздел `leads` читает `collector/db/leads.db` — если он падает с жалобой на
отсутствующую таблицу, значит `leads_source.py` всё-таки трогает `vacancies`.
Это **условие остановки**: спека утверждает обратное, и расхождение надо
отчитать, а не чинить на ходу.

- [ ] **Step 6: Поправить самопроверку `fetch.py`**

Было (`collector/services/fetch.py`, функция `demo`):

```python
        _, sidecar_path = _paths("https://hh.kz/vacancies/AI_engineer")
        sidecar_path.write_text(
            json.dumps({"url": "https://hh.kz/vacancies/AI_engineer",
                        "final_url": "https://hh.kz/vacancies",
                        "status": 200, "fetched_at": "2026-08-12T09:14:03Z"}),
            encoding="utf-8",
        )
        assert final_url("https://hh.kz/vacancies/AI_engineer") == "https://hh.kz/vacancies", \
            "сайдкар перестал отдавать конечный адрес — проверка подмены слепа"
        assert final_url("https://hh.kz/vacancies/no_such_page") is None
```

Стало:

```python
        _, sidecar_path = _paths("https://2gis.kz/almaty/rubric/653/page/7")
        sidecar_path.write_text(
            json.dumps({"url": "https://2gis.kz/almaty/rubric/653/page/7",
                        "final_url": "https://2gis.kz/almaty/rubric/653",
                        "status": 200, "fetched_at": "2026-08-12T09:14:03Z"}),
            encoding="utf-8",
        )
        assert final_url("https://2gis.kz/almaty/rubric/653/page/7") == "https://2gis.kz/almaty/rubric/653", \
            "сайдкар перестал отдавать конечный адрес — проверка подмены страницы слепа"
        assert final_url("https://2gis.kz/almaty/rubric/653/page/99") is None
```

Смысл теста не меняется: он про то, что сайдкар отдаёт конечный адрес после
редиректа. Адрес теперь берётся у источника, который остался в проекте, а текст
ассерта ссылается на живую проверку `check_pagination_guard`, а не на удалённую
`check_slug_guard`.

- [ ] **Step 7: Запустить самопроверку и весь набор**

```bash
cd collector
uv run python -c "from services import fetch; fetch.demo()"
uv run -m scripts.check
```

Ожидается: обе команды с кодом выхода 0.

- [ ] **Step 8: Коммит**

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test
git add frontend/app/api.ts writer/agent.py writer/schemas/outreach.py writer/scripts/check.py collector/services/fetch.py
git commit -m "refactor: убрать тип сигнала vacancy_sales из фронтенда и системы 2"
```

---

## Task 5: документация

Документы делятся на два вида, и обращаться с ними надо по-разному. `CLAUDE.md` и
`docs/TRD.md` описывают систему **сейчас** — они обязаны быть правдой, их правим.
`docs/ARCHITECTURE.md`, `docs/ARCHITECTURE_v2.md`, `docs/SPEC.md`, `docs/PRD.md` —
датированные записи о том, что было измерено и решено на конкретное число; их не
переписываем, а помечаем.

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/TRD.md`
- Modify: `docs/ARCHITECTURE.md`, `docs/ARCHITECTURE_v2.md`, `docs/SPEC.md`, `docs/PRD.md` — по одной врезке
- Modify: `plans/README.md`

- [ ] **Step 1: Завести секцию «Отклонённые источники» в `docs/TRD.md`**

Добавить новую секцию (перед §8 «Открытые технические вопросы»):

```markdown
## 7a. Отклонённые источники

### hh.kz — отклонён 18 августа 2026

Собран, измерен и удалён из конвейера. Замер на полном сборе:

| Величина | Значение |
|---|---|
| страниц hh забрано в `raw/` | 458 |
| вакансий разобрано | 384 |
| уникальных работодателей | 321 |
| вакансий привязано к компании | 4 |
| работодателей, достижимых при любом смягчении сравнения имён | 15 (~5%) |
| сигналов `vacancy_sales` в базе | 0 |
| сигналов `vacancy_stale` в базе | 0 |

Причина структурная, а не настроечная. Вакансии собираются с общегородских
SEO-страниц профессий, то есть по всем, кто нанимает в городе: топ работодателей
— Andersen, BI Group, Itransition, «Этажи». Компании собираются по 36 узким
рубрикам B2B-услуг 2GIS. Это две почти непересекающиеся популяции, и никакая
настройка сравнения имён их не сведёт — 5% остаётся потолком и после точного
пути `/employer/{id}` → сайт → домен.

Сырьё в `data/raw/` не удалено: 458 страниц лежат на диске, и решение обратимо
без нового похода в сеть.

**Что нужно, чтобы вернуть источник:** способ спрашивать hh о вакансиях
конкретной компании из базы, а не о вакансиях города. Пока такого способа нет
(см. закрытый вопрос №2 ниже), возвращать нечего.
```

- [ ] **Step 2: Закрыть открытые вопросы №1 и №2 в `docs/TRD.md` §8**

Обе строки таблицы (строки 191-192) — про hh. Заменить их содержимое на закрытые,
сохранив нумерацию:

```markdown
| 1 | ~~Склейка работодателя hh с компанией 2GIS~~ | **Закрыт 18.08.2026: источник отклонён, см. §7a** |
| 2 | ~~Автоматическое получение slug hh из фразы~~ | **Закрыт 18.08.2026: источник отклонён, см. §7a** |
```

- [ ] **Step 3: Пометить остальные упоминания hh в `docs/TRD.md`**

В §2 (таблица ловушек, строка 41), §2.3 «hh.kz» (строка 59), список разведчиков
(строка 87), строка `S1 SEED` (строка 125), таблица приёмки (строка 165), команда
запуска `hh_vacancies.py` (строка 178) — добавить в начало §2.3 врезку:

```markdown
> **Отклонён 18 августа 2026.** Всё в этом разделе верно как запись о том, что
> было проверено живыми запросами, но источник в конвейере не используется:
> `hh_vacancies.py`, сбор, разбор и таблица `vacancies` удалены. Причина и числа
> — §7a.
```

Сами факты в §2.3 не править: они остаются честной записью о том, что hh
технически доступен. Отклонён он не по технической причине.

- [ ] **Step 4: Актуализировать `CLAUDE.md`**

Строка 82, секция «Архитектура collector/»:

Было:

```
**`config.toml` — единственное место конфигурации.** Рубрики 2GIS,
города, slug'и hh, веса скоринга, LLM-модель.
```

Стало:

```
**`config.toml` — единственное место конфигурации.** Рубрики 2GIS,
города, веса скоринга, LLM-модель.
```

Проверить остальные упоминания и поправить, если hh назван действующим источником:

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test
grep -n -i -e 'hh' -e вакан CLAUDE.md
```

Строку 66 («страницы удаляются, вакансии закрываются» — про природу `raw/`)
**оставить**: она объясняет, почему `raw/` невосстановимо, и остаётся верной.

Добавить в секцию «Архитектура collector/» абзац:

```
**Источников сигналов два: сайт компании и лента инстаграма.** hh.kz был третьим
и отклонён 18 августа 2026 — измеренное пересечение работодателей hh с ICP из
2GIS около 5%, сигналов в базе ноль. Числа и обоснование — `docs/TRD.md` §7a.
Сырьё осталось в `data/raw/`, решение обратимо; заново подключать источник без
способа спрашивать hh о конкретной компании — не надо.
```

- [ ] **Step 5: Пометить датированные документы**

В `docs/ARCHITECTURE.md`, `docs/ARCHITECTURE_v2.md`, `docs/SPEC.md`, `docs/PRD.md`
добавить одну врезку сразу после заголовка первого уровня:

```markdown
> **Правка 18 августа 2026.** Источник hh.kz отклонён и удалён из конвейера.
> Всё, что сказано ниже про вакансии hh, — запись о том, как система была
> задумана и измерена на дату документа. Действующее решение и числа —
> `docs/TRD.md` §7a.
```

Содержимое этих документов **не переписывать**. Они датированы и служат записью
проектных решений; вычищать из них отклонённый источник значит стирать причину,
по которой он вообще появился.

- [ ] **Step 6: Закрыть план 002**

В `plans/README.md` в таблице статусов перевести строку плана 002 в `REJECTED` и
добавить обоснование одной строкой под таблицей:

```markdown
002 закрыт как `REJECTED` 18 августа 2026: замер варианта D не понадобился,
владелец продукта выбрал удаление ветки. Числа перенесены в `docs/TRD.md` §7a,
работа выполнена по `docs/superpowers/plans/2026-08-18-remove-hh-source.md`.
```

Абзац раздела «Зависимости» про то, что «002 требует 001» и про
`LINKED_VACANCIES_FLOOR`, дополнить одной строкой: ассерт удалён вместе с веткой
(задача 2 этого плана).

- [ ] **Step 7: Финальная приёмка целиком**

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test/collector
uv run build.py && uv run report.py 30
shasum -a 256 data/leads.csv
sqlite3 db/leads.db "select count(*) from signals;"
ls data/raw | wc -l
sqlite3 db/leads.db "select name from sqlite_master where name='vacancies';"
uv run -m scripts.check
uv run -m scripts.check parsers
cd ../writer && uv run -m scripts.check
cd ../frontend && npm run build
cd .. && grep -rn -e vacanc -e 'hh\.kz' collector/ --include='*.py'
```

Ожидается:

1. `sha256` = `fa96f54ede4cbe07faa06f62275fb7171268ba22932955600cb7486fe115f093`
2. `signals` = 1876
3. файлов в `raw/` = 7834
4. запрос про `vacancies` — пустой вывод
5. `scripts.check` — код 0 на всех десяти разделах
6. `scripts.check parsers` — код 0 (работает без базы и без `raw/`)
7. `writer` `scripts.check` — код 0
8. `npm run build` — успешно
9. `grep` — ровно одно совпадение, `collector/scripts/check.py`, условие размера в `check_raw`

- [ ] **Step 8: Коммит**

```bash
cd /Users/nurma/vscode_projects/Scrapling-Test
git add CLAUDE.md docs/TRD.md docs/ARCHITECTURE.md docs/ARCHITECTURE_v2.md docs/SPEC.md docs/PRD.md plans/README.md
git commit -m "docs: hh.kz отклонён как источник, секция отклонённых источников в TRD"
```

---

## Условия остановки

Остановиться и отчитаться, не импровизируя, если:

- `sha256` файла `data/leads.csv` разошёлся с эталоном. Сигналов `vacancy_*` в базе нет, выдача меняться не может — расхождение значит, что задет чужой код;
- `count(*) FROM signals` отличается от 1876 по той же причине;
- в `data/raw/` пропал хоть один файл (было 7834);
- `writer/scripts/check.py` раздел `leads` падает на отсутствующей таблице `vacancies` — значит `leads_source.py` её читает, а спека утверждает обратное;
- финальный `grep` даёт совпадения помимо `check_raw` — карта удаления неполна, дорезать по ходу нельзя;
- обнаружился потребитель `vacancies`, не перечисленный в структуре файлов выше.
