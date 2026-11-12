"""Run migrations against one SQLite file.

The path comes from core.db.migrate via config.attributes; the CLI falls back
to the production database.
"""

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

import paths

path = context.config.attributes.get("path") or paths.state_db()
# autocommit=False makes sqlite3 open a transaction before DDL too, so a failing
# migration rolls back whole instead of leaving half a table rebuild behind.
# foreign_keys stays off (SQLite default): batch mode rebuilds tables by
# copy-and-drop, and with it on the drop would cascade into child rows.
# timeout: a second process (uvicorn --reload) may hold the write lock.
engine = create_engine(f"sqlite:///{path}", poolclass=NullPool,
                       connect_args={"autocommit": False, "timeout": 10})

with engine.connect() as connection:
    context.configure(connection=connection, render_as_batch=True,
                      transactional_ddl=True, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()
