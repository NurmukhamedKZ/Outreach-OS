"""Списки организаций 2GIS по рубрике.

Запуск: uv run -m services.probes.gis_list <rubric_id> <city>
Пример: uv run -m services.probes.gis_list 653 almaty      (Бухгалтерские услуги, Алматы)
        uv run -m services.probes.gis_list demo

Города: almaty, astana. Список рубрик даёт services/probes/gis_rubrics.py.
"""

import sys

from services.fetch import JSONL_DIR, get, jsonl
from services.sources import parse_initial_state as state
from services.sources import parse_org_list as orgs
from services.sources import parse_search_meta as meta

COOKIE = {"dg5_museum_accept": "true"}  # снимает редирект на /museum
URL = "https://2gis.kz/{city}/rubric/{rubric}/page/{page}"
OUT = "2gis_list.jsonl"


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
        print(f"новых записей: {jsonl(OUT, rows, 'branch_id')} из {len(rows)} -> {JSONL_DIR}/{OUT}")
