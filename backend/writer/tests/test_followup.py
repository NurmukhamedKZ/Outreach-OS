"""Follow-up без нового повода запрещён — и это правило живёт в одном месте.

Тик системы 3 и кнопка оператора обязаны получать одну и ту же задачу: две
копии правил каденции разъехались бы молча.
"""

from writer.db import thread_store
from writer.services import followup

SEED = {"name": "Ромашка", "city": "Алматы", "signals": [
    {"type": "crm_widget", "quote": "виджет Bitrix24"},
    {"type": "ads_platform", "quote": "крутят Яндекс.Директ"},
]}


def store():
    db = thread_store.connect(":memory:")
    thread_store.open_thread(db, "+77010000001", "c1", SEED)
    return db


def test_the_task_names_the_unused_angle():
    db = store()
    message_id = thread_store.add_draft(db, "+77010000001", "Здравствуйте!", "crm_widget")
    db.execute("UPDATE messages SET sent_text = draft_text, sent_at = ?"
               " WHERE message_id = ?", (thread_store.now(), message_id))
    db.commit()

    task = followup.task(db, thread_store.thread(db, "+77010000001"))

    assert "ads_platform" in task, task
    assert "crm_widget" not in task, "повод, который уже использовали, предложен снова"


def test_without_a_new_angle_the_task_asks_for_stop():
    """Follow-up без нового повода — это тот же шаблон, отправленный второй раз."""
    db = store()
    for angle in ("crm_widget", "ads_platform"):
        message_id = thread_store.add_draft(db, "+77010000001", "текст", angle)
        db.execute("UPDATE messages SET sent_text = draft_text, sent_at = ?"
                   " WHERE message_id = ?", (thread_store.now(), message_id))
    db.commit()

    assert "stop=true" in followup.task(db, thread_store.thread(db, "+77010000001"))
