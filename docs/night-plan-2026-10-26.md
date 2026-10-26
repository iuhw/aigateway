# 夜间迭代方案：M2 (v0.3) 语义缓存（2026-10-26 23:00 启动）

> 本文档是夜间自主迭代的执行依据。目标：按 [BLUEPRINT.md](BLUEPRINT.md) 完成 M2 全部四项交付，
> 每阶段测试全绿后 commit 并推送双远端。执行日志见 `night-log-2026-10-26.md`（执行时创建）。

## 1. 范围与目标

**做什么**：M2 语义缓存四件套——

1. 原生适配器 embeddings（gemini/bedrock 补 `embed()`；anthropic 视上游支持情况）；
2. 跨副本精确缓存（`response_cache` 存储抽象为 InMemory/Redis 双后端）；
3. `semantic-cache-plugin`（复用 `/v1/embeddings` 管道，cosine 阈值检索，默认关闭）；
4. 验收测试集 + 基准回归（p50 增幅 <10%、命中路径 <1ms）。

**不做什么**：不碰路由/调度/计量 core 语义；不改既有指标名与 YAML 字段含义；
不做语义缓存的向量索引优化（暴力检索起步，蓝图既有决策）；不启动 M3 正式开发
（缓冲期最多做 pii-redaction-plugin 骨架，不 commit 除非测试齐全）。

## 2. 时间线（23:00–06:00，共 7h）

| 阶段 | 时段 | 交付 | 验收门槛 |
|---|---|---|---|
| S0 热身 | 23:00–23:15 | 基线确认（全量 251 绿 + quick 基准 p50）、方案文档 commit | 基线数字记录进夜间日志 |
| S1 适配器 embeddings | 23:15–00:15 | `GeminiProvider.embed` + `BedrockProvider.embed` + 测试 | 新测试绿；既有 251 不回归 |
| S2 跨副本精确缓存 | 00:15–01:15 | `StateBackend` 增 cache get/put；`ResponseCachePlugin` 双后端；fakeredis 跨实例测试 | 两实例共享后端命中测试绿 |
| S3 semantic-cache-plugin | 01:15–02:45 | 插件本体 + 键分区指纹 + 双后端向量存储 + 命中打标 | 单元+集成测试绿；不配置时行为与 v0.2 完全一致 |
| S4 验收加固 | 02:45–03:45 | 阈值边界/误命中防护/降级直通测试集；quick 基准回归 | 全量绿；p50 增幅 <10%；命中路径 <1ms |
| S5 收尾 | 03:45–04:15 | BLUEPRINT/operations/README 更新、版本 v0.3.0、最终全量 + push | ruff 干净、双远端推送确认 |
| 缓冲 | 04:15–06:00 | 卡点修复；全部顺利则 M3 pii-redaction 骨架（复用 keyword_filter 流式切分） | — |

## 3. 技术方案（按现有代码结构落地）

### S1 原生适配器 embeddings

现状：`Provider.embed()` 基类默认 `raise UpstreamUnavailable`（provider.py:74-81），
语义是"该 deployment 不支持 → dispatcher 失败转移到支持的 deployment"，只有
`OpenAICompatibleProvider` 实现了 embed。

- **Gemini**：`POST /v1beta/models/{model}:embedContent`（`content.parts[].text`，
  响应 `embedding.values`）→ 归一化为 OpenAI 格式 `{object:"list", data:[{embedding, index}]}`。
- **Bedrock**：`invoke-model`（Titan `amazon.titan-embed-*` / Cohere `cohere-embed-*`），
  body 按 model 前缀分支；SigV4 签名复用 `core/sigv4.py`。
- **Anthropic**：执行时先核实上游是否有公开 embeddings 端点（2026-10 时点）；
  若无 → 保持默认抛 UpstreamUnavailable 并在文档记录"Anthropic 走 openai 兼容
  embeddings 上游"——这本身就是失败转移语义的正确用法。
- 测试照抄现有 provider 测试模式（mock httpx transport / respx）。

### S2 跨副本精确缓存

现状：`ResponseCachePlugin._store` 是进程内 dict（response_cache.py:49）。

- `StateBackend` 增两个方法（带 InMemory/Redis 双实现，对齐既有抽象风格）：
  - `cache_get(key) -> dict | None`（Redis: `GET`，miss/损坏返回 None）
  - `cache_put(key, body, ttl_s) -> None`（Redis: `SET key json EX ttl`）
- `ResponseCachePlugin` 增 `shared: bool` 配置（默认 false = 现状进程内）；
  true 时通过注入接口拿 backend。注入方式沿用 estimator 注入模式（app.py 启动时
  set 到插件实例）。无 backend（`shared_state` 未配置）时降级进程内并 warn。
- Redis 后端容量上限：不做 SCAN 全量统计（贵），接受 Redis 侧仅靠 TTL 自然回收；
  进程内路径保留现有 LRU 式淘汰。此边界写进 operations.md。
- 测试：fakeredis 同实例给两个 backend/两个插件实例 → A 实例 put、B 实例 get 命中。

### S3 semantic-cache-plugin（新文件 `plugins/semantic_cache.py`）

- **启用条件**：仅当配置节存在（插件机制天然 opt-in）。额外要求配置
  `embed_model`（哪个模型路由做向量化）+ `similarity_threshold`（默认 **0.95** 保守）；
  缺 `embed_model` 时插件初始化即 warn 并自禁用（`self.enabled = False`）。
- **embed 管道复用**：app 启动时给插件注入 `embed_fn(texts) -> list[list[float]] | None`
  （内部走 dispatcher 的 embeddings 路由，即 `/v1/embeddings` 同一管道）。
  embed_fn 返回 None（路由失败/超时）→ 插件直通放行，绝不因缓存组件阻断请求。
- **键分区指纹**（误命中防线核心）：`分区键 = model + temperature/top_p/max_tokens/seed
  + tools 指纹 + response_format`（复用 `cache_key()` 的参数字段但**不含 messages/input**）。
  参数不同 → 不同分区 → 绝不命中。检索只在分区内暴力 cosine。
- **存储**：分区键 → list[(向量, 响应, 过期时间)]。
  InMemory：进程内 dict + `max_entries` 上限（默认 256 条，超限丢最旧）；
  Redis：`SETEX` 每条目 JSON（含向量），分区键前缀 SCAN 检索，容量上限同语义
  （条目自 TTL 回收，SCAN 超限丢最旧）。TTL 默认 600s，独立于精确缓存。
- **请求文本**：非流式 chat 取 `messages` 全文拼接（system+user+assistant）；
  embeddings 请求不参与语义缓存；流式直接跳过（沿用精确缓存决策）。
- **命中路径**：`ctx.cache_hit = True` + `ctx.state[...]["semantic"] = True`，
  响应 body 加 `service_meta`? —— 不改响应体（兼容），命中可见性靠指标与日志：
  metrics 增 `gateway_cache_hits_total{cache="semantic"}`（命名对齐现有 cache 指标，
  执行时看 metrics.py 现状定，遵循"不改既有指标"纪律）。
- **写路径**：`on_response` 且 status 200 且非流式且本次非命中 → 存。
- **并发写放大**：同文本并发 miss 各自 embed+写 → 接受（写幂等，覆盖同指纹条目）。

### S4 验收测试集（新增约 15–20 个用例）

1. 阈值边界：相似度 = 阈值-ε 不命中、≥ 阈值命中（构造已知角度向量对）；
2. 误命中防护：同文本不同 temperature/seed/tools/model → 不命中；
3. 降级：embed_fn 返回 None → 请求直通 200；embed 超时 → 直通；
4. 跨实例：fakeredis 两实例语义命中；
5. 流式不缓存；命中不计费（token 配额豁免路径复用精确缓存现有处理）；
6. 容量上限淘汰；TTL 过期不命中；
7. 不配置插件 → 全部行为与 v0.2 一致（既有 251 用例即回归防线）。
8. quick 基准（benchmarks/ 现有脚本）：插件全开 p50 相对基线增幅 <10%；
   语义命中路径（mock embed，预置向量）<1ms 不含 embed 往返。

### S5 收尾

- BLUEPRINT.md：M2 标记 ✅ 已交付 + 交付清单；operations.md 已知边界两处更新
  （跨副本精确缓存已可选、原生适配器 embed 现状）；README 特性表 + 版本 v0.3.0；
- `git log` 提交链：每阶段一个 commit，格式沿用 `Iteration: ...`；
- push 双远端（GitHub 偶发 reset 重试 ≤3 次，仍失败记日志晨间处理）。

## 4. 无人值守执行纪律

1. **commit 门槛**：全量测试绿 + ruff 干净，二者缺一不提交；
2. **卡点策略**：单点卡 45 分钟 → 把问题/半成品状态写进夜间日志，跳到下一独立阶段
   （S1、S2 互相独立；S3 依赖 S1 的 embed 管道与 S2 的 backend 抽象，S4 依赖 S3）；
   最后的缓冲期回头补；
3. **保守决策规则**：设计歧义时选行为不变项（默认关闭/阈值偏高/容量偏小），
   每条非常规决策记入夜间日志「决策」节，标注供晨间复核；
4. **绝不触碰**：既有指标名、既有 YAML 字段语义、priority tier/fallback/冷却语义、
   251 个既有用例的断言（蓝图 M1 期间的兼容性纪律延续）；
5. **夜间日志**：`docs/night-log-2026-10-26.md`，每阶段完成时追加一行
   （时间/阶段/结果/测试数/决策），晨间 5 分钟可读完。

## 5. 晨间复核清单（预计 5–10 分钟）

- [ ] `git log --oneline` 提交链与阶段对应；双远端 `git status` 已同步；
- [ ] 夜间日志：有无跳过阶段、待复核决策、push 失败记录；
- [ ] `uv run --no-sync python -m pytest tests -q` 本地复跑全绿；
- [ ] 语义缓存两个关注点：默认阈值 0.95 是否合理、误命中测试集覆盖是否满意；
- [ ] quick 基准 p50 与基线（0.438ms）对比。

## 6. 风险与预案

| 风险 | 预案 |
|---|---|
| Anthropic 无 embeddings 端点 | 保持 failover 默认行为并记录（不算失败，S1 验收按实际支持面定） |
| Redis 向量条目 SCAN 性能 | 容量上限默认 256 + TTL 600s 兜底；基准超标则默认值再减半 |
| 语义误命中 | 分区指纹 + 0.95 阈值 + TTL 短 + 默认关闭四重防线；测试集盯边界 |
| embed 往返拖慢请求路径 | embed_fn 带超时；失败直通；基准回归盯 p50 |
| GitHub push 连接重置 | 重试 ≤3 次后记日志，晨间补推（Gitee 通常先成功） |
| 夜间中途环境异常（Redis 不可用等） | 所有新能力默认关闭/降级路径已内建，不影响 251 基线 |
