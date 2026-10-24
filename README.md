# aigateway

生产级可扩展 AI 网关（Python 3.12）—— OpenAI 兼容统一入口 + 插件式可插拔能力。

参考 LiteLLM / Higress AI Gateway / Kong AI Gateway / Portkey / Cloudflare AI Gateway /
Envoy AI Gateway 的成熟设计自主实现（调研见 [docs/](docs/)）。

## 核心能力

**网关级**

- OpenAI 兼容 API：`POST /v1/chat/completions`（流式 SSE + 非流式）、`GET /v1/models`
- 多上游：一切 OpenAI 协议服务（OpenAI / Ollama / vLLM / DeepSeek / Groq / LM Studio …）
- 路由：模型 → 多部署映射，round-robin / weighted / least-latency 负载均衡，优先级分层（priority tier）
- 可靠性：超时、指数退避重试、部署间故障转移、模型级降级链（fallbacks）、
  连续失败冷却熔断（cooldown，借鉴 LiteLLM）、流式首块前可无感切换
- 流量治理：认证（虚拟 key + 模型白名单）、限流（token bucket，模型级/身份级 RPM）
- 缓存：精确匹配响应缓存（TTL + 容量淘汰）
- 可观测性：Prometheus 指标（请求/延迟/TTFT/token/插件拒绝/缓存命中/上游尝试）、
  结构化 JSON 请求日志（轮转）、`/healthz` `/readyz` `/metrics` `/admin/routes` `/admin/plugins`
- 运维：YAML 配置 + **热重载**（路由/插件分钟级生效，不动连接池）

**插件式可插拔能力**

| 插件 | 说明 |
|---|---|
| `keyword-filter-plugin` | 关键词检测过滤：block / mask 双模式；请求与响应双向；**流式跨块安全**（安全前缀切分算法） |
| `auth-plugin` | Bearer 虚拟 key（明文/SHA-256），模型白名单 |
| `rate-limit-plugin` | token bucket 限流（模型级 / 身份级） |
| `response-cache-plugin` | 精确匹配缓存 |
| `request-log-plugin` | 每请求 JSON 日志（文件轮转 / stdout） |

- 插件链按 `order` 排序执行；钩子点：`on_request / on_response / on_stream_start / on_stream_chunk / on_stream_end / on_error`
- **第三方插件零侵入**：外部 Python 模块即可注册（配置 `module`/`class`，或 `@register_plugin` 装饰器）
- 同一插件可多实例（不同词表/模式），配置 `plugin:` 别名

## 快速开始

```bash
# Python 3.12 虚拟环境（uv）
uv venv --python 3.12 .venv
uv pip install -e ".[dev]" -p .venv/Scripts/python.exe   # Linux/macOS 用 .venv/bin/python

# 用本地 Ollama 启动网关（假设 ollama 已在 11434 并拉取了模型）
.venv/Scripts/python -m aigateway --config gateway.ollama.yaml --port 8080

# 发请求（OpenAI 兼容）
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-e2e-demo-key" \
  -d '{"model":"glm4:9b","messages":[{"role":"user","content":"你好"}]}'
```

配置示例见 [gateway.example.yaml](gateway.example.yaml)（含注释）。

## 架构

```
client ──► FastAPI (api.py)
              │
              ▼
         GatewayApp (app.py)            编排 + 计量 + 热重载
    ┌───────┼────────┬─────────────┐
    ▼       ▼        ▼             ▼
 PluginChain Router   Dispatcher  Metrics
     │        │          │
  插件链    模型→部署    重试/退避/故障转移/降级/冷却
```

详细设计（含与各主流网关的决策对照、失败模型、流式掩码算法）：
[docs/research-summary.md](docs/research-summary.md)。

## 测试与基准

```bash
.venv/Scripts/python -m pytest tests/ -q        # 80 个单元/集成测试
.venv/Scripts/python -m ruff check src tests    # lint
.venv/Scripts/python benchmarks/bench_gateway.py  # 基准 → docs/benchmark-report.md
```

基准结果（零延迟 mock 上游，测网关自身开销）：单请求 p50 **0.44ms**（~2100 RPS），
全插件链 +0.06ms，流式 TTFT 开销 +0.03ms，冷却稳态与基线持平。见
[docs/benchmark-report.md](docs/benchmark-report.md)。

## Ollama 端到端验证

```bash
# 1) 安装 Ollama 并准备 9B 模型（GLM-4-9B-Chat Q4_K_S GGUF）
scripts/setup_ollama.ps1          # 或手动：ollama create glm4:9b -f Modelfile

# 2) 跑 11 项端到端验证（真实模型，覆盖全部网关能力）
.venv/Scripts/python scripts/validate_e2e.py
```

验证矩阵：模型列表、非流式、流式+TTFT、认证、关键词拦截/掩码、缓存命中、
限流 429、死上游故障转移、Prometheus 指标、并发突发。结果写入
[docs/e2e-ollama-validation.md](docs/e2e-ollama-validation.md)。

## 项目结构

```
src/aigateway/
  api.py            FastAPI 适配层（SSE、错误映射、运维端点）
  app.py            GatewayApp 编排（插件链→路由→调度→插件链→计量）
  config.py         YAML 配置、构建器、热重载 watcher
  schemas.py        OpenAI 兼容请求 schema 与文本抽取工具
  errors.py         OpenAI 风格错误体系
  core/
    provider.py     Provider 抽象（chat / stream / health）
    openai_provider.py   OpenAI 协议 HTTP 上游
    mock_provider.py     确定性 mock（测试/基准）
    router.py       路由、负载均衡、冷却、优先级分层
    dispatcher.py   重试/退避/故障转移/降级链/流式切换
  plugins/
    base.py         Plugin 接口、PluginChain、注册表、外部加载
    keyword_filter.py / auth.py / rate_limit.py / response_cache.py / request_log.py
  observability/metrics.py   Prometheus 指标
tests/              80 tests（含 HTTP 端到端）
benchmarks/         基准测试
scripts/            并行下载器、Ollama 安装、E2E 验证
docs/               调研（2 篇 + 综合）、基准报告、E2E 报告
```

## License

MIT
