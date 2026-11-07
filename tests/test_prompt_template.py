"""M5/S1: prompt-template-plugin — system-prompt injection with variables."""

from __future__ import annotations

import pytest

from aigateway.app import GatewayApp
from tests.conftest import base_config, write_config

PAYLOAD = {"model": "m1", "messages": [{"role": "user", "content": "hi"}]}


def plugin_config(*rules: dict) -> dict:
    return {"prompt-template-plugin": {"config": {"templates": list(rules)}}}


def capture_payloads(app: GatewayApp, provider_name: str = "p1") -> list[dict]:
    """Spy on the mock provider to observe the exact payload dispatched."""
    provider = app.state.providers[provider_name]
    seen: list[dict] = []
    original_chat, original_stream = provider.chat, provider.stream

    async def spy_chat(*, model, payload, timeout):
        seen.append(payload)
        return await original_chat(model=model, payload=payload, timeout=timeout)

    async def spy_stream(*, model, payload, timeout):
        seen.append(payload)
        async for chunk in original_stream(model=model, payload=payload, timeout=timeout):
            yield chunk

    provider.chat = spy_chat
    provider.stream = spy_stream
    return seen


def system_texts(payload: dict) -> list[str]:
    return [
        m.get("content")
        for m in payload.get("messages", [])
        if isinstance(m, dict) and m.get("role") == "system"
    ]


async def test_prepend_inserts_gateway_system_first(tmp_path):
    rule = {"system": "You are the ACME gateway.", "mode": "prepend"}
    app = GatewayApp(write_config(tmp_path, base_config(plugins=plugin_config(rule))))
    try:
        seen = capture_payloads(app)
        await app.chat(
            {
                "model": "m1",
                "messages": [
                    {"role": "system", "content": "be nice"},
                    {"role": "user", "content": "hi"},
                ],
            },
            {},
            "test",
        )
    finally:
        await app.aclose()
    assert system_texts(seen[0]) == ["You are the ACME gateway.", "be nice"]


async def test_append_merges_into_existing_system(tmp_path):
    rule = {"system": "corporate rules", "mode": "append"}
    app = GatewayApp(write_config(tmp_path, base_config(plugins=plugin_config(rule))))
    try:
        seen = capture_payloads(app)
        await app.chat(
            {
                "model": "m1",
                "messages": [
                    {"role": "system", "content": "be nice"},
                    {"role": "user", "content": "hi"},
                ],
            },
            {},
            "test",
        )
    finally:
        await app.aclose()
    assert system_texts(seen[0]) == ["be nice\n\ncorporate rules"]


async def test_append_creates_system_when_missing(tmp_path):
    rule = {"system": "corporate rules", "mode": "append"}
    app = GatewayApp(write_config(tmp_path, base_config(plugins=plugin_config(rule))))
    try:
        seen = capture_payloads(app)
        await app.chat(dict(PAYLOAD), {}, "test")
    finally:
        await app.aclose()
    assert system_texts(seen[0]) == ["corporate rules"]


async def test_replace_removes_client_system_messages(tmp_path):
    rule = {"system": "gateway owns the prompt", "mode": "replace"}
    app = GatewayApp(write_config(tmp_path, base_config(plugins=plugin_config(rule))))
    try:
        seen = capture_payloads(app)
        await app.chat(
            {
                "model": "m1",
                "messages": [
                    {"role": "system", "content": "client"},
                    {"role": "user", "content": "hi"},
                ],
            },
            {},
            "test",
        )
    finally:
        await app.aclose()
    assert system_texts(seen[0]) == ["gateway owns the prompt"]


async def test_model_filter_with_fnmatch(tmp_path):
    config = base_config(
        models={
            "m1": {"deployments": [{"provider": "p1", "model": "m1-up"}]},
            "m2": {"deployments": [{"provider": "p2", "model": "m2-up"}]},
            "glm4:9b": {"deployments": [{"provider": "p1", "model": "glm4:9b"}]},
        },
        plugins=plugin_config({"system": "only glm4", "models": ["glm4:*"]}),
    )
    app = GatewayApp(write_config(tmp_path, config))
    try:
        seen_glm = capture_payloads(app)
        await app.chat({"model": "glm4:9b", "messages": [{"role": "user", "content": "hi"}]}, {}, "test")
        seen_m1 = capture_payloads(app)
        await app.chat(dict(PAYLOAD), {}, "test")
    finally:
        await app.aclose()
    assert system_texts(seen_glm[0]) == ["only glm4"]
    assert system_texts(seen_m1[0]) == []


async def test_identity_key_and_metadata_filters(tmp_path):
    config = base_config(
        plugins={
            "auth-plugin": {
                "config": {
                    "api_keys": [
                        {"key": "sk-a", "name": "team-a", "metadata": {"tier": "premium"}},
                        {"key": "sk-b", "name": "team-b"},
                    ]
                }
            },
            "prompt-template-plugin": {
                "config": {
                    "templates": [
                        {
                            "system": "premium line for {key}",
                            "keys": ["team-a"],
                            "metadata": {"tier": "premium"},
                        }
                    ]
                }
            },
        }
    )
    app = GatewayApp(write_config(tmp_path, config))
    try:
        seen_a = capture_payloads(app)
        await app.chat(dict(PAYLOAD), {"Authorization": "Bearer sk-a"}, "test")
        seen_b = capture_payloads(app)
        await app.chat(dict(PAYLOAD), {"Authorization": "Bearer sk-b"}, "test")
    finally:
        await app.aclose()
    assert system_texts(seen_a[0]) == ["premium line for team-a"]
    assert system_texts(seen_b[0]) == []


async def test_variable_precedence_and_builtins(tmp_path):
    config = base_config(
        plugins={
            "auth-plugin": {
                "config": {
                    "api_keys": [{"key": "sk-a", "name": "team-a", "metadata": {"team": "meta-team"}}]
                }
            },
            "prompt-template-plugin": {
                "config": {
                    "templates": [
                        {
                            "system": "{model}|{key}|{team}|{env}|{region}",
                            "variables": {"team": "static-team", "env": "staging"},
                        }
                    ]
                }
            },
        }
    )
    app = GatewayApp(write_config(tmp_path, config))
    try:
        seen = capture_payloads(app)
        await app.chat(
            dict(PAYLOAD), {"Authorization": "Bearer sk-a", "X-Region": "eu-1"}, "test"
        )
    finally:
        await app.aclose()
    # metadata overrides static; env has no override; header requires allowlist
    assert system_texts(seen[0]) == ["m1|team-a|meta-team|staging|{region}"]


async def test_header_variables_allowlist(tmp_path):
    config = base_config(
        plugins={
            "prompt-template-plugin": {
                "config": {
                    "templates": [
                        {"system": "hello {x_region}", "header_variables": ["X-Region"]}
                    ]
                }
            }
        }
    )
    app = GatewayApp(write_config(tmp_path, config))
    try:
        seen = capture_payloads(app)
        await app.chat(dict(PAYLOAD), {"X-Region": "eu-1", "X-Other": "nope"}, "test")
    finally:
        await app.aclose()
    assert system_texts(seen[0]) == ["hello eu-1"]


async def test_unknown_placeholders_stay_verbatim_and_no_reexpansion(tmp_path):
    rule = {"system": "keep {not_a_var} and {\"json\": true}; hi {name}", "variables": {"name": "{key}"}}
    app = GatewayApp(write_config(tmp_path, base_config(plugins=plugin_config(rule))))
    try:
        seen = capture_payloads(app)
        await app.chat(dict(PAYLOAD), {}, "test")
    finally:
        await app.aclose()
    # substituted value is never re-scanned: "{key}" appears literally
    assert system_texts(seen[0]) == ["keep {not_a_var} and {\"json\": true}; hi {key}"]


async def test_multiple_rules_compose_in_order(tmp_path):
    rules = [
        {"system": "base", "mode": "replace"},
        {"system": "extra", "mode": "append"},
    ]
    app = GatewayApp(write_config(tmp_path, base_config(plugins=plugin_config(*rules))))
    try:
        seen = capture_payloads(app)
        await app.chat(dict(PAYLOAD), {}, "test")
    finally:
        await app.aclose()
    assert system_texts(seen0 := seen[0]) == ["base\n\nextra"]
    assert seen0["messages"][0]["role"] == "system"


async def test_streaming_requests_get_injection(tmp_path):
    rule = {"system": "stream base", "mode": "prepend"}
    app = GatewayApp(write_config(tmp_path, base_config(plugins=plugin_config(rule))))
    try:
        seen = capture_payloads(app)
        gen = await app.chat(dict(PAYLOAD, stream=True), {}, "test")
        assert [c async for c in gen]
    finally:
        await app.aclose()
    assert system_texts(seen[0]) == ["stream base"]


def test_config_validation_rejects_bad_rules(tmp_path):
    with pytest.raises(ValueError, match="needs a 'system' text"):
        GatewayApp(write_config(tmp_path, base_config(plugins=plugin_config({"mode": "replace"}))))
    with pytest.raises(ValueError, match="unknown mode"):
        GatewayApp(
            write_config(
                tmp_path,
                base_config(plugins=plugin_config({"system": "x", "mode": "sideways"})),
            )
        )


async def test_non_list_messages_left_alone(tmp_path):
    rule = {"system": "unused"}
    app = GatewayApp(write_config(tmp_path, base_config(plugins=plugin_config(rule))))
    try:
        from aigateway.plugins.base import PluginContext

        ctx = PluginContext(request_id="t", model="m1", payload={"messages": "garbage"}, stream=False)
        await app.state.plugins.run_request(ctx)
    finally:
        await app.aclose()
    assert ctx.payload["messages"] == "garbage"
