"""Справочник рубрик 2GIS — дерево категорий с id.

Рубрика = категория справочника, машинный эквивалент сегмента ICP. Её id идёт в
gis_list.py, а чёрный список конкурентов — это тоже просто набор id.

Запуск: uv run gis_rubrics.py [root_id ...]   без аргументов — весь рубрикатор
        uv run gis_rubrics.py 110609          только ветка B2B-услуг
        uv run gis_rubrics.py demo

Обход всего дерева — заметно больше запросов, чем одной ветки: лист тоже стоит GET,
потому что заранее неизвестно, есть ли у него дети.
"""

import sys

from fetch import JSONL_DIR, HttpError, get, jsonl
from sources import parse_initial_state as state

COOKIE = {"dg5_museum_accept": "true"}
ROOT = "https://2gis.kz/almaty/rubrics"
BRANCH = "https://2gis.kz/almaty/rubrics/subrubrics/{id}"
OUT = "2gis_rubrics.jsonl"


def named(rubricator, ids):
    """Пары (id, название) для перечисленных id. Длинные id — геообъекты, не рубрики."""
    out = []
    for i in ids:
        if i.isdigit() and len(i) < 10:
            v = rubricator["items"].get(i) or {}
            out.append((i, v.get("name") or v.get("caption")))
    return out


def roots():
    r = state(get(ROOT, cookies=COOKIE))["data"]["rubricator"]
    return named(r, next(iter(r["lists"].values()))["data"])  # на корне список один


def branch(rubric_id):
    """Своё название и дети рубрики. Дети — только в lists['67_<id>_ru_KZ'].

    items трогать нельзя: это общее хранилище узлов страницы, включая соседние
    ветки. Обход по нему уходит во весь рубрикатор вместо запрошенной ветки.
    """
    try:
        html = get(BRANCH.format(id=rubric_id), cookies=COOKIE)
    except HttpError as e:
        if e.status == 404:
            return None, []  # у листа нет страницы подрубрик
        raise
    r = state(html)["data"]["rubricator"]
    own = (r["items"].get(rubric_id) or {}).get("name")
    kids = r["lists"].get(f"67_{rubric_id}_ru_KZ")
    return own, (named(r, kids["data"]) if kids else [])


def walk(start=None):
    """Обход вширь от корней или от заданных рубрик."""
    queue = [(i, None, None) for i in start] if start else \
            [(i, name, None) for i, name in roots()]
    rows, seen = [], set()
    while queue:
        rid, name, parent = queue.pop(0)
        if rid in seen:
            continue
        seen.add(rid)
        own, kids = branch(rid)
        rows.append({"id": rid, "name": name or own, "parent_id": parent})
        queue += [(k, kname, rid) for k, kname in kids]
        print(f"  рубрик {len(rows)}, в очереди {len(queue)}", end="\r", flush=True)
    print()
    return rows


def demo():
    """Ветка B2B-услуг: известные id на известных местах."""
    rows = {r["id"]: r for r in walk(["110613"])}
    assert rows["110613"]["name"] == "Бизнес-услуги", rows["110613"]
    assert "653" in rows, "рубрика Бухгалтерских услуг не найдена"
    assert rows["653"]["name"] == "Бухгалтерские услуги", rows["653"]
    assert rows["653"]["parent_id"] == "110613", rows["653"]
    assert rows["339"]["name"] == "Аудиторские услуги", rows["339"]
    assert len(rows) > 20, f"ожидали больше 20 рубрик в ветке, получили {len(rows)}"
    print(f"gis_rubrics demo ok — {len(rows)} рубрик в ветке «Бизнес-услуги»")


if __name__ == "__main__":
    if sys.argv[1:2] == ["demo"]:
        demo()
    else:
        rows = walk(sys.argv[1:] or None)
        print(f"новых записей: {jsonl(OUT, rows, 'id')} из {len(rows)} -> {JSONL_DIR}/{OUT}")
