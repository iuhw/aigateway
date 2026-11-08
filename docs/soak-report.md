# Soak 测试报告

- 时间：2026-11-08 16:06:08
- 时长：60.0 分钟 @ 300 RPS（共 116241 请求，错误 0）
- 插件链：全部本地插件（auth/keyword/cache/rate-limit/log/pii-redaction/prompt-guard）+ M4 可观测插件（usage-report + otel-export）+ M5（prompt-template 模板注入 + canary 10% 切流），**collector 端点故意永久宕机**：全程导出失败，验证 fire-and-forget 不泄漏、不拖垮请求路径
- RSS：起始 71.2 MB → 结束 94.9 MB（斜率 +0.277 MB/min，后半程净增 +2.8 MB）
- 句柄：滚动 3 分钟谷值斜率 +0.47 /min（原始斜率 -3.50 /min；原始计数含死 collector 的 OTLP 重试批量尖峰，谷值反映真实基线）

| 检查项 | 阈值 | 结果 |
|---|---|---|
| 请求零错误 | errors == 0 | ✅ |
| RSS 斜率 | < 2 MB/min | ✅ |
| 句柄斜率 | <= 5 /min | ✅ |

**结论：通过 — 无内存/句柄泄漏迹象**
