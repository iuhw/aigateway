# 负载测试报告（真实 HTTP，含直连对照）

- 时间：2026-10-31 07:06:30
- 模拟上游延迟 0.3s，并发 32，每臂每阶段 30.0s
- 方法：网关臂与直连臂（同一 mock 上游、无网关逻辑）在相同真实 HTTP 环境下对测，
  网关开销 = 两臂延迟差，抵消客户端/事件循环/时钟噪声。
- 网关臂启用全部 7 个本地插件（auth/keyword/cache/rate-limit/log/pii-redaction/prompt-guard）；webhook-review 为网络型插件，同基准口径单列（见基准报告场景 9）。

## 非流式
- 网关：86.5 RPS，错误率 0.0%，p50/p99 372.3 ms / 412.3 ms
- 直连：83.3 RPS，错误率 0.0%，p50/p99 384.8 ms / 421.5 ms
- **网关附加开销 p99 ≈ -9.2 ms**

## 流式
- 网关 TTFT p50/p99：351.5 ms / 389.0 ms
- 直连 TTFT p50/p99：361.0 ms / 401.3 ms
- **TTFT 附加开销 p50 ≈ -9.5 ms，p99 ≈ -12.3 ms**

| SLO | 结果 |
|---|---|
| gateway_error_rate_lt_0.1pct | ✅ |
| gateway_overhead_p99_lt_10ms | ✅ |
| ttft_overhead_p50_lt_5ms | ✅ |
| ttft_overhead_p99_lt_25ms | ✅ |

**结论：满足 SLO**
