# aigateway 部署手册

> 适用版本：v1.0+。配置字段详解见[配置参考](configuration.md)，升级路径见[升级说明](upgrading.md)，
> 插件开发见[插件开发指南](plugin-development.md)。量化依据见 [operations.md](operations.md) 与各报告。

## 1. 部署形态

### 单进程（开发 / 小流量）

```bash
pip install 'aigateway[redis]'        # 生产建议带 Redis extra
aigateway --config gateway.yaml --host 0.0.0.0 --port 8080
```

单进程 Python 3.12 + uvicorn。实测网关自身开销 p50 ~0.44ms（~2100 RPS，零延迟 mock 上游口径）；
真实大模型请求的瓶颈在上游推理延迟，网关开销可忽略（双臂负载对测：开销 p99 与直连同水平）。

### 多 worker / 多副本（生产）

- **多进程 worker**：`--workers N`（或 k8s 多 Pod）。限流/预算/冷却等状态在进程内时各 worker 独立，
  生产多实例**必须配置 Redis 共享状态**（`shared_state.backend: redis`）——分布式限流（原子固定窗口）、
  跨副本冷却同步、跨副本动态 key、跨副本精确/语义缓存。
- **入口层终结 TLS**：网关自身不提供 TLS（设计边界）。在 nginx/Envoy/云 LB 终结，转发到 8080。
- **副本数与容量**：单进程 ~2100 RPS（mock 口径）足以覆盖大多数大模型上游的吞吐上限；
  横向扩副本即可线性扩展网关层。

### Docker

```bash
docker compose up -d        # 网关 + Redis + (可选) Ollama/Prometheus/Grafana
curl http://127.0.0.1:8080/healthz
```

镜像内健康检查用 `/healthz`；编排器就绪探针用 `/readyz`（任一 provider 健康即 ready）。

## 2. 生产配置清单（checklist）

上生产前逐项确认（字段详见[配置参考](configuration.md)）：

- [ ] `server.admin_api_key` 已设置——**未设置时 /metrics 与 /admin/* 完全开放（仅限开发）**，
      启动日志会有 UNPROTECTED 告警；
- [ ] `auth-plugin` 的 `allow_anonymous: false`；静态 key 用 `sha256:<hex>` 形式或
      `POST /admin/keys` 动态下发（明文只出现一次，存储只保留哈希）；
- [ ] `server.max_body_bytes` / `max_messages` / `max_message_chars` 按业务收紧（413 防护）；
- [ ] `server.max_tracked_keys` 设置 per-key 成本指标基数上限（默认 100，超限聚合 `other`）；
- [ ] `shared_state` 指向 Redis（多实例必需；内存后端仅单进程语义）；
- [ ] 上游 `api_key` 用环境变量注入（`${OPENAI_API_KEY}`），不落仓库；
- [ ] 请求日志 `request-log-plugin` 保持 `log_prompt/log_response` 默认关闭（内容脱敏默认值），
      打开需有合规依据；
- [ ] 语义缓存/护栏/灰度等有正确性权衡的能力保持 opt-in，逐项评审后显式开启；
- [ ] `pricing` 配置成本单价（成本指标、预算硬限、报表都依赖它）。

## 3. 安全基线（v1.0 自查结论）

- **虚拟 key 存储**：静态 key 支持 `sha256:<hex>`；解析后内存只保留摘要（明文不常驻，
  转储/堆栈不可泄漏）；动态 key（`/admin/keys`）只存 SHA-256，原始 key 创建时一次性返回。
- **管理面**：`/metrics` 与 `/admin/*` 共用 `server.admin_api_key`（常量时间比较）。
- **模型枚举**：配置 auth-plugin 后 `GET /v1/models` 需要有效虚拟 key（与 OpenAI API 一致），
  未认证枚举已关闭；无 auth-plugin 时保持开放。
- **日志**：请求日志默认不含 prompt/响应内容（opt-in）；OTel span 只含元数据；
  报表只含元数据。结构化错误不回显 key 材料。
- **失败开放面**：共享后端故障时限流 fail-open 默认开（可用性优先）、预算 fail-open 可配
  （`budget_fail_open`）——两者的取舍见 operations.md；需要严格口径时显式 fail-close。

## 4. 监控与告警

- Prometheus 抓取 `/metrics`（`deploy/prometheus.yml`）；Grafana 仪表盘
  `deploy/grafana-dashboard.json`（请求/延迟/TTFT/token/成本（全局与 per-key）/缓存命中/
  语义命中/护栏 verdict/上游尝试/canary 分流/插件拒绝）。
- 建议告警：5xx 比例、p99 延迟、`gateway_rate_limit_rejections_total` 突增、
  `gateway_state_backend_errors_total`（共享后端故障）、`gateway_upstream_attempts_total`
  失败比例、`gateway_plugin_rejections_total` 突增。
- 请求日志 `logs/requests.jsonl`（轮转），`aigateway check <config>` 做配置校验。

## 5. 升级与回滚

- 配置热重载：改 `gateway.yaml` 即生效（默认 5s 轮询），改坏自动保留旧状态并打日志；
  providers 段未变时连接池复用。
- 版本升级：见[升级说明](upgrading.md)（v1 起 YAML 配置向后兼容承诺）。
- 回滚：容器镜像回退即可；动态 key 存于 Redis，跨版本兼容（记录含 `source`/`created_at`）。

## 6. 容量与 SLO 速查

| 指标 | 数值 | 口径 |
|---|---|---|
| 网关自身开销 | p50 ~0.44ms | 零延迟 mock 上游，ASGI 直连 |
| 全插件链（11 插件）增量 | +0.20ms 量级（逐里程碑 <10% 门槛，见基准报告） | 同上 |
| 吞吐 | ~2100 RPS 单进程 | 并发 8-64 |
| 混沌（30% 故障+延迟） | >97% 成功率、零挂起 | 300 请求 |
| soak | 60min @300RPS 零错误、RSS 平稳（GA 验收） | 全插件链 |
