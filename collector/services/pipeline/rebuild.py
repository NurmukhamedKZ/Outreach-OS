"""Пересборка derived.db из raw/ — прогон вместо DROP. Сети здесь нет.

Чтение внутри сборки идёт в *_all с фильтром run_id: пока прогон строится,
current_run ещё указывает на прошлый прогон, и view orgs/companies/signals/
fetches вернули бы чужие данные. Продуктовые читатели смотрят в view текущего
прогона (только запрос отказов сменил префикс на state.suppression, Task 6/2).
"""

import gzip
import json
import re
import tomllib
from pathlib import Path

from services import enrich, resolve, score, sources

RAW = Path("data/raw")
SCHEMA = Path("store/schema.sql")

GIS_LIST_URL = re.compile(r"2gis\.kz/([a-z]+)/rubric/(\d+)(?:/page/(\d+))?$")
GIS_FIRM_URL = re.compile(r"2gis\.kz/([a-z]+)/firm/(\d+)$")


def load_pages():
    """Сайдкары raw/, отсортированные. Временная локальная копия build.load_pages;
    Task 4 вводит services/storage.iter_pages, и rebuild переходит на него в Task 7."""
    pages = []
    for sidecar in sorted(RAW.glob("*.json")):
        if sidecar.name.count(".") != 1:
            continue
        sha = sidecar.name.removesuffix(".json")
        page = RAW / f"{sha}.html.gz"
        if not page.exists():
            continue
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        meta["sha"] = sha
        meta["path"] = page
        pages.append(meta)
    return sorted(pages, key=lambda p: (p["url"], p["sha"]))


def html_of(page):
    with gzip.open(page["path"], "rt", encoding="utf-8") as fh:
        return fh.read()


def load_llm_answers(db, run_id, kind):
    """Кэшированные ответы модели заданного вида.

    Источник — state.llm_answers (Task 8). Пока таблица не заполнена (до
    миграции Task 11) читает raw/*.llm.json тем же разбором, что build.py:
    kind берётся из поля answer, subject не нужен.
    """
    rows = db.execute(
        "SELECT subject, model, prompt, answer FROM state.llm_answers WHERE kind = ?",
        (kind,),
    ).fetchall()
    if rows:
        return [{"subject": r["subject"], "model": r["model"], "prompt": r["prompt"],
                 "answer": json.loads(r["answer"])} for r in rows]
    answers = []
    for path in sorted(RAW.glob("*.llm.json")):
        answer = json.loads(path.read_text(encoding="utf-8"))
        if answer.get("kind", "company_profile") == kind:
            answers.append(answer)
    return answers


def run(ctx):
    from services import store as engine
    ctx.log("пересборка из raw/")
    db = engine.connect()
    run_id = engine.new_run(db)
    pages = load_pages()
    # Пишем *все* таблицы нового прогона. Пока current_run не переключён,
    # читатели видят прежний прогон: ни один промежуточный коммит ниже не
    # трогает current_run, поэтому атомарность выдачи держится одним финальным
    # activate_run (одна UPDATE), а не одной большой транзакцией.
    try:
        fill_fetches(db, run_id, pages)
        fill_orgs(db, run_id, pages, ctx)
        fill_contacts(db, run_id, pages)
        resolve.resolve(db, run_id)
        enrich.enrich(db, run_id, pages, scoring_weights())
        fill_profiles(db, run_id)
        score.score_all(db, run_id, *ranking_config())
        engine.activate_run(db, run_id)   # публикация — атомарно, последней
        engine.finish_run(db, run_id)
    except BaseException:
        # Прогон брошен: current_run не переключён, читатели целы. Записи
        # нового run_id остаются в *_all сиротами — это и есть «отменённый
        # прогон», который не стал текущим.
        raise
    finally:
        db.close()
    ctx.progress(1, 1, "пересборка завершена")
    return {"run_id": run_id}


def scoring_weights():
    """Веса сигналов из config.toml: в коде их держать нельзя, они калибруются."""
    config = tomllib.loads(Path("config.toml").read_text(encoding="utf-8"))["scoring"]
    return config["intent"]


def fill_profiles(db, run_id):
    """Профили от модели из state.llm_answers. Сети здесь нет: ответы уже оплачены.

    Компания опознаётся по паре (название, город) из первых двух строк промпта —
    company_id в него не входит, потому что нестабилен (`dom:` меняется на `2gis:`
    при появлении домена), и его включение обесценило бы оплаченные ответы.
    """
    # Ключ — пара с городом, а не одно название: названия в базе не уникальны
    # (Deloitte, Schneider Electric и ещё три пары стоят в двух городах), и по
    # одному названию оплаченный профиль ложился бы той компании, что попалась
    # обходу последней. Город уже есть второй строкой промпта, поэтому сам промпт
    # не меняется и кэш ответов остаётся в силе.
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
    for answer in load_llm_answers(db, run_id, "company_profile"):
        payload = answer.get("answer", answer)
        lines = answer["prompt"].splitlines()
        name = lines[0].removeprefix("Компания: ")
        city = lines[1].removeprefix("Город: ") if len(lines) > 1 else ""
        company_id = companies.get((name, city))
        if not company_id:
            continue
        profile = payload["profile"]
        db.execute(
            "INSERT OR REPLACE INTO profiles_all (run_id, company_id, model, industry,"
            " size_hint, has_sales_team, why_now, quote, confidence)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                company_id,
                answer["model"],
                profile.get("industry"),
                profile.get("size_hint"),
                profile.get("has_sales_team"),
                profile.get("why_now"),
                profile.get("quote"),
                profile.get("confidence"),
            ),
        )


def ranking_config():
    """Веса и списки для скоринга. Рубрики нужны целиком: fit считается по ним."""
    config = tomllib.loads(Path("config.toml").read_text(encoding="utf-8"))
    return config["scoring"], {
        "include": set(config["rubrics"]["include"]),
        "exclude_branch": set(config["rubrics"]["exclude"]),
        "cities": set(config["cities"]),
    }


def fill_fetches(db, run_id, pages):
    rows = [
        (run_id, p["url"], p["sha"], p.get("final_url"), p.get("status"), p.get("fetched_at"))
        for p in pages
    ]
    db.executemany(
        "INSERT OR IGNORE INTO fetches_all (run_id, url, sha, final_url, status, fetched_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )


def fill_orgs(db, run_id, pages, ctx):
    """Организации со страниц рубрик 2GIS. Город и рубрика берутся из адреса запроса."""
    for page in pages_matching(pages, GIS_LIST_URL):
        city, rubric_id, page_number = page["match"].groups()
        requested = int(page_number or 1)
        state = sources.parse_initial_state(html_of(page))
        _, _, current = sources.parse_search_meta(state)
        if current != requested:
            ctx.log(f"  подмена: {page['url']} отдал страницу {current}, пропуск")
            continue
        rows = [
            {**org, "run_id": run_id}
            for org in sources.parse_org_list(state, city, rubric_id)
        ]
        db.executemany(
            "INSERT OR IGNORE INTO orgs_all (run_id, branch_id, org_id, name, org_name,"
            " branch_count, city, rubric_id, address, rating, review_count)"
            " VALUES (:run_id, :branch_id, :org_id, :name, :org_name, :branch_count,"
            " :city, :rubric_id, :address, :rating, :review_count)",
            rows,
        )


def fill_contacts(db, run_id, pages):
    """Каналы связи из карточек филиалов 2GIS — по строке на канал."""
    for page in pages_matching(pages, GIS_FIRM_URL):
        branch_id = page["match"].group(2)
        state = sources.parse_initial_state(html_of(page))
        rows = [
            {**contact, "run_id": run_id}
            for contact in sources.parse_firm_card(state, branch_id)
        ]
        db.executemany(
            "INSERT OR IGNORE INTO contacts_all (run_id, branch_id, kind, handle, source_url)"
            " VALUES (:run_id, :branch_id, :kind, :handle, :source_url)",
            rows,
        )


def pages_matching(pages, pattern):
    """Страницы, чей адрес запроса подходит под шаблон, с готовым разбором адреса."""
    for page in pages:
        match = pattern.search(page["url"])
        if match:
            yield {**page, "match": match}