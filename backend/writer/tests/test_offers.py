"""Вариант оффера назначается треду навсегда и переживает перезапуск процесса."""

from writer.db import thread_store
from writer.services import offers

CONFIG = {"offer": {"text": "старый оффер", "variant": [
    {"id": "free_first_meetings", "text": "первые две встречи за наш счёт"},
    {"id": "pay_per_meeting", "text": "оплата за состоявшуюся встречу"},
]}}


def test_variant_is_stable_for_the_same_thread():
    first = offers.variant_of("+77010000001", CONFIG)
    again = offers.variant_of("+77010000001", CONFIG)

    assert first["id"] == again["id"], "тред сменил когорту между вызовами"


def test_variants_split_the_base():
    ids = {offers.variant_of(f"+7701000{n:04d}", CONFIG)["id"] for n in range(50)}

    assert ids == {"free_first_meetings", "pay_per_meeting"}, \
        f"когорты разъехались: {ids}"


def test_config_without_variants_falls_back_to_single_offer():
    variant = offers.variant_of("+77010000001", {"offer": {"text": "один оффер"}})

    assert variant == {"id": "", "text": "один оффер"}, \
        "треды, открытые до A/B, обязаны продолжать работать"


def test_draft_remembers_which_offer_it_argued():
    db = thread_store.connect(":memory:")
    thread_store.open_thread(db, "+77010000001", "c_ok", {"name": "Ромашка"})

    message_id = thread_store.add_draft(db, "+77010000001", "текст", "site_no_pricing",
                                        offer_variant="pay_per_meeting")

    stored = db.execute("SELECT offer_variant FROM messages WHERE message_id = ?",
                        (message_id,)).fetchone()[0]
    assert stored == "pay_per_meeting"
