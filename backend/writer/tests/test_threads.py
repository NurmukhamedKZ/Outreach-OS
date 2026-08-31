"""Черновик не становится историей, пока оператор не подтвердил отправку.

Это и есть протокол системы 2: если бы черновик попадал в историю сразу,
следующий ход агента строился бы на сообщении, которого лид не получал.
"""

from pathlib import Path

from writer.db import thread_store


def test_draft_stays_out_of_history_until_confirmed():
    db = thread_store.connect(":memory:")
    db.execute("ALTER TABLE messages ADD COLUMN provider_id TEXT")
    seed = {"name": "Ромашка", "signals": [{"type": "crm_widget", "quote": "виджет Bitrix24"}]}

    assert thread_store.open_thread(db, "+77010000001", "c_ok", seed), "тред не открылся"
    assert not thread_store.open_thread(db, "+77010000001", "c_ok", seed), "тред открылся дважды"
    assert thread_store.thread(db, "+77010000001")["seed"] == seed, "seed не пережил запись"

    message_id = thread_store.add_draft(db, "+77010000001", "Здравствуйте, ...", "crm_widget")
    assert thread_store.history(db, "+77010000001") == [], \
        "неподтверждённый черновик попал в историю"
    assert thread_store.pending_draft(db, "+77010000001")["message_id"] == message_id

    # sent_text теперь пишет ровно один автор — воркер после ответа транспорта
    # (sender/services/worker.py), поэтому тест подтверждает то же UPDATE'ом.
    db.execute("UPDATE messages SET sent_text = ?, sent_at = ? WHERE message_id = ?",
               ("Здравствуйте! Правленый оператором текст", thread_store.now(), message_id))
    db.commit()
    assert thread_store.pending_draft(db, "+77010000001") is None, "черновик остался висеть"
    history = thread_store.history(db, "+77010000001")
    assert [m["text"] for m in history] == ["Здравствуйте! Правленый оператором текст"], history
    assert history[0]["role"] == "outgoing", history[0]

    # Исходный черновик обязан пережить правку: разница между предложенным и
    # отправленным — единственная бесплатная разметка для калибровки промпта.
    stored = db.execute("SELECT draft_text FROM messages WHERE message_id = ?", (message_id,))
    assert stored.fetchone()[0] == "Здравствуйте, ...", "черновик затёрт правкой оператора"

    thread_store.add_incoming(db, "+77010000001", "а сколько это стоит?")
    roles = [m["role"] for m in thread_store.history(db, "+77010000001")]
    assert roles == ["outgoing", "incoming"], roles
    assert thread_store.used_angles(db, "+77010000001") == ["crm_widget"], \
        "угол отправленного сообщения потерян — follow-up повторит его"
    assert thread_store.silent_days(db, "+77010000001", thread_store.now()) == 0
    db.close()


def test_writer_never_writes_sent_text_itself():
    """Отправку подтверждает система 3 после ответа транспорта. Вторая точка
    записи sent_text означала бы историю треда из сообщений, которых лид не
    получал."""
    assert not hasattr(thread_store, "confirm")
    source = (Path(thread_store.__file__).parent.parent / "routes" / "threads.py").read_text()
    assert "sent_text" not in source, "writer снова пишет sent_text сам"


def test_incoming_remembers_the_provider_id():
    """Ручной ввод оператора ложится той же формой, что вебхук: два формата
    строки в одной таблице — это вопрос «почему у половины входящих пусто»."""
    db = thread_store.connect(":memory:")
    db.execute("ALTER TABLE messages ADD COLUMN provider_id TEXT")
    thread_store.open_thread(db, "+77010000001", "c1", {"name": "Ромашка", "signals": []})

    thread_store.add_incoming(db, "+77010000001", "перезвоните", provider_id="3EB0")
    thread_store.add_incoming(db, "+77010000001", "введено руками")

    rows = db.execute("SELECT provider_id FROM messages ORDER BY message_id").fetchall()
    assert [row[0] for row in rows] == ["3EB0", None]