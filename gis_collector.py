import json
import re

from fetch import get, jsonl, read, HttpError


class GisCollector:
    def __init__(self) -> None:
        self.cookie = {"dg5_museum_accept": "true"}

        self.url_rubrics = "https://2gis.kz/almaty/rubrics"
        self.url_subrubrics = "https://2gis.kz/almaty/rubrics/subrubrics/{id}"
        self.url_list = "https://2gis.kz/{city}/rubric/{rubric}/page/{page}"
        self.url_firm = "https://2gis.kz/{city}/firm/{branch_id}"

        self.source_rubrics = "2gis_rubrics.jsonl"
        self.source_list = "2gis_list.jsonl"
        self.source_firm = "2gis_firm.jsonl"

        self.contack_kinds = ("phone", "website", "email", "instagram", "whatsapp")

    def _state(self, html):
        raw = re.search(r"var initialState = JSON\.parse\('(.*?)'\);", html, re.S).group(1)
        return json.loads(re.sub(r"\\(['\\])", r"\1", raw))


    def _named(self, rubricator, ids):
        """Пары (id, название) для перечисленных id. Длинные id — геообъекты, не рубрики."""
        out = []
        for i in ids:
            if i.isdigit() and len(i) < 10:
                v = rubricator["items"].get(i) or {}
                out.append((i, v.get("name") or v.get("caption")))
        return out


    def _roots(self):
        r = self._state(get(self.source_rubrics, cookies=self.cookie))["data"]["rubricator"]
        return self._named(r, next(iter(r["lists"].values()))["data"])  # на корне список один


    def _branch(self, rubric_id):
        """Своё название и дети рубрики. Дети — только в lists['67_<id>_ru_KZ'].

        items трогать нельзя: это общее хранилище узлов страницы, включая соседние
        ветки. Обход по нему уходит во весь рубрикатор вместо запрошенной ветки.
        """
        try:
            html = get(self.url_subrubrics.format(id=rubric_id), cookies=self.cookie)
        except HttpError as e:
            if e.status == 404:
                return None, []  # у листа нет страницы подрубрик
            raise
        r = self._state(html)["data"]["rubricator"]
        own = (r["items"].get(rubric_id) or {}).get("name")
        kids = r["lists"].get(f"67_{rubric_id}_ru_KZ")
        return own, (self._named(r, kids["data"]) if kids else [])


    def analyze_rubric(self, start: list | None = None):
        """Обход вширь от корней или от заданных рубрик."""
        queue = [(i, None, None) for i in start] if start else \
                [(i, name, None) for i, name in self._roots()]
        rows, seen = [], set()
        while queue:
            rid, name, parent = queue.pop(0)
            if rid in seen:
                continue
            seen.add(rid)
            own, kids = self._branch(rid)
            rows.append({"id": rid, "name": name or own, "parent_id": parent})
            queue += [(k, kname, rid) for k, kname in kids]
            print(f"  рубрик {len(rows)}, в очереди {len(queue)}", end="\r", flush=True)
        print()
        return rows

    def _meta(self, s):
        """total, pages и фактическая страница из ветки поиска."""
        prof = s["data"]["search"]["profile"]
        d = prof[next(iter(prof))]["data"]
        return d["total"], d["pages"], d.get("currentPage")


    def _orgs(self, s, city, rubric):
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


    def collect_companies_by_rubric(self, rubric, city):
        """Собрать рубрику до потолка пагинации.

        2GIS отдаёт максимум 5 страниц (60 организаций) на рубрику на город: начиная с
        шестой возвращается HTTP 200 с содержимым ПЕРВОЙ страницы, а `pages` и `total`
        продолжают обещать больше. Единственный честный признак — `currentPage`.
        Расширять охват приходится не пагинацией, а числом рубрик и городов.
        """
        s = self._state(get(self.url_list.format(city=city, rubric=rubric, page=1), cookies=self.cookie))
        total, pages, _ = self._meta(s)
        print(f"рубрика {rubric} / {city}: {total} организаций, {pages} страниц по версии 2GIS")

        rows = self._orgs(s, city, rubric)
        for n in range(2, pages + 1):
            html = get(self.url_list.format(city=city, rubric=rubric, page=n), cookies=self.cookie)
            s = self._state(html)
            _, _, current = self._meta(s)
            if current != n:
                print(f"  стр. {n}: 2GIS вернул страницу {current} — потолок пагинации, стоп")
                print(f"  ДОСТУПНО {len(rows)} из {total}: остальное этим путём не берётся")
                break
            rows += self._orgs(s, city, rubric)
            print(f"  стр. {n}/{pages} — собрано {len(rows)}", end="\r", flush=True)
        print()
        return rows

    def _unwrap(self, url):
        """2GIS заворачивает сайт в редирект link.2gis.ru — настоящий URL в хвосте после '?'."""
        if url and "link.2gis." in url and "?" in url:
            return url.split("?", 1)[1]
        return url


    def _contacts(self, s, branch_id):
        d = s["data"]["entity"]["profile"][branch_id]["data"]
        found = {k: [] for k in self.contack_kinds}
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
            "website": self._unwrap(found["website"][0]) if found["website"] else None,
            "emails": found["email"],
            "instagram": found["instagram"][0] if found["instagram"] else None,
            "whatsapp": found["whatsapp"],
        }


    def _fetch_one(self, branch_id, city):
        html = get(self.url_firm.format(city=city, branch_id=branch_id), cookies=self.cookie)
        return self._contacts(self._state(html), branch_id)


    def collect_company_contacts(self, limit=None):
        src = read(self.source_list)
        done = {r["branch_id"] for r in read(self.source_firm)}
        todo = [r for r in src if r["branch_id"] not in done]
        if limit:
            todo = todo[:limit]
        print(f"в списке {len(src)}, уже собрано {len(done)}, берём {len(todo)}")

        rows = []
        for i, r in enumerate(todo, 1):
            try:
                rows.append(self._fetch_one(r["branch_id"], r["city"]))
            except Exception as e:  # одна битая карточка не должна ронять прогон
                print(f"\n  {r['branch_id']}: {type(e).__name__} {e}")
            print(f"  {i}/{len(todo)}", end="\r", flush=True)
        print()
        return rows




    

    