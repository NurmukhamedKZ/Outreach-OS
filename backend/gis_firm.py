"""Контакты организаций из карточек 2GIS.

Читает raw_jsonl_legacy/2gis_list.jsonl и по каждому branch_id забирает карточку филиала.
Своей фильтрации нет: отбор задаётся тем, по каким рубрикам запускался gis_list.py.

Запуск: uv run gis_firm.py [limit]
        uv run gis_firm.py demo
"""

import sys

from fetch import JSONL_DIR, get, jsonl, read
from sources import parse_firm_card
from sources import parse_initial_state as state

COOKIE = {"dg5_museum_accept": "true"}
URL = "https://2gis.kz/{city}/firm/{branch_id}"
SRC = "2gis_list.jsonl"
OUT = "2gis_firm.jsonl"


def contacts(s, branch_id):
    """Строка JSONL: по словарю на филиал, дедуп в fetch.jsonl идёт по branch_id.

    Плоские строки — в sources.parse_firm_card, и leads.db хранит именно их;
    здесь они собираются обратно только ради формы старого файла.
    """
    rows = parse_firm_card(s, branch_id)
    return {
        "branch_id": branch_id,
        "phones": handles(rows, "phone"),
        "website": first(rows, "website"),
        "emails": handles(rows, "email"),
        "instagram": first(rows, "instagram"),
        "whatsapp": handles(rows, "whatsapp"),
    }


def handles(rows, kind):
    return [r["handle"] for r in rows if r["kind"] == kind]


def first(rows, kind):
    found = handles(rows, kind)
    return found[0] if found else None


def fetch_one(branch_id, city):
    html = get(URL.format(city=city, branch_id=branch_id), cookies=COOKIE)
    return contacts(state(html), branch_id)


def collect(limit=None):
    src = read(SRC)
    done = {r["branch_id"] for r in read(OUT)}
    todo = [r for r in src if r["branch_id"] not in done]
    if limit:
        todo = todo[:limit]
    print(f"в списке {len(src)}, уже собрано {len(done)}, берём {len(todo)}")

    rows = []
    for i, r in enumerate(todo, 1):
        try:
            rows.append(fetch_one(r["branch_id"], r["city"]))
        except Exception as e:  # одна битая карточка не должна ронять прогон
            print(f"\n  {r['branch_id']}: {type(e).__name__} {e}")
        print(f"  {i}/{len(todo)}", end="\r", flush=True)
    print()
    return rows


def demo():
    """Разбор карточки с известным набором контактов."""
    r = fetch_one("70000001037962810", "almaty")
    assert len(r["phones"]) == 3, r["phones"]
    assert all(p.startswith("+7") for p in r["phones"]), r["phones"]
    assert r["website"] == "http://vash-buh.kz", r["website"]
    assert "link.2gis" not in r["website"], "редирект не развёрнут"
    assert r["emails"] == ["fin01@vash-buh.kz"], r["emails"]
    assert r["instagram"] and "instagram.com" in r["instagram"], r["instagram"]
    assert len(r["whatsapp"]) == 3, r["whatsapp"]
    print("gis_firm demo ok —", r["website"], r["emails"], f"{len(r['phones'])} тел.")


if __name__ == "__main__":
    if sys.argv[1:2] == ["demo"]:
        demo()
    else:
        limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
        rows = collect(limit)
        print(f"новых записей: {jsonl(OUT, rows, 'branch_id')} из {len(rows)} -> {JSONL_DIR}/{OUT}")
