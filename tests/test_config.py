"""Tests for YAML config loading and state building."""

from __future__ import annotations

import pytest

from aigateway.config import GatewayConfig, load_config
from aigateway.core.router import RoutingStrategy
from aigateway.errors import BadRequestError
from tests.conftest import base_config, write_config


def test_load_config_builds_state(tmp_path):
    config = load_config(write_config(tmp_path, base_config()))
    providers = config.build_providers()
    assert set(providers) == {"p1", "p2"}
    router = config.build_router()
    assert router.model_names() == ["m1", "m2"]
    chain = config.build_plugin_chain()
    assert chain.describe() == []


def test_unknown_plugin_rejected(tmp_path):
    config = base_config(plugins={"no-such-plugin": {"config": {}}})
    with pytest.raises(BadRequestError, match="unknown plugin"):
        GatewayConfig(__import__("yaml").safe_load(__import__("yaml").safe_dump(config))).build_plugin_chain()


def test_plugin_disabled_is_skipped(tmp_path):
    config = base_config(plugins={"auth-plugin": {"enabled": False, "config": {}}})
    chain = GatewayConfig(config).build_plugin_chain()
    assert chain.describe() == []


def test_route_policy_defaults_and_strategy(tmp_path):
    config = base_config(
        models={"m": {"deployments": [{"provider": "p1"}], "strategy": "least_latency", "retries": 5}}
    )
    router = GatewayConfig(config).build_router()
    policy = router.route("m").policy
    assert policy.strategy == RoutingStrategy.LEAST_LATENCY
    assert policy.retries == 5


def test_model_without_deployments_rejected():
    config = base_config(models={"m": {"deployments": []}})
    with pytest.raises(BadRequestError, match="no deployments"):
        GatewayConfig(config).build_router()


def test_external_plugin_module_loading(tmp_path):
    module_path = tmp_path / "external_plugin.py"
    module_path.write_text(
        "from aigateway.plugins.base import Plugin\n"
        "class External(Plugin):\n"
        "    async def on_request(self, ctx):\n"
        "        ctx.state['external'] = True\n",
        encoding="utf-8",
    )
    import sys

    sys.path.insert(0, str(tmp_path))
    try:
        config = base_config(plugins={"ext": {"module": "external_plugin", "class": "External"}})
        chain = GatewayConfig(config).build_plugin_chain()
        assert [d["name"] for d in chain.describe()] == ["external"]
    finally:
        sys.path.remove(str(tmp_path))
