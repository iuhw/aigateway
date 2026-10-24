# AI 网关调研（一）：LiteLLM / Portkey / Cloudflare AI Gateway

> 调研时间：2026-10。目的：为自研 aigateway 提炼可借鉴的核心能力与设计决策。

## 1. LiteLLM Proxy

LiteLLM 是目前最流行的开源 Python LLM 网关（SDK + Proxy 两种形态）。

**核心能力：**
- **统一 API**：把 100+ Provider（OpenAI/Anthropic/Azure/Bedrock/Ollama/vLLM…）统一成 OpenAI 格式；`openai/` 前缀的模型走"快速路径"，请求几乎原样转发、仅重映射模型名。
- **Router（路由器）**：
  - 多部署负载均衡：least-busy / usage-based / latency-based / simple-shuffle 等策略；
  - **冷却（cooldown）**：部署连续失败达到 `allowed_fails` 次后进入 `cooldown_time` 冷却；**429 限流错误立即冷却**；
  - **多级 fallback**：模型组（model group）级降级链，主组全挂自动切备用组；支持 content fallback（响应不合规格时换模型重试）；
  - 重试策略可按 proxy/Key/Team 三个层级配置。
- **流量治理**：虚拟 key（virtual keys）、团队/预算（budget： TPM/RPM/max budget）、按 key 的模型白名单。
- **可观测性**：Prometheus 指标、Langfuse/OTel tracing、花费追踪（cost tracking）、Admin UI。
- **Guardrails**：插件式护栏（PII 脱敏、关键词拒绝、对接外部审核）挂在请求/响应 hook 上。

**借鉴到 aigateway 的设计：**
1. ✅ deployment 冷却机制（`allowed_fails` + `cooldown_s`）——已实现；
2. ✅ 模型组 fallback 链——已实现（`fallbacks`）；
3. ✅ 429 立即冷却——`RETRYABLE_STATUSES` 包含 429，配合冷却计数；
4. ✅ 虚拟 key + 模型白名单 + 按 key 限流——auth/rate-limit 插件已实现；
5. `openai/` 快速路径思想 → 我们的 OpenAICompatibleProvider 同样"几乎原样转发"。

## 2. Portkey AI Gateway

Portkey 是商业化 LLM 网关（开源核心 gateway + 控制面），支持 1600+ LLM。

**核心能力：**
- **Config 驱动的路由策略**：`strategy`（fallback / loadbalance / single）、`on_status_codes`（哪些状态码触发 fallback）、`targets`（多个上游目标）——一个 JSON config 声明整条路由链。
- **Guardrails 库**：请求/响应级护栏插件（schema 校验、关键词、PII、审核 API 对接），支持"标注( flag )"与"阻断( block )"两种动作。
- **缓存**：simple（精确匹配）与 semantic（语义）缓存，TTL 可配。
- **可观测性**：每次请求的日志、成本、延迟、token 用量，控制面聚合。

**借鉴：**
1. ✅ "声明式配置 → 路由执行"的模型（我们用 YAML models/plugins 段落表达同类语义）；
2. ✅ 精确缓存 + TTL（ResponseCachePlugin）；块/标两种动作 → keyword_filter 的 block/mask 双模式；
3. 语义缓存/语义路由列入后续扩展（需要 embedding 服务，超出本期范围）。

## 3. Cloudflare AI Gateway

托管型边缘网关，零部署。

**核心能力：**
- **Universal Endpoint**：单一 URL 接受任意 Provider 的原生格式，配置 fallback 模型/Provider 与重试（现已演进为 routing 系列）。
- **Guardrails（一方能力）**：在边缘用 Llama Guard 拦截并评估用户 prompt 与模型响应，按危害类别配置 flag/block。
- **日志/缓存/限流/分析**：token 与成本分析、spend limits、日志留存、统一计费、安全 key 存储。

**借鉴：**
1. ✅ 请求与响应**双向**内容检测（我们 keyword_filter 的 `check: [request, response]`）；
2. ✅ "flag vs block" 两种处置强度（block/mask 模式 + 插件可只记录不阻断）。

## 能力对照（本调研 → aigateway 实现）

| 能力 | LiteLLM | Portkey | Cloudflare | aigateway |
|---|---|---|---|---|
| OpenAI 兼容统一入口 | ✅ | ✅ | ✅（原生格式） | ✅ |
| 多部署负载均衡 | ✅ 多策略 | ✅ | ✅ | ✅ rr/weighted/least_latency |
| 冷却/熔断 | ✅ | ✅ | — | ✅ allowed_fails+cooldown |
| 模型级 fallback | ✅ | ✅ | ✅ | ✅ |
| 重试+退避 | ✅ | ✅ on_status_codes | ✅ | ✅ 指数退避+抖动 |
| 虚拟 key/认证 | ✅ | ✅ | ✅ | ✅ |
| RPM 限流 | ✅ | ✅ | ✅ | ✅ token bucket（模型/key 级） |
| 精确缓存 | ✅ | ✅ | ✅ | ✅ |
| 关键词/内容护栏 | ✅ | ✅ | ✅（边缘） | ✅ block/mask，请求+响应，流式安全 |
| Prometheus 指标 | ✅ | ✅ | （自带分析） | ✅ |
| 流式 SSE 透传+TTFT 观测 | ✅ | ✅ | ✅ | ✅ |

## 参考来源

- [LiteLLM Router – Load Balancing](https://docs.litellm.ai/docs/routing)
- [LiteLLM Proxy – Load Balancing（cooldown 语义）](https://docs.litellm.ai/docs/proxy/load_balancing)
- [LiteLLM Fallbacks](https://docs.litellm.ai/docs/proxy/reliability)
- [Portkey vs LiteLLM 对比](https://medium.com/@adnanmasood/portkey-vs-litellm-routing-fallbacks-cost-tracking-and-control-the-llm-gateway-playbook-part-195855dc25c3)
- [Kong 官方三方基准（含 Portkey/LiteLLM）](https://konghq.com/blog/engineering/ai-gateway-benchmark-kong-ai-gateway-portkey-litellm)
- [Cloudflare AI Gateway Guardrails](https://developers.cloudflare.com/ai-gateway/features/guardrails)
- [Cloudflare Universal Endpoint](https://developers.cloudflare.com/ai-gateway/usage/universal)
