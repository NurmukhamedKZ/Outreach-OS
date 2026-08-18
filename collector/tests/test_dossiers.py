"""Досье — вычислимый контракт с системой 2: hooks с цитатой/ссылкой, pains
отсортированы и не больше четырёх, sources ровно те слои, что участвовали."""

import json


def _seed_dossier(stores):
    stores.execute("INSERT INTO companies_all (run_id, company_id, name_norm, city)"
                   " VALUES (1, 'c1', 'Ромашка', 'almaty')")
    stores.execute("INSERT INTO current_run (id, run_id) VALUES (1, 1)")
    answer = {
        "dossier": {
            "summary": "Бухгалтерия",
            "hooks": [{"angle": "хвалят за скорость", "quote": "быстро",
                       "url": "https://r.kz/", "source": "reviews",
                       "observed_at": "2026-08-01"}],
            # Нарочно НЕ по убыванию тяжести: проверяем, что порядок в базе
            # наводит код (dossier.fill_dossiers), а не только промпт модели.
            "pains": [
                {"statement": "давно не обновляли сайт", "evidence": [],
                 "severity": "предполагается"},
                {"statement": "не отвечают на заявки", "evidence": [],
                 "severity": "видно явно"},
            ],
            "approach": "заходить через рост",
            "sources": ["reviews"],
            "confidence": 0.8,
        }
    }
    stores.execute("INSERT INTO state.llm_answers (kind, subject, model, prompt, answer)"
                   " VALUES ('dossier', 'Ромашка | almaty', 'm', 'p', ?)",
                   (json.dumps(answer, ensure_ascii=False),))
    stores.commit()
    from services.pipeline import dossier as mod
    mod.fill_dossiers(stores, 1)


def test_dossier_hooks_have_quote_and_url(stores):
    _seed_dossier(stores)
    row = stores.execute("SELECT hooks, pains, sources FROM dossiers").fetchone()
    hooks = json.loads(row["hooks"])
    assert hooks, "нет зацепок"
    assert all(h["quote"] and h["url"] for h in hooks), "hook без цитаты или ссылки"
    pains = json.loads(row["pains"])
    assert len(pains) <= 4, "больше четырёх болей"
    # Seed нарочно пришёл в обратном порядке — если этот ассерт проходит только
    # потому что pains был из одного элемента, регрессия тише некуда:
    # fill_dossiers обязан пересортировать сам, а не полагаться на порядок модели.
    assert [p["statement"] for p in pains] == ["не отвечают на заявки", "давно не обновляли сайт"], \
        "fill_dossiers обязан пересортировать pains по severity, а не доверять порядку модели"
    assert json.loads(row["sources"]) == ["reviews"], "sources не перечисляют слои"


def test_dossier_empty_pains_legal(stores):
    stores.execute("INSERT INTO companies_all (run_id, company_id, name_norm, city)"
                   " VALUES (1, 'c2', 'Тишина', 'almaty')")
    stores.execute("INSERT INTO current_run (id, run_id) VALUES (1, 1)")
    answer = {"dossier": {"summary": "s", "hooks": [], "pains": [],
                          "approach": "a", "sources": [], "confidence": 0.5}}
    stores.execute("INSERT INTO state.llm_answers (kind, subject, model, prompt, answer)"
                   " VALUES ('dossier', 'Тишина | almaty', 'm', 'p', ?)",
                   (json.dumps(answer, ensure_ascii=False),))
    stores.commit()
    from services.pipeline import dossier as mod
    mod.fill_dossiers(stores, 1)
    row = stores.execute("SELECT count(*) FROM dossiers").fetchone()[0]
    assert row == 1, "пустой pains не должен ломать досье"


def test_dossier_pains_trimmed_to_four(stores):
    """Потолок pains = 4 не держится на послушании модели промпту."""
    stores.execute("INSERT INTO companies_all (run_id, company_id, name_norm, city)"
                   " VALUES (1, 'c3', 'Много', 'almaty')")
    stores.execute("INSERT INTO current_run (id, run_id) VALUES (1, 1)")
    pains = [{"statement": f"боль {i}", "evidence": [], "severity": "не видно"}
             for i in range(6)]
    answer = {"dossier": {"summary": "s", "hooks": [], "pains": pains,
                          "approach": "a", "sources": [], "confidence": 0.5}}
    stores.execute("INSERT INTO state.llm_answers (kind, subject, model, prompt, answer)"
                   " VALUES ('dossier', 'Много | almaty', 'm', 'p', ?)",
                   (json.dumps(answer, ensure_ascii=False),))
    stores.commit()
    from services.pipeline import dossier as mod
    mod.fill_dossiers(stores, 1)
    row = stores.execute("SELECT pains FROM dossiers").fetchone()
    assert len(json.loads(row["pains"])) == 4, "pains не обрезаны до четырёх"
