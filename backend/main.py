"""The assembler: three systems in one process — the modules, the adapters of the process, the daemons.

Only assembly lives here; every route is in its module's api.py. This is the one
place where the systems see each other.

Run from backend/:
  uv run python main.py
  uv run uvicorn main:app --port 8787 --reload
"""

import sys
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))

from fastapi import FastAPI

import clock
import logging_setup
import paths
import sandbox
from backup import api as backup_api
from backup import public as backup
from companies import api as companies_api
from companies import public as companies
from config import settings
from conversations import api as conversations_api
from conversations import public as conversations
from core import db as core_db
from core.activity import Journal
from core.errors import DomainError, domain_error_handler
from core.pages import ScraplingPages
from core.whatsapp import NodeWhatsApp
from delivery import api as delivery_api
from delivery import public as delivery
from overview import api as overview_api
from refusals import api as refusals_api
from research import api as research_api
from research import public as research


def prepare_database(path: Path) -> None:
    """Apply Alembic revisions and reset the work a stopped process left running."""
    core_db.migrate(path)
    with closing(core_db.connect(path)) as db:
        companies.forget_interrupted_collection(db)
        research.forget_interrupted(db)
        conversations.forget_interrupted(db)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # backend.log and network.log, set up once per process; at start, not on
    # import, so a test that imports the app writes no log files.
    logging_setup.configure()
    # The production database always: in the sandbox the overview and the
    # conversation read companies and dossiers there.
    for path in {paths.PRODUCTION_STATE, paths.state_db()}:
        prepare_database(path)
    # Threads, not tasks (ADR 0005): a backup copies for seconds, a tick waits
    # for Node and the model. Daemons, so work in progress does not hold
    # shutdown; a send cut short is left in sending for the next monitor.
    stop = threading.Event()
    threading.Thread(
        target=backup.loop, args=(app.state.backup, stop), name="backup", daemon=True
    ).start()
    delivery.start(
        stop, app.state.whatsapp, delivery.telegram, conversations.model, app.state.journal
    )
    yield
    stop.set()


app = FastAPI(title="Outreach OS", description="Лиды по Казахстану", lifespan=lifespan)
# One journal per process, over the database of the moment: a sandbox run switches it live.
app.state.journal = Journal(paths.state_db)
# One page adapter per process: the pace belongs to the host, two would double it.
app.state.pages = ScraplingPages(paths.PAGES, now=clock.now)
# One backup service per process: its lock keeps the daemon and the button apart.
# The production database, never a sandbox run's.
app.state.backup = backup.BackupService(
    settings.backup_dir, paths.PRODUCTION_STATE, paths.PAGES, paths.SESSIONS, app.state.journal
)
# One WhatsApp client per process: one pool of connections to Node.
app.state.whatsapp = NodeWhatsApp(settings.sender_node_url)
app.add_exception_handler(DomainError, domain_error_handler)  # type: ignore[arg-type]  # Starlette types handlers loosely
for router in (
    overview_api.router,
    refusals_api.router,
    companies_api.router,
    research_api.router,
    backup_api.router,
    conversations_api.router,
    delivery_api.router,
):
    app.include_router(router)

# The sandbox mounts here for the same reason the seams live here.
sandbox.mount(app, settings)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.uvicorn_host, port=8787)
