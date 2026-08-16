"""Отказы: suppression.csv — источник истины, таблица в базе — его копия.

Файл первым: он переживёт пересборку, таблица — нет, схема пересобирается через
DROP. Если запись в файл не удалась, в базу не пишем вовсе — лучше отсутствующий
запрет, который человек увидит и повторит, чем запрет, работающий до ближайшего
build.py.
"""

import csv
from datetime import date
from pathlib import Path

from db import lead as store

SUPPRESSION = Path("data/suppression.csv")


def refuse(db, handle, reason):
    handle, reason = handle.strip(), reason.strip()
    if handle in existing_handles():
        return False
    append_refusal(handle, reason)
    store.add_refusal(db, handle, reason, date.today().isoformat())
    return True


def existing_handles():
    if not SUPPRESSION.exists():
        return set()
    with SUPPRESSION.open(encoding="utf-8-sig", newline="") as fh:
        return {(row.get("handle") or "").strip() for row in csv.DictReader(fh)}


def append_refusal(handle, reason):
    new_file = not SUPPRESSION.exists()
    with SUPPRESSION.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(["handle", "added_at", "reason"])
        writer.writerow([handle, date.today().isoformat(), reason])
