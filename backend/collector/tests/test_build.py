"""Пересборка: прогон читает и пишет в рамках своего run_id, не трогая чужой."""

import collector.services.store as engine
from collector.services.pipeline import rebuild


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

def test_llm_answers_merge_both_stores(stores, tmp_path, monkeypatch):
    """Ответы читаются из базы И из файлов: первый ответ в базе не прячет остальные.

    Так и стояло: пока state.llm_answers пуста, пересборка брала ig-ответы из
    raw/, а первая же новая запись делала таблицу непустой и обнуляла все
    оставшиеся файлами — молча, вместе с сигналами и порядком выдачи.
    """
    import json

    import collector.services.storage as storage

    raw = tmp_path / "raw"
    raw.mkdir()
    monkeypatch.setattr(storage, "RAW", raw)
    for username in ("alpha", "beta"):
        (raw / f"{username}.llm.json").write_text(json.dumps({
            "kind": "ig_signals", "model": "m",
            "prompt": f"Инстаграм: {username}\n\n[1] пишите в директ",
            "signals": [{"post_index": 1, "type": "direct_selling", "quote": "директ"}],
        }, ensure_ascii=False), encoding="utf-8")

    db = stores
    db.execute(
        "INSERT INTO state.llm_answers (kind, subject, model, prompt, answer)"
        " VALUES ('ig_signals', 'gamma', 'm', 'Инстаграм: gamma', ?)",
        (json.dumps({"signals": []}),),
    )
    db.commit()

    answers = rebuild.load_llm_answers(db, "ig_signals")
    assert [a["subject"] for a in answers] == ["alpha", "beta", "gamma"], answers
    assert all("signals" in a for a in answers), "форма ответа разная у базы и файла"


def test_llm_answer_from_db_wins_over_file(stores, tmp_path, monkeypatch):
    """Один и тот же запрос в обоих хранилищах — одна запись, свежая из базы."""
    import json

    import collector.services.storage as storage

    raw = tmp_path / "raw"
    raw.mkdir()
    monkeypatch.setattr(storage, "RAW", raw)
    prompt = "Инстаграм: alpha"
    (raw / "alpha.llm.json").write_text(json.dumps({
        "kind": "ig_signals", "model": "m", "prompt": prompt, "signals": ["старый"],
    }, ensure_ascii=False), encoding="utf-8")

    db = stores
    db.execute(
        "INSERT INTO state.llm_answers (kind, subject, model, prompt, answer)"
        " VALUES ('ig_signals', 'alpha', 'm', ?, ?)",
        (prompt, json.dumps({"signals": ["новый"]})),
    )
    db.commit()

    answers = rebuild.load_llm_answers(db, "ig_signals")
    assert len(answers) == 1, f"ответ задвоился: {answers}"
    assert answers[0]["signals"] == ["новый"]
