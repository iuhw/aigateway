# Soak 测试报告

- 时间：2026-11-01 11:37:04
- 时长：10.0 分钟 @ 300 RPS（共 19177 请求，错误 0）
- 插件链：全部 7 个本地插件（auth/keyword/cache/rate-limit/log/pii-redaction/prompt-guard）+ M4 可观测插件（usage-report + otel-export，**collector 端点故意永久宕机**：全程导出失败，验证 fire-and-forget 不泄漏、不拖垮请求路径）
- RSS：起始 70.9 MB → 结束 83.8 MB（斜率 +1.206 MB/min，后半程净增 +0.3 MB）
- 句柄：斜率 +3.20 /min

| 检查项 | 阈值 | 结果 |
|---|---|---|
| 请求零错误 | errors == 0 | ✅ |
| RSS 斜率 | < 2 MB/min | ✅ |
| 句柄斜率 | <= 5 /min | ✅ |

**结论：通过 — 无内存/句柄泄漏迹象**
