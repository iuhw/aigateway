"""Soak test: sustained load with memory/fd-leak detection.

Runs the gateway in-process at a fixed request rate against zero-latency mock
providers and samples process RSS and open handles. Passes when RSS stays flat
(slope below threshold) and no request errors occur.

Usage: python scripts/soak_test.py [--minutes 10] [--rps 300] [--report docs/soak-report.md]
A 24h production soak uses the same script with --minutes 1440.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

import httpx

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aigateway.api import create_app  # noqa: E402


def slope_per_minute(samples: list[tuple[float, float]]) -> float:
    """Least-squares slope of value over elapsed minutes."""
    if len(samples) < 3:
        return 0.0
    t0 = samples[0][0]
    xs = [(t - t0) / 60.0 for t, _ in samples]
    ys = [v for _, v in samples]
    mean_x, mean_y = statistics.mean(xs), statistics.mean(ys)
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom


async def main_async(args: argparse.Namespace) -> int:
    process = psutil.Process() if psutil else None
    app = create_app(args.config, config_watcher=False)
    transport = httpx.ASGITransport(app=app)
    samples: list[tuple[float, float, int]] = []  # (elapsed, rss_mb, handles)
    errors = 0
    total = 0

    async with httpx.AsyncClient(transport=transport, base_url="http://soak") as client:
        started = time.monotonic()
        interval = 1.0 / args.rps
        index = 0
        next_t = started
        while time.monotonic() - started < args.minutes * 60:
            now = time.monotonic()
            if now >= next_t:
                next_t = max(next_t + interval, now)
                index += 1
                total += 1
                try:
                    response = await client.post(
                        "/v1/chat/completions",
                        json={
                            "model": "m1",
                            "messages": [{"role": "user", "content": f"soak {index}"}],
                        },
                    )
                    if response.status_code != 200:
                        errors += 1
                except Exception:  # noqa: BLE001
                    errors += 1
            await asyncio.sleep(0.001)
            elapsed = time.monotonic() - started
            if elapsed >= len(samples) * args.sample_interval_s:
                rss = process.memory_info().rss / 1e6 if process else 0.0
                handles = process.num_handles() if process and hasattr(process, "num_handles") else 0
                samples.append((elapsed, rss, handles))
                print(
                    f"[{elapsed:7.1f}s] total={total} errors={errors} "
                    f"rss={rss:7.1f}MB handles={handles}",
                    flush=True,
                )

    elapsed_total = time.monotonic() - started
    rss_samples = [(t, rss) for t, rss, _ in samples]
    handle_samples = [(t, h) for t, _, h in samples if h]
    rss_slope = slope_per_minute(rss_samples)
    handle_slope = slope_per_minute(handle_samples)
    rss_growth_total = rss_samples[-1][1] - rss_samples[len(rss_samples) // 2][1] if len(rss_samples) > 2 else 0.0

    # Leak thresholds: allow interpreter noise; a real leak grows steadily.
    rss_ok = rss_slope < 2.0 and rss_growth_total < max(30.0, args.minutes * 2.0)
    handles_ok = handle_slope <= 5.0 if handle_samples else True
    errors_ok = errors == 0
    passed = rss_ok and handles_ok and errors_ok

    lines = [
        "# Soak 测试报告",
        "",
        f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 时长：{elapsed_total / 60:.1f} 分钟 @ {args.rps} RPS（共 {total} 请求，错误 {errors}）",
        f"- RSS：起始 {samples[0][1]:.1f} MB → 结束 {samples[-1][1]:.1f} MB"
        f"（斜率 {rss_slope:+.3f} MB/min，后半程净增 {rss_growth_total:+.1f} MB）",
        f"- 句柄：斜率 {handle_slope:+.2f} /min" if handle_samples else "- 句柄：psutil 不可用",
        "",
        "| 检查项 | 阈值 | 结果 |",
        "|---|---|---|",
        f"| 请求零错误 | errors == 0 | {'✅' if errors_ok else '❌ ' + str(errors)} |",
        f"| RSS 斜率 | < 2 MB/min | {'✅' if rss_slope < 2.0 else '❌'} |",
        f"| 句柄斜率 | <= 5 /min | {'✅' if handles_ok else '❌'} |",
        "",
        f"**结论：{'通过 — 无内存/句柄泄漏迹象' if passed else '未通过 — 需要排查'}**",
    ]
    report = "\n".join(lines) + "\n"
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(report, encoding="utf-8")
    Path(args.report).with_suffix(".json").write_text(
        json.dumps(
            {
                "minutes": elapsed_total / 60,
                "rps_target": args.rps,
                "total": total,
                "errors": errors,
                "rss_slope_mb_per_min": rss_slope,
                "handle_slope_per_min": handle_slope,
                "samples": [(t, rss, h) for t, rss, h in samples],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(report)
    print(f"written: {args.report}")
    await app.state.gateway.aclose()
    return 0 if passed else 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=10.0)
    parser.add_argument("--rps", type=int, default=300)
    parser.add_argument("--sample-interval-s", type=float, default=10.0)
    parser.add_argument("--config", default=None)
    parser.add_argument("--report", default="docs/soak-report.md")
    args = parser.parse_args()

    if args.config is None:
        import tempfile

        import yaml

        from tests.conftest import base_config

        config = {
            **base_config(),
            "models": {
                "m1": {
                    "deployments": [
                        {"provider": "fast", "model": "m1-up"},
                        {"provider": "fast2", "model": "m1-up"},
                    ],
                    "retries": 1,
                    "retry_backoff_s": 0.01,
                }
            },
            "providers": {
                "fast": {"type": "mock", "latency_s": 0.005},
                "fast2": {"type": "mock", "latency_s": 0.005},
            },
        }
        tmp = Path(tempfile.mkdtemp(prefix="aigw-soak-")) / "gateway.yaml"
        tmp.write_text(yaml.safe_dump(config), encoding="utf-8")
        args.config = str(tmp)

    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
