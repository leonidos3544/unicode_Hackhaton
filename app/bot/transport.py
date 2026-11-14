from typing import Any

import httpx


class TelegramError(Exception):
    def __init__(self, code: int, retry_after: int = 0) -> None:
        self.code, self.retry_after = code, min(max(retry_after, 0), 86400)
        super().__init__(f"telegram_error_{code}")  # Never include token, URL or response body.


class TelegramClient:
    def __init__(self, token: str, client: httpx.AsyncClient) -> None:
        self._base = f"https://api.telegram.org/bot{token}/"
        self._client = client

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        if method not in {
            "getUpdates",
            "sendMessage",
            "answerCallbackQuery",
            "setMyCommands",
            "deleteMessage",
        }:
            raise ValueError("Unsupported Telegram method")
        try:
            response = await self._client.post(self._base + method, json=payload, timeout=40)
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid Telegram response")
            if response.is_error or not data.get("ok"):
                params = data.get("parameters") or {}
                raise TelegramError(
                    int(data.get("error_code", response.status_code)),
                    int(params.get("retry_after", 0)),
                )
            return data.get("result")
        except (httpx.HTTPError, ValueError, TypeError):
            raise TelegramError(503) from None
