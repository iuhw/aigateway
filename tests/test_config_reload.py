"""Config hot-reload tests (previously untested logic)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import yaml

from aigateway.app import GatewayApp
from tests.conftest import base_config, write_config


def rewrite_config(path: str, config: dict) -> None:
    """Rewrite the config and wait until its mtime actually changes — Windows
    timestamps have coarse granularity, so compare nanoseconds and fail loudly
    if the file timestamp never advances (would make the reload test moot)."""
    path_obj = Path(path)
    old = path_obj.stat().st_mtime_ns
    path_obj.write_text(yaml.safe_dump(config), encoding="utf-8")
    for _ in range(300):
        if path_obj.stat().st_mtime_ns != old:
            return
        time.sleep(0.01)
    raise AssertionError("config mtime did not change within 3s")


async def test_reload_picks_up_new_plugin(tmp_path):
    path = write_config(tmp_path, base_config())
    app = GatewayApp(path)
    assert app.plugin_info() == []

    rewrite_config(
        path,
        base_config(plugins={"keyword-filter-plugin": {"config": {"words": ["bad"], "mode": "block"}}}),
    )
    changed = await app.reload_if_changed()

    assert changed is True
    assert [p["name"] for p in app.plugin_info()] == ["keyword-filter-plugin"]
    await app.aclose()


def rewrite_raw(path: str, text: str) -> None:
    """Rewrite raw content, waiting for the mtime to actually change."""
    path_obj = Path(path)
    old = path_obj.stat().st_mtime_ns
    path_obj.write_text(text, encoding="utf-8")
    for _ in range(300):
        if path_obj.stat().st_mtime_ns != old:
            return
        time.sleep(0.01)
    raise AssertionError("config mtime did not change within 3s")


async def test_reload_with_broken_yaml_keeps_previous_state(tmp_path):
    path = write_config(tmp_path, base_config())
    app = GatewayApp(path)
    before = app.model_names()

    rewrite_raw(path, "providers: [this is: broken: yaml")
    changed = await app.reload_if_changed()

    assert changed is False  # reload failed, old state kept
    assert app.model_names() == before
    await app.aclose()


async def test_reload_with_invalid_config_keeps_previous_state(tmp_path):
    path = write_config(tmp_path, base_config())
    app = GatewayApp(path)

    # syntactically valid but semantically invalid (model without deployments)
    rewrite_config(path, base_config(models={"m1": {"deployments": []}}))
    assert await app.reload_if_changed() is False
    assert app.model_names() == ["m1", "m2"]  # old routes still live
    await app.aclose()


async def test_no_change_means_no_reload(tmp_path):
    path = write_config(tmp_path, base_config())
    app = GatewayApp(path)
    assert await app.reload_if_changed() is False
    assert await app.reload_if_changed() is False
    await app.aclose()


async def test_provider_section_change_rebuilds_providers(tmp_path):
    path = write_config(tmp_path, base_config())
    app = GatewayApp(path)
    old_provider = app.state.providers["p1"]
    closed = {"flag": False}
    orig_close = old_provider.aclose

    async def spy_close() -> None:
        closed["flag"] = True
        await orig_close()

    old_provider.aclose = spy_close

    rewrite_config(
        path,
        base_config(providers={"p1": {"type": "mock", "latency_s": 0.005}, "p2": {"type": "mock"}}),
    )
    assert await app.reload_if_changed() is True
    assert closed["flag"] is True  # old provider was closed
    assert app.state.providers["p1"] is not old_provider
    assert len(app.state.providers) == 2
    await app.aclose()


async def test_watcher_background_task_reloads(tmp_path):
    path = write_config(tmp_path, base_config())
    app = GatewayApp(path)
    await app.start_config_watcher(interval_s=0.05)
    try:
        rewrite_config(
            path,
            base_config(plugins={"response-cache-plugin": {"config": {"ttl_s": 60}}}),
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if app.plugin_info():
                break
            await asyncio.sleep(0.05)
        assert [p["name"] for p in app.plugin_info()] == ["response-cache-plugin"]
    finally:
        await app.aclose()
    assert app._watch_task is None  # cancelled on close


def test_unknown_backend_fails_safe(tmp_path):
    """A bad shared_state section must not take the gateway down: it logs and
    continues without shared state (single-process semantics)."""

    bad = base_config()
    bad["shared_state"] = {"backend": "memcached"}
    path = write_config(tmp_path, bad)
    app = GatewayApp(path)
    assert app._shared_backend is None
    assert app.model_names() == ["m1", "m2"]
