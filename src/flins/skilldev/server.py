"""`flins skilldev`: the MCP server Hermes uses to build skills (SPEC §10). stdio only.

The tool list is the whole interface. There is deliberately no tool to enable a skill, change
policy, read messages/contacts/data or send anything.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from flins.skilldev.workspace import Workspace, WorkspaceError

TOOLS = (
    "get_skill_spec",
    "list_capabilities",
    "list_skills",
    "read_skill",
    "scaffold_skill",
    "write_skill_file",
    "delete_skill_file",
    "validate_skill",
    "test_skill",
    "submit_skill",
    "list_requests",
    "read_request",
)

INSTRUCTIONS = """You build skills for FLINS, a personal WhatsApp assistant. Start with
get_skill_spec. Work only through these tools, in skills/_incoming/<name>/. Use made-up test
data, never real names or numbers. When validate_skill and test_skill pass, call submit_skill.
Only the user can enable a skill; you can't, and you shouldn't ask them to bypass review."""


def build_server(ws: Workspace) -> MCPServer:
    server = MCPServer("flins-skilldev", instructions=INSTRUCTIONS)

    @server.tool()
    def get_skill_spec() -> str:
        """How FLINS skills work: the contract, the rules the validator enforces, the
        flins.skill.yaml schema and an example skill. Read this first."""
        return ws.spec()

    @server.tool()
    def list_capabilities() -> dict[str, Any]:
        """Permissions a skill can declare and the ctx.* calls each one unlocks."""
        return ws.capabilities()

    @server.tool()
    def list_skills() -> dict[str, Any]:
        """Installed skills (read-only examples) and your skills in _incoming, with review
        status and any note from the user."""
        return ws.list_skills()

    @server.tool()
    def read_skill(name: str, where: str = "incoming") -> dict[str, str]:
        """Files of a skill. where = 'incoming' (yours) or 'installed' (examples)."""
        return _call(ws.read_skill, name, where)

    @server.tool()
    def scaffold_skill(name: str, description: str) -> list[str]:
        """Create skills/_incoming/<name>/ with a manifest, SKILL.md, handler.py and a
        fixture to start from."""
        return _call(ws.scaffold, name, description)

    @server.tool()
    def write_skill_file(name: str, path: str, content: str) -> str:
        """Create or replace one file inside skills/_incoming/<name>/, e.g. 'handler.py' or
        'fixtures/rainy_day.json'. Any edit withdraws a pending submission."""
        return _call(ws.write_file, name, path, content)

    @server.tool()
    def delete_skill_file(name: str, path: str) -> str:
        """Delete one file inside skills/_incoming/<name>/."""
        return _call(ws.delete_file, name, path)

    @server.tool()
    async def validate_skill(name: str) -> dict[str, Any]:
        """Static checks: manifest, SKILL.md, imports, banned calls, lint, fixtures present."""
        result = await _acall(ws.check(name, with_eval=False))
        data = result.as_dict()
        data.pop("fixtures", None)
        data.pop("eval", None)
        return data

    @server.tool()
    async def test_skill(name: str) -> dict[str, Any]:
        """Validate, run every fixture in the sandbox, then the 162-case safety eval with the
        skill switched on. Submitting needs all of it to pass with 0 unsafe."""
        result = await _acall(ws.check(name, with_eval=True))
        return result.as_dict()

    @server.tool()
    async def submit_skill(
        name: str, summary: str, request_id: str | None = None
    ) -> dict[str, Any]:
        """Ask the user to review the skill. Re-runs all checks first. summary: what it does
        and why, in a few plain sentences. request_id: if it answers a request."""
        return await _acall(ws.submit(name, summary, request_id))

    @server.tool()
    def list_requests() -> list[dict[str, str]]:
        """Skills the user asked for ("make a skill that ...")."""
        return ws.requests()

    @server.tool()
    def read_request(request_id: str) -> str:
        """The full text of one request."""
        return _call(ws.read_request, request_id)

    return server


def _call[**P, T](fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Refusals reach Hermes as a plain error message."""
    try:
        return fn(*args, **kwargs)
    except WorkspaceError as exc:
        raise ToolError(f"refused: {exc}") from None


async def _acall[T](coro: Awaitable[T]) -> T:
    try:
        return await coro
    except WorkspaceError as exc:
        raise ToolError(f"refused: {exc}") from None


async def serve_stdio(root: Path, incoming: Path | None = None) -> None:
    await build_server(Workspace.for_root(root, incoming)).run_stdio_async()
