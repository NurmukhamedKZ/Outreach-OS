"""Массовый сбор сырья. Единственный модуль проекта, который ходит в сеть.

В базу не кладёт ничего: разбор raw/ -> leads.db делает build.py. Здесь только
план запросов из config.toml, пул потоков поверх fetch.get и две проверки
молчаливой подмены — единственное, что отделяет собранные данные от мусора.

Учёта «что уже скачано» нет и не нужно: raw/ и есть учёт. Повторный запуск
читает страницы с диска и не делает ни одного сетевого запроса.

Запуск:
  uv run collect.py                                      полный объём из config.toml
  uv run collect.py --cities almaty --rubrics 5 --slugs 3 --budget 600 --workers 4
"""

import argparse
import sqlite3
import sys
import time
import tomllib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
from pathlib import Path
from threading import Lock

import fetch
import sources

CONFIG = Path("config.toml")
DB = Path("leads.db")

RUBRIC_PAGE = "https://2gis.kz/{city}/rubric/{rubric}/page/{page}"
FIRM_CARD = "https://2gis.kz/{city}/firm/{branch_id}"
VACANCY_LIST = "https://{city}.hh.kz/vacancies/{slug}"
VACANCY = "https://hh.kz/vacancy/{vacancy_id}"
SITE_HOME = "https://{domain}/"
SITE_HOME_INSECURE = "http://{domain}/"

GIS_COOKIE = {"dg5_museum_accept": "true"}  # снимает редирект на /museum
HH_HEADERS = {"accept-language": "ru-RU,ru;q=0.9"}

# 2GIS отдаёт 60 организаций (5 страниц) на рубрику города, но обещает больше.
# Шестая страница запрашивается намеренно: на ней срабатывает проверка подмены,
# и потолок оказывается пойманным, а не предположенным.
PAGE_LIMIT = 6
# Пауза на поток: 8 потоков без паузы — это 40 запросов в секунду, и hh на такой
# скорости отдаёт капчу с кодом 200 вместо вакансии (19 страниц из 98 в пилоте).
# Секунда на поток держит темп в пределах 4–8 запросов в секунду.
PAUSE_SECONDS = 1.0
MAX_WORKERS = 8


class BudgetSpent(RuntimeError):
    """Потолок сетевых запросов исчерпан. Сырьё цело, прогон продолжается с того же места."""


class Substituted(RuntimeError):
    """Источник молча отдал не то, что запрошено. HTTP 200 тут ничего не значит."""


def main():
    args = parse_args()
    plan = load_plan(args)
    budget = Budget(args.budget)
    started = time.time()
    raw_before = raw_file_count()

    if args.sites:
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


# --- план -------------------------------------------------------------------


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cities", nargs="+", help="города; по умолчанию все из config.toml")
    parser.add_argument("--rubrics", type=int, help="сколько первых рубрик взять")
    parser.add_argument("--slugs", type=int, help="сколько первых slug'ов hh взять")
    parser.add_argument("--budget", type=int, help="потолок сетевых запросов")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS, help="потоков (максимум 8)")
    parser.add_argument("--sites", action="store_true",
                        help="только главные страницы сайтов компаний из leads.db")
    args = parser.parse_args()
    args.workers = min(args.workers, MAX_WORKERS)
    return args


def load_plan(args):
    """Города, рубрики и slug'и: всё из config.toml, аргументы только урезают объём."""
    config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    return {
        "cities": args.cities or config["cities"],
        "rubrics": config["rubrics"]["include"][: args.rubrics],
        "slugs": config["hh"]["slugs"][: args.slugs],
    }


class Budget:
    """Потолок сетевых запросов. Страница из raw/ бесплатна и в потолок не входит."""

    def __init__(self, cap):
        self.cap = cap
        self.spent = 0
        self.lock = Lock()

    def get(self, url, **kw):
        if fetch.is_cached(url):
            return fetch.get(url, **kw)
        with self.lock:
            if self.cap is not None and self.spent >= self.cap:
                raise BudgetSpent(f"потолок {self.cap} сетевых запросов исчерпан")
            self.spent += 1
        time.sleep(PAUSE_SECONDS)
        return fetch.get(url, **kw)


# --- 2GIS -------------------------------------------------------------------


def collect_org_lists(budget, plan, workers):
    """Страницы рубрик. Возвращает пары (branch_id, город) для карточек филиалов."""
    jobs = [(city, rubric) for city in plan["cities"] for rubric in plan["rubrics"]]
    print(f"2GIS списки: {len(jobs)} рубрик×городов, до {PAGE_LIMIT} страниц каждая")

    branches = set()
    for (city, rubric), result, error in in_parallel(rubric_pages, budget, jobs, workers):
        if error:
            print(f"  рубрика {rubric}/{city}: {type(error).__name__}: {error}")
            continue
        found, reason = result
        branches |= set(found)
        print(f"  рубрика {rubric}/{city}: {len(found)} организаций — {reason}")
    print(f"  итого организаций после дедупа: {len(branches)}")
    return sorted(branches)


def rubric_pages(budget, job):
    """Страницы одной рубрики города до потолка пагинации.

    currentPage — единственный честный признак: начиная с шестой страницы 2GIS
    отвечает HTTP 200 с содержимым ПЕРВОЙ, а total и pages продолжают обещать
    больше. Расширять охват приходится числом рубрик, а не пагинацией.
    """
    city, rubric = job
    state = page_state(budget, city, rubric, 1)
    total, pages, current = sources.parse_search_meta(state)
    if current != 1:
        raise Substituted(f"первая страница отдала страницу {current}")

    branches = branch_ids(state, city, rubric)
    last = min(pages, PAGE_LIMIT)
    for page in range(2, last + 1):
        state = page_state(budget, city, rubric, page)
        current = sources.parse_search_meta(state)[2]
        if current != page:
            return branches, (
                f"стр. {page} вернула страницу {current} — потолок пагинации, "
                f"из {total} по версии 2GIS доступно {len(branches)}"
            )
        branches += branch_ids(state, city, rubric)
    return branches, f"стр. {last} — последняя из {pages} по версии 2GIS, всего {total}"


def page_state(budget, city, rubric, page):
    url = RUBRIC_PAGE.format(city=city, rubric=rubric, page=page)
    return sources.parse_initial_state(budget.get(url, cookies=GIS_COOKIE))


def branch_ids(state, city, rubric):
    return [(row["branch_id"], city) for row in sources.parse_org_list(state, city, rubric)]


def collect_firm_cards(budget, branches, workers):
    """Карточки филиалов: телефон, сайт, email, Instagram, WhatsApp. Разбор — в build.py."""
    print(f"2GIS карточки: {len(branches)}")
    download_all(firm_card, budget, branches, workers)


def firm_card(budget, job):
    branch_id, city = job
    budget.get(FIRM_CARD.format(city=city, branch_id=branch_id), cookies=GIS_COOKIE)


# --- hh.kz ------------------------------------------------------------------


def collect_vacancies(budget, plan, workers):
    """Списки slug'ов, затем сами вакансии. Возвращает slug'и, подменённые источником."""
    jobs = [(city, slug) for city in plan["cities"] for slug in plan["slugs"]]
    print(f"hh списки: {len(jobs)} slug'ов×городов")

    vacancy_ids, substituted = set(), []
    for (city, slug), found, error in in_parallel(vacancy_list, budget, jobs, workers):
        if isinstance(error, Substituted):
            substituted.append(f"{slug}/{city}")
            print(f"  ПОДМЕНА {slug}/{city}: {error}")
            continue
        if error:
            print(f"  {slug}/{city}: {type(error).__name__}: {error}")
            continue
        vacancy_ids |= set(found)
        print(f"  {slug}/{city}: {len(found)} вакансий")

    print(f"hh вакансии: {len(vacancy_ids)} после дедупа")
    download_all(vacancy_page, budget, sorted(vacancy_ids), workers)
    return substituted


def vacancy_list(budget, job):
    """id вакансий со страницы slug'а. Отказ, если hh подменил её общим списком.

    Slug — не произвольный запрос, а фиксированная SEO-страница hh. Несуществующий
    редиректит на /vacancies (все вакансии города) и отвечает HTTP 200, поэтому
    сбор без этой проверки молча наберёт 50 посторонних вакансий.
    """
    city, slug = job
    url = VACANCY_LIST.format(city=city, slug=slug)
    html = budget.get(url, headers=HH_HEADERS)
    landed = fetch.final_url(url) or ""
    if slug.lower() not in landed.lower():
        raise Substituted(f"у hh нет страницы '{slug}', запрос увело на {landed}")
    return sources.parse_vacancy_ids(html)


def vacancy_page(budget, vacancy_id):
    budget.get(VACANCY.format(vacancy_id=vacancy_id), headers=HH_HEADERS)


# --- сайты компаний ---------------------------------------------------------


def collect_sites(budget, workers):
    """Главные страницы сайтов компаний — сырьё для сигналов Ф6.

    Только главная, а не сайт целиком: CRM-виджет, рекламный пиксель, форма заявки
    и кнопка WhatsApp живут на ней. Обход 30 страниц на сайт по ARCHITECTURE §8.1
    стоил бы в пятнадцать раз дороже ради того же набора сигналов.

    Домены берутся из leads.db, поэтому шаг идёт вторым проходом: сначала обычный
    сбор и build.py, потом сайты и build.py снова. Всё идемпотентно, порядок
    восстанавливается сам.
    """
    domains = site_domains()
    print(f"сайты компаний: {len(domains)}")
    download_all(site_page, budget, domains, workers)


def site_domains():
    if not DB.exists():
        sys.exit(f"нет {DB}: сначала uv run collect.py && uv run build.py")
    db = sqlite3.connect(DB)
    rows = db.execute(
        "SELECT DISTINCT domain FROM companies WHERE domain IS NOT NULL ORDER BY domain"
    ).fetchall()
    db.close()
    return [row[0] for row in rows]


def site_page(budget, domain):
    """Главная страница сайта. Сайт малого бизнеса может не отвечать — это не ошибка сбора.

    Откат на http нужен не из небрежности: у пятой части сайтов малого бизнеса в КЗ
    сертификат выписан на другое имя или просрочен, и https до них не доходит вовсе.
    Мы читаем публичную страницу и ничего не отправляем, так что понижение протокола
    не создаёт риска — а без него теряется каждый пятый сайт.
    """
    try:
        budget.get(SITE_HOME.format(domain=domain))
    except Exception as error:
        if "certificate" not in str(error).lower() and "SSL" not in str(error):
            raise
        budget.get(SITE_HOME_INSECURE.format(domain=domain))


# --- пул потоков ------------------------------------------------------------


def in_parallel(worker, budget, jobs, workers):
    """worker(budget, job) на каждое задание. Ошибка одного не роняет прогон.

    Отдаёт (задание, результат, ошибка) по мере готовности. Печать — дело
    вызывающего: в рабочих функциях print не появляется, они бегут в потоках.
    """
    with ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(partial(worker, budget), job): job for job in jobs}
        for future in as_completed(futures):
            try:
                yield futures[future], future.result(), None
            except Exception as error:
                yield futures[future], None, error


def download_all(worker, budget, jobs, workers):
    """Скачать пачку однотипных страниц, показывая прогресс одной строкой.

    Ошибки сводятся по типу: при капче или исчерпанном потолке их сотни, и
    построчная печать закопала бы отчёт.
    """
    done, failures, example = 0, Counter(), {}
    for job, _, error in in_parallel(worker, budget, jobs, workers):
        done += 1
        if error:
            kind = type(error).__name__
            failures[kind] += 1
            example.setdefault(kind, f"{job}: {error}")
        print(f"  {done}/{len(jobs)}", end="\r", flush=True)
    print(f"  {done}/{len(jobs)} готово, ошибок {sum(failures.values())}")
    for kind, count in failures.items():
        print(f"    {kind} ×{count} — например {example[kind]}")


# --- отчёт ------------------------------------------------------------------


def report(budget, raw_before, elapsed):
    raw_after = raw_file_count()
    print(
        f"\nсетевых запросов {budget.spent}"
        + (f" из {budget.cap}" if budget.cap else "")
        + f", файлов в raw/ было {raw_before}, стало {raw_after}"
    )
    print(f"за {elapsed / 60:.1f} мин. Дальше: uv run build.py && uv run check.py")


def raw_file_count():
    return len(list(fetch.RAW.iterdir())) if fetch.RAW.exists() else 0


if __name__ == "__main__":
    main()
