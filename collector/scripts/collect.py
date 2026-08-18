"""Массовый сбор сырья. Единственный модуль проекта, который ходит в сеть.

В базу не кладёт ничего: разбор raw/ -> leads.db делает build.py. Здесь только
план запросов из config.toml, пул потоков поверх fetch.get и две проверки
молчаливой подмены — единственное, что отделяет собранные данные от мусора.

Учёта «что уже скачано» нет и не нужно: raw/ и есть учёт. Повторный запуск
читает страницы с диска и не делает ни одного сетевого запроса.

Запуск:
  uv run -m scripts.collect                                      полный объём из config.toml
  uv run -m scripts.collect --cities almaty --rubrics 5 --budget 600 --workers 4
"""

import argparse
import json
import sqlite3
import sys
import time
import tomllib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
from pathlib import Path
from threading import Lock

from services import fetch
from services import sources

CONFIG = Path("config.toml")
DB = Path("db/leads.db")

RUBRIC_PAGE = "https://2gis.kz/{city}/rubric/{rubric}/page/{page}"
FIRM_CARD = "https://2gis.kz/{city}/firm/{branch_id}"
SITE_HOME = "https://{domain}/"
SITE_HOME_INSECURE = "http://{domain}/"
IG_FEED = "https://www.instagram.com/api/v1/feed/user/{username}/username/?count={count}"

GIS_COOKIE = {"dg5_museum_accept": "true"}  # снимает редирект на /museum

# Публичный web app id инстаграма, статичный. Без него лента отвечает отказом.
IG_APP_ID = "936619743392459"
IG_COOKIES = Path("data/cookies.json")
# Двенадцать постов — столько же, сколько показывает сетка профиля. Больше не берём:
# сигналы считаются по свежему хвосту, а длина запроса растёт линейно.
IG_POST_COUNT = 12
# Шесть секунд на аккаунт. Сессия личная, и цена бана — не потерянный прогон, а
# аккаунт живого человека. 288 аккаунтов при таком темпе — около получаса.
IG_PAUSE_SECONDS = 6
# Сколько отказов подряд считать поводом заподозрить смерть сессии. Само по себе
# это не приговор: список отсортирован по алфавиту, а удалённые аккаунты в нём
# соседствуют — три подряд выпадают и при совершенно живой сессии. Поэтому по
# этому порогу прогон не останавливается, а перепроверяется на живом аккаунте.
IG_FAILURES_IN_ROW = 3

# 2GIS отдаёт 60 организаций (5 страниц) на рубрику города, но обещает больше.
# Шестая страница запрашивается намеренно: на ней срабатывает проверка подмены,
# и потолок оказывается пойманным, а не предположенным.
PAGE_LIMIT = 6
# Пауза на поток: 8 потоков без паузы — это 40 запросов в секунду, и 2GIS на такой
# скорости отвечает капчей. Секунда на поток держит темп в пределах 4–8 запросов.
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

    if args.instagram:
        collect_instagram(budget)
    elif args.sites:
        collect_sites(budget, args.workers)
    else:
        branches = collect_org_lists(budget, plan, args.workers)
        collect_firm_cards(budget, branches, args.workers)

    report(budget, raw_before, time.time() - started)


# --- план -------------------------------------------------------------------


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cities", nargs="+", help="города; по умолчанию все из config.toml")
    parser.add_argument("--rubrics", type=int, help="сколько первых рубрик взять")
    parser.add_argument("--budget", type=int, help="потолок сетевых запросов")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS, help="потоков (максимум 8)")
    parser.add_argument("--sites", action="store_true",
                        help="только главные страницы сайтов компаний из leads.db")
    parser.add_argument("--instagram", action="store_true",
                        help="только ленты инстаграма компаний без сайта")
    args = parser.parse_args()
    args.workers = min(args.workers, MAX_WORKERS)
    return args


def load_plan(args):
    """Города и рубрики: всё из config.toml, аргументы только урезают объём."""
    config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    return {
        "cities": args.cities or config["cities"],
        "rubrics": config["rubrics"]["include"][: args.rubrics],
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
        sys.exit(f"нет {DB}: сначала uv run -m scripts.collect && uv run build.py")
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


# --- инстаграм --------------------------------------------------------------


def collect_instagram(budget):
    """Ленты аккаунтов компаний без сайта — сырьё для сигналов Ф6.

    Берётся лента, а не профиль: web_profile_info отвечает 400 на половине
    аккаунтов и постов больше не отдаёт вовсе, тогда как лента ответила на всех
    восемнадцати проверенных. Один запрос на компанию, второго нет.

    В один поток и с паузой: сессия личная, и цена ошибки здесь — не потерянный
    прогон, а заблокированный аккаунт живого человека.
    """
    accounts = instagram_accounts()
    print(f"инстаграм: {len(accounts)} аккаунтов, по одному, пауза {IG_PAUSE_SECONDS} с")
    jar = instagram_cookies()

    done = failures = in_row = 0
    canary = None
    for number, username in enumerate(accounts, 1):
        try:
            instagram_feed(budget, jar, username)
            done += 1
            in_row = 0
            canary = canary or username
        except BudgetSpent as spent:
            # Не ошибка сбора, а штатная остановка: сырьё цело, повтор продолжит
            # с того же места. Отчёт должен напечататься, поэтому выходим из цикла.
            print(f"\n  {spent}")
            break
        except Exception as error:
            failures += 1
            in_row += 1
            print(f"\n  {username}: {type(error).__name__}: {error}")
            if in_row >= IG_FAILURES_IN_ROW and canary:
                if instagram_session_alive(jar, canary):
                    print(f"  (сессия жива — {canary} отвечает; это удалённые аккаунты)")
                    in_row = 0
                else:
                    sys.exit(
                        f"\nОТКАЗ: {in_row} отказа подряд, и контрольный аккаунт "
                        f"{canary} тоже молчит — сессия инстаграма умерла.\n"
                        f"Собрано {done} лент, они целы. Обнови куки: "
                        "uv run -m ig.login, потом повтори — уже скачанное не перекачивается."
                    )
        print(f"  {number}/{len(accounts)}", end="\r", flush=True)
    print(f"  {done + failures}/{len(accounts)} обработано, лент {done}, отказов {failures}")


def instagram_accounts():
    """Логины аккаунтов компаний, у которых нет сайта.

    Именно они сегодня не дают ни одного сигнала: site_signals читает главную
    страницу, а её нет. Компании с сайтом сигналы уже получают, и трогать их
    ради тех же событий незачем.
    """
    if not DB.exists():
        sys.exit(f"нет {DB}: сначала uv run -m scripts.collect && uv run build.py")
    db = sqlite3.connect(DB)
    rows = db.execute(
        "SELECT DISTINCT c.handle FROM company_links l"
        " JOIN contacts c ON c.branch_id = l.branch_id"
        " JOIN companies co ON co.company_id = l.company_id"
        " WHERE c.kind = 'instagram' AND co.domain IS NULL"
        " ORDER BY c.handle"
    ).fetchall()
    db.close()
    return [sources.ig_username(row[0]) for row in rows]


def instagram_cookies():
    if not IG_COOKIES.exists():
        sys.exit(f"нет {IG_COOKIES}: сначала uv run -m ig.login")
    return json.loads(IG_COOKIES.read_text(encoding="utf-8"))


def instagram_session_alive(jar, username):
    """Отвечает ли инстаграм на аккаунт, который уже отвечал в этом прогоне.

    Мимо слоя сырья намеренно: страница этого аккаунта уже лежит в raw/, и
    fetch.get вернул бы её с диска, ничего не проверив. Ответ здесь не сохраняется
    — это проба живости, а не данные.
    """
    from scrapling.fetchers import Fetcher

    try:
        page = Fetcher.get(
            IG_FEED.format(username=username, count=IG_POST_COUNT),
            cookies=jar,
            impersonate="chrome",
            headers={"x-ig-app-id": IG_APP_ID, "referer": f"https://www.instagram.com/{username}/"},
        )
        body = page.body if isinstance(page.body, str) else page.body.decode("utf-8", "replace")
        return page.status == 200 and bool(sources.parse_ig_feed(body)["username"])
    except Exception:
        return False


def instagram_feed(budget, jar, username):
    """Одна лента. Пустой ответ без аккаунта — это умершая сессия, а не пустой блог.

    Инстаграм на протухшей сессии отвечает HTTP 200 и страницей логина, то есть
    ровно той же молчаливой подменой, что 2GIS и hh. Статус тут не помогает.
    """
    url = IG_FEED.format(username=username, count=IG_POST_COUNT)
    cached = fetch.is_cached(url)
    body = budget.get(
        url,
        cookies=jar,
        headers={"x-ig-app-id": IG_APP_ID, "referer": f"https://www.instagram.com/{username}/"},
    )
    if not sources.parse_ig_feed(body)["username"]:
        raise Substituted(f"в ответе нет аккаунта — сессия протухла или {username} удалён")
    if not cached:
        time.sleep(IG_PAUSE_SECONDS)


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
    print(f"за {elapsed / 60:.1f} мин. Дальше: uv run build.py && uv run -m scripts.check")


def raw_file_count():
    return len(list(fetch.RAW.iterdir())) if fetch.RAW.exists() else 0


if __name__ == "__main__":
    main()
