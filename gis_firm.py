"""Контакты организаций из карточек 2GIS.

Читает raw/2gis_list.jsonl и по каждому branch_id забирает карточку филиала.
Своей фильтрации нет: отбор задаётся тем, по каким рубрикам запускался gis_list.py.

Запуск: uv run gis_firm.py [limit]
        uv run gis_firm.py demo
"""

import json
import re
import sys

from fetch import get, jsonl, read

COOKIE = {"dg5_museum_accept": "true"}
URL = "https://2gis.kz/{city}/firm/{branch_id}"
SRC = "2gis_list.jsonl"
OUT = "2gis_firm.jsonl"

KINDS = ("phone", "website", "email", "instagram", "whatsapp")


def state(html):
    raw = re.search(r"var initialState = JSON\.parse\('(.*?)'\);", html, re.S).group(1)
    return json.loads(re.sub(r"\\(['\\])", r"\1", raw))


def unwrap(url):
    """2GIS заворачивает сайт в редирект link.2gis.ru — настоящий URL в хвосте после '?'."""
    if url and "link.2gis." in url and "?" in url:
        return url.split("?", 1)[1]
    return url


def contacts(s, branch_id):
    d = s["data"]["entity"]["profile"][branch_id]["data"]
    found = {k: [] for k in KINDS}
    for group in d.get("contact_groups") or []:
        for c in group.get("contacts") or []:
            kind = c.get("type")
            if kind not in found:
                continue
            value = c.get("value") or c.get("url") or c.get("text")
            if value and value not in found[kind]:
                found[kind].append(value)
    return {
        "branch_id": branch_id,
        "phones": found["phone"],
        "website": unwrap(found["website"][0]) if found["website"] else None,
        "emails": found["email"],
        "instagram": found["instagram"][0] if found["instagram"] else None,
        "whatsapp": found["whatsapp"],
    }


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
        print(f"новых записей: {jsonl(OUT, rows, 'branch_id')} из {len(rows)} -> raw/{OUT}")
