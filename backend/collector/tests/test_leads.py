"""Выдача: лиды с каналом и обоснованием. F19/F21 — закон, не формат вывода.

Отбор веба живёт в services/leads.py и повторяет report/export — проверка
сходится на тех же правилах через state.suppression.
"""

import collector.services.store as engine


def _published_run(db, run_id=1):
    """Одна компания с каналом, скорингом и профилем — опубликованный прогон."""
    db.execute("INSERT INTO runs (run_id, started_at) VALUES (?, '2026-08-01T00:00:00Z')", (run_id,))
    db.execute("INSERT INTO companies_all (run_id, company_id, name_norm, city, domain)"
               " VALUES (?, 'c1', 'Ромашка', 'almaty', 'romashka.kz')", (run_id,))
    db.execute("INSERT INTO company_links_all (run_id, company_id, branch_id, rule, confidence)"
               " VALUES (?, 'c1', 'b1', 'self', 1.0)", (run_id,))
    db.execute("INSERT INTO orgs_all (run_id, branch_id, name, city, rubric_id)"
               " VALUES (?, 'b1', 'Ромашка', 'almaty', '653')", (run_id,))
    db.execute("INSERT INTO contacts_all (run_id, branch_id, kind, handle)"
               " VALUES (?, 'b1', 'whatsapp', '+77010000001')", (run_id,))
    db.execute("INSERT INTO contacts_all (run_id, branch_id, kind, handle)"
               " VALUES (?, 'b1', 'phone', '+77010000002')", (run_id,))
    db.execute("INSERT INTO scores_all (run_id, company_id, fit_score, intent_score, breakdown)"
               " VALUES (?, 'c1', 5.0, 6.0, '[]')", (run_id,))
    db.execute("INSERT INTO profiles_all (run_id, company_id, model, industry, why_now, quote)"
               " VALUES (?, 'c1', 'm', 'бухгалтерия', 'ищет клиентов', 'оставьте заявку')", (run_id,))
    engine.activate_run(db, run_id)


def test_lead_without_channel_is_not_picked(stores):
    """F19: компания без рабочего канала — не лид."""
    from collector.services import leads as service
    db = stores
    _published_run(db)
    db.execute("DELETE FROM contacts_all")   # каналов нет вовсе
    db.commit()
    found = service.pick(db, limit=10)
    assert found == [], "лид без канала попал в выдачу (F19)"


def test_refusal_filters_lead(stores):
    """F21: отказ из state.suppression убирает канал, а без канала — лид."""
    from collector.services import leads as service
    db = stores
    _published_run(db)
    db.execute("INSERT INTO state.suppression (handle, added_at, reason)"
               " VALUES ('+77010000001', '2026-08-10', 'просил')")
    db.execute("INSERT INTO state.suppression (handle, added_at, reason)"
               " VALUES ('+77010000002', '2026-08-10', 'просил')")
    db.commit()
    found = service.pick(db, limit=10)
    assert found == [], "отказ обязан убрать лида из выдачи (F21)"


def test_second_channel_survives_refusal(stores):
    """Отказ на одном канале не рубит лида, если остался другой."""
    from collector.services import leads as service
    db = stores
    _published_run(db)
    db.execute("INSERT INTO state.suppression (handle, added_at, reason)"
               " VALUES ('+77010000001', '2026-08-10', 'просил')")
    db.commit()
    found = service.pick(db, limit=10)
    assert len(found) == 1
    assert found[0]["channel"]["handle"] == "+77010000002", \
        "после отказа лид обязан переключиться на оставшийся канал"


def test_every_lead_has_why_now(stores):
    """F20: у лида обоснование, а не голый скор."""
    from collector.services import leads as service
    db = stores
    _published_run(db)
    found = service.pick(db, limit=10)
    assert len(found) == 1
    assert found[0]["why_now"], "у лида нет why_now (F20)"


def test_export_filters_refusals(stores, tmp_path, monkeypatch):
    """F21: отказ из state.suppression убирает канал, а без канала — лид."""
    from collector.services.pipeline import export as export_op
    monkeypatch.setattr(export_op, "OUT", tmp_path / "leads.csv")   # не трогать боевой CSV
    db = stores
    _published_run(db)
    db.execute("INSERT INTO state.suppression (handle, added_at, reason)"
               " VALUES ('+77010000001', '2026-08-10', 'просил')")
    db.execute("INSERT INTO state.suppression (handle, added_at, reason)"
               " VALUES ('+77010000002', '2026-08-10', 'просил')")
    db.commit()
    leads = export_op.build_leads(db, limit=10)
    assert leads == [], "отказ обязан убрать лида из выдачи (F21)"