# Changelog

本项目的所有显著变更记录在此文件。格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；
自 v1.0 起遵循语义化版本与 YAML 向后兼容承诺（见[升级说明](docs/upgrading.md)）。

## [1.0.0] - 2026-11-08

GA 稳定化版本。自 v0.1 交付：统一接入（OpenAI/Anthropic 双协议 + 三家原生上游）、路由与流量治理
（分层/降级/冷却/灰度）、Token 与成本治理（限流/预算/配额/报表）、两级缓存、安全护栏组、
可观测（Prometheus + OTLP/Langfuse + Grafana）、插件体系（12 个内置插件 + 第三方零侵入）。

### Added

- GA 安全自查落地（Security）：静态 key 解析后内存只保留摘要（digest-only）；
  `GET /v1/models` 在配置 auth-plugin 时要求有效虚拟 key；request-log 文件 handler
  以自有标记判断挂载（外部 handler 不再抑制文件日志）并在 aclose 时摘除。
- 文档四件套：[部署手册](docs/deployment.md)、[插件开发指南](docs/plugin-development.md)
  （含 on_request 请求改写扩展点）、[配置参考](docs/configuration.md)、[升级说明](docs/upgrading.md)。
- **YAML 向后兼容承诺**（v1 起旧配置永续可用）与语义化版本策略。

### Verified

- soak 60 分钟 @300 RPS（全插件链 + canary 切流 + 模板注入，collector 宕机口径）：
  **116,097 请求零错误**、RSS 斜率 +0.252 MB/min（后半程净增 +1.1 MB）、句柄斜率 +3.10/min；
- 双臂负载 4/4 SLO（错误率 0%、网关开销 p99 < 10ms、TTFT 附加 p50/p99 达标）；
- 真实模型 E2E **21/21**（v1.0 代码重跑）；
- 混沌门槛随全量套件复核（387 passed, 1 skipped）。

### Changed

- FastAPI `version` 从硬编码改为读取包版本。

## [0.6.0] - 2026-11-07

### Added

- **`prompt-template-plugin`（M5）**：按模型（fnmatch）/虚拟 key/身份 metadata 注入网关侧
  系统提示；变量渲染单遍替换（未知占位符保留、变量值不二次展开）；prepend/append/replace；
  同时是 on_request 请求改写扩展点的参考实现。
- **canary 灰度切流（M5，路由层）**：`models.<name>.canary`（target/percent/keys/exclude_keys/
  salt），身份 blake2b 哈希 1 万桶粘性分流；`gateway_canary_requests_total{model,branch}`；
  校验 target/自指/percent；request-log 增加 `requested_model` 字段。

### Verified

- 382 tests；基准背靠背同窗 M5 +0.025ms（+4.3% <10% 门槛）；E2E 21/21（真实 GLM-4-9B）。

## [0.5.0] - 2026-11-01

### Added

- **`otel-export-plugin`（M4，`[otel]` extra）**：OTLP/HTTP traces（请求 span + TTFT 事件；
  多尝试/失败调度附 `gateway.upstream` CLIENT 子 span）；BatchSpanProcessor 后台导出
  fire-and-forget；span 只含元数据；`ParentBased` 采样可配。
- **`usage-report-plugin`（M4）**：`/admin/usage` report 段按 key/model/小时或天桶聚合
  （per-replica 内存环形，双重上限）。
- **`langfuse-export-plugin`（M4）**：Langfuse OTLP preset，复用 otel 机器。
- Grafana 面板 +4（per-key/per-model 成本、护栏 verdict、语义命中）。

### Changed

- `base.py` PluginContext 新增 `attempts`/`upstream_started_at`/`upstream_ended_at` 可选字段。

### Verified

- 352 tests；E2E 19/19（真实 OTLP collector stub 收到含 failover 子 span 的 protobuf）；
  双臂负载 4/4 SLO；soak 10min @300RPS（collector 宕机口径）零错误。

## [0.4.0] - 2026-10-30

### Added

- **`pii-redaction-plugin`（M3）**：4 内置正则组 + mask/block + 流式跨界回拉切分。
- **`prompt-guard-plugin`（M3）**：4 规则组（中英双语）加权评分，默认 flag 观察模式；
  样本集注入检出 100%、良性误报 0%。
- **`webhook-review-plugin`（M3）**：外部审核 wire 契约，fail-open/fail-close；流式送达后补审。
- 指标 `gateway_guardrail_flags_total`；Anthropic 入口护栏错误码映射。

### Verified

- 339 tests；E2E 16/16；全插件链（7 插件）+0.069ms（<10% 门槛）。

## [0.3.0] - 2026-10-26

### Added

- **`semantic-cache-plugin`（M2）**：分区指纹（model+采样参数+tools）内 cosine 检索，
  fail-open，流式/embeddings 不参与；`gateway_semantic_cache_hits_total`。
- 跨副本精确缓存：`response-cache-plugin` `shared: true`（`StateBackend.cache_get/put`）。
- 原生适配器 embeddings：gemini（embedContent/batch）+ bedrock（Titan/Cohere）。

### Verified

- 281 tests；语义命中路径 0.469ms（<1ms 目标）；全链 +0.060ms。

## [0.2.0] - 2026-10-25

### Added

- token 周期配额（日/月，429 `token_quota_exceeded`，缓存命中不计费）。
- key 生命周期：`expires_at`（403 `key_expired`）、`concurrency`（单副本语义）、`metadata`。
- 成本归集 `gateway_key_cost_usd_total{key}`（基数上限 `max_tracked_keys`）。
- **Admin API v1**：`GET/POST/DELETE /admin/keys`（动态 key CRUD，只存哈希）、`GET /admin/usage`。

### Verified

- 25 项新测试（含 fakeredis 跨实例）。

## [0.1.0] - 2026-10

首个公开版本：OpenAI 兼容入口 + Anthropic Messages 入口（tools/流式翻译）+ `/v1/embeddings`；
4 类原生上游适配器；round_robin/weighted/least_latency、priority tier、fallbacks、冷却熔断、
流式首块前切换；RPM/token 级限流；日/月美元预算硬限（Redis 账本）；分层 cache 定价；
Unicode 感知 token 估算器；精确响应缓存；关键词过滤（流式跨块安全）；虚拟 key 认证；
Prometheus 全套指标 + JSON 请求日志；插件体系（钩子 + 外部模块加载 + 多实例）+ YAML 热重载。
