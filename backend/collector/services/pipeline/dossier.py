"""Заполнение dossiers_all из оплаченных ответов synthesize_dossier.

Досье — вычислимая таблица: выводится из state.llm_answers (ATTACH) и пишется
с run_id, как остальные. Прошлые версии остаются: видно, как менялось досье
при правке промпта синтеза. Правило «ни одна операция не пишет в обе базы» не
нарушается: пишем только в derived, а читаем state через ATTACH.
"""

import json

# Порядок тяжести. Промпт синтеза просит модель отдать pains от сильной к
# слабой (§3), но соблюдение инструкции моделью — не гарантия: сортировка
# закреплена здесь же, кодом, а не только словом в system-промпте.
SEVERITY_ORDER = {"видно явно": 0, "предполагается": 1, "не видно": 2}


def fill_dossiers(db, run_id):
    from collector.services.pipeline import rebuild
    companies = {
        (name, city): company_id
        for company_id, name, city in db.execute(
            "SELECT c.company_id, coalesce(o.org_name, o.name, c.name_norm), c.city"
            " FROM companies_all c"
            " LEFT JOIN company_links_all l ON l.company_id = c.company_id"
            "   AND l.run_id = ? AND l.rule = 'self'"
            " LEFT JOIN orgs_all o ON o.branch_id = l.branch_id AND o.run_id = ?"
            " WHERE c.run_id = ?",
            (run_id, run_id, run_id),
        )
    }
    for answer in rebuild.load_llm_answers(db, "dossier"):
        name, _, city = answer["subject"].partition(" | ")
        company_id = companies.get((name, city))
        if not company_id:
            continue
        d = answer.get("dossier") or {}
        pains = sorted(
            (d.get("pains") or [])[:4],
            key=lambda p: SEVERITY_ORDER.get(p.get("severity"), len(SEVERITY_ORDER)),
        )
        db.execute(
            "INSERT OR REPLACE INTO dossiers_all (run_id, company_id, model, summary,"
            " hooks, pains, approach, decision_maker, sources, confidence)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, company_id, answer["model"], d.get("summary"),
                json.dumps(d.get("hooks") or [], ensure_ascii=False),
                json.dumps(pains, ensure_ascii=False),
                d.get("approach"), d.get("decision_maker_hint"),
                json.dumps(d.get("sources") or [], ensure_ascii=False),
                d.get("confidence"),
            ),
        )
