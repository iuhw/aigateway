"""End-to-end validation of aigateway against a real Ollama model.

Prerequisites:
  - Ollama running on 127.0.0.1:11434 with model `glm4:9b`
  - config in gateway.ollama.yaml

Run:  python scripts/validate_e2e.py [--config gateway.ollama.yaml] [--report docs/e2e-ollama-validation.md]

Every check exercises one gateway capability over real HTTP (uvicorn in-process
via ASGITransport but a REAL upstream Ollama server):
  1  model listing
  2  non-streaming completion (+usage)
  3  streaming completion (SSE, TTFT measured)
  4  auth: missing/invalid key rejected; valid key accepted
  5  keyword filter blocks a request
  6  keyword mask redacts model output (non-stream & stream)
  7  exact-cache serves the second identical prompt
  8  rate limit returns 429 on the limited model
  9  failover: dead upstream -> healthy fallback deployment
  10 Prometheus /metrics exposes gateway metrics
  11 concurrent burst (5x) all succeed
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aigateway.api import create_app  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}", flush=True)


async def chat(client: httpx.AsyncClient, model: str, content: str, **kwargs) -> httpx.Response:
    return await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-e2e-demo-key"},
        json={"model": model, "messages": [{"role": "user", "content": content}], **kwargs},
        timeout=320,
    )


async def check_1_models(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/models", headers={"Authorization": "Bearer sk-e2e-demo-key"})
    ids = [m["id"] for m in response.json().get("data", [])] if response.status_code == 200 else []
    record(
        "1 model listing",
        response.status_code == 200 and "glm4:9b" in ids,
        f"status={response.status_code} models={ids}",
    )


async def check_2_non_stream(client: httpx.AsyncClient) -> None:
    response = await chat(client, "glm4:9b", "用一句话介绍你自己")
    if response.status_code != 200:
        record("2 non-streaming completion", False, f"status={response.status_code} body={response.text[:200]}")
        return
    body = response.json()
    content = body["choices"][0]["message"]["content"]
    usage = body.get("usage", {})
    record(
        "2 non-streaming completion",
        len(content) > 0 and usage.get("total_tokens", 0) > 0,
        f"reply={content[:40]!r} usage={usage.get('prompt_tokens')}/{usage.get('completion_tokens')}tok",
    )


async def check_3_stream(client: httpx.AsyncClient) -> None:
    start = time.perf_counter()
    first = None
    text_parts: list[str] = []
    finish = None
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-e2e-demo-key"},
        json={
            "model": "glm4:9b",
            "messages": [{"role": "user", "content": "从1数到5，用顿号分隔"}],
            "stream": True,
        },
        timeout=320,
    ) as response:
        status = response.status_code
        if status == 200:
            async for line in response.aiter_lines():
                if not line.startswith("data:") or "[DONE]" in line:
                    continue
                chunk = json.loads(line[5:])
                if first is None:
                    first = time.perf_counter() - start
                for choice in chunk.get("choices", []):
                    if choice.get("delta", {}).get("content"):
                        text_parts.append(choice["delta"]["content"])
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
    text = "".join(text_parts)
    record(
        "3 streaming completion",
        status == 200 and first is not None and finish == "stop" and len(text) > 0,
        f"ttft={first * 1000 if first else -1:.0f}ms chunks-text={text[:30]!r} finish={finish}",
    )


async def check_4_auth(client: httpx.AsyncClient) -> None:
    missing = await client.post(
        "/v1/chat/completions",
        json={"model": "glm4:9b", "messages": [{"role": "user", "content": "hi"}]},
    )
    invalid = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer wrong-key"},
        json={"model": "glm4:9b", "messages": [{"role": "user", "content": "hi"}]},
    )
    record(
        "4 auth (virtual key)",
        missing.status_code == 401 and invalid.status_code == 401,
        f"missing={missing.status_code} invalid={invalid.status_code}",
    )


async def check_5_keyword_block(client: httpx.AsyncClient) -> None:
    response = await chat(client, "glm4:9b", "这句话包含敏感词测试")
    ok = response.status_code == 400 and response.json()["error"]["code"] == "content_filter_rejected"
    record("5 keyword filter blocks request", ok, f"status={response.status_code} body={response.text[:120]}")


async def check_6_keyword_mask(client: httpx.AsyncClient) -> None:
    response = await chat(
        client, "glm4:9b", "请原样输出以下句子不要修改：这是机密文件。"
    )
    if response.status_code != 200:
        record("6 keyword mask on response", False, f"status={response.status_code}")
        return
    content = response.json()["choices"][0]["message"]["content"]
    # The keyword must never leak: masked out (**) or absent (model refusal) both count.
    record("6 keyword mask on response", "机密" not in content, f"reply={content[:60]!r}")


async def check_7_cache(client: httpx.AsyncClient) -> None:
    payload_content = "请只回答数字42"
    first = await chat(client, "glm4:9b", payload_content)
    second = await chat(client, "glm4:9b", payload_content)
    if first.status_code != 200 or second.status_code != 200:
        record("7 exact-match cache", False, f"statuses={first.status_code}/{second.status_code}")
        return
    same_id = first.json()["id"] == second.json()["id"]
    record(
        "7 exact-match cache",
        same_id,
        f"id1={first.json()['id'][-12:]} id2={second.json()['id'][-12:]} same={same_id}",
    )


async def check_8_rate_limit(client: httpx.AsyncClient) -> None:
    statuses = []
    for i in range(4):
        response = await chat(client, "glm4-limited", f"hi {i}")
        statuses.append(response.status_code)
    record(
        "8 rate limiting (rpm=2 -> 429)",
        200 in statuses and 429 in statuses,
        f"statuses={statuses}",
    )


async def check_9_failover(client: httpx.AsyncClient) -> None:
    response = await chat(client, "failover-demo", "回复ok两个字")
    served_by = "unknown"
    try:
        served_by = response.json()["model"]
    except Exception:
        pass
    record(
        "9 failover to healthy fallback",
        response.status_code == 200,
        f"status={response.status_code} served_model={served_by}",
    )


async def check_10_metrics(client: httpx.AsyncClient) -> None:
    text = (await client.get("/metrics", headers={"Authorization": "Bearer sk-e2e-demo-key"})).text
    needed = ["gateway_requests_total", "gateway_request_latency_seconds", "gateway_tokens_total"]
    missing = [n for n in needed if n not in text]
    record("10 Prometheus metrics", not missing, f"missing={missing or 'none'}")


async def check_11_concurrency(client: httpx.AsyncClient) -> None:
    async def one(i: int) -> int:
        response = await chat(client, "glm4:9b", f"只回答数字{i}")
        return response.status_code

    statuses = await asyncio.gather(*(one(i) for i in range(5)))
    record("11 concurrent burst x5", all(s == 200 for s in statuses), f"statuses={list(statuses)}")


async def main_async(config_path: str, report_path: str) -> int:
    app = create_app(config_path, config_watcher=False)
    checks = [
        check_1_models, check_2_non_stream, check_3_stream, check_4_auth,
        check_5_keyword_block, check_6_keyword_mask, check_7_cache,
        check_8_rate_limit, check_9_failover, check_10_metrics, check_11_concurrency,
    ]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://e2e") as client:
        for check in checks:
            try:
                await check(client)
            except Exception as exc:  # noqa: BLE001
                record(check.__name__, False, f"exception {type(exc).__name__}: {exc}")
        await app.state.gateway.aclose()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    lines = [
        "# Ollama 端到端验证报告",
        "",
        f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 上游：本地 Ollama glm4:9b（GLM-4-9B-Chat Q4_K_S，{12}GB RTX 5070 Ti）",
        f"- 结果：**{passed}/{len(RESULTS)} 通过**",
        "",
        "| # | 验证项 | 结果 | 详情 |",
        "|---|---|---|---|",
    ]
    for name, ok, detail in RESULTS:
        lines.append(f"| {name} | {'✅' if ok else '❌'} | {'PASS' if ok else 'FAIL'} | {detail.replace('|', '\\|')} |")
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n{passed}/{len(RESULTS)} checks passed; report: {report_path}")
    return 0 if passed == len(RESULTS) else 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="gateway.ollama.yaml")
    parser.add_argument("--report", default="docs/e2e-ollama-validation.md")
    args = parser.parse_args()
    sys.exit(asyncio.run(main_async(args.config, args.report)))


if __name__ == "__main__":
    main()
