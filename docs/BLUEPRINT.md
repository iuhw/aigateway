# aigateway 项目蓝图

> 版本基线：v0.1.0（2026-10）。基于三篇行业调研（[LiteLLM 等](research-litellm-etc.md)、
> [Higress 等](research-higress-etc.md)、[设计综合](research-summary.md)）与 2025-2026
> 最新行业格局（Azure APIM / Kong / Portkey / Cloudflare / APISIX / MCP-Agent 网关趋势）制定。
> 本文回答三个问题：现在在哪、要去哪、怎么分步去。

## 1. 愿景与定位

**一句话**：自托管、轻量、生产级的多模型 AI 网关 —— 用一个 Python 进程 + 插件体系，
提供业界头部网关的核心治理能力，且每一项能力都有测试与基准证据。

**差异化（相对 LiteLLM / Higress / Kong）**：

- **轻量**：单进程 Python 3.12，实测网关自身开销 p50 0.44ms（~2100 RPS），无 Envoy/sidecar/数据库强依赖；
- **证据链**：单元/集成/故障注入/混沌/多实例/soak/真实模型 E2E 全套验证，基准报告随仓库提交；
- **插件零侵入**：第三方插件 `module`/`class` 即插即用，钩子覆盖流式全生命周期；
- **双协议对称**：OpenAI 与 Anthropic Messages 双入口 + 三家原生上游适配（anthropic/gemini/bedrock），
  同一公开模型可跨厂商编排降级。

**不做的事（non-goals）**：模型训练/微调平台；全托管 SaaS；带数据库的控制台 UI（admin API 优先，
UI 留给社区）；prompt 工程平台的全套 IDE 能力（只做网关侧的模板/改写钩子）。

## 2. 现状基线（v0.1.0）

按行业八大能力域盘点（✅ 已有 / 🟡 部分 / ❌ 缺失）：

| 能力域 | 状态 | 已有 | 缺口 |
|---|---|---|---|
| 统一接入 | ✅ | OpenAI 兼容入口 + Anthropic Messages 入口（tools/流式翻译）+ `/v1/embeddings`；4 类原生上游适配器 | 多模态输入 |
| 路由与流量治理 | ✅ | round_robin / weighted / least_latency、priority tier、fallbacks 降级链、冷却熔断、流式首块前切换、超时重试退避 | 按身份/百分比的灰度切流；主动健康检查 |
| Token 与成本治理 | 🟡 | token 级 RPM 限流（预扣+多退少补）、模型级/身份级限流、`pricing` 单价（含分层 cache-read/write 价）→ 全局成本指标、**虚拟 key 日/月美元预算硬限**（超限 429 `budget_exceeded`，账本 Redis 跨实例共享）、Unicode 感知 token 估算器（可选 tiktoken 精确计数） | per-key 用量/成本归集指标与报表、token 周期配额、key 生命周期（有效期/并发上限）、Admin API key 管理 |
| 缓存 | 🟡 | 精确匹配响应缓存（TTL+容量，流式不缓存） | 语义缓存 |
| 安全与合规 | 🟡 | 虚拟 key 认证、关键词 block/mask（双向、流式跨块安全） | PII 脱敏、提示词注入检测、外部内容安全服务对接 |
| 可观测性 | 🟡 | Prometheus 全套（请求/延迟/TTFT/token/拒绝/缓存/上游尝试/成本）、JSON 请求日志、healthz/readyz | 分布式 tracing 导出（OTel/Langfuse）、per-key/per-model 报表 |
| 提示词工程 | ❌ | — | 提示词模板、请求改写钩子、提示词压缩 |
| 新兴方向 | ❌ | — | MCP 托管、Agent 网关、批处理接口 |

**质量基线（回归门槛的出发点）**：226 个测试（CI 全绿：lint + 全量测试 + 基准 smoke + Docker 健康检查）；
混沌注入 30% 故障下 >97% 成功率、零挂起；5min @400 RPS 零错误、RSS 无泄漏迹象；
网关开销 p50 0.44ms、流式 TTFT 开销 +0.03ms。详见 [docs/operations.md](operations.md) 与各报告。

## 3. 总路线：从「能用的网关」到「可治理的 AI 入口」

```
v0.1 现状 ──► M1 治理 ──► M2 缓存 ──► M3 安全 ──► M4 可观测 ──► M5 提示词/灰度 ──► v1.0 GA
          租户预算      语义缓存     护栏插件      tracing/报表    模板+切流          稳定化承诺
                                                                └─ 探索池(v1.x)：多模态 / 批处理 / MCP / Agent 网关
```

排序原则：**先治理后增强**。预算与配额（M1）是把"限流"升级为"经营"的分水岭，也是所有
后续能力的计量地基（缓存命中率、报表、灰度都按身份归集）；安全（M3）在治理之后，
因为护栏插件依赖 M1 的身份标签做审计归属。

### M1（v0.2）租户与预算完善 —— 从预算硬限到可经营

- **已就位**（v0.1.x，本蓝图制定时刚落地）：虚拟 key 日/月美元预算硬限（429 `budget_exceeded`）、
  Redis 共享账本、分层 cache-read/write 定价、Unicode 感知 token 估算器。
- **本里程碑补齐**：
  - token 周期配额（日/月 token 上限，与美元预算并行生效）；
  - key 生命周期：`expires_at` 过期、`concurrency` 并发上限、`metadata`（team/app 归属）；
  - 成本归集：`gateway_cost_usd_total` 增加 `key` 标签（控制基数：超上限聚合为 `other`）；
  - **Admin API v1**（`api.py`，管理密钥保护）：`/admin/keys` CRUD、`/admin/usage?key=…`
    用量与余额查询；配额状态走 Redis 共享后端（`state.py`），多副本一致。
- **涉及模块**：`plugins/auth.py`、`plugins/rate_limit.py`、`state.py`、`observability/metrics.py`、`api.py`。
- **验收**：多副本 Redis 下预算/配额一致性测试；超限拒绝与指标基数上限测试；基准回归达标。

### M2（v0.3）语义缓存 —— 复用 embeddings 管道

- **`semantic-cache-plugin`**：请求文本 → `provider.embed`（复用 M0 已有的 `/v1/embeddings`
  上游管道）→ 相似度检索（内存/Redis 两后端，与 `state.py` 语义对齐）；cosine 阈值可配，
  默认关闭、开启需显式配置。
- **正确性防线**：缓存键分区 = 模型 + messages 归一化指纹 + tools/参数指纹（参数不同绝不命中）；
  TTL 独立于精确缓存；命中打标进指标（`semantic_hit`），可一键降级回精确缓存。
- **涉及模块**：新插件 `plugins/semantic_cache.py`、`state.py`（向量检索后端）、metrics。
- **验收**：阈值边界与误命中防护测试集；流式请求不缓存沿用现有决策；embed 不可用时降级直通；
  命中路径延迟基准（目标 <1ms 不含 embed 往返）。

### M3（v0.4）安全护栏插件组

- **`pii-redaction-plugin`**：规则组（手机号/邮箱/身份证/银行卡/自定义正则），request/response
  双向 + block/mask 双动作，**复用关键词过滤的流式安全前缀切分算法**（现成资产）。
- **`prompt-guard-plugin`**：启发式提示词注入检测（规则可配、可叠加外部服务）。
- **`webhook-review-plugin`**：请求/响应异步送外部审核服务（对接云厂商内容安全），超时策略可配
  （fail-open / fail-close）。
- **涉及模块**：三个新插件；`base.py` 不变（验证插件接口表达力）。
- **验收**：流式 PII 掩码跨块安全测试（复用关键词过滤测试基建）；注入样本集误报/漏报统计；
  插件链全开时基准开销增幅 <10%。

### M4（v0.5）可观测深化

- **`otel-export-plugin`**（可选依赖 extra `[otel]`）：OTLP 导出 traces（每请求 span +
  上游尝试子 span + TTFT 事件）；**`langfuse-export-plugin`**（可选）。
- **报表**：`/admin/usage` 扩展为按 key/model/时间的聚合报表；Grafana 面板补 per-key 成本、
  缓存命中率、护栏拦截率（`deploy/`）。
- **涉及模块**：两个新插件、`api.py`、`deploy/`。
- **验收**：本地 OTel collector 集成验证；面板与文档截图；导出失败不影响请求路径（fire-and-forget 测试）。

### M5（v0.6）提示词工程与灰度

- **`prompt-template-plugin`**：按模型/身份注入系统提示模板，支持变量；`on_request` 改写钩子
  公开为受支持的扩展点（请求改写）。
- **灰度切流**：路由层新增按身份哈希/百分比的模型路由分流（canary），流量打标进指标与日志；
  在 weighted 之上实现，不改变 priority tier 语义。
- **提示词压缩**：探索项（上游 `prompt compression` 如 Kong+Bedrock 的做法）。
- **验收**：灰度比例统计测试（大样本偏差 <5%）；模板注入回归测试；灰度与 fallback 组合的失败模型测试。

### v1.0（GA）稳定化

- soak 1 小时无泄漏、混沌基准门槛复核；安全自查（key 存储、日志脱敏开关默认值、admin 越权）；
- 文档四件套：部署手册、插件开发指南、配置参考、升级说明；**YAML 向后兼容承诺**（v1 起旧配置永续可用）；
- 语义化版本 + 变更日志。

### 探索池（v1.x+，不承诺排序）

多模态输入（图像/音频，schemas 扩展）；批处理/离线异步接口；**MCP Server 托管与工具治理**；
**Agent 网关**（会话亲和、Agent 身份）；内置向量检索升级（RedisVL/专用索引）；控制台 UI。

## 4. 横切工程原则（每个里程碑都适用）

1. **插件优先**：能用插件实现的不进 core（M2/M3/M4 全部是插件）——core 只维护路由、调度、计量；
2. **性能门槛**：每里程碑基准回归，网关自身开销 p50 相对上一版增幅 <10%（0.44ms 基线）；
3. **失败模型不变式**：流式首块前可无感切换；插件错误不掩盖原始错误；上游 4xx 默认快速失败；
4. **默认关闭，显式开启**：语义缓存、灰度、外部审核等有正确性/隐私权衡的能力一律 opt-in；
5. **共享状态对齐**：新计量/配额能力必须同时提供 InMemory 与 Redis 后端，语义等价；
6. **测试随行**：特性不带测试不合并；CI 全绿是唯一合并标准。

## 5. 风险与决策点

| 风险 | 影响 | 对策 |
|---|---|---|
| 语义缓存错误命中（答非所问） | 正确性 | 参数指纹分区 + 保守默认阈值 + TTL + 默认关闭 + 命中打标可审计 |
| per-key 成本标签导致指标基数爆炸 | Prometheus 性能 | key 数量上限、超限聚合为 `other` 标签 |
| OTel/Langfuse 依赖拖重安装体积 | 采用率 | 独立 optional extra，核心零新依赖 |
| Redis 向量检索性能（纯 Python cosine） | 延迟 | 先容量上限 + 本地暴力检索起步，探索池再评估专用索引 |
| 灰度与 fallback/冷却语义叠加出歧义 | 正确性 | M5 单独立项设计失败模型文档，复用 priority tier 语义测试基建 |
| 范围蔓延（MCP/Agent 过早立项） | 交付节奏 | 锁定探索池不承诺排期，GA 前不启动 |

## 6. 附录：行业参照与调研来源

仓库内调研：[research-litellm-etc.md](research-litellm-etc.md)、
[research-higress-etc.md](research-higress-etc.md)、[research-summary.md](research-summary.md)（设计决策对照）。

2026-10 行业格局补充来源：

- LiteLLM：[虚拟 Key 与预算](https://docs.litellm.ai/docs/proxy/users)、[Fallbacks](https://docs.litellm.ai/docs/proxy/reliability)
- Portkey：[AI Gateway](https://docs.portkey.ai/docs/product/ai-gateway)、[Guardrails](https://portkey.ai/features/guardrails)
- Kong：[AI Gateway 3.8 语义缓存/路由](https://konghq.com/blog/product-releases/ai-gateway-3-8)、[语义 Prompt Guard](https://developer.konghq.com/plugins/ai-semantic-prompt-guard)
- Cloudflare：[AI Gateway 文档](https://developers.cloudflare.com/ai-gateway)
- Azure APIM：[AI 网关能力](https://learn.microsoft.com/en-us/azure/api-management/genai-gateway-capabilities)、[内容安全策略](https://learn.microsoft.com/en-us/azure/api-management/llm-content-safety-policy)
- APISIX：[ai-proxy](https://apisix.apache.org/docs/apisix/plugins/ai-proxy)、[Token 限流](https://apisix.apache.org/blog/2025/02/24/apisix-ai-gateway-features)
- Higress：[AI 网关](https://higress.ai/ai-gateway)、[AI 网关 10 大基本能力](https://higress.ai/blog/higress-gvr7dx_awbbpb_hlsz3id8cec7z1m3)
- 趋势：[LLM/MCP/Agent 网关分类](https://aembit.io/blog/what-are-the-main-types-of-ai-gateways-model-mcp-and-agent-gateways-explained)、[State of AI Gateways 2026](https://www.aklivity.io/state-of-ai-gateways-2026)
