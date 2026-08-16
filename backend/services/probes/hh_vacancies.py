"""Тексты вакансий с hh.kz — главный intent-сигнал.

Google не нужен: robots.txt запрещает строку запроса (`Disallow: *?*`), а не поиск.
SEO-урлы /vacancies/<slug> идут без параметров и под запрет не попадают.

Запуск: uv run -m services.probes.hh_vacancies <slug> <city>
Пример: uv run -m services.probes.hh_vacancies menedzher_po_prodazham almaty
        uv run -m services.probes.hh_vacancies demo

Slug — хвост SEO-страницы hh: menedzher_po_prodazham, buhgalter, ...
"""

import sys

from services.fetch import JSONL_DIR, HttpError, final_url, get, jsonl
from services.sources import parse_job_posting as posting
from services.sources import parse_vacancy_ids as ids

LIST = "https://{city}.hh.kz/vacancies/{slug}"
VACANCY = "https://hh.kz/vacancy/{id}"
HEADERS = {"accept-language": "ru-RU,ru;q=0.9"}
OUT = "hh_vacancies.jsonl"


def parse(d, vacancy_id, city, slug):
    return {
        "vacancy_id": vacancy_id,
        "title": d.get("title"),
        "employer": (d.get("hiringOrganization") or {}).get("name"),
        "published_at": d.get("datePosted"),
        "description": d.get("description"),
        "url": VACANCY.format(id=vacancy_id),
        "city": city,
        "slug": slug,
    }


def listing(slug, city):
    """HTML страницы slug'а. Падает, если hh подменил её общим списком.

    Slug — не произвольный запрос, а фиксированная SEO-страница hh. Несуществующий
    редиректит на /vacancies (все вакансии города) и отвечает HTTP 200, поэтому
    сбор без этой проверки молча наберёт 50 посторонних вакансий.
    """
    url = LIST.format(city=city, slug=slug)
    html = get(url, headers=HEADERS)
    landed = final_url(url)
    if landed and slug.lower() not in landed.lower():
        sys.exit(
            f"У hh нет страницы '{slug}' — запрос увело на {landed}\n"
            f"Валидный slug: открой поиск hh.kz по нужной фразе в браузере и возьми\n"
            f"slug из адреса, куда он сам перебросит (например menedzher_po_prodazham)."
        )
    return html


def collect(slug, city):
    found = ids(listing(slug, city))
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
        rows.append(parse(d, vid, city, slug))
        print(f"  {i}/{len(found)}", end="\r", flush=True)
    print()
    return rows


def demo():
    """Список отдаёт id, вакансия — JobPosting, несуществующий slug — отказ."""
    found = ids(listing("menedzher_po_prodazham", "almaty"))
    assert len(found) >= 20, f"ожидали десятки вакансий, получили {len(found)}"

    # выдуманный slug обязан приводить к отказу, а не к сбору чужих вакансий
    url = LIST.format(city="almaty", slug="AI_engineer")
    get(url, headers=HEADERS)
    landed = final_url(url)
    assert landed and "ai_engineer" not in landed.lower(), \
        f"подмена страницы перестала обнаруживаться: {landed}"

    vid = found[0]
    d = posting(get(VACANCY.format(id=vid), headers=HEADERS))
    assert d, "JSON-LD JobPosting не найден"
    r = parse(d, vid, "almaty", "menedzher_po_prodazham")
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
        rows = collect(sys.argv[1], sys.argv[2].lower())
        print(f"новых записей: {jsonl(OUT, rows, 'vacancy_id')} из {len(rows)} -> {JSONL_DIR}/{OUT}")
