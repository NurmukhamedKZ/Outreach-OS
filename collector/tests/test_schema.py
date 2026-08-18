"""Схема: порождённые таблицы переживают пересборку, view видят текущий прогон."""

import services.store as engine


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