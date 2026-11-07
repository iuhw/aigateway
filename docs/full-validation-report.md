# aigateway v1.0 全面实测报告

> 生成时间：2026-11-08。对象：v1.0.0（最终代码，含本轮实测发现的修复）。
> 范围：M1 租户治理、M2 语义缓存、M3 安全护栏、M4 可观测、M5 提示词/灰度、v1.0 GA 全部已完成功能。
> 方法：单元/集成套件（mock 上游，确定性）+ 真实上游 E2E 26 项（本地 Ollama GLM-4-9B +
> nomic-embed-text，逐项实弹）+ 基准/负载/soak 三件套（量化口径见 docs/ 各专项报告）。

## 1. 环境与口径

| 项 | 值 |
|---|---|
| 代码版本 | v1.0.0（主分支 HEAD，含全面实测发现的语义缓存修复） |
| 单元/集成套件 | **388 passed, 1 skipped**（41s；CI 同口径：lint + 全量 + 基准 smoke + Docker） |
| E2E 上游 | 本地 Ollama 0.40.10：glm4:9b（GLM-4-9B-Chat Q4_K_S）、glm4:9b-canary（别名标签）、nomic-embed-text（embeddings） |
| E2E 方式 | 真实 HTTP（uvicorn 进程内 ASGI + 真实 Ollama 推理 + 真实 embeddings），**26 项检查逐项 PASS/FAIL**，admin 面以 `admin_api_key` 保护的出厂形态运行 |
| 基准口径 | httpx ASGI 直连 + 零延迟 mock 上游（纯网关开销），M4/M5 场景背靠背同窗对比 |
| 负载口径 | 双臂真实 HTTP（网关臂 vs 直连臂），SLO 断言内置 |
| soak 口径 | 全插件链（含 canary 切流 + 模板注入），OTLP collector 故意宕机（最恶劣导出条件） |

## 2. 功能 × 证据矩阵

图例：🟢 真实上游实测（E2E 检查项）｜🔵 单元/集成/wire 测试（mock，确定性）｜⚪ 基准/负载/soak 量化。

### 统一接入

| 功能 | 实测 | 证据 |
|---|---|---|
| OpenAI 兼容 `/v1/chat/completions` 非流式（usage 计量） | 🟢 | E2E check-2 |
| 流式 SSE（TTFT、finish_reason） | 🟢 | E2E check-3；基准场景 3 |
| Anthropic `/v1/messages` 非流式（协议翻译） | 🟢 | E2E check-12 |
| Anthropic 流式事件翻译 | 🟢 | E2E check-13 |
| `/v1/embeddings` 管道 | 🟢 | E2E check-25（语义缓存经网关 embeddings 管道实弹向量化） |
| 模型列表 `/v1/models`（含 v1.0 认证收紧） | 🟢 | E2E check-1（携 key）/check-22（动态 key）；无 key 401 见 tests/test_security.py |
| tools（定义/tool_use/tool_result 流式事件） | 🔵 | tests/test_anthropic.py、tests/test_native_providers.py |
| 原生上游适配器 anthropic/gemini/bedrock | 🔵 | wire 故障注入测试（mock 上游协议断言；真实云厂商密钥不在测试环境，见 §5 边界） |

### 路由与流量治理

| 功能 | 实测 | 证据 |
|---|---|---|
| 多部署负载均衡 / priority tier | 🔵 | tests/test_router.py、test_dispatcher.py |
| 超时/退避重试/部署间故障转移/降级链 | 🟢🔵 | E2E check-9（dead upstream → fallback 实弹）；E2E check-17（failover 子 span 进真实 trace） |
| 冷却熔断 + 跨副本冷却同步 | 🔵 | tests/test_dispatcher.py、test_shared_integration.py |
| **canary 灰度切流**（M5：粘性哈希/percent/keys/exclude） | 🟢 | E2E check-21：`e2e-canary` key 强制分流至 `glm4:9b-canary`（响应体 served model 双侧验证）+ 默认 key 保持 stable + `gateway_canary_requests_total` 双分支计数；万级身份偏差 <5% 单元测试；canary 失败回落 stable 测试 |
| **热重载**（YAML 修改生效、改坏保留旧状态） | 🟢🔵 | E2E check-26：改配置文件→新增模型路由→实弹服务请求；改坏保留旧状态见 tests/test_config_reload.py |

### Token 与成本治理（M1）

| 功能 | 实测 | 证据 |
|---|---|---|
| 虚拟 key 认证（明文/SHA-256） | 🟢 | E2E check-4（401 双断言）；check-22（动态 key 认证） |
| 模型白名单 | 🔵 | tests/test_tenant_admin.py |
| **动态 key CRUD（Admin API v1）** | 🟢 | E2E check-22：create（201 明文一次性返回）→ list（key_hash 脱敏、无明文）→ 动态 key 实弹认证 → delete → 401；**虚拟 key 访问 /admin/* 被拒 401** |
| **预算硬限**（日/月美元，check-then-bill） | 🟢 | E2E check-23：真实定价（input_per_1k=1.0）下首请求 200 并入账，账本越限后次请求 **429 `budget_exceeded`** |
| **key 生命周期**（expires_at/concurrency） | 🟢 | E2E check-24：过期 key 实弹 **403 `key_expired`**；并发=1 时两个并行请求 **[200, 429 `concurrency_exceeded`]** |
| token 周期配额 | 🔵 | tests/test_budget.py、test_token_limit.py（含 fakeredis 跨实例） |
| 请求级/token 级 RPM 限流 | 🟢🔵 | E2E check-8（429 实弹）；tests/test_token_limit.py |
| per-key 成本指标 + 基数上限 | 🔵⚪ | tests/test_cost.py；Grafana 面板；`max_tracked_keys` 溢出聚合测试 |
| `/admin/usage` 报表对账 | 🟢 | E2E check-18（3 请求精确对账，admin key 保护下访问） |

### 缓存（M2）

| 功能 | 实测 | 证据 |
|---|---|---|
| 精确匹配响应缓存 | 🟢 | E2E check-7（同 prompt 二次命中） |
| **语义缓存**（真实 embeddings 上游） | 🟢 | E2E check-25：改写后提示词命中缓存答案（实测对相似度 0.9990 ≥ 阈值 0.9）+ `gateway_semantic_cache_hits_total` 计数；分区防误命中/阈值边界单元测试；**本轮实测发现并修复与提示词模板的交互缺陷**（见 §4） |
| 跨副本共享缓存 | 🔵 | fakeredis 集成测试（test_shared_integration.py、test_state_shared.py）；真实 Redis 跨实例见 §5 边界 |
| 原生适配器 embeddings（gemini/bedrock） | 🔵 | tests/test_embeddings.py、test_native_providers.py |

### 安全护栏（M3）

| 功能 | 实测 | 证据 |
|---|---|---|
| PII 脱敏（双向 + 流式跨块安全） | 🟢 | E2E check-14（真实回复中手机号掩码） |
| 提示词注入防护（加权评分） | 🟢 | E2E check-15（注入样本 400 `prompt_injection_detected`）；M3 样本集 18/18 检出、0 误报 |
| webhook 外部审核（fail-open） | 🟢 | E2E check-16（本地 allow-stub + verdict 计数） |
| 关键词过滤（block/mask） | 🟢 | E2E check-5/6 |

### 可观测（M4）

| 功能 | 实测 | 证据 |
|---|---|---|
| Prometheus 指标面（admin 保护形态） | 🟢 | E2E check-10/16/21（admin key 实弹访问） |
| OTLP 追踪导出（真实 collector） | 🟢 | E2E check-17：本地 OTLP stub 收到 protobuf 并解码（19 chat span、TTFT 事件、failover 父子 span） |
| 导出 fire-and-forget（collector 宕机零影响） | 🟢⚪ | E2E check-19；soak 60min collector 全程宕机 116k 请求零错误 |
| 用量报表（key/model/时间桶） | 🟢 | E2E check-18 |
| Grafana 面板 | 🔵 | deploy/grafana-dashboard.json（面板查询基于既有指标名） |

### 提示词工程（M5）

| 功能 | 实测 | 证据 |
|---|---|---|
| **提示词模板注入**（变量渲染驱动真实模型） | 🟢 | E2E check-20：`[[E2E-M5-e2e-tester]]` 签名 token 出现在真实模型回复（含 `{key}` 变量替换、无未替换字面量）；单遍渲染回归 13 项 |
| on_request 请求改写扩展点 | 🟢🔵 | prompt-template 即参考实现（经 check-20 实弹）；插件开发指南文档化 |

### GA 安全自查（v1.0）

| 功能 | 实测 | 证据 |
|---|---|---|
| 静态 key 内存 digest-only | 🔵 | tests/test_security.py::test_static_key_material_not_kept_in_memory |
| `/v1/models` 认证（防未授权枚举） | 🟢🔵 | E2E check-1/22 携 key 访问；tests/test_security.py（401/匿名/动态 key 三态） |
| admin 面保护（常量时间比较） | 🟢🔵 | E2E check-10/16/18/21/22（admin key 实弹）+ check-22（虚拟 key 被拒 401）；tests/test_security.py |
| 日志脱敏默认关闭 | 🔵 | tests/test_security.py::test_request_log_payloads_off_by_default |
| request-log handler 抗外部污染 + aclose 摘除 | 🔵 | 修复回归：pytest capture 场景复现→修复→组合套件通过 |

## 3. 量化结果汇总

| 验证 | 结果 | 口径 |
|---|---|---|
| 单元/集成套件 | **388 passed, 1 skipped** | mock 确定性；含混沌注入 30% 故障 >97% 成功率、零挂起 |
| E2E（真实模型） | **26/26**（本轮扩 5 项：Admin CRUD/预算/生命周期/语义缓存/热重载） | GLM-4-9B + nomic-embed-text，逐项见 §6 |
| 基准（最终代码复测） | 网关 p50 0.434ms；全链 7 插件 +0.069ms；M4 +0.059ms；**M5 +0.025ms（+4.4%，<10% 门槛）** | docs/benchmark-report.md |
| 双臂负载 | **4/4 SLO**（错误率 0%、网关开销 p99 <10ms、TTFT 附加 p50 -24.5ms 与直连同水平） | docs/load-report.md |
| soak | **60min 116,097 请求零错误**；RSS 斜率 +0.252 MB/min、句柄 +3.10/min | docs/soak-report.md |

## 4. 实测发现（本轮实测的直接产出）

1. **语义缓存 × 提示词模板交互缺陷（已修复）**：语义缓存原实现嵌入全部消息文本；提示词模板
   给每个请求注入字节相同的 system 提示后，实测（nomic-embed-text）任意提示词对的 cosine 全部
   ≥0.98（如「介绍故宫」vs「只回复数字1」= 0.9907）——任何可用阈值都会跨提示词误命中（答非所问）。
   **修复**：嵌入文本改为非 system 消息；system 提示并入分区指纹（不同 system 绝不共享缓存答案，
   比修复前更严格）。修复后实测：改写对 0.9990（命中）vs 跨主题 0.99 以下（不命中），E2E check-25
   与 388 套件全绿。
2. **缓存跨 canary 分支共享为既定语义**（operations.md 边界 19）：响应缓存键为请求模型，与
   fallback 跨路由共享一致。实测中分流请求可能由缓存以另一分支答案应答——这是文档化行为而非
   缺陷；E2E check-21/23 用互不相关且逐轮唯一的提示词绕开缓存以验证路由/计费本体。

## 5. 边界（未做真实云厂商/集群实测的部分）

- **原生上游适配器（anthropic/gemini/bedrock）**：协议翻译经 wire 故障注入测试验证（mock 上游按真实
  协议形状断言，含 tools/流式/embeddings/SigV4 与 botocore 交叉验证），但无真实云密钥，未对云端点实弹。
- **Redis 跨副本**：共享限流/账本/冷却/缓存经 fakeredis 集成测试与多实例单测验证；未在真实 Redis 集群
  上跑多副本（本机 Docker 未运行）。
- **tiktoken 精确计数**：可选 extra，离线环境回退启发式已测；精确路径按 tests/test_tokenizers.py
  的条件跳过语义覆盖。
- **多 worker 部署**：单进程口径实测；SO_REUSEPORT/多 Pod 属部署拓扑，超出单机验证范围。

## 6. E2E 逐项结果

见 [e2e-ollama-validation.md](e2e-ollama-validation.md)（26 项逐项 PASS/FAIL 与详情）。本轮新增：

| # | 检查 | 关键证据 |
|---|---|---|
| 22 | Admin API CRUD + 保护 | create 201（明文一次性）→ list 脱敏 → 动态 key 认证 200 → delete → 401；虚拟 key 访问 admin 被拒 401 |
| 23 | 预算硬限 | 真实定价下首请求 200 入账 → 次请求 429 `budget_exceeded` |
| 24 | key 生命周期 | 过期 403 `key_expired`；并发=1 并行两请求 [200, 429 `concurrency_exceeded`] |
| 25 | 语义缓存（真实 embeddings） | 改写提示词命中缓存答案 + 指标计数（阈值 0.9 经实测校准） |
| 26 | 热重载 | 配置文件新增模型路由 → reload → 实弹服务 200 |
