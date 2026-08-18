"""Пересборка: прогон читает и пишет в рамках своего run_id, не трогая чужой."""

import services.store as engine


def test_rebuild_reads_own_run(stores):
    """Внутренние чтения сборки идут в *_all по run_id, а не в view current_run.

    Прогон 2 строится, пока current_run указывает на прогон 1: компания из
    прогона 1 не должна попадать в чтения сборки прогона 2.
    """
    db = stores
    run1 = engine.new_run(db)
    run2 = engine.new_run(db)
    db.execute("INSERT INTO companies_all (run_id, company_id, domain) VALUES (?, 'c1', 'a.kz')",
               (run1,))
    engine.activate_run(db, run1)   # опубликован прогон 1
    # чтение «в рамках строящегося прогона» 2 ничего не видит из прогона 1
    rows = list(engine.build_read(db, run2, "companies"))
    assert rows == [], f"чтение прогона 2 подхватило данные прогона 1: {rows}"
    # а чтение прогона 1 их видит
    assert [r["company_id"] for r in engine.build_read(db, run1, "companies")] == ["c1"]


def test_build_does_not_publish_until_activate(stores):
    """До activate_run выдача (view) показывает прежний прогон, не строящийся."""
    db = stores
    db.execute("INSERT INTO runs (run_id, started_at) VALUES (1, '2026-08-01T00:00:00Z')")
    db.execute("INSERT INTO runs (run_id, started_at) VALUES (2, '2026-08-01T00:00:00Z')")
    db.execute("INSERT INTO companies_all (run_id, company_id, name_norm) VALUES (1, 'c1', 'А')")
    db.execute("INSERT INTO companies_all (run_id, company_id, name_norm) VALUES (2, 'c2', 'Б')")
    engine.activate_run(db, 1)   # опубликован прогон 1
    assert [r["company_id"] for r in db.execute("SELECT company_id FROM companies")] == ["c1"]

    # прогон 2 построен, но ещё не опубликован — выдача по-прежнему c1
    assert [r["company_id"] for r in db.execute("SELECT company_id FROM companies")] == ["c1"]
    engine.activate_run(db, 2)   # публикация
    assert [r["company_id"] for r in db.execute("SELECT company_id FROM companies")] == ["c2"]