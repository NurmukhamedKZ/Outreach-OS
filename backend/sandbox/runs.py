"""Прогон песочницы — отдельный файл базы невосстановимого слоя.

Почему файл, а не флаг на треде: thread_id — это номер телефона и первичный
ключ threads, поэтому второй прогон по тому же лиду упёрся бы в занятый ключ, а
«чистый контекст» пришлось бы изображать фильтром в каждом запросе агента — и
воронка с инбоксом учились бы про тестовые треды забывать. Файл делает чистоту
контекста физической: прошлого сценария нет, потому что его нет в базе.

Схему наполняют её настоящие владельцы (thread_store, migrate, schema.sql), а
не копия DDL: база прогона обязана отличаться от боевой только содержимым.
"""

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3

import activity
import analytics
import clock
import paths
from collector.services import store
from sender.db import migrate, numbers
from writer.db import thread_store

RUNS_DIR = Path(__file__).resolve().parent.parent / "collector" / "data" / "sandbox"

# Наш номер в песочнице один: пул проверяется прогревом и монитором, а не
# количеством SIM, и второй номер добавил бы только выбор в гейте.
SANDBOX_NUMBER = "+77000000001"

META = "CREATE TABLE IF NOT EXISTS sandbox_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"


class UnknownRunError(Exception):
    """Прогона с таким id нет — или ни один не активен."""


@dataclass(frozen=True)
class Run:
    run_id: str
    company_id: str
    created_at: str
    warmed: bool
    offset: timedelta

    @property
    def path(self) -> Path:
        return RUNS_DIR / f"{self.run_id}.db"


_active: str | None = None


def create(company_id: str, warmed: bool, moment: datetime) -> Run:
    """Новый прогон: файл, три миграции, мета, один номер в пуле.

    `warmed` — единственный параметр создания, и он не косметический: без
    прогретого номера первый же прогон упёрся бы в календарь прогрева и не
    отправил бы ничего, а без нового номера нельзя проверить сам прогрев.
    """
    run = Run(run_id=f"{moment:%Y%m%d-%H%M}-{company_id}", company_id=company_id,
              created_at=moment.isoformat(timespec="seconds"), warmed=warmed,
              offset=timedelta())
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    _apply_schemas(run.path)
    _write_meta(run.path, {"company_id": company_id, "created_at": run.created_at,
                           "warmed": str(int(warmed)), "offset_seconds": "0"})
    with closing(sqlite3.connect(run.path)) as db:
        numbers.register(db, SANDBOX_NUMBER, session_dir="sandbox", now=moment,
                         skip_warmup=warmed)
    return run


def all() -> list[Run]:
    """Новые сверху: список прогонов читается как список чатов."""
    if not RUNS_DIR.exists():
        return []
    # Файл без меты — прогон, чьё создание оборвалось между схемой и метой.
    # Пропускается, а не роняет список: иначе одна такая крошка навсегда
    # убила бы страницу, и починить её было бы нечем, кроме shell.
    found = [run for run in (_read_or_none(path) for path in RUNS_DIR.glob("*.db"))
             if run is not None]
    return sorted(found, key=lambda run: run.run_id, reverse=True)


def get(run_id: str) -> Run:
    path = RUNS_DIR / f"{run_id}.db"
    if not path.exists():
        raise UnknownRunError(run_id)
    return _read(path)


def active() -> Run | None:
    return get(_active) if _active is not None else None


def activate(run_id: str) -> Run:
    """Переключить прогон целиком: база, журнал, аналитика, часы.

    Соединения везде короткие, поэтому живой процесс переключается без
    перезапуска — тик воркера на следующем обороте откроет уже новую базу.
    """
    global _active
    run = get(run_id)
    paths.use_run(run.path)
    activity.use(run.path)
    analytics.use(run.path, store.DERIVED)
    clock.use(run.offset)
    _active = run_id
    return run


def deactivate() -> None:
    """Вернуть боевые базу и часы. Зовётся при выключении песочницы и из
    тестов: забытое смещение сдвинуло бы время всему процессу."""
    global _active
    _active = None
    paths.use_run(None)
    activity.use(paths.state_db())
    analytics.use(paths.state_db(), store.DERIVED)
    clock.use(None)


def shift(seconds: int) -> Run:
    """Двинуть часы активного прогона. Смещение накапливается и ложится в
    мету: оно принадлежит прогону, а не процессу."""
    run = active()
    if run is None:
        raise UnknownRunError("активного прогона нет")
    moved = run.offset + timedelta(seconds=seconds)
    _write_meta(run.path, {"offset_seconds": str(int(moved.total_seconds()))})
    clock.use(moved)
    return get(run.run_id)


def _apply_schemas(path: Path) -> None:
    """Три владельца по очереди. Порядок не случаен: threads и messages
    создаёт система 2, и колонки состояния системы 3 доливаются в уже
    существующие таблицы — как и при боевом старте процесса."""
    thread_store.connect(path).close()
    with closing(sqlite3.connect(path)) as db:
        db.executescript(store.state_schema())
        db.execute(META)
        db.commit()
    migrate.connect(path).close()


def _write_meta(path: Path, values: dict[str, str]) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.executemany("INSERT INTO sandbox_meta (key, value) VALUES (?, ?)"
                       " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                       list(values.items()))
        db.commit()


def _read(path: Path) -> Run:
    with closing(sqlite3.connect(path)) as db:
        meta = dict(db.execute("SELECT key, value FROM sandbox_meta").fetchall())
    return Run(run_id=path.stem, company_id=meta["company_id"],
               created_at=meta["created_at"], warmed=meta["warmed"] == "1",
               offset=timedelta(seconds=int(meta["offset_seconds"])))


def _read_or_none(path: Path) -> Run | None:
    try:
        return _read(path)
    except (sqlite3.DatabaseError, KeyError):
        return None
