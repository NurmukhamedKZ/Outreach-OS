"""Operator settings store: one JSON section per module (ADR 0003).

Each module describes its settings and defaults with a Pydantic schema; core
only keeps the values. Nothing is cached in the process, so an edit applies to
the next batch or tick without a restart.
"""

import json
import logging
import sqlite3

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)


def load[S: BaseModel](db: sqlite3.Connection, module: str, schema: type[S]) -> S:
    """Return the module's saved values laid over the schema defaults.

    Every field of `schema` needs a default and the schema must not forbid
    extra keys. Unknown keys are ignored. A saved value the schema no longer
    accepts falls back to its default, and a section rejected as a whole (a
    model validator) falls back to all defaults, both with a warning: a schema
    change must not lock the operator out of the module.
    """
    row = db.execute("SELECT data FROM operator_settings WHERE module = ?",
                     (module,)).fetchone()
    saved = json.loads(row[0]) if row else {}
    try:
        return schema.model_validate(saved)
    except ValidationError as error:
        stale = {issue["loc"][0] for issue in error.errors() if issue["loc"]}
        log.warning("settings of %s: stale keys fell back to defaults: %s",
                    module, sorted(map(str, stale)) or "the whole section")
        try:
            return schema.model_validate({k: v for k, v in saved.items() if k not in stale})
        except ValidationError:
            return schema()


def save(db: sqlite3.Connection, module: str, values: BaseModel) -> None:
    """Replace the module's whole section. Runs in the caller's transaction."""
    db.execute("INSERT INTO operator_settings (module, data) VALUES (?, ?)"
               " ON CONFLICT (module) DO UPDATE SET data = excluded.data",
               (module, values.model_dump_json()))
