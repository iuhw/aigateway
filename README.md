# aigateway

生产级可扩展 AI 网关（Python 3.12）—— OpenAI 兼容统一入口 + 插件式可插拔能力。

参考 LiteLLM / Higress AI Gateway / Kong AI Gateway / Portkey / Cloudflare AI Gateway /
Envoy AI Gateway 的成熟设计自主实现（调研见 [docs/](docs/)）。

## 核心能力

**网关级**

- **双协议入口**：OpenAI 兼容 `POST /v1/chat/completions` + **Anthropic Messages API `POST /v1/messages`**
  （非流式 + 流式事件翻译，Claude SDK 可直接接入；支持 **tools**（定义/tool_choice/tool_use/tool_result 块
  与流式 `input_json_delta` 事件翻译）；另有 **`POST /v1/embeddings`** 走同一套认证/路由/故障转移管道）
- 多上游：一切 OpenAI 协议服务（OpenAI / Ollama / vLLM / DeepSeek / Groq / LM Studio …），
  以及 **原生协议适配器**（`type: anthropic` / `type: gemini` / `type: bedrock`，含 SigV4 签名）——
  同一公开模型可跨厂商组合部署与降级
- 路由：模型 → 多部署映射，round-robin / weighted / least-latency 负载均衡，优先级分层（priority tier）
- 可靠性：超时、指数退避重试、部署间故障转移、模型级降级链（fallbacks）、
  连续失败冷却熔断（cooldown，借鉴 LiteLLM）、流式首块前可无感切换
- **多实例共享状态**：Redis 后端的分布式限流（原子固定窗口，fail-open 可配）与跨副本冷却同步；
  单进程默认内存后端，语义等价（见 `state.py` 文档）
- 流量治理：认证（虚拟 key + 模型白名单）、限流（**请求级 + token 级** RPM：预扣估算、
  按上游实际用量多退少补）、请求体/消息数/消息长度上限（413 拒绝）、`/metrics` `/admin/*` 独立管理密钥
- **成本控制**：`pricing` 每模型单价（支持 **分层 cache-read/cache-write 价**）→
  `gateway_cost_usd_total` 按模型累计；虚拟 key 可设 **日/月美元预算硬限**（超限 429
  `budget_exceeded`，账本支持 Redis 跨实例共享）
- **token 计量**：Unicode 感知启发式估算（中英文混合语料对 o200k_base 校准，聚合误差 ~±20%；
  旧版平摊 4 字符/token 对中文低估 ~3.6 倍）；可选 `tokenizer: tiktoken` 精确计数
- 缓存：精确匹配响应缓存（TTL + 容量淘汰）
- 可观测性：Prometheus 指标（请求/延迟/TTFT/token/插件拒绝/缓存命中/上游尝试/后端错误/成本）、
  结构化 JSON 请求日志（轮转）、`/healthz` `/readyz` `/metrics` `/admin/routes` `/admin/plugins`
- 运维：YAML 配置 + **热重载**（改坏自动保留旧状态）、`aigateway check` 配置校验 CLI、
  Dockerfile + docker-compose（含 Redis/Ollama/Prometheus/Grafana 全家桶）

**插件式可插拔能力**

| 插件 | 说明 |
|---|---|
| `keyword-filter-plugin` | 关键词检测过滤：block / mask 双模式；请求与响应双向；**流式跨块安全**（安全前缀切分算法） |
| `auth-plugin` | Bearer 虚拟 key（明文/SHA-256），模型白名单，**日/月美元预算硬限** |
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
.venv/Scripts/python -m pytest tests/ -q        # 226 个测试（含故障注入/混沌/多实例/安全/热重载/停机/tools/embeddings/预算/分层定价）
.venv/Scripts/python -m ruff check src tests    # lint
.venv/Scripts/python benchmarks/bench_gateway.py  # 基准 → docs/benchmark-report.md

# 生产验证三件套
.venv/Scripts/python scripts/load_test.py     # 真实HTTP双臂负载测试 + SLO 断言 → docs/load-report.md
.venv/Scripts/python scripts/soak_test.py --minutes 10   # 耐久/泄漏检测 → docs/soak-report.md
.venv/Scripts/python -m aigateway check gateway.prod.yaml  # 配置校验
```

量化结论（报告见 docs/）：

- **网关自身开销** p50 0.44ms（~2100 RPS 单进程），全插件链 +0.06ms，流式 TTFT 开销 +0.03ms
- **真实 HTTP 双臂对测**：非流式开销 p99 -4.4ms（与直连同水平）、TTFT 开销 p50 1.4ms，错误率 0%
- **混沌注入**（30% 随机故障+延迟，300 请求）：零挂起、100% 终态映射、>97% 成功率
- **耐久**：5 分钟 @400 RPS 零错误，RSS 斜率 +0.021 MB/min（无泄漏迹象）

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

## 生产部署

```bash
# 一键拉起 gateway(x2) + redis + ollama + prometheus + grafana
docker compose up -d
docker compose exec ollama ollama pull glm4:9b

# 或裸机运行（先 aigateway check 校验）
aigateway serve --config gateway.prod.yaml --port 8080
```

- SLO 与验证证据：[docs/operations.md](docs/operations.md) §2
- 告警规则与 runbook（Redis 故障、上游劣化、配置回滚等 6 类场景）：[docs/operations.md](docs/operations.md) §3-4
- 已知生产边界（token 级限流、TLS 终结点、流式过滤窗口等）：[docs/operations.md](docs/operations.md) §5
- CI（GitHub Actions）：lint + 226 测试 + 基准 smoke + Docker 构建/健康检查

## 项目结构

```
src/aigateway/
  api.py            FastAPI 适配层（OpenAI + Anthropic 双协议、embeddings、错误映射、请求上限、运维端点）
  anthropic_api.py  Anthropic Messages 协议翻译（请求/响应/流式事件/错误信封）
  app.py            GatewayApp 编排（插件链→路由→调度→插件链→计量→成本）
  config.py         YAML 配置、构建器、热重载 watcher（纳秒 mtime 防抖）
  state.py          共享状态后端：InMemory | Redis（分布式限流+冷却同步）
  schemas.py        OpenAI 兼容请求 schema 与文本抽取工具
  tokenizers.py     token 估算（Unicode 启发式 / 可选 tiktoken）
  errors.py         OpenAI 风格错误体系
  core/
    provider.py     Provider 抽象（chat / stream / embed / health）
    openai_provider.py   OpenAI 协议 HTTP 上游（可注入 transport 供故障注入）
    anthropic_provider.py  Anthropic Messages 原生上游（双向协议翻译）
    gemini_provider.py     Gemini generateContent 原生上游
    bedrock_provider.py    AWS Bedrock Converse 原生上游
    sigv4.py            AWS SigV4 请求签名（纯标准库，botocore 交叉验证）
    mock_provider.py     确定性 mock（测试/基准）
    router.py       路由、负载均衡、冷却、优先级分层、冷却导入导出
    dispatcher.py   重试/退避/故障转移/降级链/流式切换
  plugins/
    base.py         Plugin 接口、PluginChain、注册表、外部加载
    keyword_filter.py / auth.py / rate_limit.py / response_cache.py / request_log.py
  observability/metrics.py   Prometheus 指标（含成本）
tests/              226 tests（单元/集成/wire 故障注入/混沌/多实例/安全/热重载/停机/Anthropic/tools/embeddings/原生适配器/预算/定价）
benchmarks/         基准测试（--quick 供 CI）
scripts/            负载测试、soak 耐久、并行下载器、Ollama 安装、E2E 验证（13 项）
deploy/             生产配置、Prometheus 抓取、Grafana 仪表盘与数据源
docs/               调研（3 篇）、基准/负载/soak/E2E 报告、运维手册
```

## License

MIT
