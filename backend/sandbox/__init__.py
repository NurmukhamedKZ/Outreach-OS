"""Песочница аутрича: подменный транспорт, роль лида, машина времени.

Знает про все три системы; про неё не знает никто — это сторожит
sandbox/tests/test_import_graph.py.
"""

from urllib.parse import urlsplit

from fastapi import FastAPI

from config import Settings
from core.whatsapp import NodeWhatsApp

SANDBOX_NODE_PATH = "/api/sandbox/node"
# The fake Node is this very process, so whatever the sandbox talks to is on this machine.
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


class SandboxMisconfigured(RuntimeError):
    """The sandbox is on, but it would talk to something that is not its own fake Node."""


def mount(app: FastAPI, settings: Settings) -> None:
    """Mount the sandbox's routers only under SANDBOX=1, and only over its own fake Node.

    Not "if the flag, do not work" but "without the flag there are no routes":
    impossibility, not discipline. Before the routers, every address the sandbox
    talks to must lead to this machine: the transport setting, the client the
    delivery daemons get from `app.state.whatsapp` (so set it before the call),
    and the self address the fake Node posts its webhook and secret to.

    Raises:
        SandboxMisconfigured: the sandbox is on and an address leads elsewhere.
    """
    if not settings.sandbox:
        return
    transport = getattr(app.state, "whatsapp", None)
    node_urls = [settings.sender_node_url, _url_of(transport)]
    if not all(_is_local(url, SANDBOX_NODE_PATH) for url in node_urls):
        raise SandboxMisconfigured(
            f"SANDBOX=1, but the transport leads to {node_urls}, not to the fake Node:"
            " messages would reach real people. Set SENDER_NODE_URL to"
            f" http://127.0.0.1:8787{SANDBOX_NODE_PATH} or turn the sandbox off."
        )
    if not _is_local(settings.sandbox_self_url, ""):
        raise SandboxMisconfigured(
            f"SANDBOX=1, but SANDBOX_SELF_URL={settings.sandbox_self_url} is not this"
            " machine: the fake Node would post the webhook secret there."
        )
    # Imported here so that without the flag not even the routes' modules load.
    from sandbox import node, routes, runs

    # Until this line paths.state_db() is the production database while the
    # transport is already the fake: the daemons would have worked the real
    # threads through it. The newest run is activated, or an empty idle base.
    runs.activate_latest()
    app.include_router(routes.router)
    app.include_router(node.router)


def _url_of(transport: object) -> str:
    """Return where the ticks send; a client without a readable address gets none, and fails."""
    return transport.base_url if isinstance(transport, NodeWhatsApp) else ""


def _is_local(url: str, path: str) -> bool:
    parts = urlsplit(url)
    return parts.hostname in LOOPBACK and parts.path.rstrip("/") == path
