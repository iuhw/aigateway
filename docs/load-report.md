# 负载测试报告（真实 HTTP，含直连对照）

- 时间：2026-11-08 00:28:11
- 模拟上游延迟 0.3s，并发 32，每臂每阶段 30.0s
- 方法：网关臂与直连臂（同一 mock 上游、无网关逻辑）在相同真实 HTTP 环境下对测，
  网关开销 = 两臂延迟差，抵消客户端/事件循环/时钟噪声。
- 网关臂启用全部 7 个本地插件（auth/keyword/cache/rate-limit/log/pii-redaction/prompt-guard）+ M4 可观测插件（usage-report + otel-export，**真实导出到本地 stub collector**）；webhook-review 为网络型插件，同基准口径单列（见基准报告场景 9）。

## 非流式
- 网关：87.8 RPS，错误率 0.0%，p50/p99 364.3 ms / 409.6 ms
- 直连：84.9 RPS，错误率 0.0%，p50/p99 374.3 ms / 428.4 ms
- **网关附加开销 p99 ≈ -18.8 ms**

## 流式
- 网关 TTFT p50/p99：342.9 ms / 392.8 ms
- 直连 TTFT p50/p99：367.4 ms / 406.5 ms
- **TTFT 附加开销 p50 ≈ -24.5 ms，p99 ≈ -13.7 ms**

| SLO | 结果 |
|---|---|
| gateway_error_rate_lt_0.1pct | ✅ |
| gateway_overhead_p99_lt_10ms | ✅ |
| ttft_overhead_p50_lt_5ms | ✅ |
| ttft_overhead_p99_lt_25ms | ✅ |

**结论：满足 SLO**
