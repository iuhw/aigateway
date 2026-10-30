# Soak 测试报告

- 时间：2026-10-31 07:16:43
- 时长：10.0 分钟 @ 300 RPS（共 19246 请求，错误 0）
- 插件链：全部 7 个本地插件（auth/keyword/cache/rate-limit/log/pii-redaction/prompt-guard）
- RSS：起始 63.8 MB → 结束 65.9 MB（斜率 +0.054 MB/min，后半程净增 +0.0 MB）
- 句柄：斜率 -0.96 /min

| 检查项 | 阈值 | 结果 |
|---|---|---|
| 请求零错误 | errors == 0 | ✅ |
| RSS 斜率 | < 2 MB/min | ✅ |
| 句柄斜率 | <= 5 /min | ✅ |

**结论：通过 — 无内存/句柄泄漏迹象**
