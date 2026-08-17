"""Первые сообщения топ-N лидам. Сетевая сторона системы 2, как classify.py у системы 1.

Тред открывается один раз и переживает пересборку базы collector'а: seed —
снимок момента, когда мы решили писать. Компания, у которой тред уже есть,
пропускается, поэтому повторный запуск не пишет одному человеку дважды, а идёт
дальше по списку: `write N` — это всегда N новых компаний, а не первые N.

Отправляет по-прежнему человек: скрипт печатает черновики, оператор правит их в
консоли и жмёт «отправлено». Система 3 (доставка) не построена.

Запуск:
  uv run --env-file .env -m scripts.write        топ из config.toml [llm].top_n
  uv run --env-file .env -m scripts.write 3      первые 3
"""

import os
import sys

import agent
import config
import leads_source
import thread_store


def main():
    settings = config.load()
    limit = int(sys.argv[1]) if sys.argv[1:] and sys.argv[1].isdigit() else settings["llm"]["top_n"]
    require_api_key()

    leads = leads_source.connect(settings["leads_db"])
    threads = thread_store.connect(settings["threads_db"])
    fresh = [lead for lead in leads_source.candidates(leads, limit * 3)
             if not thread_store.thread(threads, lead["thread_id"])][:limit]
    print(f"писем: {len(fresh)}, модель {settings['llm']['model']}")

    llm = agent.model(settings)
    for number, lead in enumerate(fresh, 1):
        thread_store.open_thread(threads, lead["thread_id"], lead["company_id"], lead["seed"])
        proposal = agent.draft(llm, lead["seed"], [], agent.FIRST,
                               offer=settings["offer"]["text"])
        show(number, lead, proposal)
        if not proposal.stop:
            thread_store.add_draft(threads, lead["thread_id"], proposal.text, proposal.angle)

    leads.close()
    threads.close()
    print("Дальше: открыть консоль оператора и отправить черновики руками")


def require_api_key():
    """Отказать до первого запроса, а не в середине прогона."""
    if not os.environ.get("OPENROUTER_API_KEY"):
        sys.exit(
            "OPENROUTER_API_KEY пуст.\n"
            "  1) вписать ключ с https://openrouter.ai/keys в writer/.env\n"
            "  2) uv run --env-file .env -m scripts.write"
        )


def show(number, lead, proposal):
    print(f"\n{number}. {lead['seed']['name']} — {lead['thread_id']} ({lead['channel_kind']})")
    if proposal.stop:
        print("   агент советует не писать: повода в данных нет")
        return
    print(f"   угол: {proposal.angle}")
    print("   " + proposal.text.replace("\n", "\n   "))


if __name__ == "__main__":
    main()
