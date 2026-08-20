"""Единственное место, где строка лога узнаёт, к какой джобе и сущности она
относится — без протаскивания job_id/entity через сигнатуры функций, которые
об этом бизнес-смысле знать не должны (fetch.py, scrapling).
"""

import contextvars

_job_id = contextvars.ContextVar("job_id", default=None)
_entity = contextvars.ContextVar("entity", default=None)


class job:
    """Помечает все логи внутри блока текущим job_id."""

    def __init__(self, job_id):
        self._job_id = job_id
        self._token = None

    def __enter__(self):
        self._token = _job_id.set(self._job_id)
        return self

    def __exit__(self, *exc_info):
        _job_id.reset(self._token)


class entity:
    """Помечает все логи внутри блока текущей сущностью (домен/компания/лид/тред)."""

    def __init__(self, label):
        self._label = label
        self._token = None

    def __enter__(self):
        self._token = _entity.set(self._label)
        return self

    def __exit__(self, *exc_info):
        _entity.reset(self._token)


def set_job_id(job_id):
    """Прямая установка без context manager и без reset — для воркер-потоков
    ThreadPoolExecutor (collect.py::in_parallel), которые не наследуют
    contextvars вызывающего потока. Reset здесь не нужен: все задания одного
    вызова in_parallel относятся к одной и той же джобе, значение не меняется
    между ними — в отличие от entity, у которой на каждое задание свой домен."""
    _job_id.set(job_id)


def current_job_id():
    return _job_id.get()


def current_entity():
    return _entity.get()
