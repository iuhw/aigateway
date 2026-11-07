# aigateway 配置参考

> YAML 配置，改动即热重载（默认 5s 轮询；改坏自动保留旧状态）。示例见
> [gateway.example.yaml](../gateway.example.yaml)。**v1 起 YAML 向后兼容承诺**：
> 旧配置永续可用，字段只会新增或以新字段取代（弃用字段保留至少一个大版本），见
> [升级说明](upgrading.md)。

## server

| 字段 | 默认 | 说明 |
|---|---|---|
| `host` / `port` | 0.0.0.0 / 8080 | 监听地址（CLI 可覆盖） |
| `admin_api_key` | 无 | 保护 `/metrics` 与 `/admin/*`（Bearer 或裸值，常量时间比较）。**未设置 = 开发模式，管理面全开放** |
| `max_body_bytes` | 10485760 | 请求体上限（413；硬上限 64MB） |
| `max_messages` / `max_message_chars` | 128 / 200000 | 消息条数 / 总字符准入控制（embeddings 同口径） |
| `max_tracked_keys` | 100 | `gateway_key_cost_usd_total` 的 key 标签基数上限，超限聚合 `other` |
| `tokenizer` / `tokenizer_model` | unicode / gpt-4o-mini | 估算器：`unicode`（CJK 感知启发式）或 `tiktoken`（需 `[tokenizer]` extra，离线自动回退） |

## providers

`type` 五选一：

| type | 必填 | 常用可选 |
|---|---|---|
| `openai` | `base_url`, `api_key` | `max_connections`（100）、`extra_headers`、`supports_stream_usage_option` |
| `anthropic` | `api_key` | `base_url`、`api_version`、`default_max_tokens`（4096） |
| `gemini` | `api_key` | `base_url`、`api_version`（v1beta） |
| `bedrock` | `region` | `access_key_id`/`secret_access_key`/`session_token`（缺省走 AWS 环境变量）、`base_url`、`default_max_tokens` |
| `mock` | — | `latency_s`、`content_template`、`fail_status`/`fail_next_n`/`failure_rate`、`chunk_count`、`healthy`（测试/基准） |

`api_key` 支持 `${ENV_VAR}` 展开。openai 类型覆盖一切 OpenAI 协议上游（OpenAI/Ollama/vLLM/
DeepSeek/Groq/LM Studio…）；三家原生适配器做双向协议翻译（chat + tools + embeddings，
Anthropic 无 embeddings 端点、按传输故障切换部署）。

## models（路由）

```yaml
models:
  <公开模型名>:
    deployments:
      - {provider: p1, model: 上游模型, weight: 1, priority: 0}
    policy: {timeout_s: 60, retries: 2, retry_backoff_s: 0.5, cooldown_s: 30,
             allowed_fails: 2, strategy: round_robin, failover_on_client_error: false}
    fallbacks: [另一公开模型]        # 全部署耗尽后整链降级
    canary:                          # 可选，灰度切流（M5，opt-in）
      target: 另一公开模型           # 必填，不能是自身；必须是已配置路由
      percent: 10                    # 0-100，身份哈希 1 万桶粘性分流
      keys: [beta-testers]           # 可选：强制进 canary 的虚拟 key 名
      exclude_keys: [vip]            # 可选：强制留在 stable（优先于 keys）
      salt: "round-1"                # 可选：重排桶分配而不移动比例
```

- 策略语义：低 `priority` 优先（分层），层内按 `strategy` 负载均衡（round_robin /
  weighted / least_latency）；429/5xx/超时重试退避后跨部署切换；其他 4xx 快速失败。
- canary 在插件链之后解析（需认证身份做粘性哈希；匿名按请求随机）。限流与模型白名单按
  **请求模型**计；指标/定价/报表/日志按**生效模型**计（日志另有 `requested_model` 字段）。
  指标：`gateway_canary_requests_total{model, branch=canary|stable}`。
  详见 operations.md 边界 19。

## plugins

全部 opt-in，`order` 越小越先执行（同名类可配 `plugin:` 别名多实例；`enabled: false` 停用）。
第三方插件用 `module`/`class` 引入。字段速查（详见各插件模块 docstring 与
[gateway.example.yaml](../gateway.example.yaml) 注释）：

| 插件 | 关键配置 |
|---|---|
| `auth-plugin` | `header`、`allow_anonymous`、`api_keys[]`（key 支持 `sha256:`；models 白名单/rpm/budgets/token_quota/expires_at/concurrency/metadata）、`budget_fail_open`、`dynamic_key_cache_s` |
| `keyword-filter-plugin` | `words[]`、`mode: block\|mask`、`check: [request, response]`、`block_message` |
| `rate-limit-plugin` | `default_rpm`、`model_rpm{}`、`identity_rpm`、`default_token_rpm`/`token_rpm{}`/`identity_token_rpm`（token 级预扣+多退少补）、`assumed_max_tokens`、`capacity`、`fail_open` |
| `response-cache-plugin` | `ttl_s`、`max_entries`、`shared`（跨副本，需共享后端） |
| `semantic-cache-plugin` | `embed_model`（必填，走网关 embeddings 管道）、`similarity_threshold`（0.95）、`ttl_s`、`max_entries`、`backend: memory\|shared` |
| `request-log-plugin` | `path`（轮转文件）、`stdout`、`log_prompt`/`log_response`（**默认 false**）、`max_body_chars`、`max_bytes`、`backup_count` |
| `pii-redaction-plugin` | `groups[]`（phone/email/id_card/bank_card）、`custom_patterns[]`、`action: mask\|block`、`check[]`、`holdback_chars`（128） |
| `prompt-guard-plugin` | `action: flag\|block`（默认 flag 观察）、`threshold`（1.0）、`weights{}`、`custom_patterns[]`、`check[]` |
| `webhook-review-plugin` | `url`、`headers`、`timeout_s`、`on_unavailable: fail-open\|fail-close`、`check[]`、`max_text_chars` |
| `prompt-template-plugin` | `templates[]`：`models[]`（fnmatch）/`keys[]`/`metadata{}` 匹配 + `system` 模板 + `mode: prepend\|append\|replace` + `variables{}` + `header_variables[]`（白名单） |
| `usage-report-plugin` | `max_records`（50000 环形）、`retention_hours`（24） |
| `otel-export-plugin` | `endpoint`、`headers`、`timeout_s`、`sample_ratio`、`service_name`、`resource{}`（需 `[otel]` extra） |
| `langfuse-export-plugin` | `host`/`public_key`/`secret_key` + 同 otel 全部字段（Langfuse preset） |

## pricing

```yaml
pricing:
  <模型名>:
    input_per_1k: 0.00015
    output_per_1k: 0.0006
    cache_read_per_1k: 0.000075   # 缺省回退 input 价（保守）
    cache_write_per_1k: 0.00015
```

驱动成本指标（全局/per-key）、预算硬限、token 配额与用量报表；缓存命中不计费。

## shared_state

```yaml
shared_state:
  backend: redis          # memory（默认，单进程语义）
  redis_url: redis://127.0.0.1:6379/0
  key_prefix: aigw
  fail_open: true         # Redis 故障时限流/账本放行（可用性优先）
```

承载：分布式限流、预算/token 配额账本、跨副本冷却同步、跨副本精确缓存（`shared: true`）、
语义缓存共享后端、动态 key 存储。多实例部署必配。
