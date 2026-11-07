from __future__ import annotations

import logging

import pytest

from flins.core.events import Event, EventBus


async def test_topic_and_wildcard_delivery() -> None:
    bus = EventBus()
    seen: list[str] = []

    async def on_mode(e: Event) -> None:
        seen.append(f"mode:{e.data['to']}")

    async def on_all(e: Event) -> None:
        seen.append(f"all:{e.topic}")

    bus.subscribe("mode.changed", on_mode)
    bus.subscribe("*", on_all)
    await bus.publish("mode.changed", {"to": "WORKING"})
    await bus.publish("other.topic")
    assert seen == ["mode:WORKING", "all:mode.changed", "all:other.topic"]


async def test_failing_handler_does_not_block_others(caplog: pytest.LogCaptureFixture) -> None:
    bus = EventBus()
    seen: list[str] = []

    async def broken(e: Event) -> None:
        raise RuntimeError("boom")

    async def fine(e: Event) -> None:
        seen.append(e.topic)

    bus.subscribe("x", broken)
    bus.subscribe("x", fine)
    with caplog.at_level(logging.ERROR):
        await bus.publish("x", {"secret_text": "do not log me"})
    assert seen == ["x"]
    assert "do not log me" not in caplog.text


async def test_unsubscribe() -> None:
    bus = EventBus()
    seen: list[str] = []

    async def h(e: Event) -> None:
        seen.append(e.topic)

    unsubscribe = bus.subscribe("x", h)
    unsubscribe()
    await bus.publish("x")
    assert seen == []
