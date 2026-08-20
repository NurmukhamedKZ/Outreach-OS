"""Схема: порождённые таблицы переживают пересборку, view видят текущий прогон."""

import collector.services.store as engine


def test_views_read_current_run(stores):
    """View orgs читает ровно тот прогон, на который указывает current_run."""
    db = stores
    run1 = engine.new_run(db, note="первый")
    run2 = engine.new_run(db, note="второй")
    db.execute("INSERT INTO orgs_all (run_id, branch_id) VALUES (?, 'b1')", (run1,))
    db.execute("INSERT INTO orgs_all (run_id, branch_id) VALUES (?, 'b2')", (run2,))
    db.commit()

    engine.activate_run(db, run1)
    assert db.execute("SELECT branch_id FROM orgs").fetchone()["branch_id"] == "b1"

    engine.activate_run(db, run2)  # второй вызов — обновление, не вторая строка
    assert db.execute("SELECT branch_id FROM orgs").fetchone()["branch_id"] == "b2"
    assert db.execute("SELECT count(*) FROM current_run").fetchone()[0] == 1, \
        "current_run обязан держать ровно одну строку"

def test_dossiers_view_exists(stores):
    """Досье — вычислимая таблица прогона, видна через view текущего прогона."""
    stores.execute("INSERT INTO dossiers_all (run_id, company_id, summary, approach, sources)"
                   " VALUES (1, 'c', 's', 'a', '[]')")
    stores.execute("INSERT INTO current_run (id, run_id) VALUES (1, 1)")
    rows = stores.execute("SELECT * FROM dossiers").fetchall()
    assert rows and rows[0]["company_id"] == "c"


def test_views_match_schema_file(live_db):
    """Определения view в базе совпадают с db/schema.sql.

    Схема применяется через CREATE VIEW IF NOT EXISTS — иначе connect() брал бы
    блокировку записи на каждую строку лога и валил идущую пересборку. Плата за
    это: правка определения ниже не доедет до уже созданной базы сама, и код
    читал бы не тот запрос, который написан в файле. Расхождение ловится здесь.
    Починка — DROP VIEW: данных в них нет, следующий connect() создаст заново.
    """
    import re

    schema = engine.SCHEMA.read_text(encoding="utf-8")
    expected = {
        name: " ".join(body.split())
        for name, body in re.findall(
            r"CREATE VIEW IF NOT EXISTS (\w+) AS(.*?);", schema, re.S)
    }
    actual = {
        name: " ".join(sql.split("AS", 1)[1].split())
        for name, sql in live_db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'view'")
    }
    assert actual == expected, (
        "определения view в базе разошлись с schema.sql; "
        "починка: DROP VIEW для расходящихся — connect() создаст их заново"
    )
