"""Ф6-IG: смысл подписей инстаграма. Один вызов модели на аккаунт, сетевая сторона.

Почему здесь модель, а на сайте — regex. Признаки сайта технические: bitrix24,
gtag(, mc.yandex.ru. Ложных срабатываний у такой строки не бывает, и платить за
них модели незачем. Подписи — живой текст на двух языках: 13% из них казахские
(замер на 201 подписи), «директ» пишут латиницей, а стемы «консультаци» и
«регистрац» у бухгалтеров и юристов означают услугу, а не призыв, — то есть
regex ошибается ровно на белом списке рубрик.

Что модель НЕ делает. Затухание, возраст последнего поста и темп постинга —
арифметика по taken_at, она осталась в enrich.py. Модель отвечает только за смысл
подписи и не видит ни весов, ни дат: их подставляют правила (§11).

Кэш тот же, что у classify.py: raw/<sha256(модель+промпт)>.llm.json. Отсюда
главное свойство — build.py читает готовые ответы с диска, пересборка бесплатна,
платится только за впервые увиденный аккаунт.

Запуск:
  uv run --env-file .env -m scripts.classify_ig          все аккаунты с лентой в raw/
  uv run --env-file .env -m scripts.classify_ig 10       первые 10
"""

import json
import sys
import tomllib
from pathlib import Path

from schemas.ig_signals import IgSignals
from scripts.classify import cache_path, structured_model
from services import sources

CONFIG = Path("config.toml")
FEED_MARK = "feed/user/"

SYSTEM = (
    "Ты аналитик B2B-лидогенерации в Казахстане. Тебе дают подписи к постам "
    "инстаграм-аккаунта компании. Найди те, где компания САМА собирает заявки "
    "руками: зовёт написать в директ или WhatsApp, оставить контакт, звонит "
    "номером в подписи; объявляет акцию или скидку; ищет менеджера по продажам. "
    "Подписи бывают на русском и на казахском — разбирай оба. "
    "Описание услуги — не призыв: «подготовка юридических консультаций» это "
    "услуга, а «запишитесь на консультацию» — призыв. "
    "Ничего не выдумывай: quote обязана быть дословной фразой из подписи. "
    "Не нашёл ничего — верни пустой список."
)

# Потолок символов подписи в промпте. Длинные посты — это перечисление услуг
# после призыва, а призыв стоит в начале.
CAPTION_CHARS = 700


def main():
    config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))["llm"]
    limit = int(sys.argv[1]) if sys.argv[1:] and sys.argv[1].isdigit() else None

    accounts = feed_accounts(limit)
    if not accounts:
        sys.exit(
            "лент в raw/ нет: сначала uv run -m scripts.collect --instagram"
        )
    llm = structured_model(config["model"], IgSignals)
    print(f"подписи инстаграма: {len(accounts)} аккаунтов, модель {config['model']}")

    spent = failed = 0
    for number, (username, posts) in enumerate(accounts, 1):
        prompt = build_prompt(username, posts)
        path = cache_path(config["model"], prompt)
        if not path.exists():
            # Модель изредка отдаёт пустой ответ, и разбор схемы падает. Один такой
            # аккаунт не должен ронять прогон: в кэш ничего не записано, и повторный
            # запуск сам его переспросит, не переплачивая за остальные.
            try:
                ask_model(llm, config["model"], prompt, path)
                spent += 1
            except Exception as error:
                failed += 1
                print(f"\n  {username}: {type(error).__name__}: {str(error)[:120]}")
        print(f"  {number}/{len(accounts)}", end="\r", flush=True)
    print(f"\nновых вызовов {spent}, из кэша {len(accounts) - spent - failed}, отказов {failed}")
    if failed:
        print(f"{failed} аккаунтов без ответа — повтори запуск, остальные возьмутся из кэша")
    print("Дальше: uv run build.py && uv run report.py")


# --- данные ------------------------------------------------------------------


def feed_accounts(limit=None):
    """(логин, посты) по каждой ленте из raw/. Сети здесь нет — сырьё уже на диске.

    Аккаунты без единой подписи пропускаются: спрашивать модель не о чем, а вызов
    стоил бы столько же.
    """
    import build

    accounts = []
    for page in build.load_pages():
        if FEED_MARK not in page["url"]:
            continue
        feed = sources.parse_ig_feed(build.html_of(page))
        posts = [post for post in feed["posts"] if post["caption"]]
        if posts:
            accounts.append((feed["username"] or username_of(page["url"]), posts))
    accounts.sort()
    return accounts[:limit] if limit else accounts


def username_of(url):
    """Логин из адреса ленты — запасной путь, если приватный аккаунт не назвал себя."""
    return url.split(FEED_MARK, 1)[1].split("/", 1)[0]


def build_prompt(username, posts):
    """Промпт с пронумерованными подписями.

    Первая строка — «Инстаграм: <логин>»: по ней Ф6 находит аккаунт в кэше, как
    fill_profiles находит компанию по «Компания: <название>». company_id в промпт
    не входит, чтобы смена схемы идентификаторов не обесценивала оплаченные ответы.
    """
    lines = [f"Инстаграм: {username}", ""]
    for number, post in enumerate(posts, 1):
        lines.append(f"[{number}] {post['caption'][:CAPTION_CHARS]}")
    return "\n".join(lines)


# --- вызов -------------------------------------------------------------------


def ask_model(llm, model, prompt, path):
    """Один вызов на аккаунт. Ответ ложится в raw/ рядом с промптом.

    kind обязателен: build.fill_profiles разбирает все raw/*.llm.json разом и без
    метки принял бы находки ленты за профиль компании.
    """
    found = llm.invoke([("system", SYSTEM), ("human", prompt)])
    path.write_text(
        json.dumps(
            {
                "kind": "ig_signals",
                "model": model,
                "prompt": prompt,
                "signals": found.model_dump()["signals"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
