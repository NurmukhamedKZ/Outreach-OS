"""Операция очереди: черновики первым сообщениям top_n новым лидам.

Замена writer/scripts/write.py. В реестр её подключает collector/api.py (тот
же шов, что монтирует роутер writer'а) — сам writer по-прежнему не знает о
существовании collector'а.
"""

from config import settings
from writer.services import agent, config
from writer.db import leads_source, thread_store

import logctx

CONFIG = config.load()


def open_new_threads(ctx):
    require_api_key()
    limit = CONFIG["llm"]["top_n"]
    leads = leads_source.connect(CONFIG["leads_db"])
    threads = thread_store.connect(CONFIG["threads_db"])
    try:
        fresh = [lead for lead in leads_source.candidates(leads)
                 if not thread_store.thread(threads, lead["thread_id"])][:limit]
        ctx.log(f"писем: {len(fresh)}, модель {CONFIG['llm']['model']}")
        llm = agent.model(CONFIG)
        for number, lead in enumerate(fresh, 1):
            ctx.check_cancelled()
            ctx.progress(number, len(fresh), lead["seed"]["name"])
            with logctx.entity(lead["thread_id"]):
                thread_store.open_thread(threads, lead["thread_id"], lead["company_id"], lead["seed"])
                proposal = agent.draft(llm, lead["seed"], [], agent.FIRST,
                                        session_id=lead["thread_id"], name="writer.first",
                                        offer=CONFIG["offer"]["text"],
                                        model_name=CONFIG["llm"]["model"])
                if proposal.draft.stop:
                    ctx.log(f"{lead['seed']['name']}: агент советует не писать — повода в данных нет")
                    continue
                thread_store.add_draft(threads, lead["thread_id"], proposal.draft.text,
                                       proposal.draft.angle,
                                       prompt=proposal.prompt, model=proposal.model)
                ctx.log(f"{lead['seed']['name']}: черновик готов ({proposal.draft.angle})")
        return {"drafted": len(fresh)}
    finally:
        leads.close()
        threads.close()


def require_api_key():
    """Отказать до сети и до открытия баз, а не в середине прогона."""
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY пуст. Задать в backend/.env — см. backend/.env.example")
