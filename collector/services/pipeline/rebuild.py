"""Пересборка derived.db из raw/ — прогон вместо DROP. Сети здесь нет.

Чтение внутри сборки идёт в *_all с фильтром run_id: пока прогон строится,
current_run ещё указывает на прошлый прогон, и view orgs/companies/signals/
fetches вернули бы чужие данные. Продуктовые читатели смотрят в view текущего
прогона (только запрос отказов сменил префикс на state.suppression, Task 6/2).
"""

import json
import re
import tomllib
from pathlib import Path

from services import enrich, resolve, score, sources, storage

CONFIG = Path("config.toml")

GIS_LIST_URL = re.compile(r"2gis\.kz/([a-z]+)/rubric/(\d+)(?:/page/(\d+))?$")
GIS_FIRM_URL = re.compile(r"2gis\.kz/([a-z]+)/firm/(\d+)$")


def load_pages():
    return storage.iter_pages()


def html_of(page):
    """HTML страницы снимка — через адаптер по sha, а не по пути из словаря:
    иначе переезд хранилища ломался бы на первом же чтении."""
    return storage.get(page["sha"])


def load_llm_answers(db, kind):
    """Оплаченные ответы модели заданного вида — из обоих хранилищ, без дублей.

    Источники два и оба читаются: state.llm_answers (сюда пишет analyze) и
    файлы raw/*.llm.json (так платили до переезда). Выбирать источник по
    признаку «таблица пуста» нельзя: первый же новый ответ сделал бы таблицу
    непустой и спрятал бы все ответы, оставшиеся файлами.

    Ключ дедупа — (model, prompt): промпт уникален по компании/аккаунту и им же
    ключуется файловый кэш. Совпало — выигрывает таблица. Порядок фиксирован:
    пересборка обязана быть функцией снимка, а не порядка строк на диске.
    """
    answers = {}
    for answer in storage.llm_answers():
        if answer.get("kind", "company_profile") == kind:
            answers[(answer["model"], answer["prompt"])] = {
                "subject": subject_of(kind, answer["prompt"]), **answer,
            }
    for row in db.execute(
        "SELECT subject, model, prompt, answer FROM state.llm_answers WHERE kind = ?"
        " ORDER BY subject, id",
        (kind,),
    ):
        answers[(row["model"], row["prompt"])] = {
            "kind": kind, "subject": row["subject"], "model": row["model"],
            "prompt": row["prompt"], **json.loads(row["answer"]),
        }
    return [answers[key] for key in sorted(answers)]


def subject_of(kind, prompt):
    """Кого спрашивали — из первой строки промпта, как её строит analyze.

    Файловый кэш subject не хранил: у него ключ был sha256(model+prompt).
    Восстанавливается он тем же разбором, которым пользуются fill_profiles и
    enrich, — второго способа опознать компанию в проекте нет.
    """
    lines = prompt.splitlines()
    if kind == "ig_signals":
        return lines[0].removeprefix("Инстаграм: ")
    name = lines[0].removeprefix("Компания: ")
    city = lines[1].removeprefix("Город: ") if len(lines) > 1 else ""
    return f"{name} | {city}"


def run(ctx):
    """Собрать прогон и опубликовать его последним действием.

    Пока current_run не переключён, читатели видят прежний прогон: ни один
    промежуточный коммит стадий его не трогает, поэтому атомарность выдачи
    держится одним activate_run, а не одной большой транзакцией. Прогон,
    брошенный отменой или ошибкой, остаётся в *_all без finished_at и текущим
    не становится — откатывать нечего, публиковать нечего.
    """
    from services import store as engine
    ctx.log("пересборка из raw/")
    db = engine.connect()
    run_id = engine.new_run(db)
    try:
        pages = load_pages()
        ctx.log(f"  снимок: {len(pages)} страниц")
        stage = stage_reporter(ctx, STAGE_COUNT)

        stage("страницы снимка", lambda: fill_fetches(db, run_id, pages))
        stage("организации 2GIS", lambda: fill_orgs(db, run_id, pages, ctx))
        stage("контакты филиалов", lambda: fill_contacts(db, run_id, pages))
        stage("склейка компаний", lambda: resolve.resolve(db, run_id))
        stage("сигналы", lambda: enrich.enrich(db, run_id, pages, scoring_weights()))
        stage("отзывы от модели", lambda: enrich.reviews_signals(db, run_id, pages, scoring_weights()))
        stage("профили от модели", lambda: fill_profiles(db, run_id))
        stage("скоринг", lambda: score.score_all(db, run_id, *ranking_config()))
        stage(f"публикация прогона {run_id}", lambda: publish(engine, db, run_id))
    finally:
        db.close()
    return {"run_id": run_id}


STAGE_COUNT = 9


def stage_reporter(ctx, total):
    """Один способ пройти стадию: спросить об отмене, показать её и сделать.

    Отмена проверяется перед стадией, а не внутри: единица работы пересборки —
    стадия целиком, и обрывать её на середине незачем — прогон всё равно не
    станет текущим.
    """
    done = 0

    def stage(label, work):
        nonlocal done
        ctx.check_cancelled()
        done += 1
        ctx.progress(done, total, label)
        work()

    return stage


def publish(engine, db, run_id):
    """Подмена выдачи одним UPDATE — последнее, что делает пересборка."""
    engine.activate_run(db, run_id)
    engine.finish_run(db, run_id)


def config():
    return tomllib.loads(CONFIG.read_text(encoding="utf-8"))


def scoring_weights():
    """Веса сигналов из config.toml: в коде их держать нельзя, они калибруются."""
    return config()["scoring"]["intent"]


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
    for answer in load_llm_answers(db, "company_profile"):
        name, _, city = answer["subject"].partition(" | ")
        company_id = companies.get((name, city))
        if not company_id:
            continue
        profile = answer["profile"]
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
    settings = config()
    return settings["scoring"], {
        "include": set(settings["rubrics"]["include"]),
        "exclude_branch": set(settings["rubrics"]["exclude"]),
        "cities": set(settings["cities"]),
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