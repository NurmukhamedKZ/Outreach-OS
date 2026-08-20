"""Разовый логин в Instagram руками. Сохраняет cookies в cookies.json.

Запуск из backend/ (там живёт venv): uv run ../scripts/ig/login.py
Откроется настоящий Chrome — логинься сам (пароль/2FA). Скрипт ждёт появления
sessionid и закрывается. Профиль остаётся в collector/data/ig_profile/,
повторно обычно не нужен.
"""

import json
from pathlib import Path

from scrapling.fetchers import StealthySession

ROOT = Path(__file__).resolve().parent.parent.parent
COOKIES = ROOT / "backend" / "collector" / "data" / "cookies.json"
PROFILE = ROOT / "backend" / "collector" / "data" / "ig_profile"


def wait_for_login(page):
    print("Логинься в открывшемся окне. Жду sessionid (до 10 минут)...")
    for _ in range(600):
        jar = {c["name"]: c["value"] for c in page.context.cookies()}
        if jar.get("sessionid"):
            page.wait_for_timeout(3000)  # дать IG дописать остальные куки
            jar = {c["name"]: c["value"] for c in page.context.cookies()}
            COOKIES.write_text(json.dumps(jar, indent=2))
            print(f"Готово, {len(jar)} cookies -> {COOKIES}")
            return
        page.wait_for_timeout(1000)
    raise TimeoutError("sessionid так и не появился — логин не завершён")


def main():
    PROFILE.mkdir(exist_ok=True)
    with StealthySession(
        headless=False,
        real_chrome=True,
        user_data_dir=str(PROFILE),
        google_search=False,
        timeout=600_000,
    ) as session:
        session.fetch("https://www.instagram.com/", page_action=wait_for_login)


if __name__ == "__main__":
    main()
