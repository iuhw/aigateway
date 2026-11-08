# 功能复盘与行业差距分析（2026-11）

> 调研截至 2026-11-08。对比对象：**LiteLLM v1.108 / Higress v2.2.5（CNCF Sandbox）/ Portkey（Prisma AIRS）/ Kong AI Gateway 2.3 / Cloudflare AI Gateway**，
> 另加 2026 新格局扫描（agentgateway、Bifrost、Kuadrant 等）。口径：**核心功能完备性**——以
> "2026 年末现代 AI 网关能力基线"为标尺逐条对照。本文是探索池重排与后续迭代的选型依据；
> 蓝图探索池（BLUEPRINT.md）尚未按本文重排。

## 1. 功能复盘：aigateway v1.1.0 现状

九类能力均已交付（细节见 [BLUEPRINT.md](BLUEPRINT.md) 与 [full-validation-report.md](full-validation-report.md)）：

1. **协议入口**：OpenAI 兼容 `/v1/chat/completions`（SSE）+ `/v1/embeddings` + `/v1/models`；
   Anthropic `/v1/messages`（SSE）；多模态图像输入（v1.1）。
2. **上游适配**：openai 类型（Ollama/vLLM/DeepSeek/Groq…）+ gemini / bedrock / anthropic 原生适配器，四家均支持 embeddings。
3. **路由与可靠性**：多部署 LB（round_robin/weighted/least_latency）、优先级分层、fallback 链、
   重试+退避、冷却熔断（Redis 跨副本）、**canary 粘性灰度**（万桶哈希）。
4. **治理与成本**：虚拟 key（digest-only）、日/月美元预算硬限、token 周期配额、key 生命周期
   （expires_at/concurrency/metadata）、Admin key CRUD、分层 cache-read/write 定价、`/admin/usage` 报表。
5. **缓存**：精确缓存（进程内/Redis 共享）+ 语义缓存（五重分区：模型/参数/tools/system/图像指纹）。
6. **安全护栏**：keyword-filter（流式跨块）、pii-redaction（流式回拉）、prompt-guard（加权评分）、
   webhook-review（fail-open/close 流式补审）。
7. **可观测**：Prometheus（含 per-key 成本）、OTLP traces（TTFT/子 span）、Langfuse、请求日志、Grafana 面板。
8. **插件系统**：12 内置 + 外部模块零侵入 + 多实例 + on_request 改写扩展点。
9. **运维**：YAML 热重载、`aigateway check`、docker-compose 一键五容器、文档四件套。

质量资产：417 tests、E2E 28/28（实弹视觉）、soak 60min@300RPS 零错误、混沌注入、
网关开销 p50 0.42ms（全插件链 +0.07ms）。

## 2. 行业格局速览（2026-11）

- **五强定位**：LiteLLM＝"AI API 管理面"（100+ provider、key/预算体系最深）；Higress＝Envoy/Istio 系
  流量网关（MCP 托管+市场）；Portkey（2026-07 起并入 Palo Alto，改名 Prisma AIRS）＝可靠性+治理+安全
  控制面（guardrails 最深）；Kong＝"API+AI 统一治理"（MCP 治理最深、唯一 A2A 产品化）；Cloudflare＝
  零运维边缘托管（agent 生态绑定、流式弹性独有）。
- **赛道演变**：从"LLM 请求转发器"演进为 **AI 流量治理控制面**；**MCP 网关已独立成品类**
  （Mastra/Lunar.dev 均有专门对比文）；安全厂商（Palo Alto/Snyk/Wiz/Cisco）进场。
- **2026 新玩家**：**agentgateway**（Linux Foundation，Rust，LLM/MCP/A2A 三协议原生，token exchange、
  实时成本预留）、**Bifrost**（Go，自报 ~11μs 开销——口径不同，作参照不作结论）、Kuadrant（K8s
  TokenRateLimitPolicy）、Helicone（可观测切入）、Gravitee（传统 APIM AI 化）。
- **标准动向**：**MCP 2026-07-28 规范**——协议无状态化（去 initialize/Mcp-Session-Id），新增
  `Mcp-Method`/`Mcp-Name` 头使网关**免解析 JSON 体即可路由与计量**（对我们将来做 MCP 切片是重要设计
  输入）；MCP Registry（registry.modelcontextprotocol.io，API v1.0.0）；**A2A v1.0.0**（2026-03-12）；
  **OpenAI Assistants API 2026-08 退役**→ Responses API 全面迁移；K8s Gateway API Inference Extension
  GA + AI Gateway WG 成立（2026-03）。

## 3. 基线对照（"现代 AI 网关"14 条基线逐条对照）

| # | 基线能力 | 状态 | 说明 |
|---|---|---|---|
| 1 | 多提供商统一代理 + OpenAI 兼容入口 | ✅ | 标配具备 |
| 2 | **Responses API 入口** | ❌ | 五强中四家有（CF 2026-05、Kong 3.11、Portkey 2026-07、LiteLLM）；Assistants 退役后成迁移潮入口。注意 `previous_response_id` 有后端粘性、跨 provider fallback 默认失效（调研确认的坑） |
| 3 | 多方言端点 + passthrough 逃生通道 | ⚠️ | OpenAI/Anthropic 双入口✅；但无通用 passthrough 端点（LiteLLM/Portkey 有） |
| 4 | 智能路由（LB/fallback/重试/熔断） | ✅ | 标配具备；缺 cost-based/usage-based LB 策略（增强项） |
| 5 | 虚拟密钥 | ✅ | 具备；缺 key 轮换宽限与 Vault/Secrets Manager 托管引用（Portkey 有） |
| 6 | 请求限流 + token 级限流 | ⚠️ | RPM token bucket + 周期 token 配额✅；缺"按 token 计量的实时限流"（Kuadrant TokenRateLimitPolicy 口径） |
| 7 | 预算与配额 | ✅ | 日/月硬限+token 配额；缺多级继承（见 §5 次级） |
| 8 | token 成本核算 + showback | ⚠️ | per-key 成本指标+usage 报表✅；metadata.team 有归属但**无 team 聚合报表** |
| 9 | 全链路可观测 | ✅ | metrics/OTel/Langfuse/日志；缺 prompt-cache 命中率指标 |
| 10 | guardrails（PII/injection/内容） | ✅ | 四插件齐；缺生态 preset（Lakera/Presidio/Bedrock Guardrails…），webhook-review 可自适配 |
| 11 | 精确缓存 + **prompt caching 指令保真** | ⚠️ | 精确+语义缓存✅（语义缓存 CF 尚未有）；**实测确认 `src/` 无 `cache_control` 处理：Anthropic 入口翻译为 OpenAI parts 时静默丢弃**（与 LiteLLM #26477 同类缺陷）；cached-token 折扣单价有（分层 pricing），缺命中率观测 |
| 12 | **批处理 API** | ❌ | LiteLLM/Portkey 产品化（多 provider），Kong 部分，CF 仅 Workers AI；五强口径下算基线 |
| 13 | 多模态透传 | ⚠️ | 图像✅（v1.1）；音频输入/rerank/图像生成 ❌ |
| 14 | **Realtime/语音 WebSocket** | ❌ | 四家有（Kong 除外），"快速标配化"中；有语音场景才算核心缺口 |

**结论：基线 14 条——完全具备 6、部分具备 5、缺失 3（#2/#12/#14）。**

## 4. 前沿对照（11 条差异化能力）

| # | 前沿能力 | 状态 | 判定 |
|---|---|---|---|
| 15 | MCP 网关（托管/per-key 工具过滤/工具审计） | ❌ | **五强全有，主战场缺口**；探索池③应升格 |
| 16 | MCP 无状态透传（2026-07-28 规范，`Mcp-Method` 头计量） | ❌ | 做 #15 时直接按新规范设计 |
| 17 | A2A v1 代理 | ❌ | 仅 Kong/agentgateway/Azure APIM——**前沿非基线**，可后置 |
| 18 | agent 会话亲和/长任务 | ❌ | LiteLLM/Portkey/Higress 3/5 有，正快速基线化；**v1.2 提案在案** |
| 19 | 语义缓存（向量库） | ✅ | 持平；专用向量索引升级＝探索池⑤（低急迫） |
| 20 | 网关侧 hosted tools（web search 代执行） | ❌ | CF/Vercel/LiteLLM 有；前沿，远期 |
| 21 | usage-based billing / token 转售 | ❌ | OpenRouter 5.5% 平台费模式；非本项目定位，远期 |
| 22 | chargeback + 预请求成本预留 | ⚠️ | 有 showback 基础（per-key 成本）；聚合与预留远期 |
| 23 | MCP Registry 集成 | ❌ | 并入 #15 做服务发现 |
| 24 | KV-cache 感知推理路由（Gateway API Inference Extension/EPP） | ❌ | 对 vLLM 自托管用户有价值；K8s 部署形态成熟后评估 |
| 25 | agent 沙箱分工（网关管策略+身份，沙箱管执行） | ❌ | 形态观察，远期 |

## 5. 差距清单与优先级

**核心缺口（基线，清零才算"功能全"）：**
1. **Responses API 透传**（#2）——最大基线缺口。第一期只做 OpenAI 兼容上游直通；翻译层（Responses↔Chat，像 LiteLLM/Portkey 那样任意后端可用）后置；`previous_response_id` 粘性与 fallback 的交互须写失败模型文档。
2. **批处理 `/v1/batches`+`/v1/files`**（#12）——先 OpenAI 兼容上游。
3. **Realtime/语音 WebSocket**（#14）——视用户场景决定先后。
4. **cache_control 保真**（#11 的缺陷面）——小改动可先行（见 §6）。
5. **passthrough 逃生通道**（#3 的缺口面）——通用透传端点，低风险小功能。

**竞争缺口（前沿但主战场）：**
6. **MCP 工具治理切片**（#15/16/23）——按 2026-07-28 无状态规范设计：`Mcp-Method`/`Mcp-Name` 头路由
   计量 + per-tool ACL/限流/审计 + Registry 发现；端点代理优先、Server 托管后置。
7. **会话亲和**（#18）——v1.2 提案在案，3/5 验证。
8. **控制台 UI**——五强全有；功能上非核心、采用上是核心（前述零构建链方案）。

**次级（有它才"全"，非准入门槛）：** token 级实时限流口径、team 聚合 showback、密钥托管引用/轮换宽限、
护栏生态 preset、音频输入/rerank、cost-based LB、prompt-cache 命中率指标、语义缓存专用向量索引（探索池⑤）。

**建议补齐顺序**：v1.1.x 小修（cache_control 保真 + passthrough）→ v1.2 会话亲和 → v1.3 Responses API →
v1.4 MCP 治理切片 → v1.5 批处理 → v1.6 控制台 UI → Realtime/音频/多租户三层等再排。
每版维持既有纪律：插件优先、默认关闭、p50 开销增幅 <10%、YAML 向后兼容。

## 6. 立即可修的缺陷项（本次对照的实测发现）

**Anthropic 入口 `cache_control` 静默丢弃**：客户端在 `/v1/messages` 消息块上携带
`cache_control: {"type":"ephemeral"}`（Anthropic prompt caching 指令）时，入口翻译层丢弃该字段，
翻译后的 OpenAI 形态与原生适配器均无此字段——anthropic 原生上游的缓存指令丢失（**缓存成本/性能
静默劣化，无报错**）。LiteLLM 曾有同类缺陷（GitHub #26477，归一化路由丢弃 `cache_control`）。
修法：规范化中间表示保留 cache_control 字段，anthropic 原生适配器回写；gemini（context caching）/bedrock
（prompt caching）映射后置。属 v1.1.x 热修量级。

> **2026-11-08 更新**：已于 v1.2.0 修复（连同 passthrough 逃生通道，见 operations.md 边界 21/22）。

## 7. 我们领先/差异化的（保持项）

- **canary 粘性灰度**：五强均无产品化切流功能；
- **语义缓存正确性细节**：五重分区防误命中（图像指纹分区为 v1.1 新增，CF 语义缓存尚未上线）；
- **插件零侵入第三方扩展**（竞品为回调/Wasm）；
- **轻量部署 + 0.42ms 开销**（LiteLLM 硬依赖 Postgres+Redis；Portkey 宣称 <1ms）；
- **工程验证深度**：417 tests + E2E 实弹 + soak/混沌 + 失败模型文档——开源网关少见的验证口径。

## 8. 方法与来源

三份调研底稿（2026-11-08，WebSearch/WebFetch 核对官方文档与 release notes）：

- LiteLLM：[release notes](https://docs.litellm.ai/release_notes)｜[v1.108.0](https://docs.litellm.ai/release_notes/v1.108.0/v1-108-0)｜[Responses API](https://docs.litellm.ai/docs/response_api)｜[MCP](https://docs.litellm.ai/docs/mcp)｜[Batch](https://docs.litellm.ai/docs/batches)｜[GitHub releases](https://github.com/BerriAI/litellm/releases)
- Higress：[releases](https://github.com/higress-group/higress/releases)｜[docs](https://higress.ai/docs/latest/overview/what-is-higress)｜[MCP 托管博客](https://higress.ai/blog/higress-gvr7dx_awbbpb_nvygddyr4wff1g5v)
- Portkey/Prisma AIRS：[官网](https://portkey.ai)｜[changelog](https://portkey.ai/docs/changelog/enterprise)｜[GitHub](https://github.com/Portkey-AI/gateway)
- Kong：[AI Gateway 2.0 发布](https://konghq.com/blog/product-releases/kong-ai-gateway-2-0-agentic-ai)｜[Agent Gateway](https://konghq.com/blog/product-releases/kong-agent-gateway)｜[changelog](https://developer.konghq.com/ai-gateway/changelog/)
- Cloudflare：[changelog](https://developers.cloudflare.com/ai-gateway/changelog/)｜[AI 平台博客](https://blog.cloudflare.com/ai-platform)｜[Agents Week](https://blog.cloudflare.com/agents-week-in-review/)
- 格局与标准：[TrueFoundry 2026 指南](https://www.truefoundry.com/blog/a-definitive-guide-to-ai-gateways-in-2026-competitive-landscape-comparison)｜[agentgateway](https://www.solo.io/products/agentgateway)｜[Bifrost](https://github.com/maximhq/bifrost)｜[MCP 2026-07-28 规范](https://blog.modelcontextprotocol.io/posts/2026-07-28)｜[MCP Registry](https://modelcontextprotocol.io/registry/about)｜[A2A v1.0](https://authzed.com/learn/agent-to-agent-communication-guide-google-a2a-protocol)｜[Gateway API Inference Extension](https://gateway-api-inference-extension.sigs.k8s.io)｜[K8s AI Gateway WG](https://www.kubernetes.dev/blog/2026/03/09/announcing-ai-gateway-wg)｜[MCP 网关品类对比](https://mastra.ai/articles/best-mcp-gateways)
