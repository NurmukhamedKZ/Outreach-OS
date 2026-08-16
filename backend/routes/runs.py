"""Запуск скриптов конвейера из браузера. Вывод копится в памяти, клиент дочитывает.

Белый список — единственное, что здесь есть от безопасности: из веба нельзя
запустить ничего, кроме перечисленного ниже, а аргументы уходят в argv списком,
без оболочки. Свою логику ни одна команда тут не дублирует: это тот же
`uv run ...`, что в README, только нажатый мышкой.

Ответ не стримится: `next dev` проксирует `/api/*` и на долгих ответах теряет
тело. Клиент забирает лог кусками по offset — и заодно видит запуск, начатый до
перезагрузки страницы.

Запуск проверки: uv run -m routes.runs
"""

import asyncio
import os
import shlex
import signal
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/runs")

BACKEND = Path(__file__).resolve().parent.parent
LOG_LIMIT = 20_000

COMMANDS = {
    "collect": ("Сбор сырья", ["uv", "run", "-m", "scripts.collect"]),
    "classify": ("Профиль и why_now", ["uv", "run", "--env-file", ".env", "-m", "scripts.classify"]),
    "build": ("Пересборка базы", ["uv", "run", "build.py"]),
    "report": ("Выгрузка leads.csv", ["uv", "run", "report.py"]),
    "check": ("Проверки", ["uv", "run", "-m", "scripts.check"]),
    "probe-gis-rubrics": ("Разведка: рубрики 2GIS", ["uv", "run", "-m", "services.probes.gis_rubrics", "demo"]),
    "probe-gis-list": ("Разведка: список 2GIS", ["uv", "run", "-m", "services.probes.gis_list", "demo"]),
    "probe-gis-firm": ("Разведка: карточка 2GIS", ["uv", "run", "-m", "services.probes.gis_firm", "demo"]),
    "probe-hh": ("Разведка: вакансии hh", ["uv", "run", "-m", "services.probes.hh_vacancies", "demo"]),
    "probe-serp": ("Разведка: выдача поиска", ["uv", "run", "-m", "services.probes.serp", "demo"]),
    "ig-login": ("Instagram: логин руками", ["uv", "run", "ig/login.py"]),
    "ig-posts": ("Instagram: посты аккаунта", ["uv", "run", "ig/fetch_posts.py"]),
}


class Run:
    """Один запуск: команда, накопленный вывод и код возврата, пока он не известен."""

    def __init__(self, name, argv):
        self.name = name
        self.title = COMMANDS[name][0]
        self.argv = argv
        self.lines = [f"$ {shlex.join(argv)}"]
        self.code = None
        self.process = None

    @property
    def active(self):
        return self.code is None

    def log(self, line):
        if len(self.lines) < LOG_LIMIT:
            self.lines.append(line)
        elif len(self.lines) == LOG_LIMIT:
            self.lines.append(f"— вывод длиннее {LOG_LIMIT} строк, дальше не пишем")


# ponytail: один запуск на весь бэкенд. build.py пересобирает базу через DROP, и
# параллельный collect писал бы в неё же. Очередь понадобится, когда операторов
# станет двое.
current: Run | None = None


@router.get("")
def catalogue():
    return [
        {"name": name, "title": title, "command": " ".join(argv)}
        for name, (title, argv) in COMMANDS.items()
    ]


@router.get("/current")
def tail(offset: int = 0):
    """Хвост лога начиная с offset. Новый offset клиент присылает следующим запросом."""
    if current is None:
        return {"name": None, "title": "", "lines": [], "offset": 0, "code": 0}
    return {
        "name": current.name,
        "title": current.title,
        "lines": current.lines[offset:],
        "offset": len(current.lines),
        "code": current.code,
    }


@router.post("/current/stop")
def stop():
    """Убивает всю группу: `uv run` порождает python, и выживший потомок держал бы
    трубу открытой — запуск не завершился бы никогда и заблокировал очередь."""
    if current is None or not current.active or current.process is None:
        raise HTTPException(409, "нечего прерывать")
    try:
        os.killpg(os.getpgid(current.process.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    return {"name": current.name}


@router.post("/{name}")
async def start(name: str, args: str = ""):
    global current
    if name not in COMMANDS:
        raise HTTPException(404, f"нет команды {name}")
    if current is not None and current.active:
        raise HTTPException(409, f"{current.title} ещё выполняется")
    current = Run(name, COMMANDS[name][1] + shlex.split(args))
    asyncio.create_task(pump(current))
    return {"name": name}


async def pump(run):
    """Гоняет процесс до конца. Код возврата ставится всегда — иначе запуск завис бы."""
    try:
        run.process = await asyncio.create_subprocess_exec(
            *run.argv,
            cwd=BACKEND,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            start_new_session=True,  # своя группа процессов, чтобы stop() убил и потомков
        )
        async for line in run.process.stdout:
            run.log(line.decode(errors="replace").rstrip("\n"))
        run.code = await run.process.wait()
    except OSError as failure:
        run.log(f"— запустить не удалось: {failure}")
        run.code = -1


def demo():
    """Вывод доезжает до клиента кусками, код возврата виден, чужая команда не запускается."""
    for title, argv in COMMANDS.values():
        assert title and argv[:2] == ["uv", "run"], f"{argv} — не команда uv run"

    run = asyncio.run(finished(["python", "-c", "print('привет')"]))
    assert run.code == 0, run.lines
    assert "привет" in run.lines, run.lines
    assert tail(offset=len(run.lines))["lines"] == [], "хвост по offset не пуст"
    assert tail(offset=1)["lines"] == run.lines[1:], "offset режет не там"

    assert asyncio.run(finished(["python", "-c", "raise SystemExit(3)"])).code == 3
    assert asyncio.run(finished(["нет-такой-команды"])).code == -1, "провал запуска повесил бы очередь"
    asyncio.run(check_stop_kills_children())
    print("runs demo ok — лог копится, код возврата виден, прерванный запуск не блокирует")


async def finished(argv):
    global current
    current = Run("check", argv)
    await pump(current)
    return current


async def check_stop_kills_children():
    """`sh -c 'sleep & wait'` — модель `uv run python`: потомок переживает смерть родителя."""
    global current
    current = Run("check", ["sh", "-c", "sleep 30 & wait"])
    running = asyncio.create_task(pump(current))
    while current.process is None:
        await asyncio.sleep(0.05)
    stop()
    await asyncio.wait_for(running, timeout=5)
    assert current.code != 0, "прерванный запуск отчитался успехом"


if __name__ == "__main__":
    demo()
