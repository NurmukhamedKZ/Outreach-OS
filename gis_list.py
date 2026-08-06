"""Списки организаций 2GIS по рубрике.

Запуск: uv run gis_list.py <rubric_id> <city>
Пример: uv run gis_list.py 653 almaty      (Бухгалтерские услуги, Алматы)
        uv run gis_list.py demo

Города: almaty, astana. Список рубрик даёт gis_rubrics.py.
"""

import json
import re
import sys

from fetch import get, jsonl

COOKIE = {"dg5_museum_accept": "true"}  # снимает редирект на /museum
URL = "https://2gis.kz/{city}/rubric/{rubric}/page/{page}"
OUT = "2gis_list.jsonl"


def state(html):
    """initialState — JS-строка с JSON внутри. Снимаем только JS-экранирование."""
    raw = re.search(r"var initialState = JSON\.parse\('(.*?)'\);", html, re.S).group(1)
    return json.loads(re.sub(r"\\(['\\])", r"\1", raw))


def meta(s):
    """total, pages и фактическая страница из ветки поиска."""
    prof = s["data"]["search"]["profile"]
    d = prof[next(iter(prof))]["data"]
    return d["total"], d["pages"], d.get("currentPage")


def orgs(s, city, rubric):
    """Карточки организаций из ветки entity. Записи без org — не организации."""
    rows = []
    for branch_id, node in s["data"]["entity"]["profile"].items():
        d = node.get("data") or {}
        org = d.get("org")
        if not org:
            continue
        rev = d.get("reviews") or {}
        rows.append({
            "branch_id": branch_id,
            "org_id": org.get("id"),
            "name": d.get("name"),
            "org_name": org.get("name"),
            "branch_count": org.get("branch_count"),
            "rubrics": [r.get("name") for r in d.get("rubrics") or []],
            "address": d.get("address_name"),
            "point": d.get("point"),
            "review_count": rev.get("general_review_count"),
            "rating": rev.get("general_rating"),
            "city": city,
            "rubric_id": str(rubric),
        })
    return rows


def collect(rubric, city):
    """Собрать рубрику до потолка пагинации.

    2GIS отдаёт максимум 5 страниц (60 организаций) на рубрику на город: начиная с
    шестой возвращается HTTP 200 с содержимым ПЕРВОЙ страницы, а `pages` и `total`
    продолжают обещать больше. Единственный честный признак — `currentPage`.
    Расширять охват приходится не пагинацией, а числом рубрик и городов.
    """
    s = state(get(URL.format(city=city, rubric=rubric, page=1), cookies=COOKIE))
    total, pages, _ = meta(s)
    print(f"рубрика {rubric} / {city}: {total} организаций, {pages} страниц по версии 2GIS")

    rows = orgs(s, city, rubric)
    for n in range(2, pages + 1):
        html = get(URL.format(city=city, rubric=rubric, page=n), cookies=COOKIE)
        s = state(html)
        _, _, current = meta(s)
        if current != n:
            print(f"  стр. {n}: 2GIS вернул страницу {current} — потолок пагинации, стоп")
            print(f"  ДОСТУПНО {len(rows)} из {total}: остальное этим путём не берётся")
            break
        rows += orgs(s, city, rubric)
        print(f"  стр. {n}/{pages} — собрано {len(rows)}", end="\r", flush=True)
    print()
    return rows


def demo():
    """Разбор страницы рубрики 653 (Алматы) и потолок пагинации."""
    s = state(get(URL.format(city="almaty", rubric=653, page=1), cookies=COOKIE))
    total, pages, current = meta(s)
    assert total > 100 and pages > 1, (total, pages)
    assert current == 1, current

    # шестая страница молча подменяется первой — collect обязан это ловить
    s6 = state(get(URL.format(city="almaty", rubric=653, page=6), cookies=COOKIE))
    assert meta(s6)[2] == 1, "потолок пагинации исчез — проверить логику collect"
    assert len(collect(653, "almaty")) == 60, "collect должен остановиться на 60"

    rows = orgs(s, "almaty", 653)
    assert len(rows) == 12, f"ожидали 12 организаций на странице, получили {len(rows)}"

    r = rows[0]
    assert r["branch_id"] and r["org_id"], r
    assert r["branch_id"] != r["org_id"], "филиал и компания не должны совпадать"
    assert r["name"], r
    # кириллица не побита разбором JS-строки
    assert any("Бухгалтерские услуги" in (x["rubrics"] or []) for x in rows), \
        [x["rubrics"] for x in rows]
    assert all(x["city"] == "almaty" and x["rubric_id"] == "653" for x in rows)
    print(f"gis_list demo ok — {total} организаций в рубрике, разобрано {len(rows)}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["demo"]:
        demo()
    else:
        if len(sys.argv) < 3:
            sys.exit(__doc__)
        rubric, city = sys.argv[1], sys.argv[2]
        rows = collect(rubric, city)
        print(f"новых записей: {jsonl(OUT, rows, 'branch_id')} из {len(rows)} -> raw/{OUT}")
