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
    """The sandbox would talk to something that is not its own fake Node,
    or the offline mode is on outside a sandbox with a data directory of its own."""


def mount(app: FastAPI, settings: Settings) -> None:
    """Mount the sandbox's routers only under SANDBOX=1, and only over its own fake Node.

    Not "if the flag, do not work" but "without the flag there are no routes":
    impossibility, not discipline. Before the routers, every address the sandbox
    talks to must lead to this very backend, its host and its port: the
    transport setting, the client the delivery daemons get from
    `app.state.whatsapp` (so set it before the call), and the self address the
    fake Node posts its webhook and secret to. Another port on this machine is
    another backend: the live Node, or another stand with its own database.

    The offline mode (OFFLINE=1) is checked first, the sandbox on or off: its
    model invents text and dossiers, so it needs the sandbox and a data
    directory other than the default one.

    Raises:
        SandboxMisconfigured: the sandbox is on and an address leads elsewhere,
            or the offline mode is on without the sandbox or on the default data directory.
    """
    _refuse_offline_outside_a_stand(settings)
    if not settings.sandbox:
        return
    port = settings.uvicorn_port
    transport = getattr(app.state, "whatsapp", None)
    node_urls = [settings.sender_node_url, _url_of(transport)]
    if not all(_is_ours(url, port, SANDBOX_NODE_PATH) for url in node_urls):
        raise SandboxMisconfigured(
            f"SANDBOX=1, but the transport leads to {node_urls}, not to this backend's"
            " fake Node: messages would reach real people. Set SENDER_NODE_URL to"
            f" http://127.0.0.1:{port}{SANDBOX_NODE_PATH} (UVICORN_PORT={port})"
            " or turn the sandbox off."
        )
    if not _is_ours(settings.sandbox_self_url, port, ""):
        raise SandboxMisconfigured(
            f"SANDBOX=1, but SANDBOX_SELF_URL={settings.sandbox_self_url} is not this"
            f" backend (http://127.0.0.1:{port}, UVICORN_PORT={port}): the fake Node would"
            " post the webhook secret there."
        )
    # Imported here so that without the flag not even the routes' modules load.
    from sandbox import node, routes, runs

    # Until this line paths.state_db() is the production database while the
    # transport is already the fake: the daemons would have worked the real
    # threads through it. The newest run is activated, or an empty idle base.
    runs.activate_latest()
    app.include_router(routes.router)
    app.include_router(node.router)


def _refuse_offline_outside_a_stand(settings: Settings) -> None:
    """Refuse the offline mode where its invented text could reach a person or production data."""
    if not settings.offline:
        return
    if not settings.sandbox:
        raise SandboxMisconfigured(
            "OFFLINE=1 without SANDBOX=1: the offline model's invented text would go to real"
            " people through the live transport. Turn the sandbox on (scripts/stand does)"
            " or the offline mode off."
        )
    default = Settings.model_fields["data_dir"].default
    if settings.data_dir.resolve() == default.resolve():
        raise SandboxMisconfigured(
            f"OFFLINE=1 on the default data directory {default}: invented dossiers would land"
            " in the production database. Set DATA_DIR to a directory of its own"
            " (scripts/stand does) or turn the offline mode off."
        )


def _url_of(transport: object) -> str:
    """Return where the ticks send; a client without a readable address gets none, and fails."""
    return transport.base_url if isinstance(transport, NodeWhatsApp) else ""


def _is_ours(url: str, port: int, path: str) -> bool:
    """Return True when `url` is exactly `path` on this machine's `port`; a URL without one never is."""
    parts = urlsplit(url)
    try:
        url_port = parts.port
    except ValueError:  # not a number or out of range
        return False
    return parts.hostname in LOOPBACK and url_port == port and parts.path.rstrip("/") == path
