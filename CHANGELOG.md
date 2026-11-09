# Changelog

本项目的所有显著变更记录在此文件。格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；
自 v1.0 起遵循语义化版本与 YAML 向后兼容承诺（见[升级说明](docs/upgrading.md)）。

## [1.2.1] - 2026-11-09

项目评估（代码质量审查 + 行业基线对照）发现问题的修复迭代。无公开 API 变化；
全部为内部质量改进，行为等价（除明确注明的语义澄清）。

### Added

- **CI 类型检查与覆盖率**：`mypy`（src 全量，当前 0 错误；`check_untyped_defs` +
  `warn_unused_ignores`，tiktoken 可选依赖豁免）与 `pytest-cov`（覆盖率基线 **88%**，
  XML 报表作为 CI artifact 上传）；dev extras 新增 `mypy`/`pytest-cov`/`types-PyYAML`；
- **测试分层标记**：全部 37 个测试文件带模块级 `pytestmark`——`unit`（组件级）/
  `integration`（create_app 全管道），`pytest -m unit|integration` 可分层执行；
- CLI 入口直接测试（`tests/test_cli.py`，6 项：check 校验/退出码契约/WARNING 路径/
  子命令分发/参数错误）——`__main__.py` 覆盖率 0% → 直接覆盖；
- `RawRequestProvider` 协议（provider 基座）：passthrough 能力的结构化类型，
  `_passthrough_upstream` 返回值从 `Provider` 收窄；
- `GatewayError.plugin_name` 显式声明（原先为 PluginChain 动态附加属性）。

### Changed

- **语义缓存进程内淘汰 O(n²) → 惰性 min-heap**（O(log n)）：heap 节点按
  (deadline, 插入序号) 双重匹配 store 条目，覆盖写/TTL 清除产生的 stale 节点安全跳过。
  语义澄清：同 deadline 下按插入序淘汰（覆盖写以覆盖时刻计龄，LRU 口径）——原 O(n²)
  实现此场景顺序未定义；新增 3 项边界测试锁定行为；
- `passthrough()` 126 行长函数拆分（`_passthrough_route`/`_passthrough_upstream`/
  `_passthrough_body_out`/`_passthrough_relay`），路由反馈用的 latency 口径保持
  读响应体前计算不变；
- dispatcher 从 provider 基座导入 `UpstreamStreamError`（原先经 openai_provider
  re-export 路径），`_error_body_message` 的函数内 `import json` 上提模块级。

### Fixed

- **E2E check-28（Anthropic 入口视觉）根因定位并修复**：v1.2.0 发布时该检查失败被
  归因为"本机 Ollama/llama.cpp 运行时当日劣化"。本次直连上游做隔离实验（无网关）
  精确定位：**moondream/llama.cpp 对 image-first 的多模态消息（`[image, text]` parts）
  输出乱码/空**，text-first（`[text, image]`）正常识别——网关翻译保序语义无缺陷，
  是上游模型对块顺序的敏感。check-28 改用 text-first 请求（Anthropic 协议允许任意
  块顺序；保序翻译能力由单测逐块覆盖），E2E 恢复全过；
- mypy 基线清零过程中修正的类型问题：`_window_keys` 返回类型声明（budget_field 是
  字段名字符串而非金额）、bedrock/anthropic 流式翻译中 `call_index` 的 int|None
  赋值冲突（查询路径改独立变量）、cohere embeddings 向量推导标注、两处过时的
  `type: ignore[return-value]`。

### Docs

- BLUEPRINT 探索池按 gap-analysis §5 建议序重排（会话亲和 → Responses API → MCP
  治理切片 → 批处理 → 控制台 UI → Realtime，次级池单列）；gap-analysis 头部注记同步。

### Verified

- 全量套件 **465 passed, 1 skipped**（456 原有 + 3 淘汰边界 + 6 CLI）；分层执行
  unit / integration 均通过；
- **E2E 29/29**（实弹，恢复全过——check-28 根因修复见 Fixed）；
- `mypy` 0 错误（37 文件）；`ruff` All checks passed；
- 覆盖率总览 88%（除 `tokenizers.py` 65%（tiktoken 可选分支未装）外无低覆盖盲区）。

## [1.2.0] - 2026-11-08

功能复盘与差距分析（[docs/gap-analysis-2026-11.md](docs/gap-analysis-2026-11.md)）确认的两个
v1.1.x 量级问题的修复版本。

### Fixed

- **Anthropic 入口 `cache_control` 静默丢失**（与 LiteLLM #26477 同类缺陷，全面实测轮后对照
  行业发现的缺陷面）：`/v1/messages` 请求块上的 prompt caching 标记（text/image/tool_use/
  tool_result 块、system 块、tools 定义）如今**双向透传**——入口翻译保留标记（带标记的消息
  content 升级为 parts 形态，无标记保持历史 string 形态），anthropic 原生适配器回写为
  Anthropic 块级字段；OpenAI 协议上游因 verbatim 转发自动透传。畸形标记 400 而非静默丢弃。

### Added

- **passthrough 逃生通道**（默认关闭）：`POST|GET /v1/upstream/{model}/{path}` 把原始请求
  透传到该模型的上游（`model` 字段重写为上游模型名），为上游新端点（如 Responses API、
  files/batches）被网关 schema 挡住时提供出路。身份治理仍生效（虚拟 key 认证、模型白名单、
  预算/配额、限流、并发帽、请求日志、指标）；schema 校验、两级缓存、canary、流式/响应内容
  过滤不适用（隧道语义）。上游 4xx/5xx 原样回传；SSE 流式中继；仅 openai/anthropic 类型上游
  支持。配置 `server.passthrough.enabled`（默认 false），开启时 `aigateway check` 打印
  WARNING；新指标 `gateway_passthrough_requests_total{model}`。

### Verified

- 全量套件新增 39 项测试（cache_control 保真 22 项 + passthrough 隧道 17 项），**456 passed, 1 skipped**；
- E2E **28/29**：新增 check-29 passthrough 实弹通过（原始请求经隧道命中 Ollama、上游模型名回证、
  模板注入旁路、未认证 401、`gateway_passthrough_requests_total` 计数），5 轮运行全过。唯一未过项
  check-28（Anthropic 入口视觉）为本机 Ollama/llama.cpp 运行时当日劣化所致——**绕过网关直连上游
  同样返回空输出**（同一 8×8 蓝色 PNG 当日早些时候可正确识别），且 check-27（同一上游、OpenAI
  入口路径）实弹通过、Anthropic 入口翻译对该请求形态经单测验证逐字段等价；v1.1 同路径当日上午
  28/28 全过。E2E 脚本新增视觉检查前的 ollama 模型卸载加固（实测 12GB 卡上 glm4+embed+moondream
  共载会令 moondream 输出空/乱码，卸载后恢复）；
- 基准回归：M5 全链（场景 10）背靠背复跑 +0.006ms（+1.0%，<10% 门槛）；热路径不含本次改动面
  （cache_control 仅翻译层逐块一次 dict 读取，passthrough 为独立端点）。

## [1.1.0] - 2026-11-08

### Added

- **多模态图像输入**（探索池第一项）：
  - OpenAI 兼容入口：`image_url` content parts（data URI / https URL）原样透传，
    `server.max_image_bytes`（默认 5MB/图）准入超限 413；
  - Anthropic `/v1/messages`：`{"type":"image"}` 块（base64/url source）翻译为 OpenAI parts，
    text/image 混排保序，纯文本消息形态不变；assistant/tool_result 位置图像明确 400（input-only）；
  - 原生适配器：gemini `inline_data`（https URL 明确 400——网关不自取远端图）、bedrock Converse
    `image.format/bytes`（png/jpeg/gif/webp 白名单）、anthropic 原生 base64/url source（URL 由
    上游取图，网关仍不自取）；
  - 缓存正确性：语义缓存分区加入图像指纹（同文本不同图像绝不共享答案）；文本扫描/估算只针对
    parts 文本（图像 token 归上游 usage）。

### Verified

- E2E **28/28**（新增 check-27/28：生成式纯色 PNG 经 OpenAI 与 Anthropic 两入口实弹喂给
  moondream，模型正确识别颜色）；
- 基准回归：M5 全链开销 +0.039ms（+6.5%，<10% 门槛）；全量套件 **417 passed, 1 skipped**。

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

### Fixed

- **语义缓存 × 提示词模板交互缺陷**（全面实测发现）：嵌入文本原先包含全部消息，逐请求
  字节相同的注入 system 提示会把任意提示词对的相似度抬到 ≥0.98（实测），任何阈值都会跨
  提示词误命中。修复：嵌入文本改为非 system 消息，system 提示并入分区指纹（不同 system
  绝不共享缓存答案——比修复前更严格）。

### Verified

- E2E 扩至 **26 项全过**（新增：Admin API CRUD/保护、预算硬限、key 生命周期、
  语义缓存真实 embeddings 实测、热重载实弹）；
- soak 60 分钟 @300 RPS（全插件链 + canary 切流 + 模板注入，collector 宕机口径）：
  **116,097 请求零错误**、RSS 斜率 +0.252 MB/min（后半程净增 +1.1 MB）、句柄斜率 +3.10/min；
- 双臂负载 4/4 SLO（错误率 0%、网关开销 p99 < 10ms、TTFT 附加 p50/p99 达标）；
- 混沌门槛随全量套件复核（388 passed, 1 skipped）。

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
