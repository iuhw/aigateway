# Soak 测试报告

- 时间：2026-11-08 00:24:58
- 时长：60.0 分钟 @ 300 RPS（共 116097 请求，错误 0）
- 插件链：全部本地插件（auth/keyword/cache/rate-limit/log/pii-redaction/prompt-guard）+ M4 可观测插件（usage-report + otel-export）+ M5（prompt-template 模板注入 + canary 10% 切流），**collector 端点故意永久宕机**：全程导出失败，验证 fire-and-forget 不泄漏、不拖垮请求路径
- RSS：起始 71.0 MB → 结束 95.0 MB（斜率 +0.252 MB/min，后半程净增 +1.1 MB）
- 句柄：斜率 +3.10 /min

| 检查项 | 阈值 | 结果 |
|---|---|---|
| 请求零错误 | errors == 0 | ✅ |
| RSS 斜率 | < 2 MB/min | ✅ |
| 句柄斜率 | <= 5 /min | ✅ |

**结论：通过 — 无内存/句柄泄漏迹象**
