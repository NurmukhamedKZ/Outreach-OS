"""Массовый сбор сырья — операции воркера. Единственный, кто ходит в сеть.

В базу не кладёт ничего: разбор raw/ -> derived.db делает rebuild. Здесь только
план запросов из config.toml, пул потоков поверх fetch.get и проверки
молчаливой подмены — единственное, что отделяет собранные данные от мусора.

Учёта «что уже скачано» нет и не нужно: raw/ и есть учёт. Повторный запуск
читает страницы с диска и не делает ни одного сетевого запроса.

Операции принимают ровно один аргумент — RunContext (контракт воркера).
Параметры (города, рубрики) берутся из config.toml.
"""

import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit

import logctx
from collector.services import fetch
from collector.services import sources
from collector.services.pipeline import rebuild


RUBRIC_PAGE = "https://2gis.kz/{city}/rubric/{rubric}/page/{page}"
FIRM_CARD = "https://2gis.kz/{city}/firm/{branch_id}"
SITE_HOME = "https://{domain}/"
SITE_HOME_INSECURE = "http://{domain}/"
IG_FEED = "https://www.instagram.com/api/v1/feed/user/{username}/username/?count={count}"

GIS_COOKIE = {"dg5_museum_accept": "true"}  # снимает редирект на /museum

# Публичный web app id инстаграма, статичный. Без него лента отвечает отказом.
IG_APP_ID = "936619743392459"
IG_COOKIES = Path(__file__).resolve().parent.parent.parent / "data" / "cookies.json"
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
# Шестнадцать, а не восемь: 6188 запросов боевого сбора шли 75 минут — это
# 1,36 запроса в секунду при потолке восемь, то есть воркеры семь восьмых
# времени ждут ответа сети. Темп источника держит не число воркеров, а
# Pacer ниже, поэтому пул можно поднимать, не приближая капчу.
MAX_WORKERS = 16


class BudgetSpent(RuntimeError):
    """Потолок сетевых запросов исчерпан. Сырьё цело, прогон продолжается с того же места."""


class Substituted(RuntimeError):
    """Источник молча отдал не то, что запрошено. HTTP 200 тут ничего не значит."""


def gis(ctx):
    """Рубрики и карточки филиалов из config.toml — две стадии одного прогона."""
    config = rebuild.config()
    budget = Budget(None)   # без потолка: дедуп по raw/
    plan = {"cities": config["cities"], "rubrics": config["rubrics"]["include"]}

    jobs = [(city, rubric) for city in plan["cities"] for rubric in plan["rubrics"]]
    ctx.log(f"2GIS списки: {len(jobs)} рубрик×городов, до {PAGE_LIMIT} страниц каждая")

    branches = set()
    for number, (city, rubric), result, error in in_parallel(rubric_pages, budget, jobs):
        ctx.check_cancelled()
        if error:
            ctx.log(f"  рубрика {rubric}/{city}: {type(error).__name__}: {error}")
            continue
        found, reason = result
        branches |= set(found)
        ctx.log(f"  рубрика {rubric}/{city}: {len(found)} организаций — {reason}")
        ctx.progress(number, len(jobs), "рубрики 2GIS")
    ctx.log(f"  итого организаций после дедупа: {len(branches)}")
    sorted_branches = sorted(branches)

    ctx.log(f"2GIS карточки: {len(sorted_branches)}")
    collected, skipped = download_all(firm_card, budget, sorted_branches, ctx, "карточки 2GIS")
    return {"lists": len(jobs), "branches": len(sorted_branches),
            "cards_collected": collected, "cards_skipped": skipped}


def sites(ctx):
    """Главные страницы сайтов компаний — сырьё для сигналов Ф6.

    Только главная, а не сайт целиком: CRM-виджет, рекламный пиксель, форма заявки
    и кнопка WhatsApp живут на ней. Обход 30 страниц на сайт по ARCHITECTURE §8.1
    стоил бы в пятнадцать раз дороже ради того же набора сигналов.

    Домены берутся из view текущего прогона, поэтому шаг идёт вторым проходом:
    компании, найденные сбором выше, появятся в выдаче только после rebuild, и
    их сайты соберёт следующий запуск. Всё идемпотентно, порядок восстанавливается
    сам — но пустая выдача значит «сначала пересборка», а не «сайтов нет».
    """
    from collector.services import store as engine
    db = engine.connect()
    try:
        domains = [r[0] for r in db.execute(
            "SELECT DISTINCT domain FROM companies WHERE domain IS NOT NULL"
            " ORDER BY domain")]
    finally:
        db.close()
    if not domains:
        ctx.log("выдача пуста — собирать нечего. Сначала пересборка (rebuild)")
        return {"domains": 0, "collected": 0, "skipped": 0}
    budget = Budget(None)   # без потолка: только главные страницы, дедуп по raw/
    ctx.log(f"сайты компаний: {len(domains)}")
    collected, skipped = download_all(site_page, budget, domains, ctx, "сайты компаний")
    return {"domains": len(domains), "collected": collected, "skipped": skipped}


REVIEWS_API = "https://public-api.reviews.2gis.com/2.0/branches/{branch_id}/reviews"


def reviews(ctx):
    """Отзывы филиалов 2GIS. Один запрос на филиал, свежие сверху.

    Источник — публичный API отзывов, недокументированный: ключ живёт в
    config.toml ([reviews].key). branch_id берётся из view текущего прогона
    (склейка Ф5 уже свела филиалы к компаниям). Страницы ложатся в raw/ как
    обычно — дедуп по ним же, повторный запуск не делает ни одного запроса.
    """
    from collector.services import store as engine
    config = rebuild.config()
    reviews_cfg = config["reviews"]
    db = engine.connect()
    try:
        branches = [r[0] for r in db.execute(
            "SELECT DISTINCT branch_id FROM orgs WHERE review_count > 0"
            " ORDER BY branch_id")]
    finally:
        db.close()
    if not branches:
        ctx.log("филиалов с отзывами нет — собирать нечего")
        return {"branches": 0, "collected": 0, "skipped": 0}
    budget = Budget(None)
    ctx.log(f"отзывы 2GIS: {len(branches)} филиалов с отзывами")

    def fetch_reviews(budget, branch_id):
        url = REVIEWS_API.format(branch_id=branch_id) + (
            f"?limit={reviews_cfg['limit']}&sort_by=date_edited&rated=true"
            f"&locale=ru_KZ&key={reviews_cfg['key']}")
        budget.get(url, headers={"Accept": "application/json"})

    collected, skipped = download_all(fetch_reviews, budget, branches, ctx, "отзывы 2GIS")
    return {"branches": len(branches), "collected": collected, "skipped": skipped}


def site_pages(ctx):
    """Внутренние страницы сайта — по ссылкам с главной из словаря config.toml.

    Главная уже собрана (collect.sites). Здесь достраивается глубина: до
    max_pages внутренних страниц (о компании, услуги, цены, кейсы, вакансии).
    Страница вакансий возвращает hiring-сигнал, потерянный с удалением hh.
    """
    from collector.services import store as engine
    config = rebuild.config()
    links_cfg = config["site"]["links"]
    db = engine.connect()
    try:
        rows = db.execute("SELECT DISTINCT domain FROM companies WHERE domain IS NOT NULL"
                          " ORDER BY domain").fetchall()
    finally:
        db.close()
    if not rows:
        ctx.log("выдача пуста — собирать нечего. Сначала пересборка (rebuild)")
        return {"sites": 0, "collected": 0, "skipped": 0}
    budget = Budget(None)
    jobs = [(row[0], SITE_HOME.format(domain=row[0])) for row in rows]
    ctx.log(f"внутренние страницы сайтов: {len(jobs)} главных")

    def inner_pages(budget, job):
        domain, home = job
        html = budget.get(home)
        for url in sources.parse_site_links(html, home, domain,
                                            links_cfg["keywords"], links_cfg["max_pages"]):
            budget.get(url)

    collected, skipped = download_all(inner_pages, budget, jobs, ctx, "внутренние страницы")
    return {"sites": len(jobs), "collected": collected, "skipped": skipped}


def instagram(ctx):
    """Ленты аккаунтов компаний без сайта — сырьё для сигналов Ф6.

    Берётся лента, а не профиль: web_profile_info отвечает 400 на половине
    аккаунтов и постов больше не отдаёт вовсе, тогда как лента ответила на всех
    восемнадцати проверенных. Один запрос на компанию, второго нет.

    В один поток и с паузой: сессия личная, и цена ошибки здесь — не потерянный
    прогон, а заблокированный аккаунт живого человека.
    """
    from collector.services import store as engine
    db = engine.connect()
    try:
        rows = db.execute(
            "SELECT DISTINCT c.handle FROM company_links l"
            " JOIN contacts c ON c.branch_id = l.branch_id"
            " JOIN companies co ON co.company_id = l.company_id"
            " WHERE c.kind = 'instagram' AND co.domain IS NULL"
            " ORDER BY c.handle"
        ).fetchall()
    finally:
        db.close()
    accounts = [sources.ig_username(row[0]) for row in rows]
    if not accounts:
        ctx.log("аккаунтов в выдаче нет — сначала пересборка (rebuild)")
        return {"accounts": 0, "collected": 0, "failed": 0}
    if not IG_COOKIES.exists():
        raise RuntimeError(f"нет {IG_COOKIES}: сначала uv run ../scripts/ig/login.py")
    jar = json.loads(IG_COOKIES.read_text(encoding="utf-8"))

    ctx.log(f"инстаграм: {len(accounts)} аккаунтов, по одному, пауза {IG_PAUSE_SECONDS} с")
    budget = Budget(None)
    done = failures = in_row = 0
    canary = None
    for number, username in enumerate(accounts, 1):
        ctx.check_cancelled()
        try:
            instagram_feed(budget, jar, username)
            done += 1
            in_row = 0
            canary = canary or username
        except BudgetSpent as spent:
            ctx.log(f"\n  {spent}")
            break
        except Exception as error:
            failures += 1
            in_row += 1
            ctx.log(f"\n  {username}: {type(error).__name__}: {error}")
            if in_row >= IG_FAILURES_IN_ROW and canary:
                if instagram_session_alive(jar, canary):
                    ctx.log(f"  (сессия жива — {canary} отвечает; это удалённые аккаунты)")
                    in_row = 0
                else:
                    raise RuntimeError(
                        f"ОТКАЗ: {in_row} отказа подряд, и контрольный аккаунт "
                        f"{canary} тоже молчит — сессия инстаграма умерла.\n"
                        f"Собрано {done} лент, они целы. Обнови куки: "
                        "uv run ../scripts/ig/login.py, потом повтори — уже скачанное не перекачивается."
                    )
        ctx.progress(number, len(accounts), "ленты инстаграма")
    ctx.log(f"  {done + failures}/{len(accounts)} обработано, лент {done}, отказов {failures}")
    return {"accounts": len(accounts), "collected": done, "failed": failures}


def ig_comments(ctx):
    """Комментарии постов с comment_count > 0. Основная защита от бана.

    Правило «только посты с комментариями» вычёркивает половину запросов.
    В один поток с паузой, как ленты: сессия личная, цена бана — аккаунт человека.
    """
    config = rebuild.config()
    media_url = config["instagram"]["comments_media_url"]
    jar = instagram_cookies()
    posts = posts_with_comments()
    if not posts:
        ctx.log("постов с комментариями в raw/ нет — сначала сбор (collect.instagram)")
        return {"posts": 0, "collected": 0, "failed": 0}
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
    """Профили аккаунтов users/{pk}/info/. Полнота не гарантируется: часть откажет.

    Эндпоинт требует числового pk пользователя, а не логина: карта username->pk
    собирается из ответов лент (feed/user отдаёт user.pk). Био и подписчики
    приходят только этим запросом — feed/user их не отдаёт.
    """
    config = rebuild.config()
    info_url = config["instagram"]["profile_info_url"]
    jar = instagram_cookies()
    accounts = instagram_user_ids()
    if not accounts:
        ctx.log("аккаунтов в raw/ нет — сначала сбор (collect.instagram)")
        return {"accounts": 0, "collected": 0, "failed": 0}
    ctx.log(f"инстаграм: профили {len(accounts)} аккаунтов")
    budget = Budget(None)
    done = failed = 0
    for number, (pk, username) in enumerate(accounts, 1):
        ctx.check_cancelled()
        url = info_url.format(pk=pk)
        try:
            budget.get(url, cookies=jar,
                       headers={"x-ig-app-id": IG_APP_ID,
                                "referer": f"https://www.instagram.com/{username}/"})
            done += 1
            time.sleep(IG_PAUSE_SECONDS)
        except Exception as error:
            failed += 1
            ctx.log(f"\n  {username}: {type(error).__name__}: {error}")
        ctx.progress(number, len(accounts), "профили инстаграма")
    return {"accounts": len(accounts), "collected": done, "failed": failed}


def posts_with_comments():
    """(media_pk, username) постов с comment_count > 0 из сырья лент в raw/.

    Срез тот же, что берёт analyze.instagram_targets: в ленте 12 постов
    (IG_POST_COUNT входит в адрес, а значит в ключ кэша страницы, и менять
    его нельзя), а в промпт уходят первые posts_limit. Комментарии к
    остальным не читает никто — на живом raw/ это 221 запрос из 1441,
    по шесть секунд каждый.
    """
    limit = rebuild.config()["instagram"]["posts_limit"]
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


def instagram_user_ids():
    """(числовой pk пользователя, username) из сырья лент в raw/.

    users/{pk}/info/ принимает числовой id, а не логин: pk берётся из объекта
    user ответа feed/user, где он есть всегда.
    """
    import json as _json
    out = []
    for page in rebuild.load_pages():
        if "feed/user/" not in page["url"]:
            continue
        data = _json.loads(sources.json_body(rebuild.html_of(page)))
        user = data.get("user") or {}
        pk, username = user.get("pk"), user.get("username")
        if pk and username:
            out.append((pk, username))
    return out


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

    def recalibrate(self, intervals, default):
        """Новые темпы из конфига. Карта занятых слотов не трогается — она и
        есть состояние темпа, а не его настройка."""
        self.intervals, self.default = intervals, default

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


_PACER = None
# Собственный лок, а не Pacer.lock: тот сторожит карту слотов и появляется
# вместе с экземпляром, а сторожить надо как раз его создание.
_PACER_LOCK = Lock()


def default_pacer():
    """Темпы из config.toml. Значения — калибровочная ручка: источник ответил
    капчей — поднимают их, а не правят код.

    Pacer один на процесс, а не один на Budget. Каждая операция строит свой
    Budget, и до задачи 4 это было безразлично — операции шли по очереди. С
    дорожками две операции идут одновременно, и по своему Pacer у каждой
    означало бы по своему лимиту на один и тот же хост: темп удвоился бы
    молча, ровно в той правке, которая дорожки и вводит. Темп принадлежит
    хосту — значит и карта занятых слотов одна на процесс.

    Интервалы перечитываются на каждый вызов: config.toml — ручка, и правка
    не должна ждать перезапуска бэкенда. А занятые слоты переживают
    перечитывание: они и есть состояние темпа, и обнулить их значило бы
    выпустить пул залпом ровно в тот момент, когда оператор крутит ручку
    из-за капчи.

    default вынимается отдельной строкой: «default» — не имя домена, и в карте
    хостов ему места нет.
    """
    global _PACER
    pacing = dict(rebuild.config()["pacing"])
    default = pacing.pop("default")
    # Проверка и присваивание — под локом: дорожки одной стадии строят свои
    # Budget одновременно, и без него обе успевали в окно между «is None» и
    # присваиванием. Каждая уносила свой Pacer со своей картой слотов — то
    # есть по своему лимиту на один хост, ровно то, что этот экземпляр и
    # существует, чтобы не допустить.
    with _PACER_LOCK:
        if _PACER is None:
            _PACER = Pacer(pacing, default)
        else:
            _PACER.recalibrate(pacing, default)
        return _PACER


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


# --- 2GIS -------------------------------------------------------------------


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


def firm_card(budget, job):
    branch_id, city = job
    budget.get(FIRM_CARD.format(city=city, branch_id=branch_id), cookies=GIS_COOKIE)


# --- сайты компаний ---------------------------------------------------------


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


def instagram_cookies():
    if not IG_COOKIES.exists():
        raise RuntimeError(f"нет {IG_COOKIES}: сначала uv run ../scripts/ig/login.py")
    return json.loads(IG_COOKIES.read_text(encoding="utf-8"))


def instagram_session_alive(jar, username):
    """Отвечает ли инстаграм на аккаунт, который уже отвечал в этом прогоне.

    Мимо слоя сырья намеренно: страница этого аккаунта уже лежит в raw/, и
    fetch.get вернул бы её с диска, ничего не проверив. Ответ здесь не сохраняется
    — это проба живости, а не данные.

    Живость — валидный JSON с "status": "ok", а не наличие постов у самого
    аккаунта: протухшая сессия отвечает HTML-страницей логина (json.loads падает,
    ловится ниже), а живая сессия отвечает JSON и на аккаунт без своих постов —
    приватный, деактивированный или просто пустой. Раньше проверялось наличие
    user.username, и контрольный аккаунт без доступных постов ложно считался
    признаком мёртвой сессии на каждом прогоне.
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
        data = json.loads(sources.json_body(body))
        return page.status == 200 and data.get("status") == "ok"
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


def in_parallel(worker, budget, jobs):
    """worker(budget, job) на каждое задание. Ошибка одного не роняет прогон.

    Отдаёт (номер, задание, результат, ошибка) по мере готовности. Печать — дело
    вызывающего: в рабочих функциях print не появляется, они бегут в потоках.

    job_id пробрасывается в пул явно: ThreadPoolExecutor не копирует
    contextvars вызывающего потока в свои воркер-потоки (в отличие от
    asyncio.to_thread). entity ставится и сбрасывается на каждое задание —
    пул переиспользует воркер-потоки между заданиями, и без reset домен
    предыдущего задания утёк бы в лог следующего, выполненного на том же
    потоке.
    """
    job_id = logctx.current_job_id()

    def run(job):
        logctx.set_job_id(job_id)
        with logctx.entity(str(job)):
            return worker(budget, job)

    with ThreadPoolExecutor(MAX_WORKERS) as pool:
        futures = {pool.submit(run, job): job for job in jobs}
        for number, future in enumerate(as_completed(futures), 1):
            try:
                yield number, futures[future], future.result(), None
            except Exception as error:
                yield number, futures[future], None, error


def download_all(worker, budget, jobs, ctx, label):
    """Скачать пачку однотипных страниц, показывая прогресс одной строкой.

    Ошибки сводятся по типу: при капче или исчерпанном потолке их сотни, и
    построчная печать закопала бы отчёт.
    """
    done, failures, example = 0, Counter(), {}
    for number, job, _, error in in_parallel(worker, budget, jobs):
        ctx.check_cancelled()
        done += 1
        if error:
            kind = type(error).__name__
            failures[kind] += 1
            example.setdefault(kind, f"{job}: {error}")
        ctx.progress(number, len(jobs), label)
    ctx.log(f"  {done}/{len(jobs)} готово, ошибок {sum(failures.values())}")
    for kind, count in failures.items():
        ctx.log(f"    {kind} ×{count} — например {example[kind]}")
    return done - sum(failures.values()), sum(failures.values())