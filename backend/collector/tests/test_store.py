"""store.connect(): DERIVED-соединение можно использовать из чужого потока —
нужно для конкурентного analyze.* (см. docs/superpowers/specs/2026-08-22-analyze-concurrency-design.md)."""

import threading


def test_derived_connection_usable_from_other_thread(stores):
    db = stores
    errors = []

    def query_from_thread():
        try:
            db.execute("SELECT 1").fetchone()
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=query_from_thread)
    thread.start()
    thread.join()

    assert errors == []