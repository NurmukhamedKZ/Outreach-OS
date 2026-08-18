"""Версионирование: во время открытой пересборки читатель видит прошлый прогон;
после COMMIT — новый; отменённый прогон не становится текущим.

Идентичность двух прогонов на неизменном сырье (спека §6) — в конце файла."""

import gzip
import hashlib
import json
import sqlite3
import types
from pathlib import Path

import services.storage as storage
import services.store as engine
from services.pipeline import rebuild

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"   # collector/fixtures
CONTEXT = types.SimpleNamespace(log=lambda *a: None,
                                progress=lambda *a: None,
                                check_cancelled=lambda: None)


def _seed(db, run_id, branch):
    db.execute("INSERT INTO runs (run_id, started_at) VALUES (?, '2026-08-01T00:00:00Z')", (run_id,))
    db.execute("INSERT INTO orgs_all (run_id, branch_id) VALUES (?, ?)", (run_id, branch))


def test_reader_sees_snapshot(stores):
    """Читатель на отдельном соединении видит прошлый прогон, пока писатель
    держит открытую транзакцию пересборки; после COMMIT — новый (WAL).

    Транзакция имитирует то, что делает rebuild.run: пишет новый прогон и
    переключает current_run внутри одного незакрытого BEGIN IMMEDIATE, а
    activate_run снаружи не зовётся (он коммитит сам)."""
    db = stores
    _seed(db, 1, "old")
    _seed(db, 2, "new")
    engine.activate_run(db, 1)
    db.commit()

    reader = sqlite3.connect(f"file:{engine.DERIVED}?mode=ro", uri=True)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE current_run SET run_id = 2")   # переключение до COMMIT
        assert reader.execute("SELECT branch_id FROM orgs").fetchone()[0] == "old", \
            "читатель увидел незакоммиченный прогон"
        db.commit()
        assert reader.execute("SELECT branch_id FROM orgs").fetchone()[0] == "new"
    finally:
        reader.close()


def test_rollback(stores):
    """activate прошлого прогона возвращает прежнюю выдачу."""
    db = stores
    _seed(db, 1, "v1")
    _seed(db, 2, "v2")
    engine.activate_run(db, 2)
    db.commit()
    assert db.execute("SELECT branch_id FROM orgs").fetchone()[0] == "v2"

    engine.activate_run(db, 1)   # откат к прогону 1
    assert db.execute("SELECT branch_id FROM orgs").fetchone()[0] == "v1"


def test_cancelled_run_not_current(stores):
    """Отменённый прогон остаётся в runs без finished_at и не становится текущим."""
    db = stores
    _seed(db, 1, "v1")
    engine.activate_run(db, 1)
    db.commit()

    run_id = engine.new_run(db)          # строящийся прогон
    db.execute("INSERT INTO orgs_all (run_id, branch_id) VALUES (?, 'v2')", (run_id,))
    db.commit()
    # отменён: finish_run и activate_run не вызваны
    assert db.execute("SELECT run_id FROM current_run").fetchone()[0] == 1
    row = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    assert row["finished_at"] is None
    assert db.execute("SELECT branch_id FROM orgs").fetchone()[0] == "v1"


def test_generated_tables_survive(stores):
    """Отказ, тред и ответ модели на месте после нового прогона."""
    db = stores
    # отказ
    db.execute("INSERT INTO state.suppression (handle, added_at, reason)"
               " VALUES ('+77010000000', '2026-08-10', 'просил')")
    # тред + сообщение
    db.execute("INSERT INTO state.threads (thread_id, company_id, seed, created_at)"
               " VALUES ('+77010000001', 'c1', '{}', '2026-08-10')")
    db.execute("INSERT INTO state.messages (thread_id, role, sent_text, created_at, sent_at)"
               " VALUES ('+77010000001', 'outgoing', 'привет', '2026-08-10', '2026-08-10')")
    # ответ модели
    db.execute("INSERT INTO state.llm_answers (kind, subject, model, prompt, answer)"
               " VALUES ('company_profile', 'X | Y', 'm', 'p', '{}')")
    db.commit()
    # новый прогон: пересборка не трогает state.*
    run_id = engine.new_run(db)
    db.execute("INSERT INTO companies_all (run_id, company_id, name_norm)"
               " VALUES (?, 'c1', 'А')", (run_id,))
    engine.activate_run(db, run_id)
    assert db.execute("SELECT count(*) FROM state.suppression").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM state.messages").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM state.llm_answers").fetchone()[0] == 1


def _snapshot(tmp_path):
    """Снимок raw/ из двух эталонных страниц 2GIS (рубрика + карточка)."""
    raw = tmp_path / "raw"
    raw.mkdir()
    urls = [
        ("https://2gis.kz/almaty/rubric/653", "gis_rubric"),
        ("https://2gis.kz/almaty/firm/70000001017502602", "gis_firm"),
    ]
    for url, name in urls:
        sha = hashlib.sha1(url.encode()).hexdigest()
        with gzip.open(FIXTURES / f"{name}.html.gz", "rb") as src, \
             gzip.open(raw / f"{sha}.html.gz", "wb") as dst:
            dst.write(src.read())
        (raw / f"{sha}.json").write_text(json.dumps(
            {"url": url, "final_url": url, "status": 200,
             "fetched_at": "2026-08-12T09:14:03Z"}, ensure_ascii=False), encoding="utf-8")
    return raw


def _dump(db, table, run_id):
    """Строки *_all ровно одного прогона без колонки run_id: сравнение двух
    прогонов сравнивает содержимое, а не версии (run_id в записях различается
    по построению)."""
    rows = db.execute(
        f"SELECT * FROM {table}_all WHERE run_id = ? ORDER BY rowid", (run_id,)
    ).fetchall()
    return [tuple(r)[1:] for r in rows]


def test_two_runs_identical(tmp_path, monkeypatch):
    raw = _snapshot(tmp_path)
    monkeypatch.setattr(storage, "RAW", raw)
    monkeypatch.setattr(engine, "DERIVED", tmp_path / "derived.db")
    monkeypatch.setattr(engine, "STATE", tmp_path / "state.db")

    db = engine.connect()
    rebuild.run(CONTEXT)
    run1 = db.execute("SELECT max(run_id) FROM runs").fetchone()[0]
    first = {t: _dump(db, t, run1) for t in ("fetches", "orgs", "contacts", "signals", "scores")}

    rebuild.run(CONTEXT)
    run2 = db.execute("SELECT max(run_id) FROM runs").fetchone()[0]
    second = {t: _dump(db, t, run2) for t in ("fetches", "orgs", "contacts", "signals", "scores")}

    for table in first:
        assert first[table] == second[table], \
            f"прогоны дали разные данные в {table}: {first[table]} vs {second[table]}"
    # второй прогон — отдельный run_id, а не перезапись первого
    run_ids = {r[0] for r in db.execute("SELECT run_id FROM runs")}
    assert len(run_ids) == 2, run_ids
    db.close()