"""Shared fixtures: YAML-config-driven GatewayApp instances with mock providers."""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from aigateway.app import GatewayApp


def write_config(tmp_path, config: dict[str, Any]) -> str:
    path = tmp_path / "gateway.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return str(path)


def base_config(**overrides: Any) -> dict[str, Any]:
    """Two-model config over mock providers, no plugins by default."""
    config: dict[str, Any] = {
        "providers": {
            "p1": {"type": "mock", "latency_s": 0.0},
            "p2": {"type": "mock", "latency_s": 0.0},
        },
        "models": {
            "m1": {"deployments": [{"provider": "p1", "model": "m1-up"}]},
            "m2": {"deployments": [{"provider": "p2", "model": "m2-up"}]},
        },
        "plugins": {},
    }
    config.update(overrides)
    return config


@pytest.fixture
def make_app(tmp_path):
    def _make(config: dict[str, Any] | None = None) -> GatewayApp:
        path = write_config(tmp_path, config or base_config())
        return GatewayApp(path)

    return _make


@pytest.fixture
async def app(make_app) -> GatewayApp:
    application = make_app()
    yield application
    await application.aclose()
