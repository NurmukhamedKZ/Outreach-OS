"""Склейка дублей: филиалы 2GIS в компании.

Одна компания приходит из нескольких рубрик, городов и источников под разными
идентификаторами. Без склейки получим дубли и напишем одному человеку трижды.

Правила по убыванию надёжности (ARCHITECTURE.md §9.3). Модель не участвует:
правила здесь точнее и дешевле.

    org_id 2GIS   точно      источник сам пометил филиалы одной компании
    домен         точно      после нормализации
    телефон       почти      в КЗ номер редко делят два бизнеса
    instagram     точно      username уникален
    имя в городе  нечётко    только с записью правила и confidence

Порядок обхода везде отсортирован: build.py обязан давать побайтово тот же дамп
при повторной сборке, а множества Python такого не гарантируют.
"""

import re
from difflib import SequenceMatcher

# Телефон, общий у многих филиалов, — это бизнес-центр или колл-центр, а не одна
# компания. Домен, общий у двух десятков, — маркетплейс. Оба склеили бы половину
# базы в один ком, поэтому у обоих правил есть потолок.
MAX_BRANCHES_PER_PHONE = 3
MAX_BRANCHES_PER_DOMAIN = 20

# Порог нечёткого сравнения имён. 0.88, а не 0.8 из ARCHITECTURE: на живых данных
# «Альфа» и «Альфа-Строй» дают 0.85 и склеиваются, а это разные компании.
NAME_SIMILARITY = 0.88

LEGAL_FORMS = ("тоо", "ип", "ао", "оао", "зао", "ооо", "чп", "кх", "llp", "llc", "ltd")

# Соцсети, мессенджеры и виджеты попадают в карточку 2GIS в том же поле, что и сайт.
# Как признак тождества они катастрофичны: tiktok.com склеил 16 несвязанных компаний,
# jivo.chat — 11. Домен опознаёт компанию, только если он её собственный.
NON_COMPANY_DOMAINS = frozenset(
    """
    tiktok.com taplink.cc linktr.ee linktree.com jivo.chat jivosite.com instagram.com facebook.com fb.com
    wa.me whatsapp.com t.me telegram.me youtube.com youtu.be vk.com
    linkedin.com twitter.com x.com 2gis.kz 2gis.com google.com
    """.split()
)


def resolve(db):
    """Наполнить companies и company_links."""
    branches = load_branches(db)
    groups = group_branches(branches)
    write_companies(db, branches, groups)


# --- склейка филиалов --------------------------------------------------------


def load_branches(db):
    """branch_id -> всё, что нужно для склейки. Отсортировано ради дампа."""
    rows = db.execute(
        "SELECT branch_id, org_id, org_name, name, city, rubric_id FROM orgs ORDER BY branch_id"
    ).fetchall()
    contacts = {}
    for branch_id, kind, handle in db.execute(
        "SELECT branch_id, kind, handle FROM contacts ORDER BY branch_id, kind, handle"
    ):
        contacts.setdefault(branch_id, []).append((kind, handle))

    branches = {}
    for branch_id, org_id, org_name, name, city, rubric_id in rows:
        own = contacts.get(branch_id, [])
        branches[branch_id] = {
            "org_id": org_id,
            "name_norm": normalize_name(org_name or name),
            "city": city,
            "rubric_id": rubric_id,
            "domain": first(normalize_domain(h) for k, h in own if k == "website"),
            "phones": sorted({e164(h) for k, h in own if k == "phone"} - {None}),
            "instagram": first(
                normalize_instagram(h) for k, h in own if k == "instagram"
            ),
        }
    return branches


def group_branches(branches):
    """Union-find по правилам. Возвращает представитель -> отсортированный список."""
    parent = {b: b for b in branches}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)  # представитель минимален — детерминизм

    join_by(branches, union, lambda d: [d["org_id"]], None)
    join_by(branches, union, lambda d: [d["domain"]], MAX_BRANCHES_PER_DOMAIN)
    join_by(branches, union, lambda d: [d["instagram"]], None)
    join_by(branches, union, lambda d: d["phones"], MAX_BRANCHES_PER_PHONE)
    join_similar_names(branches, union, find)

    groups = {}
    for branch_id in sorted(branches):
        groups.setdefault(find(branch_id), []).append(branch_id)
    return groups


def join_by(branches, union, keys_of, cap):
    """Склеить филиалы с общим ключом. cap отсекает общие телефоны и маркетплейсы."""
    buckets = {}
    for branch_id, data in sorted(branches.items()):
        for key in keys_of(data):
            if key:
                buckets.setdefault(key, []).append(branch_id)
    for key, members in sorted(buckets.items()):
        if cap is not None and len(members) > cap:
            continue
        for other in members[1:]:
            union(members[0], other)


def join_similar_names(branches, union, find):
    """Похожие имена в пределах города. Самое слабое правило — идёт последним.

    Кандидаты сужены городом и первой буквой: иначе 1867² сравнений вместо сотен.
    Группы с разными известными доменами не склеиваются никогда — домен точнее имени.
    """
    buckets = {}
    for branch_id, data in sorted(branches.items()):
        name = data["name_norm"]
        if name:
            buckets.setdefault((data["city"], name[0]), []).append(branch_id)

    for key, members in sorted(buckets.items()):
        for i, left in enumerate(members):
            for right in members[i + 1:]:
                if find(left) == find(right):
                    continue
                if conflicting_domains(branches, left, right):
                    continue
                if similarity(branches[left]["name_norm"], branches[right]["name_norm"]) >= NAME_SIMILARITY:
                    union(left, right)


def conflicting_domains(branches, left, right):
    a, b = branches[left]["domain"], branches[right]["domain"]
    return bool(a) and bool(b) and a != b


def similarity(left, right):
    return SequenceMatcher(None, left, right).ratio()


# --- запись ------------------------------------------------------------------


def write_companies(db, branches, groups):
    for representative, members in sorted(groups.items()):
        company_id = pick_company_id(branches, members)
        head = branches[representative]
        domain = first(branches[b]["domain"] for b in members)
        db.execute(
            "INSERT INTO companies (company_id, name_norm, domain, city, rubric_id, first_seen)"
            " VALUES (?, ?, ?, ?, ?, NULL)",
            (company_id, head["name_norm"], domain, head["city"], head["rubric_id"]),
        )
        for branch_id in members:
            rule, confidence = link_reason(branches, representative, branch_id, members)
            db.execute(
                "INSERT OR IGNORE INTO company_links (company_id, branch_id, rule, confidence)"
                " VALUES (?, ?, ?, ?)",
                (company_id, branch_id, rule, confidence),
            )


def pick_company_id(branches, members):
    """Самый надёжный из найденных идентификаторов — он же формат из §9.1."""
    domain = first(branches[b]["domain"] for b in members)
    if domain:
        return f"dom:{domain}"
    instagram = first(branches[b]["instagram"] for b in members)
    org_id = min(branches[b]["org_id"] for b in members)
    if org_id:
        return f"2gis:{org_id}"
    return f"ig:{instagram}" if instagram else f"branch:{members[0]}"


def link_reason(branches, representative, branch_id, members):
    """Каким правилом филиал попал в компанию. Нечёткое имя обязано быть видно.

    Точные правила сверяются с представителем группы. Нечёткое — со всеми её
    участниками: склейка по имени транзитивна (A≈B, B≈C), и сходство именно с
    представителем у крайнего звена цепочки бывает низким, хотя связь реальна.
    """
    if branch_id == representative:
        return "self", 1.0
    own = branches[branch_id]
    others = [branches[b] for b in members if b != branch_id]
    if any(own["org_id"] and own["org_id"] == o["org_id"] for o in others):
        return "org_id", 1.0
    if any(own["domain"] and own["domain"] == o["domain"] for o in others):
        return "domain", 1.0
    if any(own["instagram"] and own["instagram"] == o["instagram"] for o in others):
        return "instagram", 1.0
    if any(set(own["phones"]) & set(o["phones"]) for o in others):
        return "phone", 0.9
    closest = max(
        similarity(own["name_norm"], branches[other]["name_norm"])
        for other in members
        if other != branch_id
    )
    return "name_city_fuzzy", round(closest, 3)


# --- нормализация ------------------------------------------------------------


def normalize_name(raw):
    """Имя без правовой формы, кавычек и лишних пробелов — основа сравнения."""
    if not raw:
        return ""
    name = raw.lower().replace("ё", "е")
    name = re.sub(r"[«»\"'`,.()]", " ", name)
    words = [w for w in name.split() if w not in LEGAL_FORMS]
    return " ".join(words).strip()


def normalize_domain(url):
    """Домен без схемы, www и хвоста. Разные написания одного сайта — один ключ."""
    if not url:
        return None
    host = re.sub(r"^https?://", "", url.strip().lower()).split("/")[0]
    host = host.split("?")[0].split(":")[0].removeprefix("www.")
    if not host or host in NON_COMPANY_DOMAINS:
        return None
    return host


def e164(raw):
    """Телефон КЗ в +7XXXXXXXXXX. Короткие сервисные номера отбрасываются."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    return f"+{digits}" if len(digits) == 11 and digits[0] == "7" else None


def normalize_instagram(raw):
    if not raw:
        return None
    handle = raw.strip().lower().rstrip("/").split("/")[-1].lstrip("@")
    return handle.split("?")[0] or None


def first(values):
    for value in sorted(v for v in values if v):
        return value
    return None
