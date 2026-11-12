"""wikipedia: the opening section of the best-matching English Wikipedia article.

Two requests: search for the topic, then fetch the top article's introduction as plain text.
Wikipedia refuses automated clients without a contact address, so this tool exists only when
you set one (assistant.tools.online.wikipedia_contact).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, ValidationError

from flins import __version__
from flins.assistant.tools.base import Tool, ToolResult, fail, ok
from flins.assistant.tools.net import (
    ONLINE_TOOL_TIMEOUT_S,
    Fetcher,
    NetError,
    QueryError,
    safe_query,
)

API_URL = "https://en.wikipedia.org/w/api.php"
MAX_EXTRACT_CHARS = 1800


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _Hit(_In):
    title: str


class _SearchQuery(_In):
    search: list[_Hit] = []


class _Search(_In):
    query: _SearchQuery = _SearchQuery()


class _Page(_In):
    title: str
    extract: str = ""


class _PageQuery(_In):
    pages: list[_Page] = []


class _Pages(_In):
    query: _PageQuery = _PageQuery()


def make_wiki_tool(fetcher: Fetcher, contact: str) -> Tool:
    """`contact` (a URL or email of yours) goes in the User-Agent: Wikipedia refuses
    automated clients that don't give one. It is sent to Wikipedia only."""
    agent = {"User-Agent": f"FLINS-assistant/{__version__} ({contact})"}

    async def run(arg: str, now: datetime) -> ToolResult:
        try:
            topic = safe_query(arg, max_chars=100)
        except QueryError as exc:
            return fail(f"Can't look that up: {exc}.")
        base = {"action": "query", "format": "json", "formatversion": "2"}
        try:
            hits = _Search.model_validate(
                await fetcher.get_json(
                    API_URL,
                    {**base, "list": "search", "srsearch": topic, "srlimit": "4"},
                    agent,
                )
            ).query.search
            if not hits:
                return fail(f"Wikipedia has nothing for '{topic}'.")
            pages = _Pages.model_validate(
                await fetcher.get_json(
                    API_URL,
                    {
                        **base,
                        "prop": "extracts",
                        "exintro": "1",
                        "explaintext": "1",
                        "redirects": "1",
                        "titles": hits[0].title,
                    },
                    agent,
                )
            ).query.pages
        except NetError as exc:
            return fail(f"Couldn't reach Wikipedia: {exc}.")
        except ValidationError:
            return fail("Couldn't read Wikipedia's answer.")
        page = pages[0] if pages else _Page(title=hits[0].title)
        text = " ".join(page.extract.split())
        if not text:
            return fail(f"Wikipedia's article '{page.title}' has no summary I could read.")
        if len(text) > MAX_EXTRACT_CHARS:
            text = text[:MAX_EXTRACT_CHARS].rsplit(" ", 1)[0] + " …"
        lines = [f"Wikipedia, article '{page.title}':", text]
        others = [h.title for h in hits[1:] if h.title != page.title]
        if others:
            lines.append("Other matching articles: " + "; ".join(others) + ".")
        return ok("\n".join(lines))

    return Tool(
        name="wikipedia",
        description=(
            "Looks a topic up on Wikipedia and returns the article's introduction. Goes online: "
            "only the topic is sent."
        ),
        usage="a short topic or name, e.g. 'Chandrayaan-3' or 'Carnot cycle'. Not a question.",
        risk="online",
        run=run,
        outside_content=True,
        timeout_s=ONLINE_TOOL_TIMEOUT_S,
    )
