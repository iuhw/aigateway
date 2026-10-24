# AI 网关调研（二）：Higress / Kong AI Gateway / Envoy AI Gateway

> 调研时间：2026-10。目的：为自研 aigateway 提炼可借鉴的核心能力与设计决策。

## 1. Higress AI 网关（阿里开源）

基于 Envoy + wasm 插件体系的云原生网关，AI 能力以**插件链**形式叠加在路由上。

**核心能力：**
- **ai-proxy**：协议转换与模型映射（OpenAI/Anthropic/Gemini 原生格式互转），是 AI 路由的基座插件。
- **ai-statistics**：从请求/响应中提取 token 用量，输出 AI 专属指标/日志/追踪（依赖 ai-proxy）。
- **ai-token-ratelimit**：基于 Redis 的 **token 级**限流（规则级全局 + 消费者级），区别于传统 QPS 限流。
- **ai-cache**：语义缓存；**内容安全插件**：关键词过滤、微信/阿里内容审核对接。
- **wasm 插件机制**：插件按声明顺序组成链条，插件在请求/响应两个阶段设置 hook 点，可读取/改写 body；配置按路由粒度下发，热更新。

**借鉴到 aigateway 的设计：**
1. ✅ **插件链 + 请求/响应/流式三个阶段的 hook 点**——这是我们插件系统的直接原型（on_request / on_response / on_stream_*）；
2. ✅ "基座插件 + 观测插件 + 安全插件"按序组合——我们的 PluginChain 按order排序执行；
3. ✅ token 用量统计与限流分离为独立关注点（usage 记录在 ctx，metrics/限流各自消费）；
4. ✅ 关键词过滤插件（block 与替换两种动作，双向检测）；
5. wasm 级隔离/多语言插件列为远期方向（我们用 Python 入口点 + entry_points 注册等价实现可插拔）。

## 2. Kong AI Gateway

成熟 API 网关 + AI 插件族。

**核心能力：**
- **AI Proxy 插件**：路由级挂载，支持多 Provider、模型映射、协议转换。
- **AI 限流**：`ai-rate-limiting-advanced` 支持 **token 维度**窗口限流与成本配额。
- **语义三件套（3.8+）**：semantic caching（语义缓存）、semantic routing（按意图路由到不同模型）、semantic guardrails（语义合规检测）。
- **Guardrails 插件体系**：4 个一方护栏插件 + 5 个外部护栏 Provider 的第一方对接 + AI Custom 插件（自定义执行器）。
- **消费者体系**：Kong Consumer + key-auth 映射到 AI 用量与配额。

**借鉴：**
1. ✅ "护栏是一类插件，可对接外部服务"——我们的 Plugin 接口允许外部模块插件（`module/class` 配置加载）；
2. ✅ 限流支持请求维度起步、token 维度演进（ctx 中已有 prompt/completion tokens，扩展点就位）；
3. 语义缓存/语义路由列为后续扩展方向。

## 3. Envoy AI Gateway

Kubernetes Gateway API + Envoy Gateway 之上的 AI 网关（CNCF 项目）。

**核心能力：**
- **统一 API**：客户端说 OpenAI 格式，网关翻译到各 Provider。
- **Usage limiting**：基于 token/词数的用量控制（防成本失控），按优先级/公平共享分配容量。
- **继承 Envoy 全部流量治理**：重试、熔断、超时、故障转移、mTLS、gRPC。
- **性能**：官方基准单 vCPU ~350+ RPS（含 LLM 上游）。
- **护栏短板**：一方护栏能力弱，内容安全依赖外部审核服务集成。

**借鉴：**
1. ✅ "网关只做流量治理，内容安全交给插件/外部服务"的分层哲学——我们同样把关键词过滤做成可插拔插件而非核心硬编码；
2. ✅ token 维度 usage limiting 作为演进方向；
3. 性能基准方法论（固定上游、测网关自身开销）→ 我们的基准测试同思路（零延迟 mock 上游测网关自身开销）。

## 插件机制对比：为什么我们选进程内 Python 插件

| 维度 | Higress (wasm) | Kong (插件) | aigateway（Python） |
|---|---|---|---|
| 隔离性 | 强（wasm 沙箱） | 进程内 Lua/Go | 进程内（异常边界保护） |
| 热插拔 | ✅ 配置下发 | ✅ | ✅（配置热重载重建插件链） |
| 外部扩展 | 需写 wasm | 插件 SDK | import 一个 Python 类即可 |
| hook 点 | request/response | request/response | request/response/stream_start/chunk/end/error |
| 流式逐块处理 | ✅ | 部分 | ✅（on_stream_chunk 可改写/丢弃块） |

进程内方案换取开发效率与部署简单；通过"插件异常不拖垮网关"（错误边界）、"插件链可配置排序"补足工程性。

## 参考来源

- [Higress ai-token-ratelimit](https://higress.ai/en/docs/latest/user/plugins/ai/api-consumer/ai-token-ratelimit)
- [Higress ai-statistics](https://higress.ai/en/docs/latest/plugins/ai/api-o11y/ai-statistics)
- [Higress 插件市场](https://higress.ai/en/plugins)
- [Higress AI 网关架构与安装](https://aik8s.run/en/ai-k8s/inference/higress-ai-gateway)
- [Kong AI Gateway 3.8（语义缓存/路由/护栏）](https://github.com/Kong/kong/discussions/12569)
- [AI Gateway Guardrails 生态调研（LiteLLM/Kong 等）](https://www.pillar.security/blog/ai-gateway-guardrails-litellm-kong-truefoundry-2026)
- [Kong vs LiteLLM](https://api7.ai/kong-ai-gateway-vs-litellm)
- [Envoy AI Gateway 评测](https://www.truefoundry.com/blog/envoy-ai-gateway-review)
- [Envoy AI Gateway 官网](https://aigateway.envoyproxy.io)
