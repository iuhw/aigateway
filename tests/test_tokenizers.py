"""Tokenizers: unicode heuristic calibration, tiktoken option, app wiring."""

from __future__ import annotations

import pytest

from aigateway.tokenizers import (
    UnicodeHeuristicEstimator,
    build_tokenizer,
)


def test_empty_and_short_strings():
    est = UnicodeHeuristicEstimator()
    assert est.estimate("") == 0
    assert est.estimate("a") == 1
    assert est.estimate("hi") == 1


def test_cjk_much_higher_than_flat_4char():
    """The whole point: Chinese must not be under-counted ~3.6x like the old
    flat ~4 chars/token rule."""
    est = UnicodeHeuristicEstimator()
    text = "今天天气怎么样？我想去公园散步，顺便买杯咖啡。"  # 23 chars, ~21 real tokens
    estimate = est.estimate(text)
    assert 12 <= estimate <= 32  # old estimate would have been 6
    assert estimate >= len(text) // 3


def test_english_words():
    est = UnicodeHeuristicEstimator()
    # "The quick brown fox jumps over the lazy dog." -> ~10 real tokens
    assert 7 <= est.estimate("The quick brown fox jumps over the lazy dog.") <= 15


def test_digit_runs_and_punctuation():
    est = UnicodeHeuristicEstimator()
    assert est.estimate("123456789") == 3  # 3 digits per token
    assert est.estimate("!!!") == 2  # 0.4 each -> ceil(1.2)


def test_mixed_content():
    est = UnicodeHeuristicEstimator()
    # CJK 2*0.7 + word(5) 1 + digits 2 + punct 3*0.4 = 5.6 -> 6
    assert est.estimate("混合 mixed 12345 !!!") == 6


def test_monotonic_in_length():
    est = UnicodeHeuristicEstimator()
    a = est.estimate("hello world this is a sentence")
    b = est.estimate("hello world this is a sentence and here is even more text to count")
    assert b > a


def test_build_tokenizer_default_and_unknown():
    assert isinstance(build_tokenizer(None), UnicodeHeuristicEstimator)
    assert isinstance(build_tokenizer({"tokenizer": "unicode"}), UnicodeHeuristicEstimator)
    assert isinstance(build_tokenizer({"tokenizer": "nonsense"}), UnicodeHeuristicEstimator)


def test_build_tokenizer_tiktoken_or_fallback():
    try:
        import tiktoken  # noqa: F401
    except ImportError:
        estimator = build_tokenizer({"tokenizer": "tiktoken"})
        assert isinstance(estimator, UnicodeHeuristicEstimator)  # offline fallback
        return
    estimator = build_tokenizer({"tokenizer": "tiktoken"})
    # Either the real estimator (BPE files cached) or the documented fallback.
    from aigateway.tokenizers import TiktokenEstimator

    if isinstance(estimator, TiktokenEstimator):
        assert estimator.estimate("hello world") == 2


@pytest.mark.skipif(
    not pytest.importorskip("tiktoken", reason="tiktoken not installed"),
    reason="calibration check needs tiktoken",
)
def test_unicode_heuristic_aggregate_error_bounded():
    """Aggregate error vs o200k_base on a mixed corpus must stay bounded."""
    try:
        import tiktoken
    except Exception:  # pragma: no cover - no network for BPE files
        pytest.skip("tiktoken BPE files unavailable")
    enc = tiktoken.get_encoding("o200k_base")
    est = UnicodeHeuristicEstimator()
    corpus = [
        "你好世界",
        "今天天气怎么样？我想去公园散步，顺便买杯咖啡。",
        "用户请求网关转发到上游模型，网关负责认证、限流和成本核算。",
        "请帮我总结这篇文章的主要观点，并用三点说明其论据。",
        "The quick brown fox jumps over the lazy dog.",
        "Hello world, this is a test of the emergency broadcast system 12345.",
        "a very much longer english sentence with many common words in it indeed",
        "混合文本 mixed content 你说 what do you think? 数字 12345 和 and symbols !!!",
        "### Role\nYou are a helpful assistant.\n\n### Task\nAnswer concisely.",
    ]
    real = sum(len(enc.encode_ordinary(text)) for text in corpus)
    mine = sum(est.estimate(text) for text in corpus)
    assert abs(mine - real) / real <= 0.25


async def test_app_injects_estimator_into_plugins(tmp_path):
    from aigateway.app import GatewayApp
    from tests.conftest import base_config, write_config

    config = base_config(plugins={"rate-limit-plugin": {"config": {"default_rpm": 10}}})
    app = GatewayApp(write_config(tmp_path, config))
    try:
        rate_limit = next(
            p for p in app.state.plugins.plugins if type(p).__name__ == "RateLimitPlugin"
        )
        assert rate_limit.estimator is app.tokenizer
    finally:
        await app.aclose()


async def test_hot_reload_picks_up_tokenizer_change(tmp_path):
    import yaml

    from aigateway.app import GatewayApp
    from tests.conftest import base_config

    config = base_config()
    path = tmp_path / "gateway.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    app = GatewayApp(str(path))
    try:
        assert type(app.tokenizer).__name__ == "UnicodeHeuristicEstimator"
        config["server"] = {"tokenizer": "tiktoken"}
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        changed = await app.reload_if_changed()
        assert changed
        # tiktoken missing or present: reload must not crash; type follows config
        assert type(app.tokenizer).__name__ in ("TiktokenEstimator", "UnicodeHeuristicEstimator")
    finally:
        await app.aclose()
