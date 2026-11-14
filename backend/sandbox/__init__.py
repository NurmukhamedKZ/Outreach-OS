"""Песочница аутрича: подменный транспорт, роль лида, машина времени.

Знает про все три системы; про неё не знает никто — это сторожит
sandbox/tests/test_import_graph.py.
"""

from fastapi import FastAPI

from config import Settings
from core.whatsapp import NodeWhatsApp

SANDBOX_NODE_PATH = "/api/sandbox/node"


class SandboxMisconfigured(RuntimeError):
    """Песочница включена, а транспорт боевой — письма ушли бы живым людям."""


def mount(app: FastAPI, settings: Settings) -> None:
    """Mount the sandbox's routers only under SANDBOX=1, and only over its own fake Node.

    Not "if the flag, do not work" but "without the flag there are no routes":
    impossibility, not discipline. Two walls before the routers: the setting,
    and the transport the delivery daemons actually get from `app.state.whatsapp`.

    Raises:
        SandboxMisconfigured: the sandbox is on and a transport leads to the real Node.
    """
    if not settings.sandbox:
        return
    transport = getattr(app.state, "whatsapp", None)
    for url in (settings.sender_node_url, _url_of(transport)):
        if SANDBOX_NODE_PATH not in url:
            raise SandboxMisconfigured(
                f"SANDBOX=1, но транспорт ведёт на {url or 'неизвестный клиент'}, а не в"
                f" подменный Node: письма ушли бы живым людям. Пропишите SENDER_NODE_URL"
                f" с {SANDBOX_NODE_PATH} или выключите песочницу."
            )
    from sandbox import node, routes, runs

    # Until this line paths.state_db() is the production database while the
    # transport is already the fake: the daemons would have worked the real
    # threads through it. The newest run is activated, or an empty idle base.
    runs.activate_latest()
    app.include_router(routes.router)
    app.include_router(node.router)


def _url_of(transport: object) -> str:
    """Return where the ticks send; an unknown client has no address the sandbox can trust."""
    return transport.base_url if isinstance(transport, NodeWhatsApp) else ""
