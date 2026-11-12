"""github: read-only facts about repositories from the GitHub REST API.

The input has a few fixed forms (see `usage`); the model never builds a URL. A token is
optional: without one only public repositories are visible and the hourly limit is low. The
token is sent only in the header of requests to api.github.com and never appears in a result.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from flins.assistant.tools.base import Tool, ToolResult, fail, ok
from flins.assistant.tools.net import ONLINE_TOOL_TIMEOUT_S, Fetcher, NetError

API = "https://api.github.com"
_USER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
_REPO = re.compile(
    rf"({_USER})/([A-Za-z0-9._-]{{1,100}})(?:\s+(issues|pulls|prs|commits|releases))?",
    re.IGNORECASE,
)
_REPOS = re.compile(rf"repos(?:\s+({_USER}))?", re.IGNORECASE)
USAGE = (
    "one of: 'owner/repo' (summary), 'owner/repo issues', 'owner/repo pulls', "
    "'owner/repo commits', 'owner/repo releases', 'repos' (the user's own) or 'repos <user>'."
)


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _Repo(_In):
    full_name: str
    description: str | None = None
    private: bool = False
    fork: bool = False
    archived: bool = False
    language: str | None = None
    stargazers_count: int = 0
    forks_count: int = 0
    open_issues_count: int = 0
    default_branch: str | None = None
    pushed_at: str | None = None


class _Who(_In):
    login: str = "?"


class _Issue(_In):
    number: int
    title: str = ""
    user: _Who | None = None
    pull_request: dict[str, Any] | None = None


class _Person(_In):
    name: str | None = None
    date: str | None = None


class _CommitBody(_In):
    message: str = ""
    author: _Person | None = None


class _Commit(_In):
    sha: str
    commit: _CommitBody


class _Release(_In):
    tag_name: str
    name: str | None = None
    published_at: str | None = None
    prerelease: bool = False


def _day(stamp: str | None) -> str:
    return (stamp or "")[:10] or "unknown date"


def _clip(text: str | None, n: int = 120) -> str:
    one = " ".join((text or "").split())
    return one if len(one) <= n else one[: n - 1].rstrip() + "…"


def _repo_summary(r: _Repo) -> str:
    tags = [
        t for t, on in (("private", r.private), ("fork", r.fork), ("archived", r.archived)) if on
    ]
    head = f"GitHub repository {r.full_name}" + (f" ({', '.join(tags)})" if tags else "") + ":"
    return "\n".join(
        [
            head,
            f"Description: {_clip(r.description, 300) or 'none'}",
            f"Main language: {r.language or 'not detected'}. Stars: {r.stargazers_count}. "
            f"Forks: {r.forks_count}. Open issues and pull requests: {r.open_issues_count}.",
            f"Default branch: {r.default_branch or '?'}. Last push: {_day(r.pushed_at)}.",
        ]
    )


def make_github_tool(fetcher: Fetcher, user: str | None, token: str | None) -> Tool:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async def get(path: str, params: dict[str, str] | None = None) -> Any:
        return await fetcher.get_json(f"{API}{path}", params, headers)

    async def repos(who: str) -> ToolResult:
        found = [
            _Repo.model_validate(x)
            for x in await get(f"/users/{who}/repos", {"sort": "pushed", "per_page": "10"})
        ]
        if not found:
            return ok(f"{who} has no public repositories on GitHub.")
        lines = [f"{who}'s most recently updated public repositories on GitHub:"]
        for r in found:
            about = f": {_clip(r.description, 80)}" if r.description else ""
            language = r.language or "no language"
            lines.append(f"- {r.full_name} ({language}, pushed {_day(r.pushed_at)}){about}")
        return ok("\n".join(lines))

    async def listing(slug: str, what: str) -> ToolResult:
        if what == "issues":
            raw = await get(f"/repos/{slug}/issues", {"state": "open", "per_page": "20"})
            issues = [i for i in map(_Issue.model_validate, raw) if i.pull_request is None][:10]
            if not issues:
                return ok(f"{slug} has no open issues.")
            rows = [
                f"- #{i.number} {_clip(i.title)} (by {(i.user or _Who()).login})" for i in issues
            ]
            return ok("\n".join([f"Open issues in {slug} (newest first):", *rows]))
        if what == "pulls":
            raw = await get(f"/repos/{slug}/pulls", {"state": "open", "per_page": "10"})
            pulls = [_Issue.model_validate(x) for x in raw]
            if not pulls:
                return ok(f"{slug} has no open pull requests.")
            rows = [
                f"- #{p.number} {_clip(p.title)} (by {(p.user or _Who()).login})" for p in pulls
            ]
            return ok("\n".join([f"Open pull requests in {slug} (newest first):", *rows]))
        if what == "commits":
            raw = await get(f"/repos/{slug}/commits", {"per_page": "10"})
            commits = [_Commit.model_validate(x) for x in raw]
            rows = [
                f"- {c.sha[:7]} {_day(c.commit.author.date if c.commit.author else None)} "
                f"{_clip(c.commit.message.splitlines()[0] if c.commit.message else '')}"
                for c in commits
            ]
            return ok("\n".join([f"Latest commits in {slug}:", *rows]))
        raw = await get(f"/repos/{slug}/releases", {"per_page": "5"})
        releases = [_Release.model_validate(x) for x in raw]
        if not releases:
            return ok(f"{slug} has no releases.")
        rows = [
            f"- {r.tag_name} ({_day(r.published_at)})"
            + (f" {_clip(r.name, 80)}" if r.name and r.name != r.tag_name else "")
            + (" [pre-release]" if r.prerelease else "")
            for r in releases
        ]
        return ok("\n".join([f"Latest releases of {slug}:", *rows]))

    async def run(arg: str, now: datetime) -> ToolResult:
        text = " ".join(arg.split())
        try:
            if m := _REPOS.fullmatch(text):
                who = m.group(1) or user
                if not who:
                    return fail(
                        "Whose repositories? No GitHub user is set "
                        "(assistant.tools.online.github_user)."
                    )
                return await repos(who)
            m = _REPO.fullmatch(text)
            if m is None or m.group(2) in (".", ".."):
                return fail(f"I can't read that as a GitHub request. Use {USAGE}")
            slug = f"{m.group(1)}/{m.group(2)}"
            what = (m.group(3) or "").lower().replace("prs", "pulls")
            if not what:
                return ok(_repo_summary(_Repo.model_validate(await get(f"/repos/{slug}"))))
            return await listing(slug, what)
        except NetError as exc:
            if exc.status == 404:
                return fail(f"GitHub has nothing public at '{text}' (wrong name, or private).")
            return fail(f"Couldn't reach GitHub: {exc}.")
        except (ValidationError, TypeError):
            return fail("Couldn't read GitHub's answer.")

    return Tool(
        name="github",
        description=(
            "Read-only facts about GitHub repositories: summary, open issues, pull requests, "
            "commits, releases, a user's repositories. Goes online (GitHub)."
        ),
        usage=USAGE,
        risk="online",
        run=run,
        outside_content=True,
        timeout_s=ONLINE_TOOL_TIMEOUT_S,
    )
