"""Graceful shutdown tests: every owned resource is released exactly once."""

from __future__ import annotations

from typing import Any

import pytest

from aigateway.app import GatewayApp
from tests.conftest import base_config, write_config


class SpyProvider:
    """Records aclose calls so shutdown can be asserted."""

    def __init__(self, name: str):
        self.name = name
        self.closed = False
        self.close_count = 0

    async def chat(self, **kwargs: Any):  # pragma: no cover - unused here
        raise NotImplementedError

    def stream(self, **kwargs: Any):  # pragma: no cover - unused here
        raise NotImplementedError

    async def health(self) -> bool:
        return True

    async def aclose(self) -> None:
        self.closed = True
        self.close_count += 1


def build_app_with_spies(tmp_path) -> tuple[GatewayApp, dict[str, SpyProvider]]:
    path = write_config(tmp_path, base_config())
    app = GatewayApp(path)
    spies = {name: SpyProvider(name) for name in ("p1", "p2")}
    app.state.providers = dict(spies)  # type: ignore[assignment]
    return app, spies


async def test_aclose_closes_providers_and_backend(tmp_path):
    app, spies = build_app_with_spies(tmp_path)
    await app.aclose()
    assert all(s.closed for s in spies.values())
    assert all(s.close_count == 1 for s in spies.values()), "exactly-once close"
    assert app._shared_backend is None


async def test_aclose_cancels_background_tasks(tmp_path):
    path = write_config(tmp_path, base_config())
    app = GatewayApp(path)
    await app.start_config_watcher(interval_s=0.05)
    watcher, syncer = app._watch_task, app._sync_task
    assert watcher is not None
    await app.aclose()
    for task in (watcher, syncer):
        if task is not None:
            await asyncio_sleep_0()
            assert task.cancelled() or task.done(), "background task must stop"


async def asyncio_sleep_0() -> None:
    import asyncio

    await asyncio.sleep(0)


async def test_lifespan_startup_and_shutdown(tmp_path):
    """FastAPI lifespan starts the watcher and stops everything on exit."""
    from aigateway.api import create_app

    app = create_app(write_config(tmp_path, base_config()), config_watcher=True)
    gateway = app.state.gateway
    async with app.router.lifespan_context(app):
        assert gateway._watch_task is not None
    assert gateway._watch_task is None
    assert gateway._shared_backend is None


async def test_chat_rejected_after_close(tmp_path):
    app, _spies = build_app_with_spies(tmp_path)
    await app.aclose()
    with pytest.raises(Exception):
        await app.chat(
            {"model": "m1", "messages": [{"role": "user", "content": "hi"}]}, {}, "t"
        )
