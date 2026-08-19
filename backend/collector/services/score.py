"""Два скоринга, обе оси — по правилам, не моделью.

Клиенту придётся объяснять, почему выбран этот лид, а чёрный ящик объяснить нельзя.
Поэтому каждая функция возвращает не только число, но и разбивку: что именно
сработало, с каким весом и с какой ссылкой. Из разбивки же собирается why_now.

    fit_score     соответствие ICP: рубрика, город, наличие сайта
    intent_score  нужна ли лидогенерация сейчас: Σ(weight × 2^(-days/half_life))

Нет сигналов ≠ отрицательный вес. Компания без рекламы, но с CRM и отделом продаж —
лучший лид из возможных: есть люди, которым нечего обзванивать.
"""

import json
from math import exp, log


def score_all(db, run_id, config, rubrics):
    """Наполнить scores_all. Опорная дата — последний забор сырья, а не сегодня.

    Иначе пересборка той же raw/ через месяц дала бы другие числа, и проверка
    воспроизводимости из Ф3 стала бы ложной.
    """
    horizon = db.execute(
        "SELECT max(fetched_at) FROM fetches_all WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    signals = signals_by_company(db, run_id)

    for company_id, city, rubric_id, domain in db.execute(
        "SELECT company_id, city, rubric_id, domain FROM companies_all"
        " WHERE run_id = ? ORDER BY company_id", (run_id,)
    ).fetchall():
        fit, fit_parts = fit_score(config["fit"], rubrics, city, rubric_id, domain)
        intent, intent_parts = intent_score(
            config["half_life_days"], signals.get(company_id, []), horizon
        )
        db.execute(
            "INSERT INTO scores_all (run_id, company_id, fit_score, intent_score, breakdown)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                run_id,
                company_id,
                round(fit, 2),
                round(intent, 2),
                json.dumps(fit_parts + intent_parts, ensure_ascii=False),
            ),
        )


def fit_score(weights, rubrics, city, rubric_id, domain):
    """Соответствие ICP. Чистые правила: рубрика из белого списка, город, сайт."""
    parts = []
    if rubric_id and int(rubric_id) in rubrics["exclude_branch"]:
        return 0.0, [{"rule": "конкурент из чёрного списка", "contribution": 0.0}]
    if rubric_id and int(rubric_id) in rubrics["include"]:
        parts.append(add("рубрика в ICP", weights["rubric_in_include"]))
    if city in rubrics["cities"]:
        parts.append(add("целевой город", weights["city_in_target"]))
    if domain:
        parts.append(add("есть сайт", weights["has_website"]))
    return sum(p["contribution"] for p in parts), parts


def intent_score(half_life_days, signals, horizon):
    """Нужна ли лидогенерация сейчас. Свежий сигнал весит больше старого.

    Затухание вдвое за half_life_days: вакансия вчерашняя и полугодовой давности —
    разные лиды, и колонка has_hiring_signal boolean это различие потеряла бы.
    """
    parts = []
    for signal_type, observed_at, weight, quote, url in signals:
        age = days_between(observed_at, horizon)
        contribution = weight * exp(-log(2) * age / half_life_days)
        parts.append(
            {
                "signal": signal_type,
                "weight": weight,
                "age_days": age,
                "contribution": round(contribution, 3),
                "quote": quote,
                "url": url,
            }
        )
    return sum(p["contribution"] for p in parts), parts


def signals_by_company(db, run_id):
    grouped = {}
    for company_id, signal_type, observed_at, weight, quote, url in db.execute(
        "SELECT company_id, type, observed_at, weight, quote, url FROM signals_all"
        " WHERE run_id = ? ORDER BY company_id, type, observed_at, url",
        (run_id,),
    ):
        grouped.setdefault(company_id, []).append(
            (signal_type, observed_at, weight, quote, url)
        )
    return grouped


def days_between(observed_at, horizon):
    """Возраст сигнала в днях. Сигнал из будущего считается свежим, а не отрицательным."""
    from datetime import datetime

    observed = parse_date(observed_at)
    reference = parse_date(horizon)
    if not observed or not reference:
        return 0
    return max(0, (reference - observed).days)


def parse_date(value):
    from datetime import datetime

    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def add(rule, weight):
    return {"rule": rule, "weight": weight, "contribution": weight}
