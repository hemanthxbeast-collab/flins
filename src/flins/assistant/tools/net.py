"""The one door online tools go through (JARVIS phase 4b, docs/milestones/J4b.md).

`Fetcher` can only GET JSON over https from a short list of hosts fixed here in code. It never
follows a redirect, stops reading after MAX_BYTES and always has a timeout. Nothing else in
`assistant/tools/` may import an HTTP library (a test scans for it).

`safe_query` decides what text may leave the PC: short, and nothing that looks like a secret,
an email address, a phone number or a long run of digits.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from flins.assistant.memory import is_secret

ALLOWED_HOSTS = frozenset(
    {
        "api.open-meteo.com",
        "geocoding-api.open-meteo.com",
        "en.wikipedia.org",
        "api.github.com",
    }
)
MAX_BYTES = 1_000_000
TIMEOUT_S = 5.0
ONLINE_TOOL_TIMEOUT_S = 12.0  # a tool may make two requests
USER_AGENT = "FLINS-assistant (personal local assistant)"  # no contact details, on purpose

_PERSONAL = re.compile(
    r"[^\s@]+@[^\s@]+\.[^\s@]+"  # an email address
    r"|\d{6,}"  # account numbers, codes, PIN codes
    r"|\+?\d[\d\s().-]{7,}\d"  # a phone number
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class NetError(Exception):
    """A plain reason, safe to show. `status` is the HTTP status when there was one."""

    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(reason)
        self.status = status


class QueryError(ValueError):
    pass


def safe_query(text: str, *, max_chars: int = 100) -> str:
    """The text that may be sent to a public service, or QueryError with a plain reason."""
    if _CONTROL.search(text.strip()):
        raise QueryError("that input has characters I won't send online")
    q = " ".join(text.split())
    if not q:
        raise QueryError("there's nothing to look up")
    if len(q) > max_chars:
        raise QueryError(f"that is too long to send online (over {max_chars} characters)")
    if is_secret(q) or _PERSONAL.search(q):
        raise QueryError("I won't send that online: it looks like it has personal details")
    return q


class Fetcher:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = TIMEOUT_S,
    ) -> None:
        self._transport = transport
        self._timeout = timeout_s

    async def get_json(
        self,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname not in ALLOWED_HOSTS
            or parts.port not in (None, 443)
            or parts.username is not None
            or parts.password is not None
        ):
            raise NetError("that address isn't on the allowed list")
        try:
            async with (
                httpx.AsyncClient(
                    transport=self._transport,
                    timeout=self._timeout,
                    follow_redirects=False,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                ) as client,
                client.stream("GET", url, params=params, headers=headers) as resp,
            ):
                if 300 <= resp.status_code < 400:
                    raise NetError("the service redirected the request, which isn't allowed")
                if resp.status_code == 404:
                    raise NetError("that wasn't found", 404)
                if resp.status_code in (403, 429):
                    raise NetError(
                        "the service is limiting requests right now; try again in a while",
                        resp.status_code,
                    )
                if resp.status_code >= 400:
                    raise NetError(
                        f"the service answered with an error ({resp.status_code})",
                        resp.status_code,
                    )
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise NetError("the answer was too large")
        except httpx.TimeoutException:
            raise NetError("the service took too long to answer") from None
        except httpx.HTTPError:
            raise NetError("I couldn't reach the service (are you online?)") from None
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise NetError("the service sent something I couldn't read") from None
