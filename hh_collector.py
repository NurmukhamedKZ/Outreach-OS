import json
import re
import sys

from fetch import HttpError, final_url, get, jsonl


class HHCollector:
    def __init__(self) -> None:
        self.url_list = "https://{city}.hh.kz/vacancies/{slug}"
        self.url_vacancy = "https://hh.kz/vacancy/{id}"
        self.headers = {"accept-language": "ru-RU,ru;q=0.9"}
        self.source_hh = "hh_vacancies.jsonl"


    def _ids(self, html) -> list:
        """id вакансий со страницы списка."""
        return sorted(set(re.findall(r"/vacancy/(\d{6,})", html)))


    def _posting(self, html) -> dict|None:
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


    def _parse(self, d, vacancy_id, city, slug) -> dict:
        return {
            "vacancy_id": vacancy_id,
            "title": d.get("title"),
            "employer": (d.get("hiringOrganization") or {}).get("name"),
            "published_at": d.get("datePosted"),
            "description": d.get("description"),
            "url": self.url_vacancy.format(id=vacancy_id),
            "city": city,
            "slug": slug,
        }


    def _listing(self, slug, city):
        """HTML страницы slug'а. Падает, если hh подменил её общим списком.

        Slug — не произвольный запрос, а фиксированная SEO-страница hh. Несуществующий
        редиректит на /vacancies (все вакансии города) и отвечает HTTP 200, поэтому
        сбор без этой проверки молча наберёт 50 посторонних вакансий.
        """
        url = self.url_list.format(city=city, slug=slug)
        html = get(url, headers=self.headers)
        landed = final_url(url)
        if landed and slug.lower() not in landed.lower():
            sys.exit(
                f"У hh нет страницы '{slug}' — запрос увело на {landed}\n"
                f"Валидный slug: открой поиск hh.kz по нужной фразе в браузере и возьми\n"
                f"slug из адреса, куда он сам перебросит (например menedzher_po_prodazham)."
            )
        return html


    def collect_hh_vacancies(self, slug, city) -> list[dict]:
        found = self._ids(self._listing(slug, city))
        print(f"{slug} / {city}: {len(found)} вакансий на странице")

        rows = []
        for i, vid in enumerate(found, 1):
            try:
                d = self._posting(get(self.url_vacancy.format(id=vid), headers=self.headers))
            except HttpError as e:
                print(f"\n  {vid}: {e}")
                continue
            if not d:
                print(f"\n  {vid}: JobPosting не найден — вакансия снята или закрыта")
                continue
            rows.append(self._parse(d, vid, city, slug))
            print(f"  {i}/{len(found)}", end="\r", flush=True)
        print()
        return rows