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


def test_the_reply_move_goes_through_the_seller(monkeypatch):
    """У ответа лиду один автор. Второй промпт на ту же ситуацию дал бы вторую
    калибровку и вопрос «а что именно мы правим»."""
    from writer.routes import threads as route
    from writer.services import agent, seller

    assert not hasattr(agent, "REPLY"), "старый одноходовый ответ остался в коде"

    asked = []
    monkeypatch.setattr(route, "seller_agent", lambda: object())
    monkeypatch.setattr(
        seller, "respond",
        lambda agent_, seed, history, offer, *, session_id:
            asked.append(session_id) or seller.Reply(
                text="Ответ продавца", status=None, reason=None))

    thread = {"thread_id": "+77010000001", "seed": {"name": "Ромашка", "signals": []}}
    move = route._seller_move(None, thread, [])

    assert move.text == "Ответ продавца" and move.angle == "answer" and not move.stop
    assert asked == ["+77010000001"], "сессия Langfuse не равна треду"


def test_a_verdict_leaves_no_draft(monkeypatch):
    """Агент решил закрыть тред — черновика в этом ходе нет, и это правильно."""
    from writer.routes import threads as route
    from writer.services import seller

    monkeypatch.setattr(route, "seller_agent", lambda: object())
    monkeypatch.setattr(
        seller, "respond",
        lambda agent_, seed, history, offer, *, session_id:
            seller.Reply(text=None, status="refusal", reason="не интересно"))

    thread = {"thread_id": "+77010000001", "seed": {"name": "Ромашка", "signals": []}}
    assert route._seller_move(None, thread, []) is None