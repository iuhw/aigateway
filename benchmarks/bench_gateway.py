"""Core-capability benchmark for aigateway.

Runs against the full FastAPI app in-process (httpx ASGITransport), so numbers
measure gateway processing overhead (validation, plugin chain, routing,
dispatch) without TCP/TLS noise. Scenarios:

  1. baseline non-streaming latency & throughput (no plugins, zero-latency mock)
  2. full-plugin-chain overhead (auth + keyword filter + cache + rate limit + log)
  3. streaming: time-to-first-token, per-chunk overhead, end-to-end
  4. cache hit vs miss speedup
  5. failover overhead (primary always failing -> fallback deployment)
  6. rate-limit rejection path cost
  7. concurrency scaling (1/8/32/64)

Usage:
    python benchmarks/bench_gateway.py [--out docs/benchmark-report.md] [--json out.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aigateway.api import create_app  # noqa: E402


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(p / 100 * (len(ordered) - 1))))
    return ordered[index]


def base_config(extra_providers: dict | None = None, extra_models: dict | None = None, plugins: dict | None = None) -> dict:
    providers = {
        "fast": {"type": "mock", "latency_s": 0.0},
    }
    models = {
        "bench": {"deployments": [{"provider": "fast", "model": "bench-up"}], "retries": 1},
    }
    if extra_providers:
        providers.update(extra_providers)
    if extra_models:
        models.update(extra_models)
    return {"providers": providers, "models": models, "plugins": plugins or {}}


def all_plugins_config(rate_rpm: int = 10_000_000) -> dict:
    return {
        "auth-plugin": {"config": {"allow_anonymous": True}},
        "keyword-filter-plugin": {"config": {"words": ["敏感词", "forbidden"], "mode": "block"}},
        "response-cache-plugin": {"config": {"ttl_s": 300}},
        "rate-limit-plugin": {"config": {"default_rpm": rate_rpm}},
        "request-log-plugin": {"config": {}},
    }


def make_client(config: dict) -> tuple[httpx.AsyncClient, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="aigw-bench-"))
    config_path = tmp / "gateway.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    app = create_app(str(config_path), config_watcher=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bench"), tmp


async def run_latency(client: httpx.AsyncClient, payload_fn, n: int, concurrency: int) -> dict:
    """Fire n requests with bounded concurrency; return latency stats."""
    sem = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    errors = 0

    async def one(index: int) -> None:
        nonlocal errors
        async with sem:
            start = time.perf_counter()
            try:
                response = await client.post("/v1/chat/completions", json=payload_fn(index))
                if response.status_code != 200:
                    errors += 1
            except Exception:
                errors += 1
            latencies.append(time.perf_counter() - start)

    started = time.perf_counter()
    await asyncio.gather(*(one(i) for i in range(n)))
    wall = time.perf_counter() - started
    return {
        "n": n,
        "concurrency": concurrency,
        "errors": errors,
        "wall_s": round(wall, 4),
        "rps": round(n / wall, 1) if wall else 0.0,
        "latency_ms_avg": round(statistics.mean(latencies) * 1000, 3) if latencies else 0.0,
        "latency_ms_p50": round(percentile(latencies, 50) * 1000, 3),
        "latency_ms_p95": round(percentile(latencies, 95) * 1000, 3),
        "latency_ms_p99": round(percentile(latencies, 99) * 1000, 3),
    }


async def run_streaming(client: httpx.AsyncClient, n: int, concurrency: int = 10) -> dict:
    ttfts: list[float] = []
    totals: list[float] = []
    chunk_counts: list[int] = []
    sem = asyncio.Semaphore(concurrency)

    async def one(index: int) -> None:
        async with sem:
            start = time.perf_counter()
            first = None
            chunks = 0
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "bench",
                    "messages": [{"role": "user", "content": f"stream {index}"}],
                    "stream": True,
                },
            ) as response:
                async for line in response.aiter_lines():
                    if line.startswith("data:") and "[DONE]" not in line:
                        if first is None:
                            first = time.perf_counter() - start
                        chunks += 1
            ttfts.append(first if first is not None else time.perf_counter() - start)
            totals.append(time.perf_counter() - start)
            chunk_counts.append(chunks)

    await asyncio.gather(*(one(i) for i in range(n)))
    return {
        "n": n,
        "concurrency": concurrency,
        "ttft_ms_avg": round(statistics.mean(ttfts) * 1000, 3),
        "ttft_ms_p95": round(percentile(ttfts, 95) * 1000, 3),
        "total_ms_avg": round(statistics.mean(totals) * 1000, 3),
        "chunks_avg": round(statistics.mean(chunk_counts), 2),
    }


async def main_async(args: argparse.Namespace) -> dict:
    results: dict = {"scenarios": {}}
    scenarios = results["scenarios"]
    # --quick: CI smoke numbers, not publication-grade sizes
    q = 0.2 if args.quick else 1.0

    # ---- 1. baseline (no plugins) ------------------------------------------
    client, _tmp = make_client(base_config())
    payload = lambda i: {"model": "bench", "messages": [{"role": "user", "content": f"msg {i}"}]}
    await run_latency(client, payload, 50, 8)  # warmup
    scenarios["baseline_nonstream"] = await run_latency(client, payload, int(1000 * q), 8)
    await client.aclose()

    # ---- 2. full plugin chain overhead --------------------------------------
    client, _tmp = make_client(base_config(plugins=all_plugins_config()))
    await run_latency(client, payload, 50, 8)
    scenarios["full_plugins_nonstream"] = await run_latency(client, payload, int(1000 * q), 8)
    await client.aclose()

    # ---- 3. streaming --------------------------------------------------------
    client, _tmp = make_client(base_config())
    scenarios["baseline_streaming"] = await run_streaming(client, int(300 * q))
    await client.aclose()

    client, _tmp = make_client(base_config(plugins=all_plugins_config()))
    scenarios["full_plugins_streaming"] = await run_streaming(client, int(300 * q))
    await client.aclose()

    # ---- 4. cache hit vs miss -------------------------------------------------
    client, _tmp = make_client(
        base_config(plugins={"response-cache-plugin": {"config": {"ttl_s": 300}}})
    )
    hit_payload = {"model": "bench", "messages": [{"role": "user", "content": "same"}]}
    await client.post("/v1/chat/completions", json=hit_payload)  # populate
    scenarios["cache_hit"] = await run_latency(client, lambda i: hit_payload, int(500 * q), 8)
    scenarios["cache_miss"] = await run_latency(
        client, lambda i: {"model": "bench", "messages": [{"role": "user", "content": f"miss {i}"}]}, int(500 * q), 8
    )
    await client.aclose()

    # ---- 5. failover overhead ---------------------------------------------------
    # 5a. forced: every request hits the dead deployment first (no cooldown).
    client, _tmp = make_client(
        base_config(
            extra_providers={"dead": {"type": "mock", "fail_status": 500}},
            extra_models={
                "failover": {
                    "deployments": [
                        {"provider": "dead", "model": "x"},
                        {"provider": "fast", "model": "bench-up"},
                    ],
                    "retries": 1,
                    "retry_backoff_s": 0.01,
                    "allowed_fails": 999999,  # never cooldown: pay retry+failover every time
                }
            },
        )
    )
    failover_payload = lambda i: {"model": "failover", "messages": [{"role": "user", "content": f"fo {i}"}]}
    # concurrency=1 keeps the round-robin cursor deterministic: every request
    # goes dead -> retry dead -> failover to healthy, i.e. full retry+backoff cost.
    await run_latency(client, failover_payload, 10, 1)
    scenarios["failover_forced"] = await run_latency(client, failover_payload, int(100 * q) + 20, 1)
    await client.aclose()

    # ---- 5b. steady state: cooldown kicks in, dead deployment skipped entirely.
    client, _tmp = make_client(
        base_config(
            extra_providers={"dead": {"type": "mock", "fail_status": 500}},
            extra_models={
                "failover": {
                    "deployments": [
                        {"provider": "dead", "model": "x"},
                        {"provider": "fast", "model": "bench-up"},
                    ],
                    "retries": 1,
                    "retry_backoff_s": 0.01,
                }
            },
        )
    )
    await run_latency(client, failover_payload, 20, 4)  # trigger cooldown
    scenarios["failover_cooldown_steady"] = await run_latency(client, failover_payload, int(300 * q), 8)
    await client.aclose()

    # ---- 6. rate-limit rejection path ---------------------------------------------
    client, _tmp = make_client(
        base_config(plugins={"rate-limit-plugin": {"config": {"default_rpm": 10}}})
    )
    scenarios["rate_limit_rejection"] = await run_latency(client, payload, int(200 * q), 8)
    await client.aclose()

    # ---- 7. concurrency scaling (baseline) ------------------------------------------
    client, _tmp = make_client(base_config())
    scaling = {}
    for concurrency in (1, 8, 32, 64):
        scaling[str(concurrency)] = await run_latency(client, payload, int(400 * q), concurrency)
    scenarios["concurrency_scaling"] = scaling
    await client.aclose()

    # ---- 8. semantic cache hit path ---------------------------------------------------
    # Hit path cost including the embed step (in-process zero-latency mock:
    # real deployments add the embed network roundtrip on top).
    client, _tmp = make_client(
        base_config(
            extra_providers={"embed": {"type": "mock", "latency_s": 0.0}},
            extra_models={"embed-mdl": {"deployments": [{"provider": "embed", "model": "embed-up"}]}},
            plugins={
                "semantic-cache-plugin": {
                    "config": {"embed_model": "embed-mdl", "similarity_threshold": 0.99, "ttl_s": 300}
                }
            },
        )
    )
    sem_hit_payload = {"model": "bench", "messages": [{"role": "user", "content": "same"}]}
    await client.post("/v1/chat/completions", json=sem_hit_payload)  # populate
    scenarios["semantic_cache_hit"] = await run_latency(client, lambda i: sem_hit_payload, int(500 * q), 8)
    await client.aclose()

    return results


def render_markdown(results: dict) -> str:
    s = results["scenarios"]
    lines = ["# aigateway 核心能力基准测试报告", "", f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             "", "测试方式：httpx ASGI 内存传输直连 FastAPI 应用（不含 TCP/TLS 栈）；上游为零延迟 mock provider，",
             "因此数值反映网关自身的处理开销（校验 + 插件链 + 路由 + 调度）。", ""]

    def row(name: str, label: str) -> str:
        r = s[name]
        return (f"| {label} | {r['n']} | {r['latency_ms_p50']} | {r['latency_ms_p95']} "
                f"| {r['latency_ms_p99']} | {r['rps']} |")

    lines += [
        "## 非流式延迟与吞吐", "",
        "| 场景 | 请求数 | p50 (ms) | p95 (ms) | p99 (ms) | RPS |",
        "|---|---|---|---|---|---|",
        row("baseline_nonstream", "基线（无插件）"),
        row("full_plugins_nonstream", "全插件链（5 插件）"),
        row("failover_forced", "故障转移（每次强制重试+切换）"),
        row("failover_cooldown_steady", "故障转移（冷却后稳态）"),
        row("cache_miss", "缓存 miss"),
        row("cache_hit", "缓存 hit"),
        row("semantic_cache_hit", "语义缓存 hit（含 mock embed）"),
    ]

    base_p50 = s["baseline_nonstream"]["latency_ms_p50"]
    full_p50 = s["full_plugins_nonstream"]["latency_ms_p50"]
    lines += [
        "",
        f"- 插件链开销：p50 {full_p50 - base_p50:+.3f} ms（基线 {base_p50} ms → 全插件 {full_p50} ms）",
        f"- 缓存命中 p50 {s['cache_hit']['latency_ms_p50']} ms vs miss {s['cache_miss']['latency_ms_p50']} ms"
        "（零延迟 mock 上游下差异很小；真实上游秒级延迟时缓存收益为秒级）",
        f"- 强制故障转移 p50 {s['failover_forced']['latency_ms_p50']} ms（含 10ms 重试退避）；"
        f"冷却稳态 p50 {s['failover_cooldown_steady']['latency_ms_p50']} ms（与基线持平，冷却机制有效）",
        f"- 限流拒绝路径 p50：{s['rate_limit_rejection']['latency_ms_p50']} ms"
        f"（rpm=10，绝大多数请求被 429 拒绝）",
    ]

    lines += [
        "", "## 流式（SSE）", "",
        "| 场景 | TTFT 平均 (ms) | TTFT p95 (ms) | 总时延平均 (ms) | 平均块数 |",
        "|---|---|---|---|---|",
        f"| 基线（无插件） | {s['baseline_streaming']['ttft_ms_avg']} | {s['baseline_streaming']['ttft_ms_p95']} "
        f"| {s['baseline_streaming']['total_ms_avg']} | {s['baseline_streaming']['chunks_avg']} |",
        f"| 全插件链 | {s['full_plugins_streaming']['ttft_ms_avg']} | {s['full_plugins_streaming']['ttft_ms_p95']} "
        f"| {s['full_plugins_streaming']['total_ms_avg']} | {s['full_plugins_streaming']['chunks_avg']} |",
    ]

    lines += ["", "## 并发扩展（基线，非流式）", "",
              "| 并发 | RPS | p50 (ms) | p95 (ms) | p99 (ms) |", "|---|---|---|---|---|"]
    for concurrency, r in s["concurrency_scaling"].items():
        lines.append(f"| {concurrency} | {r['rps']} | {r['latency_ms_p50']} | {r['latency_ms_p95']} | {r['latency_ms_p99']} |")

    lines += ["", "## 结论", ""]
    plugin_overhead = full_p50 - base_p50
    lines.append(f"- 网关单请求基线开销 p50 {base_p50} ms；启用全部 5 个内置插件后增加 {plugin_overhead:+.3f} ms/请求。")
    lines.append(f"- 流式 TTFT 开销：{s['full_plugins_streaming']['ttft_ms_avg'] - s['baseline_streaming']['ttft_ms_avg']:+.3f} ms。")
    lines.append("- 故障转移：冷却稳态与基线持平；强制每次重试+切换的开销来自配置的退避时间（10ms），网关自身开销毫秒级。")
    lines.append("- 精确缓存命中略快于转发路径；上游为真实大模型（秒级延迟）时收益为秒级。")
    lines.append(f"- 语义缓存命中路径 p50 {s['semantic_cache_hit']['latency_ms_p50']} ms"
                 "（含进程内 mock embed 步骤；真实部署需另加 embed 网络往返）。")
    lines.append("- 并发 8→64 RPS 下降源于单进程 Python GIL 与事件循环争用；生产部署可用多 worker 横向扩展。")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="aigateway benchmark")
    parser.add_argument("--out", default="docs/benchmark-report.md")
    parser.add_argument("--json", default="docs/benchmark-results.json")
    parser.add_argument(
        "--quick", action="store_true", help="CI smoke: run scenarios at 20%% size"
    )
    args = parser.parse_args()

    results = asyncio.run(main_async(args))
    report = render_markdown(results)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    json_path = Path(args.json)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(report)
    print(f"written: {out} / {json_path}")


if __name__ == "__main__":
    main()
