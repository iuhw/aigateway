# aigateway 升级说明

## 版本策略（v1.0 起）

- **语义化版本**：`主.次.修订`。修订 = 缺陷修复；次 = 向后兼容的新能力（新插件/新字段/新指标）；
  主 = 仅在不兼容时递增，且必须提前一个大版本在本文公告弃用计划。
- **YAML 向后兼容承诺**：自 v1.0 起，任何曾受支持的配置文件在后续版本中**永续可用**——
  字段只新增；如需以新字段取代旧字段，旧字段保留至少一个大版本并打 deprecation 日志；
  从不改变既有字段语义。
- **指标承诺**：既有 Prometheus 指标名与标签语义不删不改（新信息一律新增序列，如
  `gateway_key_cost_usd_total` 独立于 `gateway_cost_usd_total`）；基数上限行为在文档中固定。
- **API 面**：`/v1/chat/completions`、`/v1/messages`、`/v1/embeddings`、`/v1/models`、
  `/healthz`、`/readyz`、`/metrics`、`/admin/*` 的响应形状按 OpenAI/Anthropic 兼容语义稳定；
  新字段只增不改。

## v0.x → v1.0

**行为变更（1 项）**：

- `GET /v1/models`：配置了 auth-plugin 时现在要求有效虚拟 key（无 key/错 key 返回 401），
  与 OpenAI API 行为一致；未认证的模型枚举关闭。`allow_anonymous: true` 的部署不受影响；
  未配置 auth-plugin 的部署不受影响。若你的客户端匿名拉取模型列表，请为其下发虚拟 key。

**无需配置迁移**：v0.1–v0.6 的全部 YAML 配置在 v1.0 下原样可用。

## v1.0 → v1.1

**新增能力**：多模态图像输入（OpenAI 入口 `image_url` parts、Anthropic 入口 image 块、
三家原生适配器图像翻译）。此前显式拒绝（400）的图像请求现在被支持或以更具体的错误拒绝
（畸形 part / 超尺寸 / 不支持的 URL 形态）——向兼容方向变化，无破坏。

**新配置项**（均有默认值，零配置可用）：`server.max_image_bytes`（默认 5MB/图）。

## 历史版本速览（均向后兼容）

| 版本 | 新增 |
|---|---|
| v0.2 | token 周期配额、key 生命周期（expires_at/concurrency/metadata）、per-key 成本指标、Admin API v1（/admin/keys、/admin/usage） |
| v0.3 | semantic-cache-plugin、跨副本精确缓存、原生适配器 embeddings（gemini/bedrock） |
| v0.4 | pii-redaction / prompt-guard / webhook-review 插件、guardrail 指标、Anthropic 入口护栏错误码 |
| v0.5 | otel-export / langfuse-export / usage-report 插件、/admin/usage report 段、Grafana 面板 |
| v0.6 | prompt-template-plugin、canary 灰度切流（models.canary）、canary 指标 |
| v1.0 | GA：安全自查、文档四件套、YAML 兼容承诺、语义化版本 |
| v1.1 | 多模态图像输入（OpenAI/Anthropic 入口 + 三家原生适配器 + 缓存图像指纹） |

## 升级操作

1. 备份 `gateway.yaml` 与（使用 Redis 时）确认 Redis 数据可保留——动态 key、预算账本、
   共享缓存都在其中，跨版本兼容。
2. 替换二进制/镜像后重启；配置无需改动即可启动（`aigateway check gateway.yaml` 预检）。
3. 观察 `/metrics` 与启动日志：出现 deprecation 警告按提示迁移（当前无）。
4. 回滚：镜像回退即可，无需数据迁移。
