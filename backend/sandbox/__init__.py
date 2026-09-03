"""Песочница аутрича: подменный транспорт, роль лида, машина времени.

Знает про все три системы; про неё не знает никто — это сторожит
sandbox/tests/test_import_graph.py.
"""

from fastapi import FastAPI

SANDBOX_NODE_PATH = "/api/sandbox/node"


class SandboxMisconfigured(RuntimeError):
    """Песочница включена, а транспорт боевой — письма ушли бы живым людям."""


def mount(app: FastAPI, settings) -> None:
    """Роутеры песочницы существуют только при SANDBOX=1.

    Не «если флаг, то не работать», а «если нет флага, то и ручек нет»: тот же
    принцип, которым rebuild лишён возможности сходить в сеть — невозможность,
    а не дисциплина.
    """
    if not settings.sandbox:
        return
    if SANDBOX_NODE_PATH not in settings.sender_node_url:
        raise SandboxMisconfigured(
            f"SANDBOX=1, но SENDER_NODE_URL={settings.sender_node_url} — это боевой"
            f" транспорт. Пропишите {SANDBOX_NODE_PATH} или выключите песочницу.")
    from sandbox import node, routes, runs

    # До этой строки paths.state_db() — боевая база, а транспорт уже подменён:
    # фоновые задачи (прогрев, обработка входящего, очередь) успели бы
    # поработать с настоящей перепиской через фейк. Активируется свежайший
    # прогон, а если сценариев ещё нет — пустая холостая база.
    runs.activate_latest()
    app.include_router(routes.router)
    app.include_router(node.router)
