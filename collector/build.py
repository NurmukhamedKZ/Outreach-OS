"""Пересборка leads.db из raw/ — чистая функция, ни одного сетевого запроса.

Это главная граница системы. Слева от неё collect.py и единственное, что нельзя
восстановить: страница удаляется, вакансия закрывается. Справа — то, что
пересобирается бесплатно сколько угодно раз.

Поэтому здесь не импортируются ни fetch.py, ни scrapling: невозможность похода в
сеть обеспечивается отсутствием инструмента, а не дисциплиной. check.py это
проверяет.

Сборка идёт целиком: DROP всех таблиц и наполнение заново. Инкрементальности нет
и не нужно — на текущем объёме это секунды.

Запуск: uv run build.py
"""

import csv
import gzip
import json
import re
import sqlite3
import time
import tomllib
from pathlib import Path

from services import enrich, resolve, score, sources

RAW = Path("data/raw")
DB = Path("db/leads.db")
# Сборка идёт в соседний файл и подменяет боевую базу одним движением в конце.
# api.py читает leads.db всё время, пока build.py работает, а первое, что делает
# схема, — DROP всех таблиц: без подмены оператор несколько секунд смотрел бы на
# базу без единой строки. Неудачная сборка по той же причине не портит рабочую.
BUILDING = DB.with_suffix(".building")
SCHEMA = Path("db/schema.sql")
# Отказы живут в файле, а не только в базе: схема пересобирается через DROP, и
# запись, сделанная напрямую в таблицу, исчезла бы на ближайшей сборке. Список,
# который «никогда не очищается» (PRD F21), нельзя хранить в том, что стирается.
SUPPRESSION = Path("data/suppression.csv")

GIS_LIST_URL = re.compile(r"2gis\.kz/([a-z]+)/rubric/(\d+)(?:/page/(\d+))?$")
GIS_FIRM_URL = re.compile(r"2gis\.kz/([a-z]+)/firm/(\d+)$")
HH_LIST_URL = re.compile(r"https://([a-z]+)\.hh\.kz/vacancies/([a-z0-9_]+)$")
HH_VACANCY_URL = re.compile(r"hh\.kz/vacancy/(\d+)$")
# Канонический адрес вакансии. Из сайдкара его брать нельзя: hh уводит запрос на
# поддомен города, и у страниц, мигрировавших из старого кэша, в url лежит уже
# конечный адрес. Ссылка идёт в why_now и обязана быть одинаковой у всех записей.
VACANCY_URL = "https://hh.kz/vacancy/{id}"


def main():
    started = time.time()
    pages = load_pages()
    BUILDING.unlink(missing_ok=True)
    db = sqlite3.connect(BUILDING)
    db.executescript(SCHEMA.read_text(encoding="utf-8"))

    fill_fetches(db, pages)
    fill_orgs(db, pages)
    fill_contacts(db, pages)
    fill_vacancies(db, pages)
    # Склейка живёт внутри сборки, а не отдельной командой: база обязана
    # оставаться чистой функцией от raw/, иначе рушится воспроизводимость.
    resolve.resolve(db)
    enrich.enrich(db, pages, scoring_weights())
    fill_profiles(db)
    fill_suppression(db)
    score.score_all(db, *ranking_config())
    db.commit()

    report(db, len(pages), time.time() - started)
    db.close()
    BUILDING.replace(DB)


def scoring_weights():
    """Веса сигналов из config.toml: в коде их держать нельзя, они калибруются."""
    config = tomllib.loads(Path("config.toml").read_text(encoding="utf-8"))["scoring"]
    return config["intent"]


def fill_profiles(db):
    """Профили от модели из raw/*.llm.json. Сети здесь нет: ответы уже на диске.

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
            " FROM companies c"
            " LEFT JOIN company_links l ON l.company_id = c.company_id AND l.rule = 'self'"
            " LEFT JOIN orgs o ON o.branch_id = l.branch_id"
        )
    }
    for answer_path in sorted(RAW.glob("*.llm.json")):
        answer = json.loads(answer_path.read_text(encoding="utf-8"))
        # Рядом лежат ответы classify_ig.py с другой схемой. Ответы, записанные до
        # появления метки, — профили компаний: тогда другого вида и не было.
        if answer.get("kind", "company_profile") != "company_profile":
            continue
        lines = answer["prompt"].splitlines()
        name = lines[0].removeprefix("Компания: ")
        city = lines[1].removeprefix("Город: ") if len(lines) > 1 else ""
        company_id = companies.get((name, city))
        if not company_id:
            continue
        profile = answer["profile"]
        db.execute(
            "INSERT OR REPLACE INTO profiles (company_id, model, industry, size_hint,"
            " has_sales_team, why_now, quote, confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
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


def fill_suppression(db):
    """Отказы из suppression.csv. Файл — источник истины, таблица — его копия.

    Пустой handle пропускается молча: строка без адресата ничего не запрещает, а
    падение сборки из-за кривой строки в юридическом списке лишило бы защиты все
    остальные записи разом.
    """
    if not SUPPRESSION.exists():
        return
    with SUPPRESSION.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            handle = (row.get("handle") or "").strip()
            if not handle:
                continue
            db.execute(
                "INSERT OR IGNORE INTO suppression (handle, added_at, reason)"
                " VALUES (?, ?, ?)",
                (handle, row.get("added_at") or "", row.get("reason") or ""),
            )


def ranking_config():
    """Веса и списки для скоринга. Рубрики нужны целиком: fit считается по ним."""
    config = tomllib.loads(Path("config.toml").read_text(encoding="utf-8"))
    return config["scoring"], {
        "include": set(config["rubrics"]["include"]),
        "exclude_branch": set(config["rubrics"]["exclude"]),
        "cities": set(config["cities"]),
    }


def load_llm_answers(kind):
    """Кэшированные ответы модели заданного вида. Сети здесь нет: они уже на диске.

    Ответы, записанные до появления метки, — профили компаний: другого вида тогда
    не существовало.
    """
    answers = []
    for path in sorted(RAW.glob("*.llm.json")):
        answer = json.loads(path.read_text(encoding="utf-8"))
        if answer.get("kind", "company_profile") == kind:
            answers.append(answer)
    return answers


def load_pages():
    """Сайдкары raw/, отсортированные: порядок вставки задаёт содержимое дампа.

    Страница без сайдкара считается недокачанной и не берётся — иначе потерялся
    бы final_url, на котором держится обнаружение подмены.
    """
    pages = []
    for sidecar in RAW.glob("*.json"):
        if sidecar.name.endswith(".serp.json"):
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


def fill_fetches(db, pages):
    db.executemany(
        "INSERT OR IGNORE INTO fetches (url, sha, final_url, status, fetched_at)"
        " VALUES (:url, :sha, :final_url, :status, :fetched_at)",
        pages,
    )


def fill_orgs(db, pages):
    """Организации со страниц рубрик 2GIS. Город и рубрика берутся из адреса запроса."""
    for page in pages_matching(pages, GIS_LIST_URL):
        city, rubric_id, page_number = page["match"].groups()
        requested = int(page_number or 1)
        state = sources.parse_initial_state(html_of(page))
        _, _, current = sources.parse_search_meta(state)
        if current != requested:
            print(f"  подмена: {page['url']} отдал страницу {current}, пропуск")
            continue
        db.executemany(
            "INSERT OR IGNORE INTO orgs (branch_id, org_id, name, org_name,"
            " branch_count, city, rubric_id, address, rating, review_count)"
            " VALUES (:branch_id, :org_id, :name, :org_name, :branch_count, :city,"
            " :rubric_id, :address, :rating, :review_count)",
            sources.parse_org_list(state, city, rubric_id),
        )


def fill_contacts(db, pages):
    """Каналы связи из карточек филиалов 2GIS — по строке на канал."""
    for page in pages_matching(pages, GIS_FIRM_URL):
        branch_id = page["match"].group(2)
        state = sources.parse_initial_state(html_of(page))
        db.executemany(
            "INSERT OR IGNORE INTO contacts (branch_id, kind, handle, source_url)"
            " VALUES (:branch_id, :kind, :handle, :source_url)",
            sources.parse_firm_card(state, branch_id),
        )


def fill_vacancies(db, pages):
    origins = vacancy_origins(pages)
    for page in pages_matching(pages, HH_VACANCY_URL):
        vacancy_id = page["match"].group(1)
        posting = sources.parse_job_posting(html_of(page))
        if not posting:
            print(f"  {page['url']}: JobPosting не найден — вакансия снята")
            continue
        city, slug = origins.get(vacancy_id, (None, None))
        db.execute(
            "INSERT OR IGNORE INTO vacancies (id, employer, title, text,"
            " published_at, city, slug, url) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                vacancy_id,
                (posting.get("hiringOrganization") or {}).get("name"),
                posting.get("title"),
                posting.get("description"),
                posting.get("datePosted"),
                city,
                slug,
                VACANCY_URL.format(id=vacancy_id),
            ),
        )


def vacancy_origins(pages):
    """{vacancy_id: (город, slug)} по страницам slug'ов hh.

    Страница, которую hh молча подменил общим списком города, отбрасывается: её
    50 посторонних вакансий — ровно тот мусор, против которого написана проверка
    конечного адреса.

    Вакансия, попавшая в несколько slug'ов, закрепляется за первым по алфавиту:
    выбор произвольный, но воспроизводимый, а slug здесь только происхождение —
    сигналы Ф6 читаются из текста вакансии, а не из него.
    """
    origins = {}
    for page in pages_matching(pages, HH_LIST_URL):
        city, slug = page["match"].groups()
        landed = page.get("final_url") or ""
        if slug.lower() not in landed.lower():
            print(f"  подмена: у hh нет страницы '{slug}', запрос увело на {landed}")
            continue
        for vacancy_id in sources.parse_vacancy_ids(html_of(page)):
            if vacancy_id not in origins or slug < origins[vacancy_id][1]:
                origins[vacancy_id] = (city, slug)
    return origins


def pages_matching(pages, pattern):
    """Страницы, чей адрес запроса подходит под шаблон, с готовым разбором адреса."""
    for page in pages:
        match = pattern.search(page["url"])
        if match:
            yield {**page, "match": match}


def report(db, page_count, elapsed):
    counts = ", ".join(
        f"{table} {db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]}"
        for table in ("fetches", "orgs", "contacts", "vacancies")
    )
    print(f"собрано из {page_count} страниц raw/: {counts}")
    print(f"{DB} готова за {elapsed:.1f} с")


if __name__ == "__main__":
    main()
