"""Следующее касание молчащему лиду: одна задача, два потребителя.

Кнопка оператора в инбоксе и тик системы 3 обязаны получать одну и ту же
задачу — иначе автомат и человек пишут по разным правилам, и калибровать
промпт не по чему. Правило само простое: касание без НОВОГО повода не
отправляется, потому что напоминание о предыдущем сообщении поводом не
является.
"""

from writer.db import thread_store
from writer.services import agent


def task(threads, thread) -> str:
    """Задача хода: сколько лид молчит и какой повод ещё не использован.

    Единственное, что здесь общего у кнопки и тика, — сама задача. Обёртку над
    `agent.draft` сюда не поднимаем: у неё был бы один потребитель, а базу тик
    обязан читать в своём потоке и до вызова модели.
    """
    days = thread_store.silent_days(threads, thread["thread_id"], thread_store.now()) or 0
    unused = agent.unused_angles(
        thread["seed"], thread_store.used_angles(threads, thread["thread_id"]))
    return agent.followup_task(days, unused)
