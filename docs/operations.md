# aigateway 运维手册（生产部署）

> 本文回答三个问题：怎么部署、承诺什么 SLO、出事了怎么办。
> 配套交付物：`Dockerfile`、`docker-compose.yml`、`deploy/`（生产配置、Prometheus、Grafana）、`.github/workflows/ci.yml`。

## 1. 部署拓扑

```
                ┌──────────────┐
 clients ──────►│   LB / TLS   │  TLS 终结在入口层（网关本身不做 TLS）
                └──────┬───────┘
             ┌─────────┴─────────┐
             ▼                   ▼
        ┌─────────┐         ┌─────────┐      ┌─────────┐
        │ gateway │         │ gateway │ ...  │ gateway │   N 个无状态副本
        └────┬────┘         └────┬────┘      └────┬────┘
             │    共享限流/冷却    │                │
             └────────┬──────────┴────────────────┘
                      ▼
                 ┌─────────┐                    ┌──────────┐
                 │  Redis  │                    │  Ollama  │  模型上游
                 └─────────┘                    └──────────┘
                      │  指标
                      ▼
              Prometheus → Grafana（仪表盘已内置 deploy/grafana-dashboard.json）
```

- **副本**：`docker compose up -d` 默认 2 副本（`deploy.replicas` 可调）。网关进程无本地状态，可任意扩缩。
- **共享状态**：所有副本必须指向同一 Redis（`shared_state.redis_url`）。Redis 挂了的行为见 §4。
- **配置下发**：改 YAML 文件即可（`ConfigWatcher` 每 5s 轮询热重载；路由/插件/上游全部生效，连接池仅在 providers 段变化时重建）。上线前用 `aigateway check <config>` 校验。
- **密钥管理**：虚拟 key 支持 `sha256:<hex>` 形态存储；生产配置示例（`deploy/gateway.prod.yaml`）默认只放哈希。轮换 = 换配置 + 热重载。

## 2. SLO（基于本仓库的量化验证）

| SLO | 目标 | 验证方法 | 结果 |
|---|---|---|---|
| 网关附加开销（p99，非流式） | < 10 ms | `scripts/load_test.py`（网关臂 vs 直连臂差分） | -4.4 ms ✅ |
| 流式 TTFT 附加开销（p50） | < 5 ms | 同上 | 1.4 ms ✅ |
| 零延迟下网关自身 p50 | —（观测值） | `benchmarks/bench_gateway.py` | 0.44 ms |
| 混沌下无挂起请求 | 100% 请求有终态 | `tests/test_chaos.py`（30% 故障率，硬超时） | 通过 ✅ |
| 故障注入下成功率 | > 97% | 同上 | 通过 ✅ |
| 耐久（泄漏） | RSS < 2 MB/min，句柄不涨 | `scripts/soak_test.py` | +0.021 MB/min ✅ |
| 可用性 | 99.9%（排除上游故障） | 上游健康由冷却+降级吸收；需要多副本 + LB | 拓扑就绪，需在生产流量下复测 |

**多副本限流语义**：Redis 固定窗口（60s 窗口，原子 INCR）。窗口边界处瞬时突发可达 2×rpm——这是无锁分布式限流的标准取舍（与 Kong 相同）；内存后端为令牌桶。两后端语义差异已文档化于 `state.py`。

## 3. 监控与告警（Grafana 仪表盘已提供全部指标）

关键指标 → 建议告警阈值：

| 指标 | 告警条件 | 含义 |
|---|---|---|
| `gateway_requests_total{status=~"5.."}` | 5xx/min > 5 持续 5 分钟 | 网关/上游故障（4xx 是调用方问题，不告警） |
| `gateway_state_backend_errors_total` | > 0/min 持续 5 分钟 | Redis 不可达（限流已 fail-open，正在裸奔） |
| `gateway_rate_limit_rejections_total` | 突增 10x | 疑似攻击或客户端 bug |
| `gateway_upstream_attempts_total{outcome=...}` | failure 率 > 20% 持续 10 分钟 | 上游劣化，观察冷却/降级是否吸收 |
| `gateway_plugin_rejections_total` | 突增 | 关键词过滤命中激增（业务事件） |
| `gateway_cache_requests_total` hit ratio | 突降 | 缓存失效模式变化（可接受） |

## 4. Runbook（常见事故）

**R1：Redis 不可达**
- 现象：`gateway_state_backend_errors_total` 上涨；限流退化为各副本独立计数（fail-open，不拒绝请求）。
- 处置：恢复 Redis 即可（自动重连）。若需严格限流而 Redis 短期无法恢复，将 `rate-limit-plugin.config.fail_open` 改为 `false`（拒绝而非放行）并热重载。

**R2：某上游持续 5xx/超时**
- 现象：`gateway_upstream_attempts_total{outcome="error"}` 上涨；`/admin/routes` 显示部署进入冷却。
- 处置：网关自动冷却（`allowed_fails` 次 → `cooldown_s`）并切换同路由其他部署 / fallback 模型。修复上游后冷却自动过期。若是配置错误（404/401 等 4xx），默认**不会**转移到其他部署——修复配置并热重载，或临时设置该路由 `failover_on_client_error: true`。

**R3：模型输出被关键词过滤拦截**
- 现象：400 `content_filter_rejected`，`gateway_plugin_rejections_total{plugin=...}` 上涨。
- 处置：确认词表（`/admin/plugins` 看启用实例）；流式 block 模式无法撤回已发出的块——对延迟敏感的场景建议把检测前置到 `check: [request]`。

**R4：配置改坏导致网关异常**
- 防线：热重载物化失败时**自动保留旧状态**（有测试覆盖）；启动前跑 `aigateway check`。
- 处置：恢复上一版配置文件 → 5s 内自动回滚生效。

**R5：某副本僵死**
- 处置：直接重启该副本（无状态）。LB 摘除后其余副本无缝接管；进行中的流式请求会中断（客户端应实现重试，标准 SSE 客户端行为）。

**R6：上游 429 风暴**
- 行为：429 属可重试状态 → 同部署退避重试 → 冷却 → 切换部署/降级链。
- 处置：若多上游同时 429，说明配额整体不足——调低 `model_rpm`/`identity_rpm`（热重载生效）或提升上游配额。

## 5. 已知边界（生产使用须知）

1. **token 级限流**基于估算预扣 + 完成后按上游实际用量多退少补；估算默认 Unicode 感知启发式
   （对 o200k_base 校准，聚合误差 ~±20%；短高频词可能高估 ~2x）。可配 `server.tokenizer: tiktoken`
   精确计数（离线安装自动回退启发式）。窗口边界处固定窗口允许最多 2x 突发（与 Kong 相同取舍）。
2. **Anthropic 兼容面**：`/v1/messages` 支持 system/text blocks/stop_sequences/temperature/
   top_p/max_tokens、完整流式事件与 **tools**（定义、tool_choice、tool_use/tool_result 块、
   流式 tool 参数事件）；图片与 citations 明确拒绝（400）。
3. **单机网络栈**：SO_REUSEPORT 多进程 / k8s 多 Pod 均可，但网关不提供 TLS——在入口层终结。
4. **流式 block 过滤的窗口**：检测到关键词前的块已发出（与 Higress 同类插件的已知取舍）。
5. **上游协议面**：OpenAI 协议（openai）+ 原生适配器 anthropic / gemini / bedrock（Converse，
   SigV4 纯标准库实现）。适配器覆盖 chat + tools（含流式）；**embeddings 仅 openai 类型支持**——
   不支持的部署按传输故障语义处理，调度器自动切换到支持 embeddings 的部署。
6. **缓存为单键精确匹配**（进程内——同副本重复请求才命中；跨副本响应缓存在路线图）。
7. **成本追踪**按 `pricing` 配置的单价计算，支持 cache-read/write 分层价（缺省回退 input 价）。
   预算硬限是 check-then-bill 语义：预算耗尽瞬间在途请求可能小幅超出；`usage` 缺失时按估算
   token 计价，估算口径下成本同样为近似值。账本随重启/热重载重建（Redis 后端除外）。
8. Windows 上热重载受目录元数据缓存影响可能延迟数秒生效（Linux 即时）。

## 6. 升级与回滚

- 镜像：`docker build` → CI 通过后推送 → 滚动替换副本（无状态，随时滚动）。
- 配置：`aigateway check` → 替换文件 → 5s 自动生效；改坏则放回旧文件即回滚。
- 依赖：`uv.lock` 锁定，镜像内 `uv sync --frozen` 保证可复现构建。
