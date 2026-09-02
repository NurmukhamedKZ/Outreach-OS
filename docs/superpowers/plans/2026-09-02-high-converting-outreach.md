# План реализации: высококонвертирующая холодная переписка

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать первое сообщение конкретным (свежий повод с цитатой, имя ЛПР), диалог — этапным, оффер — тестируемым, а результат — измеримым воронкой на `/analytics`.

**Architecture:** Правки идут по слоям владения. Система 1 отдаёт то, что уже собрала (`decision_maker`), и затухает сигналы быстрее. Система 2 решает, о чём писать (`[signals] pitchable`), в каком этапе (`threads.stage`, переход зажат кодом) и каким оффером (`[[offer.variant]]`, версия пишется на сообщение). Новый модуль верхнего уровня `backend/analytics.py` читает обе базы и считает воронку по тредам.

**Tech Stack:** Python 3.12 (uv, один venv на `backend/`), SQLite, pydantic + langchain/OpenRouter, pytest; фронтенд — Next.js App Router, `@phosphor-icons/react`.

**Spec:** `docs/superpowers/specs/2026-09-02-high-converting-outreach-design.md`

## Global Constraints

- Тесты гоняются из `backend/`: `uv run pytest writer/tests/` и `uv run pytest collector/tests/`. Сети в тестах нет.
- `state.db` — невосстановимый слой: только `CREATE TABLE IF NOT EXISTS` и `ALTER TABLE ADD COLUMN`, `DROP` запрещён.
- Колонки таблицы доливает её владелец: `threads`/`messages` — `writer/db/thread_store.py`, `outbox`/`numbers` — `sender/db/migrate.py`. Чужую колонку не создают, а терпят её отсутствие.
- `writer.*` не импортирует `collector.*` — только через шов в `collector/api.py`.
- Тип-хинты на каждой сигнатуре, `is None` вместо `== None`, `pathlib.Path` вместо строк, никаких изменяемых значений по умолчанию.
- Комментарии объясняют «почему», а не «что». Русский язык в комментариях, логах и UI.
- Модули верхнего уровня `backend/*.py` тестируются в `collector/tests/` (прецедент: `test_activity.py`, `test_observability.py`).
- Каждая задача заканчивается коммитом с `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

---

### Task 1: Поводы отделены от сигналов, повод обязан быть свежим

**Files:**
- Modify: `backend/writer/config.toml`
- Modify: `backend/writer/db/leads_source.py:140-147` (`signals_of`), `:44-79` (`candidates`)
- Modify: `backend/writer/services/operations.py:19-27`, `backend/writer/routes/threads.py:216`
- Modify: `backend/collector/config.toml` (`half_life_days`)
- Test: `backend/writer/tests/test_leads.py`

**Interfaces:**
- Consumes: ничего.
- Produces: `leads_source.PitchRules(pitchable: frozenset[str], max_age_days: int, today: date)`, `leads_source.pitch_rules(config: dict, today: date | None = None) -> PitchRules`, изменённые `signals_of(db, company_id: str, rules: PitchRules) -> list[dict]` и `candidates(db, rules: PitchRules, limit: int | None = None) -> list[dict]`.

- [ ] **Step 1: Написать падающий тест**

Дописать в `backend/writer/tests/test_leads.py`. Хелпер `_seed` кладёт всем компаниям сигнал `crm_widget` от `2026-08-01` — добавляем к нему три новых сигнала у `c_ok`:

```python
from datetime import date

from writer.db.leads_source import PitchRules

RULES = PitchRules(
    pitchable=frozenset({"reviews_unanswered_complaint", "site_hiring_sales"}),
    max_age_days=60,
    today=date(2026, 9, 2),
)


def _extra_signals(db, run_id=1):
    """Три случая рядом с уже лежащим crm_widget: годный, старый, безцитатный."""
    rows = (
        ("reviews_unanswered_complaint", "2026-08-25", "второй раз не дозвонился", "https://2gis.kz/1"),
        ("site_hiring_sales", "2026-05-01", "ищем менеджера по продажам", "https://romashka.kz/jobs"),
        ("reviews_missed_lead", "2026-08-30", None, "https://2gis.kz/2"),
    )
    for kind, observed_at, quote, url in rows:
        db.execute("INSERT INTO signals_all (run_id, company_id, type, observed_at, weight,"
                   " quote, url) VALUES (?, 'c_ok', ?, ?, 4.0, ?, ?)",
                   (run_id, kind, observed_at, quote, url))
    db.commit()


def test_pitchable_fresh_and_quoted_signals_only(leads_db):
    db = leads_db
    _seed(db)
    _extra_signals(db)

    signals = leads_source.signals_of(db, "c_ok", RULES)
    kinds = [signal["type"] for signal in signals]

    assert kinds == ["reviews_unanswered_complaint"], (
        "в поводы просочилось лишнее: crm_widget — не повод, site_hiring_sales старше"
        f" 60 дней, reviews_missed_lead без цитаты. Получено: {kinds}"
    )


def test_company_without_pitchable_signals_stays_in_candidates(leads_db):
    db = leads_db
    _seed(db)

    found = leads_source.candidates(db, RULES)
    without_pitch = [lead for lead in found if lead["company_id"] == "c_phone"]

    assert without_pitch, "лид без поводов исчез из выдачи вместо stop=true у агента"
    assert without_pitch[0]["seed"]["signals"] == [], "непригодный сигнал попал в seed"
```

- [ ] **Step 2: Убедиться, что тест падает**

Запустить: `cd backend && uv run pytest writer/tests/test_leads.py -v`
Ожидание: FAIL — `AttributeError: module 'writer.db.leads_source' has no attribute 'PitchRules'`.

- [ ] **Step 3: Добавить секцию `[signals]` в `backend/writer/config.toml`**

```toml
[signals]
# Типы, которые годятся как повод для письма. Остальные сигналы продолжают
# влиять на intent_score (компания с CRM и отделом продаж — лучший лид из
# возможных), но в промпт не идут: «у вас на сайте виджет CRM» — не боль.
# Что считать сигналом, решает система 1; о чём писать — система 2, поэтому
# список живёт здесь, а не в collector/config.toml.
pitchable = [
  "reviews_unanswered_complaint",
  "reviews_missed_lead",
  "ig_unanswered_question",
  "site_hiring_sales",
  "ig_reach_declining",
  "ig_dormant",
  "site_no_pricing",
]
# Наблюдение старше этого в промпт не попадает вовсе. Не затухание, а отсечка:
# «увидел, что вы ищете продажника» про вакансию трёхмесячной давности убивает
# доверие одним предложением.
max_age_days = 60
```

- [ ] **Step 4: Реализовать фильтр в `backend/writer/db/leads_source.py`**

Дописать импорты (`from dataclasses import dataclass`, `from datetime import date`) и заменить `signals_of`:

```python
@dataclass(frozen=True)
class PitchRules:
    """Чем разрешено цеплять. Три параметра одного решения — «годится ли этот
    сигнал как повод», поэтому едут вместе, а не тремя аргументами."""
    pitchable: frozenset[str]
    max_age_days: int
    today: date


def pitch_rules(config: dict, today: date | None = None) -> PitchRules:
    return PitchRules(
        pitchable=frozenset(config["signals"]["pitchable"]),
        max_age_days=config["signals"]["max_age_days"],
        today=today or date.today(),
    )


def signals_of(db, company_id: str, rules: PitchRules) -> list[dict]:
    """Поводы для письма: только pitchable-типы, только с цитатой, только свежие.

    Сигнал без цитаты — не наблюдение, а догадка: процитировать его в письме
    нечем, а письмо без цитаты и есть тот шаблон, ради отсутствия которого
    написана система 2.
    """
    rows = db.execute(
        "SELECT type, quote, url, observed_at FROM signals WHERE company_id = ?"
        " ORDER BY weight DESC, observed_at DESC, type",
        (company_id,),
    )
    return [
        {"type": kind, "quote": quote, "url": url, "observed_at": observed_at}
        for kind, quote, url, observed_at in rows
        if kind in rules.pitchable and quote and _fresh(observed_at, rules)
    ]


def _fresh(observed_at: str | None, rules: PitchRules) -> bool:
    """Нечитаемая дата считается свежей: сигнал теряется молча только когда мы
    точно знаем, что он стар."""
    if not observed_at:
        return True
    try:
        observed = date.fromisoformat(observed_at[:10])
    except ValueError:
        return True
    return (rules.today - observed).days <= rules.max_age_days
```

В `candidates` заменить сигнатуру на `def candidates(db, rules: PitchRules, limit: int | None = None) -> list[dict]:` и вызов на `signals_of(db, company_id, rules)`.

- [ ] **Step 5: Протянуть правила через вызывающих**

`backend/writer/services/operations.py` — в `open_new_threads` перед циклом:

```python
        rules = leads_source.pitch_rules(CONFIG)
        fresh = [lead for lead in leads_source.candidates(leads, rules)
                 if not thread_store.thread(threads, lead["thread_id"])][:limit]
```

`backend/writer/routes/threads.py:216` (в `demo()`) — `leads_source.candidates(leads, leads_source.pitch_rules(CONFIG), 1)`.

- [ ] **Step 6: Ускорить затухание в `backend/collector/config.toml`**

```toml
[scoring]
# Затухание intent_score: вклад сигнала = weight * exp(-ln2 * days / half_life_days).
# Тридцать, а не девяносто: intent_score отвечает на вопрос «нужна ли им
# лидогенерация СЕЙЧАС», и трёхмесячный сигнал на этот вопрос не отвечает.
# Отбор поводов для письма живёт отдельно — writer/config.toml [signals].
half_life_days = 30
```

- [ ] **Step 7: Починить существующие тесты, которые проверяли старое поведение**

Новая сигнатура ломает три места, и одно из них требует осмысленной переписи, а не подгонки.

`backend/writer/tests/test_leads.py:62` и `:93` — добавить `RULES` вторым аргументом: `leads_source.candidates(db, RULES, limit=10)` и `leads_source.candidates(db, RULES)`.

`backend/writer/tests/test_leads.py:77` утверждает ровно то поведение, которое эта задача отменяет:

```python
    assert [s["type"] for s in lead["seed"]["signals"]] == ["crm_widget"], lead["seed"]
```

Заменить, объяснив почему:

```python
    # crm_widget остаётся сигналом и продолжает поднимать лид в очереди по
    # intent_score, но поводом для письма не является: «у вас на сайте виджет
    # CRM» — не боль, и цитировать в первом касании нечего.
    assert [s["type"] for s in lead["seed"]["signals"]] == [], lead["seed"]
```

`backend/writer/tests/test_operations.py:80` — фейковый `candidates` в `monkeypatch.setattr` должен принимать новую сигнатуру: `lambda db, rules, limit=None: ...`.

- [ ] **Step 8: Прогнать тесты**

Запустить: `cd backend && uv run pytest writer/tests/ collector/tests/ -q`
Ожидание: PASS. Если падает тест скоринга с зашитым числом — пересчитать ожидание под `half_life_days = 30`, не возвращая 90.

- [ ] **Step 9: Коммит**

```bash
git add backend/writer/config.toml backend/writer/db/leads_source.py \
        backend/writer/services/operations.py backend/writer/routes/threads.py \
        backend/collector/config.toml backend/writer/tests/test_leads.py
git commit -m "feat(writer): повод для письма — только свежий сигнал с цитатой"
```

---

### Task 2: Имя ЛПР доезжает до промпта

**Files:**
- Modify: `backend/collector/schemas/dossier.py:27`
- Modify: `backend/writer/db/leads_source.py:25-32` (`CANDIDATES`), `:64-78` (сборка `seed`)
- Modify: `backend/writer/services/agent.py:158-180` (`prompt`)
- Test: `backend/writer/tests/test_leads.py`, `backend/writer/tests/test_prompt.py`

**Interfaces:**
- Consumes: `PitchRules`, `pitch_rules` из Task 1.
- Produces: `seed["dossier"]["decision_maker"]: str | None`; строка `Кто решает: …` в промпте.

- [ ] **Step 1: Написать падающие тесты**

В `backend/writer/tests/test_leads.py`:

```python
def test_seed_carries_decision_maker(leads_db):
    db = leads_db
    _seed(db)
    db.execute("UPDATE dossiers_all SET decision_maker = 'Айгуль, основатель'"
               " WHERE company_id = 'c_ok'")
    db.commit()

    lead = next(l for l in leads_source.candidates(db, RULES) if l["company_id"] == "c_ok")

    assert lead["seed"]["dossier"]["decision_maker"] == "Айгуль, основатель", \
        "имя ЛПР собрано системой 1, но до системы 2 не доехало"
```

В `backend/writer/tests/test_prompt.py`:

```python
def test_prompt_names_decision_maker_when_known():
    seed = {
        "name": "Ромашка", "city": "almaty",
        "dossier": {"summary": "бухгалтерия", "hooks": [], "pains": [],
                    "approach": "заходить через рост", "sources": [],
                    "decision_maker": "Айгуль, основатель"},
        "signals": [],
    }
    assert "Кто решает: Айгуль, основатель" in agent.prompt(seed, [], TASK)


def test_prompt_stays_silent_about_unknown_decision_maker():
    seed = {
        "name": "Ромашка", "city": "almaty",
        "dossier": {"summary": "бухгалтерия", "hooks": [], "pains": [],
                    "approach": "заходить через рост", "sources": [],
                    "decision_maker": None},
        "signals": [],
    }
    assert "Кто решает" not in agent.prompt(seed, [], TASK), \
        "пустая строка про ЛПР — приглашение модели выдумать имя"
```

- [ ] **Step 2: Убедиться, что тесты падают**

Запустить: `cd backend && uv run pytest writer/tests/test_leads.py::test_seed_carries_decision_maker writer/tests/test_prompt.py -v`
Ожидание: FAIL — `KeyError: 'decision_maker'` и `assert ... in` не проходит.

- [ ] **Step 3: Дать полю внятное описание**

`backend/collector/schemas/dossier.py`:

```python
    decision_maker_hint: str | None = Field(None, description=(
        "имя и роль человека, принимающего решение, если они названы прямо в"
        " данных: страница «Команда», био инстаграма, подпись под ответом на"
        " отзыв. Формат «Айгуль, основатель». Не выдумывать и не выводить из"
        " названия компании — не названо, значит null"
    ))
```

- [ ] **Step 4: Протащить поле через запрос и seed**

`backend/writer/db/leads_source.py` — в `CANDIDATES` после `d.summary` добавить `d.decision_maker`, расширить распаковку цикла в `candidates` и положить в досье:

```python
                "dossier": {
                    "summary": summary,
                    "decision_maker": decision_maker,
                    "hooks": json.loads(hooks or "[]"),
                    ...
```

- [ ] **Step 5: Показать имя агенту**

`backend/writer/services/agent.py`, в `prompt()` сразу после `summary`:

```python
    if dossier.get("decision_maker"):
        parts.append(f"Кто решает: {dossier['decision_maker']}")
```

- [ ] **Step 6: Прогнать тесты**

Запустить: `cd backend && uv run pytest writer/tests/ -q`
Ожидание: PASS.

- [ ] **Step 7: Коммит**

```bash
git add backend/collector/schemas/dossier.py backend/writer/db/leads_source.py \
        backend/writer/services/agent.py backend/writer/tests/
git commit -m "feat(writer): имя ЛПР из досье доезжает до промпта письма"
```

Правка описания меняет промпт `dossier` и обесценивает кэш `kind="dossier"` — досье пересчитается по базе один раз. Кэш `reviews`/`site`/`instagram` не затронут.

---

### Task 3: Этап диалога как состояние треда

**Files:**
- Create: `backend/writer/services/stages.py`
- Modify: `backend/writer/db/thread_store.py:17-59` (SCHEMA, `_ensure_prompt_columns` → `_ensure_columns`)
- Test: `backend/writer/tests/test_threads.py`

**Interfaces:**
- Consumes: ничего.
- Produces: `stages.STAGES: tuple[str, ...]`, `stages.FIRST: str`, `stages.stage_after(current: str, proposed: str) -> str`, `stages.advance(stage: str) -> str`, `stages.rules_for(stage: str) -> str`; `thread_store.set_stage(db, thread_id: str, stage: str) -> None`, `thread["stage"]` в результате `thread_store.thread`.

- [ ] **Step 1: Написать падающий тест**

Создать `backend/writer/tests/test_stages.py`:

```python
"""Этап переписки двигается на один шаг и только вперёд.

Правило «нельзя перескакивать этапы» живёт в коде, а не в промпте, по той же
причине, что запрет пустых follow-up живёт в схеме: инструкцию модель нарушает
ровно там, где это дороже всего — на вопросе «сколько стоит», где она прыгает
в closing и называет цену.
"""

import pytest

from writer.db import thread_store
from writer.services import stages


def test_stage_moves_one_step_forward():
    assert stages.stage_after("contact", "probing") == "probing"
    assert stages.stage_after("contact", "contact") == "contact"


def test_stage_never_skips_and_never_goes_back():
    assert stages.stage_after("contact", "closing") == "contact", "перескок через два этапа"
    assert stages.stage_after("offer", "contact") == "offer", "откат назад"


def test_unknown_stage_leaves_thread_where_it_was():
    assert stages.stage_after("probing", "переговоры") == "probing"
    assert stages.stage_after("probing", "") == "probing"


def test_each_reply_moves_the_thread_one_step():
    assert stages.advance("contact") == "probing"
    assert stages.advance("probing") == "offer"
    assert stages.advance("offer") == "closing"


def test_last_stage_is_a_dead_end_not_an_error():
    assert stages.advance("closing") == "closing"
    assert stages.advance("что-то своё") == stages.FIRST


def test_rules_differ_by_stage():
    assert stages.rules_for("contact") != stages.rules_for("closing")
    assert "созвон" not in stages.rules_for("contact").lower(), \
        "в первом касании созвон предлагать нельзя"


def test_new_thread_starts_in_contact():
    db = thread_store.connect(":memory:")
    thread_store.open_thread(db, "+77010000001", "c_ok", {"name": "Ромашка"})

    assert thread_store.thread(db, "+77010000001")["stage"] == stages.FIRST


def test_stage_survives_write():
    db = thread_store.connect(":memory:")
    thread_store.open_thread(db, "+77010000001", "c_ok", {"name": "Ромашка"})

    thread_store.set_stage(db, "+77010000001", "probing")

    assert thread_store.thread(db, "+77010000001")["stage"] == "probing"
```

- [ ] **Step 2: Убедиться, что тест падает**

Запустить: `cd backend && uv run pytest writer/tests/test_stages.py -v`
Ожидание: FAIL — `ModuleNotFoundError: No module named 'writer.services.stages'`.

- [ ] **Step 3: Создать `backend/writer/services/stages.py`**

```python
"""Четыре этапа холодного диалога и правила каждого.

Этап — состояние треда, а не пожелание в промпте: модель, увидев вопрос
«сколько стоит», охотно прыгает сразу в closing и называет цену, которой в
оффере нет. Переход зажимает stage_after, а не инструкция.

Правила читает и agent (холодное касание), и seller (ответ в диалоге):
два промпта на одну ситуацию дали бы две калибровки.
"""

STAGES: tuple[str, ...] = ("contact", "probing", "offer", "closing")
FIRST: str = STAGES[0]

RULES: dict[str, str] = {
    "contact": (
        "Этап — первый контакт. Задача одна: втянуть в тематический разговор.\n"
        "Структура сообщения: обращение по имени (нет имени — просьба передать"
        " тому, кто отвечает за продажи) -> наблюдение с дословной цитатой из"
        " данных -> чем это им обходится -> один короткий вопрос.\n"
        "Запрещено: продавать, называть услугу, звать на разговор, упоминать"
        " цену и сроки."
    ),
    "probing": (
        "Этап — разговорить. Задача: понять, как они ищут клиентов сейчас.\n"
        "Задай один вопрос про их процесс, опираясь на то, что они уже"
        " ответили.\n"
        "Запрещено: делать предложение, называть цену."
    ),
    "offer": (
        "Этап — предложение. Назови оффер своими словами и сними ровно одно"
        " возражение, которое собеседник высказал.\n"
        "Запрещено: назначать время, придумывать цифры, которых нет в оффере."
    ),
    "closing": (
        "Этап — договориться о разговоре. Предложи конкретное окно и спроси,"
        " удобно ли.\n"
        "Запрещено: новые аргументы и новые темы — решение уже принято."
    ),
}


def stage_after(current: str, proposed: str) -> str:
    """Текущий этап или следующий, ничего больше.

    Неизвестное значение — не авария, а вход для предохранителя: решение
    принимает `if`, а не traceback (тот же принцип, что seller.UNKNOWN).
    """
    if current not in STAGES:
        return FIRST
    if proposed not in STAGES:
        return current
    step = STAGES.index(proposed) - STAGES.index(current)
    return proposed if step in (0, 1) else current


def advance(stage: str) -> str:
    """Следующий этап; из последнего — он же.

    Каждый ответ лида двигает разговор на шаг: молчание не приближает сделку, а
    ответ приближает. Кто именно двигает — важно: не модель своим мнением о
    неотправленном черновике, а факт входящего сообщения.
    """
    if stage not in STAGES:
        return FIRST
    return STAGES[min(STAGES.index(stage) + 1, len(STAGES) - 1)]


def rules_for(stage: str) -> str:
    return RULES.get(stage, RULES[FIRST])
```

- [ ] **Step 4: Долить колонку в `backend/writer/db/thread_store.py`**

В `SCHEMA`, в `CREATE TABLE IF NOT EXISTS threads`, после `seed`:

```sql
  stage      TEXT NOT NULL DEFAULT 'contact',  -- contact | probing | offer | closing
```

Переименовать `_ensure_prompt_columns` в `_ensure_columns` (имя обязано описывать то, что функция делает) и дополнить:

```python
def _ensure_columns(db):
    """CREATE TABLE IF NOT EXISTS не трогает существующую таблицу, а базы
    переписки у всех давно созданы. Колонки владельца доливает владелец:
    полагаться на то, что до него добежит migrate системы 3, значит уронить
    writer везде, где система 3 не стартовала."""
    for table, columns in (
        ("messages", (("prompt", "TEXT"), ("model", "TEXT"))),
        ("threads", (("stage", "TEXT NOT NULL DEFAULT 'contact'"),)),
    ):
        existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for column, definition in columns:
            if column not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    db.commit()
```

Обновить вызов в `connect`. В `thread()` добавить `stage` в `SELECT` и в возвращаемый словарь. Добавить:

```python
def set_stage(db, thread_id: str, stage: str) -> None:
    db.execute("UPDATE threads SET stage = ? WHERE thread_id = ?", (stage, thread_id))
    db.commit()
```

- [ ] **Step 5: Прогнать тесты**

Запустить: `cd backend && uv run pytest writer/tests/ -q`
Ожидание: PASS, включая старые тесты `test_threads.py`.

- [ ] **Step 6: Коммит**

```bash
git add backend/writer/services/stages.py backend/writer/db/thread_store.py \
        backend/writer/tests/test_stages.py
git commit -m "feat(writer): этап диалога — колонка треда, перескок запрещён кодом"
```

---

### Task 4: Промпт по этапам и нормализованный `angle`

**Files:**
- Modify: `backend/writer/schemas/outreach.py`
- Modify: `backend/writer/services/agent.py:47-70` (`SYSTEM`), `:120-140` (`draft`)
- Modify: `backend/writer/services/operations.py`
- Test: `backend/writer/tests/test_prompt.py`

**Interfaces:**
- Consumes: `stages.rules_for`, `stages.STAGES`, `stages.FIRST` (Task 3); `PitchRules` (Task 1).
- Produces: `agent.system_prompt(offer: str, stage: str) -> str`; `agent.normalize_angle(angle: str, pitchable: frozenset[str]) -> str`; `agent.OTHER_ANGLE: str`; `agent.draft(..., stage: str, pitchable: frozenset[str])` — оба параметра именованные.

**Схема остаётся без новых полей и без зависимостей.** Этап треда двигает ответ лида (Task 6), а не мнение модели о неотправленном черновике, поэтому `next_stage` не нужен никому. `angle` схемой тоже не валидируется: список поводов приходит из конфига, и загонять его в `writer/schemas/` пришлось бы скрытым глобальным состоянием, после которого один и тот же `Draft` проходит или падает в зависимости от того, кто его создал. Угол нормализуется там, где список поводов и так есть, — в `draft()`.

- [ ] **Step 1: Написать падающие тесты**

В `backend/writer/tests/test_prompt.py`:

```python
def test_unknown_angle_collapses_into_other():
    """Модель вернула описание фразой вместо типа сигнала. Ронять из-за этого
    готовый черновик незачем, но и в разрез аналитики такой угол пускать
    нельзя: таблица by_angle наполнится вариациями одного и того же повода."""
    assert agent.normalize_angle("рассказал про отзывы",
                                 frozenset({"site_no_pricing"})) == agent.OTHER_ANGLE


def test_known_angle_and_answer_survive():
    pitchable = frozenset({"site_no_pricing"})

    assert agent.normalize_angle("site_no_pricing", pitchable) == "site_no_pricing"
    assert agent.normalize_angle("answer", pitchable) == "answer"
```

В `backend/writer/tests/test_prompt.py`:

```python
from writer.services import stages


def test_system_prompt_carries_rules_of_current_stage_only():
    system = agent.system_prompt(offer="оплата за встречу", stage="contact")

    assert stages.rules_for("contact") in system
    assert stages.rules_for("closing") not in system, \
        "правила чужого этапа в промпте — приглашение перескочить"
```

- [ ] **Step 2: Убедиться, что тесты падают**

Запустить: `cd backend && uv run pytest writer/tests/test_prompt.py -v`
Ожидание: FAIL — `agent` не знает ни `normalize_angle`, ни `system_prompt`.

- [ ] **Step 3: Расширить `backend/writer/schemas/outreach.py`**

Новых полей нет. `next_stage` в схему **не добавляется**: холодный агент пишет
в тред, где лид ещё молчит, — этап там объективно `contact`, что бы модель ни
вернула, а двигать состояние треда по мнению модели о неотправленном черновике
значит записать в историю то, чего не произошло. Этап двигает факт ответа лида
(Task 6).

Меняется только описание `angle` — модель видит список поводов в человеческом
сообщении, и от неё требуется машинный тип, а не фраза:

```python
    angle: str = Field(description=(
        "чем цепляем: ровно один тип сигнала из блока «Сигналы» выше или"
        " 'answer', если это ответ на реплику лида. Машинный тип, не описание"
        " сообщения фразой"
    ))
```

- [ ] **Step 4: Пересобрать системный промпт блоками в `backend/writer/services/agent.py`**

Заменить константу `SYSTEM` на шаблон с блоками RIC+ECO и функцию:

```python
SYSTEM = """# Роль
Ты пишешь исходящие сообщения в WhatsApp от лица команды, которая предлагает:
{offer}

# Инструкции
- Одно сообщение — один конкретный факт об этой компании, взятый из данных ниже.
  Без факта сообщение не отправляется: пиши stop=true.
- Пиши так, как пишет человек в мессенджере: на «вы», без списков, без
  «Надеюсь, у вас всё хорошо», без слова «уникальный».
- Не выдумывай фактов о компании. Всё, чего нет в данных, не существует.
- Цены, сроки и любые цифры бери только из оффера выше. Их там нет — значит их
  нет и в сообщении: на вопрос о цене отвечай, что назовём после короткого
  разговора. Выдуманная цифра — это обещание, которое даёт живому человеку
  компания, а не модель.
- Каждое следующее сообщение несёт новый повод. Напоминание о предыдущем
  письме поводом не является.

# Ограничения этапа
{stage_rules}

# Формат
- 3-5 коротких предложений, заканчивай одним вопросом, на который легко
  ответить «да» или «нет».
- angle — машинный тип повода из данных, а не описание сообщения фразой."""


def system_prompt(offer: str, stage: str) -> str:
    return SYSTEM.format(offer=offer, stage_rules=stages.rules_for(stage))
```

В `draft()` заменить сборку `messages` и нормализовать угол у готового ответа:

```python
ANSWER_ANGLE = "answer"
OTHER_ANGLE = "other"


def normalize_angle(angle: str, pitchable: frozenset[str]) -> str:
    """Угол, которого нет среди поводов, схлопывается в один «other».

    Ронять готовый черновик из-за неудачного слова незачем, но и пускать
    свободную фразу в разрез by_angle нельзя: таблица наполнится вариациями
    одного повода — «отзывы», «жалоба в отзывах», «неотвеченный отзыв» — и
    перестанет отвечать на вопрос, какой повод работает.
    """
    return angle if angle in pitchable or angle == ANSWER_ANGLE else OTHER_ANGLE


def draft(llm, seed, history, task, *, session_id, name, offer="", model_name="",
          stage=stages.FIRST, pitchable=frozenset()):
    handler = observability.langfuse_handler()
    messages = [
        ("system", system_prompt(offer, stage)),
        ("human", prompt(seed, history, task)),
    ]
    ...  # существующий цикл ретраев без изменений, только успешная ветка:
            checked = result.model_copy(
                update={"angle": normalize_angle(result.angle, pitchable)})
            return Attempt(draft=checked, prompt=messages, model=model_name)
```

`model_copy`, а не присваивание в поле: ответ модели — улика, и править её на месте значит потерять то, что она на самом деле вернула, ещё до того, как это попадёт в Langfuse.

- [ ] **Step 5: Передать этап и углы из операции**

`backend/writer/services/operations.py` — в вызове `agent.draft` добавить `stage=stages.FIRST, pitchable=rules.pitchable`. Этап треда операция не двигает: первое сообщение уходит в тред, где лид ещё не отвечал, и `contact` — это правда о нём до самого ответа.

- [ ] **Step 6: Прогнать тесты**

Запустить: `cd backend && uv run pytest writer/tests/ -q`
Ожидание: PASS.

- [ ] **Step 7: Коммит**

```bash
git add backend/writer/schemas/outreach.py backend/writer/services/agent.py \
        backend/writer/services/operations.py backend/writer/tests/
git commit -m "feat(writer): системный промпт по RIC и правила текущего этапа"
```

---

### Task 5: Варианты оффера и запись версии на сообщении

**Files:**
- Create: `backend/writer/services/offers.py`
- Modify: `backend/writer/config.toml`, `backend/writer/db/thread_store.py` (`_ensure_columns`, `add_draft`)
- Modify: `backend/writer/services/operations.py`, `backend/writer/routes/threads.py:106-139`
- Test: `backend/writer/tests/test_offers.py`

**Interfaces:**
- Consumes: `thread_store.add_draft` (Task 3).
- Produces: `offers.variant_of(thread_id: str, config: dict) -> dict` (ключи `id`, `text`); `thread_store.add_draft(db, thread_id, text, angle, prompt=None, model=None, offer_variant=None)`.

- [ ] **Step 1: Написать падающий тест**

Создать `backend/writer/tests/test_offers.py`:

```python
"""Вариант оффера назначается треду навсегда и переживает перезапуск процесса."""

from writer.db import thread_store
from writer.services import offers

CONFIG = {"offer": {"text": "старый оффер", "variant": [
    {"id": "free_first_meetings", "text": "первые две встречи за наш счёт"},
    {"id": "pay_per_meeting", "text": "оплата за состоявшуюся встречу"},
]}}


def test_variant_is_stable_for_the_same_thread():
    first = offers.variant_of("+77010000001", CONFIG)
    again = offers.variant_of("+77010000001", CONFIG)

    assert first["id"] == again["id"], "тред сменил когорту между вызовами"


def test_variants_split_the_base():
    ids = {offers.variant_of(f"+7701000{n:04d}", CONFIG)["id"] for n in range(50)}

    assert ids == {"free_first_meetings", "pay_per_meeting"}, \
        f"когорты разъехались: {ids}"


def test_config_without_variants_falls_back_to_single_offer():
    variant = offers.variant_of("+77010000001", {"offer": {"text": "один оффер"}})

    assert variant == {"id": "", "text": "один оффер"}, \
        "треды, открытые до A/B, обязаны продолжать работать"


def test_draft_remembers_which_offer_it_argued():
    db = thread_store.connect(":memory:")
    thread_store.open_thread(db, "+77010000001", "c_ok", {"name": "Ромашка"})

    message_id = thread_store.add_draft(db, "+77010000001", "текст", "site_no_pricing",
                                        offer_variant="pay_per_meeting")

    stored = db.execute("SELECT offer_variant FROM messages WHERE message_id = ?",
                        (message_id,)).fetchone()[0]
    assert stored == "pay_per_meeting"
```

- [ ] **Step 2: Убедиться, что тест падает**

Запустить: `cd backend && uv run pytest writer/tests/test_offers.py -v`
Ожидание: FAIL — нет модуля `writer.services.offers`.

- [ ] **Step 3: Создать `backend/writer/services/offers.py`**

```python
"""Какой оффер аргументирует этот тред.

Формулировка оффера тестируется отдельно от текста письма и отдельно от ICP —
это три независимые переменные, и смешать их значит не узнать ничего ни об
одной.
"""

import hashlib

FALLBACK_ID = ""


def variants(config: dict) -> list[dict]:
    return config["offer"].get("variant") or []


def variant_of(thread_id: str, config: dict) -> dict:
    """Детерминированно и стабильно между процессами.

    random нельзя — тред не должен менять когорту между запусками. Встроенный
    hash() тоже нельзя: он рандомизирован PYTHONHASHSEED и после перезапуска
    раскидал бы те же треды иначе, тихо смешав когорты теста.
    """
    options = variants(config)
    if not options:
        return {"id": FALLBACK_ID, "text": config["offer"]["text"]}
    digest = hashlib.sha1(thread_id.encode("utf-8")).hexdigest()
    return options[int(digest, 16) % len(options)]
```

- [ ] **Step 4: Добавить варианты в `backend/writer/config.toml`**

```toml
[[offer.variant]]
id   = "free_first_meetings"
text = """
Находим компании, которым ваша услуга нужна прямо сейчас, пишем каждой лично и
доводим до встречи. Первые две встречи — за наш счёт: платите только за те, что
состоялись после них. Списков контактов не продаём, работаем по Казахстану.
"""

[[offer.variant]]
id   = "pay_per_meeting"
text = """
Находим компании, которым ваша услуга нужна прямо сейчас, пишем каждой лично и
доводим до встречи. Оплата только за состоявшуюся встречу — не за письма, не за
базу, не помесячно. Работаем по Казахстану.
"""
```

Ключ `[offer].text` не удалять: он — фоллбэк для тредов, открытых до A/B.

- [ ] **Step 5: Записать вариант на сообщение**

`backend/writer/db/thread_store.py` — в `_ensure_columns` в кортеж колонок `messages` добавить `("offer_variant", "TEXT")`, в `SCHEMA` — `offer_variant TEXT,`. В `add_draft` добавить параметр `offer_variant: str | None = None` и колонку в `INSERT`.

- [ ] **Step 6: Передать вариант из операции и роутера**

`backend/writer/services/operations.py` — внутри цикла:

```python
                variant = offers.variant_of(lead["thread_id"], CONFIG)
                proposal = agent.draft(llm, lead["seed"], [], agent.FIRST,
                                       session_id=lead["thread_id"], name="writer.first",
                                       offer=variant["text"], model_name=CONFIG["llm"]["model"],
                                       stage=stages.FIRST, pitchable=rules.pitchable)
                ...
                thread_store.add_draft(threads, lead["thread_id"], proposal.draft.text,
                                       proposal.draft.angle, prompt=proposal.prompt,
                                       model=proposal.model, offer_variant=variant["id"])
```

То же в `backend/writer/routes/threads.py::_writer_move` — оффер берётся `offers.variant_of(thread["thread_id"], CONFIG)`, а не `CONFIG["offer"]["text"]`.

- [ ] **Step 7: Прогнать тесты**

Запустить: `cd backend && uv run pytest writer/tests/ -q`
Ожидание: PASS.

- [ ] **Step 8: Коммит**

```bash
git add backend/writer/services/offers.py backend/writer/config.toml \
        backend/writer/db/thread_store.py backend/writer/services/operations.py \
        backend/writer/routes/threads.py backend/writer/tests/test_offers.py
git commit -m "feat(writer): варианты оффера и версия, записанная на сообщение"
```

---

### Task 6: Продавец знает этап и двигает его

**Files:**
- Modify: `backend/writer/services/seller.py:33-52` (`SELL_MANAGER`), `:113-125` (`prompt`)
- Modify: `backend/sender/services/incoming.py:47-83`
- Test: `backend/writer/tests/test_seller.py`

**Interfaces:**
- Consumes: `stages.rules_for`, `stages.stage_after` (Task 3), `thread_store.set_stage` (Task 3), `offers.variant_of` (Task 5).
- Produces: `seller.prompt(seed: dict, history: list[dict], stage: str) -> str`, `seller.respond(agent, seed, history, offer, *, session_id, stage)`.

Агент собирается один раз на процесс (`create_agent` компилирует граф), поэтому правила этапа едут в человеческом сообщении, а не в системном промпте: пересобирать агента на каждое входящее ради одного абзаца — дороже, чем передать абзац.

- [ ] **Step 1: Написать падающий тест**

В `backend/writer/tests/test_seller.py`:

```python
from writer.services import stages


def test_prompt_carries_rules_of_the_current_stage():
    seed = {"name": "Ромашка", "city": "almaty", "dossier": {}, "signals": []}

    text = seller.prompt(seed, [{"role": "incoming", "text": "а сколько стоит?"}], "probing")

    assert stages.rules_for("probing") in text
    assert stages.rules_for("closing") not in text, \
        "правила чужого этапа рядом с вопросом о цене — прямой путь в выдуманную цифру"
```

- [ ] **Step 2: Убедиться, что тест падает**

Запустить: `cd backend && uv run pytest writer/tests/test_seller.py -v`
Ожидание: FAIL — `prompt() takes 2 positional arguments but 3 were given`.

- [ ] **Step 3: Передать этап в промпт продавца**

`backend/writer/services/seller.py`:

```python
def prompt(seed: dict, history: list[dict], stage: str) -> str:
    """Карточка компании, диалог и ограничения текущего этапа.

    Правила этапа идут сюда, а не в системный промпт: агент собирается один раз
    на процесс, а этап меняется от хода к ходу.
    """
    return writer_agent.prompt(
        seed, history,
        f"Ограничения этапа:\n{stages.rules_for(stage)}\n\n"
        "Задача: ответить на последнюю реплику собеседника — или закрыть тред"
        " инструментом classify.")
```

`respond` получает именованный параметр `stage: str` и передаёт его в `prompt`.

- [ ] **Step 4: Двигать этап на тике**

`backend/sender/services/incoming.py`. Три вещи, каждая — ловушка:

**Этап берётся из `thread_store`, а не из `thread`.** Переменная `thread` в `handle_one` — это `conversation.get()`, чей `SELECT` перечисляет фиксированный список колонок системы 3; `stage` в него не входит, и `thread.get("stage")` вернул бы `None` молча, оставив все треды в `contact` навсегда. Читать нужно оттуда же, откуда уже читается `seed`:

```python
        record = thread_store.thread(db, thread["thread_id"])
        seed, stage = record["seed"], record["stage"]
        history = thread_store.history(db, thread["thread_id"])
        reply = await asyncio.to_thread(_ask, _seller(), seed, history,
                                        thread["thread_id"], stage)
```

**Этап и оффер уезжают в `_ask` готовыми значениями.** Соединение принадлежит потоку цикла, и любой поход в базу внутри `_ask` даст `ProgrammingError` — об этом прямо сказано в его докстроке. Сигнатура:

```python
def _ask(agent, seed: dict, history: list[dict], thread_id: str, stage: str):
    """Ровно поход в сеть — его и уносит to_thread. Базы здесь нет и быть не
    может: соединение принадлежит потоку цикла."""
    config = writer_config.load()
    return seller.respond(agent, seed, history,
                          offers.variant_of(thread_id, config)["text"],
                          session_id=thread_id, stage=stage)
```

Оффер — вариант этого треда, а не общий `["offer"]["text"]`: иначе первое сообщение аргументирует вариант A, ответы в диалоге — другой текст, и разрез `by_offer` припишет встречу формулировке, которая её не приносила.

**Этап двигается после успешного ответа** — в `_answer`, где текст уже прошёл гейты и лёг в очередь:

```python
    thread_store.set_stage(db, thread["thread_id"],
                           stages.stage_after(stage, stages.advance(stage)))
```

`stage` передаётся в `_answer` параметром (он уже принимает `thread`, `row`, `text`, `now`, `config` — шестым идёт `stage`). Именно `advance`, а не фиксированный `"probing"`: иначе тред застрял бы на втором этапе навсегда и никогда не дошёл бы до `offer` и `closing`. `stage_after` вокруг `advance` не тавтология — он остаётся единственной дверью, через которую этап меняется, и защищает от чужого кода, который однажды передаст сюда не то.

- [ ] **Step 5: Прогнать тесты**

Запустить: `cd backend && uv run pytest writer/tests/ sender/tests/ -q`
Ожидание: PASS.

- [ ] **Step 6: Коммит**

```bash
git add backend/writer/services/seller.py backend/sender/services/incoming.py \
        backend/writer/tests/test_seller.py
git commit -m "feat(sender): ответ в диалоге знает этап и двигает его на шаг"
```

---

### Task 7: Исход треда — встреча, и её отмечает человек

**Files:**
- Modify: `backend/writer/db/thread_store.py` (`_ensure_columns`, `SCHEMA`, `thread`, новая `set_outcome`)
- Modify: `backend/writer/routes/threads.py`
- Modify: `frontend/app/api.ts`, `frontend/app/threads/page.tsx`
- Test: `backend/writer/tests/test_threads.py`

**Interfaces:**
- Consumes: `thread_store.set_stage` (Task 3).
- Produces: `thread_store.OUTCOMES: tuple[str, ...]`, `thread_store.set_outcome(db, thread_id: str, outcome: str, at: str | None = None) -> None`; `POST /api/threads/{company_id}/outcome` с телом `{"outcome": "meeting_held"}`.

- [ ] **Step 1: Написать падающий тест**

В `backend/writer/tests/test_threads.py`:

```python
def test_meeting_is_recorded_with_its_moment():
    db = thread_store.connect(":memory:")
    thread_store.open_thread(db, "+77010000001", "c_ok", {"name": "Ромашка"})

    thread_store.set_outcome(db, "+77010000001", "meeting_held", at="2026-09-10T11:00:00+00:00")

    stored = thread_store.thread(db, "+77010000001")
    assert stored["outcome"] == "meeting_held"
    assert stored["meeting_at"] == "2026-09-10T11:00:00+00:00"


def test_unknown_outcome_is_refused():
    db = thread_store.connect(":memory:")
    thread_store.open_thread(db, "+77010000001", "c_ok", {"name": "Ромашка"})

    with pytest.raises(ValueError):
        thread_store.set_outcome(db, "+77010000001", "почти согласился")
```

- [ ] **Step 2: Убедиться, что тест падает**

Запустить: `cd backend && uv run pytest writer/tests/test_threads.py -v`
Ожидание: FAIL — `AttributeError: module has no attribute 'set_outcome'`.

- [ ] **Step 3: Долить колонки и запись исхода**

`backend/writer/db/thread_store.py` — в `SCHEMA` (таблица `threads`) и в `_ensure_columns` (кортеж `threads`):

```sql
  outcome    TEXT,   -- meeting_agreed | meeting_held | refused | lost
  meeting_at TEXT    -- когда созвон состоялся
```

```python
OUTCOMES: tuple[str, ...] = ("meeting_agreed", "meeting_held", "refused", "lost")


def set_outcome(db, thread_id: str, outcome: str, at: str | None = None) -> None:
    """Исход треда. meeting_held — единица оплаты, поэтому значение проверяется
    здесь: опечатка в исходе стоит денег, а не строки в журнале.

    meeting_at заполняется только у встреч: колонка с таким именем, хранящая
    момент отказа, врала бы всякому, кто прочитает её через полгода.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"исход {outcome!r} не из {OUTCOMES}")
    moment = (at or now()) if outcome.startswith("meeting_") else None
    db.execute("UPDATE threads SET outcome = ?, meeting_at = ? WHERE thread_id = ?",
               (outcome, moment, thread_id))
    db.commit()
```

Дописать в тест Step 1 проверку:

```python
def test_refusal_leaves_meeting_time_empty():
    db = thread_store.connect(":memory:")
    thread_store.open_thread(db, "+77010000001", "c_ok", {"name": "Ромашка"})

    thread_store.set_outcome(db, "+77010000001", "refused")

    assert thread_store.thread(db, "+77010000001")["meeting_at"] is None
```

`thread()` возвращает `outcome` и `meeting_at`.

- [ ] **Step 4: Открыть ручку**

`backend/writer/routes/threads.py`:

```python
class OutcomeRequest(BaseModel):
    outcome: str


@router.post("/{company_id}/outcome")
def set_outcome(company_id: str, request: OutcomeRequest) -> dict:
    """Исход ставит человек, а не автомат: встреча — то, за что платят, и
    вероятностный классификатор не имеет права её выписывать."""
    leads, threads = open_stores()
    try:
        thread = thread_store.thread_of_company(threads, company_id)
        if thread is None:
            raise HTTPException(404, "тред не найден")
        try:
            thread_store.set_outcome(threads, thread["thread_id"], request.outcome)
        except ValueError as bad:
            raise HTTPException(400, str(bad)) from bad
        return {"outcome": request.outcome}
    finally:
        leads.close()
        threads.close()
```

- [ ] **Step 5: Кнопка в карточке треда**

`frontend/app/api.ts` — тип и вызов:

```ts
export type ThreadOutcome = "meeting_agreed" | "meeting_held" | "refused" | "lost";

export function setThreadOutcome(companyId: string, outcome: ThreadOutcome) {
  return json<{ outcome: string }>(
    `/api/threads/${encodeURIComponent(companyId)}/outcome`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ outcome }),
    },
  );
}
```

`frontend/app/threads/page.tsx` — в карточке треда ряд из двух кнопок: «Согласился на созвон» (`meeting_agreed`) и «Созвон состоялся» (`meeting_held`), плюс «Не сложилось» (`lost`). После успеха — перечитать карточку.

- [ ] **Step 6: Прогнать тесты и собрать фронт**

Запустить: `cd backend && uv run pytest writer/tests/ -q` — PASS.
Запустить: `cd frontend && npx tsc --noEmit` — без ошибок.

- [ ] **Step 7: Коммит**

```bash
git add backend/writer/db/thread_store.py backend/writer/routes/threads.py \
        backend/writer/tests/test_threads.py frontend/app/api.ts frontend/app/threads/page.tsx
git commit -m "feat(writer): исход треда и встреча, отмеченная человеком"
```

---

### Task 8: `backend/analytics.py` — воронка и разрезы

**Files:**
- Create: `backend/analytics.py`
- Test: `backend/collector/tests/test_analytics.py`

**Interfaces:**
- Consumes: колонки `threads.stage` (Task 3), `threads.outcome` (Task 7), `messages.offer_variant` (Task 5), `messages.angle`.
- Produces: `analytics.use(state_db: Path | None, leads_db: Path | None = None) -> None`, `analytics.report(days: int = 30) -> dict`, `analytics.diagnose(funnel: list[dict]) -> str`.

Единица воронки — **тред**, а не сообщение: смешивать «отправлено 412 сообщений» с «7 тредов согласились» значит показывать проценты, которые ничего не значат.

- [ ] **Step 1: Написать падающий тест**

Создать `backend/collector/tests/test_analytics.py`:

```python
"""Воронка считается тредами и не растёт вверх; диагноз молчит на малой выборке.

Модуль верхнего уровня тестируется здесь по тому же основанию, что
test_activity.py: своего каталога у backend/*.py нет.
"""

import sqlite3
from pathlib import Path

import pytest

import analytics
from writer.db import thread_store


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "state.db"
    db = thread_store.connect(path)
    db.executescript(
        "CREATE TABLE IF NOT EXISTS outbox ("
        "  id INTEGER PRIMARY KEY, message_id INTEGER, status TEXT NOT NULL,"
        "  delivered_at TEXT);"
    )
    yield db, path
    db.close()
    analytics.use(None)


def _thread(db, thread_id, *, stage="contact", outcome=None, sent=True, delivered=False,
            replied=False, angle="site_no_pricing", variant="pay_per_meeting"):
    thread_store.open_thread(db, thread_id, f"c{thread_id}", {"name": "Ромашка"})
    thread_store.set_stage(db, thread_id, stage)
    if outcome:
        thread_store.set_outcome(db, thread_id, outcome)
    if sent:
        message_id = thread_store.add_draft(db, thread_id, "текст", angle,
                                            offer_variant=variant)
        db.execute("UPDATE messages SET sent_text = 'текст', sent_at = ?"
                   " WHERE message_id = ?", (thread_store.now(), message_id))
        db.execute("INSERT INTO outbox (message_id, status, delivered_at)"
                   " VALUES (?, 'sent', ?)",
                   (message_id, thread_store.now() if delivered else None))
    if replied:
        thread_store.add_incoming(db, thread_id, "интересно")
    db.commit()


def test_funnel_counts_threads_and_never_grows(state):
    db, path = state
    _thread(db, "+77010000001", stage="closing", outcome="meeting_held",
            delivered=True, replied=True)
    _thread(db, "+77010000002", stage="probing", delivered=True, replied=True)
    _thread(db, "+77010000003", delivered=True)
    analytics.use(path)

    steps = {row["step"]: row["count"] for row in analytics.report()["funnel"]}

    assert steps["sent"] == 3
    assert steps["delivered"] == 3
    assert steps["replied"] == 2
    assert steps["dialog"] == 2
    assert steps["meeting_held"] == 1
    counts = [row["count"] for row in analytics.report()["funnel"]]
    assert counts == sorted(counts, reverse=True), f"воронка выросла вверх: {counts}"


def test_breakdowns_split_by_offer_and_angle(state):
    db, path = state
    _thread(db, "+77010000001", replied=True, angle="site_no_pricing",
            variant="pay_per_meeting")
    _thread(db, "+77010000002", angle="ig_dormant", variant="free_first_meetings")
    analytics.use(path)

    report = analytics.report()

    assert {row["key"] for row in report["by_offer"]} == {"pay_per_meeting", "free_first_meetings"}
    assert {row["key"] for row in report["by_angle"]} == {"site_no_pricing", "ig_dormant"}


def test_diagnosis_stays_silent_on_a_small_sample(state):
    db, path = state
    _thread(db, "+77010000001", replied=True)
    analytics.use(path)

    assert analytics.report()["diagnosis"] == "ok", \
        "диагноз по одному ответу — шум, из-за которого странице перестанут верить"


def test_missing_state_db_gives_an_empty_funnel(tmp_path):
    """Чистая установка: переписки ещё нет. Страница обязана показать нули, а
    не пятисотку от sqlite3, который в режиме ro не создаёт файл."""
    analytics.use(tmp_path / "нет-такой.db")

    report = analytics.report()

    assert [row["count"] for row in report["funnel"]] == [0] * len(analytics.STEPS)
    assert report["diagnosis"] == "ok"
```

- [ ] **Step 2: Убедиться, что тест падает**

Запустить: `cd backend && uv run pytest collector/tests/test_analytics.py -v`
Ожидание: FAIL — `ModuleNotFoundError: No module named 'analytics'`.

- [ ] **Step 3: Создать `backend/analytics.py`**

```python
"""Воронка холодной переписки: где мы теряем и по какой из трёх переменных.

Верхний уровень backend/, рядом с activity.py и config.py, по той же причине:
данные лежат в трёх системах (threads/messages — система 2, outbox — система 3,
рубрика и город — derived системы 1), и ни одна из них не имеет права знать
две другие.

Единица счёта — тред, а не сообщение: «412 отправленных сообщений» и «7
согласившихся тредов» в одной воронке дали бы проценты, которые ничего не
значат.

Только чтение: обе базы открываются в режиме ro.
"""

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

STEPS = ("sent", "delivered", "replied", "dialog", "meeting_agreed", "meeting_held")

# Меньше этого числа ответов диагноз не ставится: на трёх ответах он шум, а
# страница, посоветовавшая «чинить ICP» после первого молчащего лида, научит
# оператора себе не верить.
MIN_REPLIES_FOR_DIAGNOSIS = 10
LOW_REPLY_RATE = 0.02

_state: Path | None = None
_leads: Path | None = None


def use(state_db: Path | None, leads_db: Path | None = None) -> None:
    """Шов из collector/api.py, как activity.use(): модуль верхнего уровня не
    ищет базы сам."""
    global _state, _leads
    _state, _leads = state_db, leads_db


def report(days: int = 30) -> dict:
    if _state is None:
        raise RuntimeError("analytics.use() не вызван: путь к state.db неизвестен")
    if not Path(_state).exists():
        # На чистой установке переписки ещё нет, а sqlite3 в режиме ro на
        # отсутствующем файле бросает — и страница отвечала бы 500 вместо
        # пустой воронки. activity.py этой беды не знает: он открывает базу на
        # запись и создаёт её сам, а аналитика писать не имеет права.
        return _empty(days)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with closing(_connect()) as db:
        rows = _funnel(db, since)
        return {
            "days": days,
            "funnel": rows,
            "by_offer": _breakdown(db, since, "m.offer_variant"),
            "by_angle": _breakdown(db, since, "m.angle"),
            "by_segment": _segments(db, since),
            "edited_share": _edited_share(db, since),
            "diagnosis": diagnose(rows),
        }


def _empty(days: int) -> dict:
    return {"days": days, "funnel": [{"step": step, "count": 0} for step in STEPS],
            "by_offer": [], "by_angle": [], "by_segment": [],
            "edited_share": 0.0, "diagnosis": "ok"}


def diagnose(funnel: list[dict]) -> str:
    counts = {row["step"]: row["count"] for row in funnel}
    sent, replied = counts.get("sent", 0), counts.get("replied", 0)
    if sent and replied / sent < LOW_REPLY_RATE:
        return "reply_rate_low"
    if replied >= MIN_REPLIES_FOR_DIAGNOSIS and not counts.get("meeting_held"):
        return "icp_mismatch"
    return "ok"
```

Приватные запросы (тот же файл, ниже — газетная метафора):

```python
def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{_state}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    if _leads is not None and Path(_leads).exists():
        db.execute(f"ATTACH DATABASE 'file:{_leads}?mode=ro' AS leads")
    return db


SENT_THREADS = (
    " FROM threads t JOIN messages m ON m.thread_id = t.thread_id"
    " AND m.role = 'outgoing' AND m.sent_text IS NOT NULL AND m.sent_at >= ?"
)


def _funnel(db: sqlite3.Connection, since: str) -> list[dict]:
    counts = {
        "sent": _scalar(db, f"SELECT count(DISTINCT t.thread_id){SENT_THREADS}", since),
        "delivered": _scalar(db, (
            f"SELECT count(DISTINCT t.thread_id){SENT_THREADS}"
            " JOIN outbox o ON o.message_id = m.message_id AND o.delivered_at IS NOT NULL"),
            since),
        "replied": _scalar(db, (
            f"SELECT count(DISTINCT t.thread_id){SENT_THREADS}"
            " WHERE EXISTS (SELECT 1 FROM messages i WHERE i.thread_id = t.thread_id"
            "               AND i.role = 'incoming')"), since),
        "dialog": _scalar(db, (
            f"SELECT count(DISTINCT t.thread_id){SENT_THREADS}"
            " WHERE t.stage IN ('probing', 'offer', 'closing')"), since),
        "meeting_agreed": _scalar(db, (
            f"SELECT count(DISTINCT t.thread_id){SENT_THREADS}"
            " WHERE t.outcome IN ('meeting_agreed', 'meeting_held')"), since),
        "meeting_held": _scalar(db, (
            f"SELECT count(DISTINCT t.thread_id){SENT_THREADS}"
            " WHERE t.outcome = 'meeting_held'"), since),
    }
    return [{"step": step, "count": counts[step]} for step in STEPS]
```

```python
def _scalar(db: sqlite3.Connection, sql: str, since: str) -> int:
    return db.execute(sql, (since,)).fetchone()[0] or 0


BEFORE_AB = "до A/B"

REPLIED = ("EXISTS (SELECT 1 FROM messages i WHERE i.thread_id = t.thread_id"
           "        AND i.role = 'incoming')")
HELD = "t.outcome = 'meeting_held'"


def _breakdown(db: sqlite3.Connection, since: str, column: str) -> list[dict]:
    """Разрез по одной из трёх переменных. Треды, открытые до A/B, идут своей
    строкой, а не подмешиваются к варианту, которого тогда не существовало."""
    rows = db.execute(
        f"SELECT coalesce({column}, '') AS key,"
        "        count(DISTINCT t.thread_id) AS sent,"
        f"       count(DISTINCT CASE WHEN {REPLIED} THEN t.thread_id END) AS replied,"
        f"       count(DISTINCT CASE WHEN {HELD} THEN t.thread_id END) AS meetings"
        + SENT_THREADS +
        " GROUP BY key ORDER BY sent DESC",
        (since,),
    )
    return [{"key": row["key"] or BEFORE_AB, "sent": row["sent"],
             "replied": row["replied"], "meetings": row["meetings"]} for row in rows]


def _segments(db: sqlite3.Connection, since: str) -> list[dict]:
    """Рубрика и город лида. Подпись рубрики остаётся идентификатором 2GIS:
    таблицы имён рубрик в derived нет, а придумывать её ради заголовка колонки
    дороже, чем прочитать id.

    Пустой список, когда derived не приаттачен: аналитика системы 2 не обязана
    падать оттого, что база системы 1 ещё не собрана.
    """
    if _leads is None or not Path(_leads).exists():
        return []
    rows = db.execute(
        "SELECT c.rubric_id || ' · ' || c.city AS key,"
        "       count(DISTINCT t.thread_id) AS sent,"
        f"      count(DISTINCT CASE WHEN {REPLIED} THEN t.thread_id END) AS replied,"
        f"      count(DISTINCT CASE WHEN {HELD} THEN t.thread_id END) AS meetings"
        + SENT_THREADS +
        " JOIN leads.companies c ON c.company_id = t.company_id"
        " GROUP BY key ORDER BY sent DESC",
        (since,),
    )
    return [dict(row) for row in rows]


def _edited_share(db: sqlite3.Connection, since: str) -> float:
    """Доля отправленных сообщений, которые оператор правил, — единственная
    бесплатная разметка качества промпта."""
    row = db.execute(
        "SELECT count(*) AS sent,"
        "       sum(CASE WHEN draft_text IS NOT NULL AND sent_text != draft_text"
        "                THEN 1 ELSE 0 END) AS edited"
        " FROM messages WHERE role = 'outgoing' AND sent_text IS NOT NULL"
        " AND sent_at >= ?",
        (since,),
    ).fetchone()
    return round((row["edited"] or 0) / row["sent"], 2) if row["sent"] else 0.0
```

- [ ] **Step 4: Прогнать тесты**

Запустить: `cd backend && uv run pytest collector/tests/test_analytics.py -v`
Ожидание: PASS.

- [ ] **Step 5: Коммит**

```bash
git add backend/analytics.py backend/collector/tests/test_analytics.py
git commit -m "feat(analytics): воронка тредами и разрезы по офферу, поводу и сегменту"
```

---

### Task 9: `/api/analytics` и страница «Аналитика»

**Files:**
- Create: `backend/collector/routes/analytics.py`, `frontend/app/analytics/page.tsx`
- Modify: `backend/collector/api.py` (шов `analytics.use` + монтаж роутера)
- Modify: `frontend/app/api.ts`, `frontend/components/Sidebar.tsx`
- Modify: `CLAUDE.md`
- Test: `backend/collector/tests/test_jobs.py` (контракт фронтенда)

**Interfaces:**
- Consumes: `analytics.use`, `analytics.report` (Task 8).
- Produces: `GET /api/analytics?days=30` → `{days, funnel, by_offer, by_angle, by_segment, edited_share, diagnosis}`.

- [ ] **Step 1: Написать падающий тест**

В `backend/collector/tests/test_analytics.py`:

```python
def test_endpoint_answers_with_the_full_report(state):
    """Роутер поднимается отдельным приложением, а не collector.api: импорт
    боевого app зовёт analytics.use(store.STATE) на своих путях и тянет за
    собой lifespan трёх систем — тест роутера не должен от этого зависеть."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from collector.routes import analytics as analytics_routes

    db, path = state
    _thread(db, "+77010000001", replied=True)
    analytics.use(path)

    app = FastAPI()
    app.include_router(analytics_routes.router)
    response = TestClient(app).get("/api/analytics?days=7")

    assert response.status_code == 200
    body = response.json()
    assert body["days"] == 7
    assert [row["step"] for row in body["funnel"]] == list(analytics.STEPS)
    assert "diagnosis" in body
```

- [ ] **Step 2: Убедиться, что тест падает**

Запустить: `cd backend && uv run pytest collector/tests/test_analytics.py -v`
Ожидание: FAIL — 404.

- [ ] **Step 3: Создать `backend/collector/routes/analytics.py`**

```python
"""Воронка холодной переписки для страницы «Аналитика».

Диагноз считает бэкенд, а не страница: порог выборки и правило «reply < 2% —
чинить текст, ответы без встреч — чинить ICP» относятся к предметной области,
и второй их копией во фронтенде мы получили бы два разных совета.
"""

from fastapi import APIRouter

import analytics

router = APIRouter(prefix="/api/analytics")

MAX_DAYS = 365


@router.get("")
def report(days: int = 30) -> dict:
    return analytics.report(days=min(max(days, 1), MAX_DAYS))
```

- [ ] **Step 4: Подключить шов и роутер в `backend/collector/api.py`**

Рядом с существующим `activity.use(store.STATE)` (строка 61):

```python
analytics.use(store.STATE, store.DERIVED)
```

и `app.include_router(analytics_routes.router)` в блоке `include_router` (строки 99–110), сразу после `activity_routes.router`. Импорт — `from collector.routes import analytics as analytics_routes`, по образцу соседних (`activity as activity_routes`).

- [ ] **Step 5: Добавить контракт во фронтенд**

`frontend/app/api.ts`:

```ts
export type FunnelStep = { step: string; count: number };
export type Breakdown = { key: string; sent: number; replied: number; meetings: number };
export type AnalyticsReport = {
  days: number;
  funnel: FunnelStep[];
  by_offer: Breakdown[];
  by_angle: Breakdown[];
  by_segment: Breakdown[];
  edited_share: number;
  diagnosis: "ok" | "reply_rate_low" | "icp_mismatch";
};

export function fetchAnalytics(days = 30) {
  return json<AnalyticsReport>(`/api/analytics?days=${days}`);
}
```

`frontend/components/Sidebar.tsx` — пункт `{ href: "/analytics", label: "Аналитика", icon: ChartLineUpIcon }` после «Диалоги», иконка из `@phosphor-icons/react`.

- [ ] **Step 6: Создать `frontend/app/analytics/page.tsx`**

Клиентская страница по образцу `app/activity/page.tsx`: один `useEffect` с `fetchAnalytics`, перечитывание по `refreshTick` из `useLive()`, свой EventSource не открывается. Содержимое:

1. строка диагноза сверху — подписи только здесь, значения приходят с бэкенда:
   `{ ok: "Воронка в норме", reply_rate_low: "Ответов меньше 2% — чинить текст и оффер", icp_mismatch: "Ответы есть, встреч нет — чинить ICP" }`;
2. воронка шестью строками: подпись шага, абсолют, % от предыдущего шага;
3. три таблицы разрезов (оффер, повод, сегмент) с колонками «отправлено / ответили / встречи»;
4. доля правок оператора одной строкой.

Подписи шагов: `sent` — «Отправлено», `delivered` — «Доставлено», `replied` — «Ответили», `dialog` — «Разговор», `meeting_agreed` — «Согласились», `meeting_held` — «Встреча состоялась».

- [ ] **Step 7: Прогнать тесты и собрать фронт**

Запустить: `cd backend && uv run pytest -q` — PASS.
Запустить: `cd frontend && npx tsc --noEmit && npm run build` — без ошибок.

- [ ] **Step 8: Обновить `CLAUDE.md`**

В секции «Архитектура frontend/» заменить «шесть страниц» на «семь» и добавить `/analytics` — «воронка холодной переписки и три разреза». В секции про `collector/` добавить строку про `backend/analytics.py` рядом с описанием `activity.py`. В «Архитектура writer/» добавить, что этап переписки живёт в `threads.stage`, а вариант оффера — в `messages.offer_variant`.

- [ ] **Step 9: Коммит**

```bash
git add backend/collector/routes/analytics.py backend/collector/api.py \
        backend/collector/tests/test_analytics.py frontend/app/analytics/page.tsx \
        frontend/app/api.ts frontend/components/Sidebar.tsx CLAUDE.md
git commit -m "feat(web): страница «Аналитика» — воронка, разрезы и диагноз"
```

---

## Порядок и зависимости

```
Task 1 (поводы) ──> Task 2 (ЛПР) ──┐
Task 3 (этапы) ────────────────────┼──> Task 4 (промпт) ──> Task 5 (офферы) ──> Task 6 (продавец)
                                   └──> Task 7 (исход) ──> Task 8 (analytics) ──> Task 9 (страница)
```

Task 1 и Task 3 независимы и могут идти параллельно. Task 8 требует колонок из задач 3, 5 и 7 — раньше ей нечего считать.
