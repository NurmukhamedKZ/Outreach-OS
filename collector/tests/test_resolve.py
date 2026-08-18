"""Ф5: филиалы схлопнулись в компании, и каждая склейка объяснима.

Требует собранной data/derived.db — на чистом клоне раздел пропускается.
"""


def test_branches_merged_into_companies(live_db):
    db = live_db
    branches, companies = count(db, "orgs"), count(db, "companies")
    assert companies < branches, f"склейка ничего не дала: {branches} филиалов, {companies} компаний"

    linked = db.execute("SELECT count(DISTINCT branch_id) FROM company_links").fetchone()[0]
    assert linked == branches, f"без компании остались {branches - linked} филиалов"

    orphan = db.execute(
        "SELECT count(*) FROM company_links l"
        " WHERE NOT EXISTS (SELECT 1 FROM orgs o WHERE o.branch_id = l.branch_id)"
    ).fetchone()[0]
    assert orphan == 0, f"{orphan} связей ссылаются на несуществующий филиал"

    # Нечёткая склейка обязана быть видна: правило названо, confidence проставлен.
    # Без этого «похоже по имени» неотличимо от «совпал домен», и разбирать
    # ошибочную склейку будет не по чему.
    unexplained = db.execute(
        "SELECT count(*) FROM company_links WHERE rule = 'name_city_fuzzy'"
        " AND (confidence IS NULL OR confidence < 0.8)"
    ).fetchone()[0]
    assert unexplained == 0, f"{unexplained} склеек по имени без внятного confidence"

    multi = db.execute(
        "SELECT company_id, count(*) c FROM company_links GROUP BY company_id"
        " HAVING c > 1 ORDER BY c DESC LIMIT 1"
    ).fetchone()
    assert multi, "ни одной компании из нескольких филиалов — правила не сработали"


def count(db, table):
    return db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]