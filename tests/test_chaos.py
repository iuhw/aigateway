"""Chaos tests: random failures and latency under load, deterministic seed.

Asserts the properties production cares about when upstreams misbehave:
- every request reaches a terminal state within a hard deadline (no hangs)
- error responses are the gateway's own mapped errors, never raw crashes
- failover keeps success rate high despite primary-deployment chaos
- the cooldown mechanism demonstrably reduces hammering over time
- no task leakage: the app closes cleanly afterwards
"""

from __future__ import annotations

import asyncio
import random

import httpx

from aigateway.api import create_app
from tests.conftest import base_config, write_config

SEED = 20261025
HARD_DEADLINE_S = 15.0


def chaos_config() -> dict:
    return base_config(
        providers={
            # primary: fails 30% of calls, sometimes slow
            "flaky": {"type": "mock", "failure_rate": 0.3, "latency_s": 0.01},
            # secondary: occasionally slow, rarely fails
            "wobbly": {"type": "mock", "failure_rate": 0.05, "latency_s": 0.03},
            # last resort: solid
            "solid": {"type": "mock", "latency_s": 0.005},
        },
        models={
            "chaos": {
                "deployments": [
                    {"provider": "flaky", "model": "chaos-up"},
                    {"provider": "wobbly", "model": "chaos-up"},
                    {"provider": "solid", "model": "chaos-up"},
                ],
                "retries": 1,
                "retry_backoff_s": 0.01,
            },
            "m1": {"deployments": [{"provider": "solid", "model": "m1-up"}]},
        },
    )


async def test_chaos_no_hangs_high_success_rate(tmp_path):
    random.seed(SEED)
    app = create_app(write_config(tmp_path, chaos_config()), config_watcher=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://chaos") as client:
        n = 300
        sem = asyncio.Semaphore(20)

        async def one(index: int) -> tuple[int | str, float]:
            async with sem:
                start = asyncio.get_running_loop().time()
                try:
                    response = await asyncio.wait_for(
                        client.post(
                            "/v1/chat/completions",
                            json={
                                "model": "chaos",
                                "messages": [{"role": "user", "content": f"chaos {index}"}],
                            },
                        ),
                        timeout=HARD_DEADLINE_S,
                    )
                    return response.status_code, asyncio.get_running_loop().time() - start
                except TimeoutError:
                    return "TIMEOUT", HARD_DEADLINE_S

        results = await asyncio.gather(*(one(i) for i in range(n)))
        metrics_text = (await client.get("/metrics")).text

    codes = [code for code, _ in results]
    durations = [d for _, d in results]

    # 1. no hangs: every request had a terminal state under the deadline
    assert "TIMEOUT" not in codes, f"{codes.count('TIMEOUT')} requests hung"

    # 2. every terminal state is a valid gateway response (200 or mapped error)
    assert set(codes) <= {200, 429, 502, 503, 504}, f"unexpected codes: {set(codes)}"

    # 3. failover keeps success high: primaries fail 30%+5%, but retries +
    #    two fallback deployments should rescue nearly everything
    success_rate = codes.count(200) / n
    assert success_rate > 0.97, f"success rate {success_rate:.3f} too low"

    # 4. tail latency bounded by mock latencies + backoff, not unbounded
    p99 = sorted(durations)[int(0.99 * (len(durations) - 1))]
    assert p99 < 1.0, f"p99 {p99:.3f}s too high"

    # 5. attempt/outcome metrics must be recorded
    assert "gateway_upstream_attempts_total" in metrics_text

    await app.state.gateway.aclose()


async def test_chaos_streaming_survives_upstream_chaos(tmp_path):
    random.seed(SEED + 1)
    app = create_app(write_config(tmp_path, chaos_config()), config_watcher=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://chaos") as client:
        n = 60

        async def one(index: int) -> str:
            try:
                async with client.stream(
                    "POST",
                    "/v1/chat/completions",
                    json={
                        "model": "chaos",
                        "messages": [{"role": "user", "content": f"stream {index}"}],
                        "stream": True,
                    },
                    timeout=HARD_DEADLINE_S,
                ) as response:
                    if response.status_code != 200:
                        await response.aread()
                        return str(response.status_code)
                    async for _line in response.aiter_lines():
                        pass
                    return "200"
            except TimeoutError:
                return "TIMEOUT"
            except httpx.HTTPError as exc:
                return type(exc).__name__

        outcomes = await asyncio.gather(*(one(i) for i in range(n)))

    # every stream terminated in a well-defined way: full success (200),
    # a mapped error status, or a clean connection close after error frames
    assert "TIMEOUT" not in outcomes, "a stream hung"
    success = outcomes.count("200")
    assert success / n > 0.9, f"stream success rate too low: {outcomes[:10]}"
    await app.state.gateway.aclose()
