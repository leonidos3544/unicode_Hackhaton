import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urljoin, urlsplit

import httpx
from protego import Protego

from app.common.clock import utcnow
from app.ingestion.sources.config import SourceConfig

USER_AGENT = "BashCultureCatalog/0.1 (+public-cultural-schedules; no-images)"
MAX_BODY = 2_097_152


class FetchError(Exception):
    pass


@dataclass(frozen=True)
class PageSnapshot:
    url: str
    body: str
    content_hash: str
    fetched_at: datetime
    status_code: int


def validate_url(url: str, allowed_hosts: list[str]) -> None:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname not in allowed_hosts
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
    ):
        raise FetchError("URL не входит в разрешённые HTTPS-источники")


class SourceClient:
    """Only fixed, operator-reviewed hostnames; no arbitrary URL endpoint or external redirects."""

    def __init__(self, client: httpx.AsyncClient, config: SourceConfig) -> None:
        self.client = client
        self.config = config
        self.pages: dict[str, PageSnapshot] = {}
        self.robots: dict[str, Protego] = {}
        self.request_count = 0

    async def _download(self, url: str) -> PageSnapshot:
        for _ in range(4):
            validate_url(url, self.config.allowed_hosts)
            if self.request_count >= 200:
                raise FetchError("Достигнут лимит запросов к источнику")
            self.request_count += 1
            await asyncio.sleep(0.4)
            try:
                async with self.client.stream("GET", url, follow_redirects=False) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError("Пустой адрес перенаправления")
                        url = urljoin(url, location)
                        continue
                    if response.status_code not in (200, 404):
                        raise FetchError(f"Источник вернул HTTP {response.status_code}")
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > MAX_BODY:
                            raise FetchError("Страница превышает лимит размера")
                    raw = bytes(content)
                    # Official source pages declare UTF-8; never silently replace broken characters.
                    try:
                        body = raw.decode("utf-8-sig")
                    except UnicodeDecodeError as exc:
                        raise FetchError("Неподдерживаемая кодировка источника") from exc
                    return PageSnapshot(
                        str(response.url),
                        body,
                        hashlib.sha256(raw).hexdigest(),
                        utcnow(),
                        response.status_code,
                    )
            except httpx.HTTPError as exc:
                raise FetchError(f"Сетевая ошибка: {type(exc).__name__}") from exc
        raise FetchError("Слишком много перенаправлений")

    async def fetch(self, url: str) -> PageSnapshot:
        validate_url(url, self.config.allowed_hosts)
        if url in self.pages:
            return self.pages[url]
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self.robots:
            page = await self._download(origin + "/robots.txt")
            rules = page.body if page.status_code == 200 else "User-agent: *\nDisallow:\n"
            rules = rules.replace("\r\r\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
            parser = Protego.parse(rules)
            self.robots[origin] = parser
        policy = self.robots[origin]
        if not policy.can_fetch(url, USER_AGENT):
            raise FetchError("Сбор этой страницы запрещён robots.txt")
        delay = policy.crawl_delay(USER_AGENT)
        if delay:
            delay_seconds = int(delay)
            if delay_seconds > 30:
                raise FetchError("Требуемый интервал источника превышает лимит текущего сборщика")
            await asyncio.sleep(delay_seconds)
        page = await self._download(url)
        if page.status_code != 200:
            raise FetchError("Страница не найдена (HTTP 404)")
        self.pages[url] = page
        return page
