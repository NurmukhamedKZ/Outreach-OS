"""Тексты вакансий с hh.kz — главный intent-сигнал.

Google не нужен: robots.txt запрещает строку запроса (`Disallow: *?*`), а не поиск.
SEO-урлы /vacancies/<slug> идут без параметров и под запрет не попадают.

Запуск: uv run hh_vacancies.py <slug> <city>
Пример: uv run hh_vacancies.py menedzher_po_prodazham almaty
        uv run hh_vacancies.py demo

Slug — хвост SEO-страницы hh: menedzher_po_prodazham, buhgalter, ...
"""

import json
import re
import sys

from fetch import HttpError, get, jsonl

LIST = "https://{city}.hh.kz/vacancies/{slug}"
VACANCY = "https://hh.kz/vacancy/{id}"
HEADERS = {"accept-language": "ru-RU,ru;q=0.9"}
OUT = "hh_vacancies.jsonl"


def ids(html):
    """id вакансий со страницы списка."""
    return sorted(set(re.findall(r"/vacancy/(\d{6,})", html)))


def posting(html):
    """JSON-LD JobPosting — hh отдаёт вакансию структурно, парсить HTML не нужно."""
    for block in re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.S
    ):
        try:
            d = json.loads(block)
        except json.JSONDecodeError:
            continue
        if d.get("@type") == "JobPosting":
            return d
    return None


def parse(d, vacancy_id, city):
    return {
        "vacancy_id": vacancy_id,
        "title": d.get("title"),
        "employer": (d.get("hiringOrganization") or {}).get("name"),
        "published_at": d.get("datePosted"),
        "description": d.get("description"),
        "url": VACANCY.format(id=vacancy_id),
        "city": city,
    }


def collect(slug, city):
    html = get(LIST.format(city=city, slug=slug), headers=HEADERS)
    found = ids(html)
    print(f"{slug} / {city}: {len(found)} вакансий на странице")

    rows = []
    for i, vid in enumerate(found, 1):
        try:
            d = posting(get(VACANCY.format(id=vid), headers=HEADERS))
        except HttpError as e:
            print(f"\n  {vid}: {e}")
            continue
        if not d:
            print(f"\n  {vid}: JobPosting не найден — вакансия снята или закрыта")
            continue
        rows.append(parse(d, vid, city))
        print(f"  {i}/{len(found)}", end="\r", flush=True)
    print()
    return rows


def demo():
    """Список отдаёт id, вакансия — структурированный JobPosting."""
    html = get(LIST.format(city="almaty", slug="menedzher_po_prodazham"), headers=HEADERS)
    found = ids(html)
    assert len(found) >= 20, f"ожидали десятки вакансий, получили {len(found)}"

    vid = found[0]
    d = posting(get(VACANCY.format(id=vid), headers=HEADERS))
    assert d, "JSON-LD JobPosting не найден"
    r = parse(d, vid, "almaty")
    assert r["title"], r
    assert r["employer"], "работодатель пуст — по нему клеится компания"
    assert r["published_at"] and r["published_at"].startswith("20"), r["published_at"]
    assert len(r["description"] or "") > 200, len(r["description"] or "")
    print(f"hh demo ok — {len(found)} вакансий; {r['employer']}: {r['title'][:50]}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["demo"]:
        demo()
    else:
        if len(sys.argv) < 3:
            sys.exit(__doc__)
        rows = collect(sys.argv[1], sys.argv[2])
        print(f"новых записей: {jsonl(OUT, rows, 'vacancy_id')} из {len(rows)} -> raw/{OUT}")
