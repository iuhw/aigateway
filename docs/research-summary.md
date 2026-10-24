# aigateway 设计综合：从调研到实现

> 基于 [调研（一）](research-litellm-etc.md) 与 [调研（二）](research-higress-etc.md) 的设计综合，
> 说明 aigateway 采纳了哪些主流网关的设计决策，以及架构分层。

## 1. 目标定位

自研一个**生产级可用、可扩展**的核心 AI 网关：

- OpenAI 兼容统一入口（`/v1/chat/completions`、`/v1/models`），上游覆盖一切 OpenAI 协议服务
  （OpenAI、Ollama、vLLM、DeepSeek、Groq…）；
- 网关级核心能力：路由、负载均衡、超时/重试/退避、故障转移、降级链、冷却熔断、限流、缓存、认证、
  可观测性（Prometheus 指标 + 结构化请求日志 + 健康检查）；
- **插件式可插拔能力**：关键词检测过滤、以及第三方插件无侵入接入（外部模块加载）；
- 流式优先：SSE 透传、流式关键词拦截/掩码、TTFT 观测。

## 2. 架构分层

```
client ──► FastAPI (api.py)                    HTTP 适配：SSE、错误映射、/metrics /admin /healthz
              │
              ▼
         GatewayApp (app.py)                   编排：解析→插件链→路由→调度→插件链→计量
              │
     ┌───────┼────────┬──────────────────┐
     ▼       ▼        ▼                  ▼
 PluginChain Router   Dispatcher      Metrics
 (plugins/) (router.py) (dispatcher.py) (observability/)
     │         │           │
 关键词过滤  模型→部署   重试/退避/故障转移/降级链/冷却上报
 认证/限流   负载均衡    流式：首块前可失败转移
 缓存/日志   冷却/分层
```

**关键决策与出处：**

| 决策 | 出处（调研） |
|---|---|
| Provider 抽象为"规范化 OpenAI 协议"，请求近原样转发 | LiteLLM `openai/` 快速路径 |
| deployment 冷却：连续失败 `allowed_fails` → `cooldown_s` | LiteLLM cooldown |
| 429/408/409/425/5xx 可重试，4xx 客户端错误默认快速失败 | LiteLLM/Portkey `on_status_codes` |
| 优先级分层（priority tier）+ 组级 fallback 链 | LiteLLM fallbacks / Higress 分层路由 |
| 插件链 hook：request / response / stream_start / chunk / end / error | Higress wasm 插件链 |
| 关键词过滤 block/mask 双动作、请求+响应双向、流式跨块安全 | Cloudflare Guardrails + Higress 内容安全 |
| 虚拟 key + 模型白名单 + 按身份限流 | LiteLLM virtual keys |
| 精确缓存（TTL+容量淘汰），流式不缓存 | Portkey simple cache / Higress ai-cache |
| Prometheus 指标 + 每请求结构化日志 | LiteLLM/Higress ai-statistics |
| token 用量在 ctx 上归集，指标与限流各自消费 | Higress ai-statistics 与 ai-token-ratelimit 分离 |

## 3. 插件系统

```python
class Plugin:
    async def on_request(ctx)                 # 路由前：认证/限流/关键词/缓存查询
    async def on_response(ctx)                # 非流式响应（可改写 body）
    async def on_stream_start(ctx)
    async def on_stream_chunk(ctx, chunk)     # 逐块：返回 None 丢弃，可改写
    async def on_stream_end(ctx)
    async def on_error(ctx, error)
```

- 内置插件（`@register_plugin` 注册，YAML `plugins:` 段启用配置）：
  `auth-plugin`、`keyword-filter-plugin`、`response-cache-plugin`、`rate-limit-plugin`、`request-log-plugin`。
- **外部插件零侵入接入**：配置 `module`/`class` 即可加载任意 Python 包里的插件；
- 插件抛 `GatewayError` 即阻断请求（错误归属插件名，进 `gateway_plugin_rejections_total` 指标）；
- `on_error` 钩子永不掩盖原始错误。

### 流式关键词掩码的跨块安全

逐块扫描无法看到跨块关键词。采用"**安全前缀切分**"：累积缓冲，每次只释放
"不再可能成为任何关键词前缀且无关键词跨越切分点"的前缀，其余留缓冲；finish 块冲刷。
代价是最多 `max(keyword_len)-1` 字符的额外缓冲延迟（本机微秒级）。

## 4. 失败模型

```
传输错误(连接/超时) ─┐
429/408/409/425/5xx ─┴─► 同部署重试(指数退避+抖动) ─► 换部署 ─► 冷却计数
其他 4xx            ───► 快速失败（failover_on_client_error=true 时才切换）
路由内全部部署失败   ───► fallback 模型组 ─► 全部失败 503 no_healthy_deployment
流式：首个块发给客户端之前才允许失败转移；之后中断如实上抛
```

## 5. 已验证的质量门

- **80 个单元/集成测试**全绿（路由策略/冷却/优先级、重试/故障转移/降级、全部插件、HTTP 端到端含 SSE）；
- **基准测试**（零延迟 mock 上游，测网关自身开销）：单请求 p50 0.44ms，全插件链 +0.06ms，
  流式 TTFT 开销 +0.03ms，冷却稳态与基线持平，强制故障转移开销 = 配置的退避时间；
- **端到端**：真实 Ollama 9B 模型验证（见 docs/e2e-ollama-validation.md）。
