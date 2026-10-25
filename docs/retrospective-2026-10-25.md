# 项目复盘：调研 → 蓝图 → M1（2026-10-25）

> 时点复盘，覆盖 aigateway 从行业调研、蓝图制定到 M1 交付的完整循环。
> 数据基准：251 tests 全绿（32s）、基准 p50 0.438ms、CI 绿、仓库 8 个 commit。

## 1. 历程（三阶段一天完成）

| 阶段 | 产出 | 关键节点 |
|---|---|---|
| 深度调研 | docs/research-{litellm-etc,higress-etc,summary}.md + 2026-10 行业格局补充 | 8+ 主流网关横评（LiteLLM/Portkey/Kong/Cloudflare/Higress/APISIX/Azure APIM/MCP-Agent 网关趋势），归纳八大能力域 |
| 项目蓝图 | docs/BLUEPRINT.md | 定位与 non-goals、现状差距矩阵（✅/🟡/❌）、M1-M6 路线图、横切原则（插件优先/默认关闭/性能门槛 <10%）、风险表 |
| M1 迭代 | `1ac79f5`（12 文件，+1168/-193） | token 周期配额、key 生命周期（expires_at/concurrency/metadata）、per-key 成本指标、Admin API v1（动态 key CRUD + usage） |

## 2. 当前状态快照

- **能力面**：双协议入口（OpenAI + Anthropic Messages，tools/流式翻译）、embeddings、
  4 类原生上游适配（anthropic/gemini/bedrock + SigV4）、路由 LB/优先级分层/降级链/冷却熔断、
  请求级+token 级限流、日/月美元预算硬限、token 周期配额、key 生命周期与动态 key、
  成本归集（模型级 + key 级）、精确缓存、5 内置插件 + 零侵入外部插件、
  Prometheus 全套指标、Admin API、YAML 热重载、Docker 全家桶、CI。
- **质量面**：251 tests 全绿、ruff 干净、基准 p50 0.438ms（~2100 RPS，全插件 +0.06ms）、
  混沌注入 >97% 成功率零挂起、soak 无泄漏、真实模型 E2E 11 项通过。
- **对齐蓝图**：M1 ✅ 已交付（含验收测试）；M2 已吸收两条 operations.md 已知边界
  （跨副本精确缓存、原生适配器 embeddings）。

## 3. 做得好的（Keep）

1. **蓝图先行且基线校准**：制定蓝图时发现并行会话已交付预算硬限/分层定价/token 估算器，
   立即修正 M1 范围为"补齐租户治理"——没有重复规划已存在的能力。
2. **复用优先**：token 配额直接复用通用 spend 计数器（`tokquota:` 命名空间隔离）；
   动态 key 走 state backend 覆盖层而非新存储；并发释放放在 GatewayApp 层，
   用 ctx 标志位做到错误/断连路径零泄漏——插件钩子覆盖不了流式 GeneratorExit。
3. **兼容性纪律**：per-key 成本用新增 `gateway_key_cost_usd_total` 而非给既有指标加 label，
   既有面板与 5 处测试断言零破坏（偏离蓝图原写法，已在蓝图注明理由）。
4. **测试抓到真 bug**：动态 key 重名未查重（`test_admin_duplicate_name_rejected`）——
   先写用例后修实现，测试不是形式。
5. **性能门槛执行**：M1 交付前跑 quick 基准，p50 0.438ms 与基线 0.44ms 持平才算完成。

## 4. 问题与改进（Problem → Try）

1. **并行会话协同风险**：README 在读取后被外部修改导致 Edit 冲突。
   → Try：动文件前先重读（已记入项目记忆）；重要文档改动前 `git status` 确认。
2. **测试运行假象**：`pytest | tail` 管道缓冲造成两次"疑似挂起"误判（实际全量仅 32s）。
   → Try：Windows 上判断测试进度一律用 `-u --capture=no` 直出，管道只用于取最终摘要。
3. **网络型用例偏慢**：fail-open/fail-closed 类测试依赖真实连接超时（2-6s/个），
   全量里占比最大。
   → Try：后续给这类测试注入短超时 transport，可把全量再压缩一半。
4. **GitHub 连接不稳**：push/fetch 偶发 connection reset（已知，需重试）。
   → 现状接受；push 失败一律重试直至服务器确认。

## 5. 下一步（按蓝图）

- **M2 语义缓存**：复用 `/v1/embeddings` 管道 + 跨副本精确缓存 + 原生适配器 embed 能力；
- M3 安全护栏插件组（PII 脱敏/注入检测/webhook 审核）；
- M4 可观测深化（OTel/Langfuse 导出、per-key 报表面板）；
- M5 提示词模板 + 灰度切流；v1.0 GA 稳定化。
