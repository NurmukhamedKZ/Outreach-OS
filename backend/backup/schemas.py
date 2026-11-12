from core.schemas import Schema, Timestamp


class Snapshot(Schema):
    name: str
    taken_at: Timestamp
    pages_added: int


class BackupStatus(Schema):
    enabled: bool
    running: bool
    target: str
    last_at: Timestamp | None
    snapshots: int
