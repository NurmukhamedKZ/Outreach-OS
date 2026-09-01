"""Шов между Python и Node: единственный модуль системы 3, ходящий в сеть.

Node — тупая труба: держит сокеты Baileys, шлёт текст, принимает события. Он не
знает про лидов, треды, лимиты и расписание. Причина не эстетическая: Node —
единственная часть системы, которую нельзя протестировать без живого WhatsApp,
поэтому всё, что содержит решение, обязано жить там, где есть база и pytest.

Отсюда же деление ответов. `Sent(sent=False)` — честное «фрейм в сокет не ушёл»,
на нём строится безопасный ретрай. `TransportError` — «мы не знаем, ушло ли»,
и это уже случай для человека, а не для повтора.
"""

from dataclasses import dataclass
from functools import lru_cache

import httpx

from config import settings

TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class Sent:
    sent: bool
    provider_id: str | None
    error: str | None


class TransportError(Exception):
    """Node не ответил или ответил не-2xx: результат отправки неизвестен."""


class Transport:
    def __init__(self, base_url: str, client: httpx.AsyncClient):
        self._base_url = base_url.rstrip("/")
        self._client = client

    async def send(self, number: str, to: str, text: str, key: str,
                   kind: str = "text") -> Sent:
        payload = {"number": number, "to": to, "text": text, "type": kind, "key": key}
        body = await self._post("/send", payload)
        return Sent(sent=bool(body.get("sent")),
                    provider_id=body.get("provider_id"),
                    error=body.get("error"))

    async def check(self, number: str, to: str) -> bool:
        body = await self._post("/check", {"number": number, "to": to})
        return bool(body["has_whatsapp"])

    async def pair(self, number: str) -> str:
        body = await self._post("/pair", {"number": number})
        return body["code"]

    async def qr(self, number: str) -> str:
        body = await self._post("/qr", {"number": number})
        return body["qr"]

    async def health(self) -> dict[str, dict]:
        try:
            response = await self._client.get(f"{self._base_url}/health",
                                              timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise TransportError(f"GET /health: {error}") from error
        return response.json()

    async def _post(self, path: str, payload: dict) -> dict:
        try:
            response = await self._client.post(f"{self._base_url}{path}", json=payload,
                                               timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise TransportError(f"POST {path}: {error}") from error
        return response.json()


@lru_cache(maxsize=1)
def build() -> Transport:
    """Один клиент на процесс. Новый `AsyncClient` на каждый вызов никто не
    закрывает, а зовут `build()` из тика прогрева, часового монитора и ручки
    /pair — это сотни повисших пулов соединений в сутки."""
    return Transport(settings.sender_node_url, httpx.AsyncClient())
