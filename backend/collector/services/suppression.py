"""Отказы: единственный источник истины — state.suppression (невосстановимый слой)."""

from collector.db import lead as store


def refuse(db, handle, reason):
    """Одна запись в state.suppression. Повтор не задваивается."""
    handle, reason = handle.strip(), reason.strip()
    if handle in store.suppression_handles(db):
        return False
    from datetime import date
    store.add_refusal(db, handle, reason, date.today().isoformat())
    return True