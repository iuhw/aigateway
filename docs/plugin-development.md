# aigateway 插件开发指南

> 插件是 aigateway 的核心扩展机制：限流、认证、缓存、护栏、可观测导出全部是插件，
> core 只维护路由、调度、计量。本文面向第三方插件作者。

## 1. 最小插件

```python
# mypkg/myplugin.py
from aigateway.plugins.base import Plugin, register_plugin


@register_plugin
class MyFilter(Plugin):
    name = "my-filter"            # 配置键 / 注册名
    default_order = 100           # 链上位置，越小越先执行

    async def on_request(self, ctx) -> None:
        text = str(ctx.payload.get("messages", ""))
        if "forbidden" in text:
            raise ctx.reject("forbidden content", code="my_filter_rejected")
```

```yaml
plugins:
  my-filter:
    module: mypkg.myplugin       # 第三方：模块 + 类名，零侵入网关核心
    class: MyFilter
    config:
      whatever: 1
```

也可以不装包、不改配置注册方式：`from aigateway.plugins.base import register_plugin`。
同一插件类可多实例（不同 config key + `plugin: my-filter` 别名）。

## 2. 钩子与执行模型

```
request ──► on_request ──► [canary 解析] ──► [路由/调度] ──► on_response   (非流式)
                                                │
                                                └─ 流式 ──► on_stream_start
                                                              on_stream_chunk*
                                                              on_stream_end
on_error：任何以错误收尾的请求都会触发（fire-and-forget，异常被吞、不掩盖原始错误）
aclose：shutdown / 配置热重载时释放资源（重载会重建整个插件链）
```

- 插件按 `order` 排序执行（`default_order` 或配置 `order`）；请求钩子链完成后才做 canary
  解析与调度，因此 `on_request` 看到的是认证后的 `ctx.identity` 与原始请求模型。
- `on_stream_chunk` 可检查/改写/丢弃单个块（返回 None 丢弃）；流式响应的块不可撤回，
  block 类动作只能在首块前决定（护栏类插件的已知取舍，见 operations.md）。
- 拒绝请求：抛 `ctx.reject(msg, status_code=400, code=...)` 或任何 `GatewayError`；
  API 层会映射为 OpenAI 格式错误（Anthropic 入口有对应错误码翻译）。
  拒绝自动计入 `gateway_plugin_rejections_total{plugin, code}`。

## 3. PluginContext 字段（每个请求一份，插件间共享）

| 字段 | 说明 |
|---|---|
| `request_id` / `model` / `payload` / `stream` / `headers` / `client_host` | 请求基本量；`model` 在 canary 分流后是**生效模型** |
| `identity` | auth-plugin 写入的身份 dict（name/models/rpm/budgets/...），匿名时 name=anonymous |
| `requested_model` | canary 分流发生时的原始请求模型；未分流为 None |
| `route` / `deployment` | 调度后回填的实际路由与部署 |
| `response` | 非流式响应体（on_response 中可原地修改） |
| `status_code` / `latency_s` / `first_token_latency_s` | 结算指标（API 层回填） |
| `prompt_tokens` / `completion_tokens` / `cost_usd` / `cache_hit` / `usage_reported` | 计量（成本在响应钩子前结算） |
| `cache_read_tokens` / `cache_write_tokens` | 分层定价的缓存命中拆分 |
| `attempts` / `upstream_started_at` / `upstream_ended_at` | 调度窗口（导出插件构建上游子 span 用） |
| `state` | 插件私有暂存区，按插件名分桶（如去重标记） |

## 4. 请求改写扩展点（受支持的公开约定）

在 `on_request` 中修改 `ctx.payload` 即实现请求改写——这是**受支持的扩展点**，
`prompt-template-plugin`（系统提示注入）是其参考实现：

```python
async def on_request(self, ctx) -> None:
    messages = ctx.payload.get("messages", [])
    ctx.payload["messages"] = [{"role": "system", "content": "corporate rules"}, *messages]
```

注意事项：

- 改写发生在护栏插件（默认 order 50+）扫描**之前**还是**之后**取决于你的 `order`——
  prompt-template 用 order 40，保证护栏扫描的是注入后的最终 prompt。
- 限流/白名单/canary 身份哈希都基于改写前语义（模型名不变）；token 估算基于改写后的
  payload（计费按实际发给上游的内容）。
- 流式与非流式走同一 `on_request`，无需分别处理。

## 5. 注入与协作约定

- `set_shared(backend, metrics)`：需要共享状态（Redis/内存等价后端）的插件实现它，
  GatewayApp 会注入（与 rate-limit 同一后端实例）。
- `set_metrics(metrics)`：需要打自定义指标的插件实现它（Prometheus 自定义注册表，
  新指标请加标签基数上限，参见 `max_tracked_keys` 的做法）。
- `self.estimator`：token 估算器（Unicode 启发式或 tiktoken），限流/估算类插件用它保持
  与网关计量一致。
- 失败模型不变式：插件错误不得掩盖原始上游错误；共享后端故障优先 fail-open（可配）；
  外部服务调用一律设超时并声明 fail-open/fail-close 策略。

## 6. 测试你的插件

仓库测试基建可直接复用（`tests/conftest.py`）：

```python
from tests.conftest import base_config, write_config

config = base_config(plugins={"my-filter": {"config": {...}}})
app = GatewayApp(write_config(tmp_path, config))
body = await app.chat({"model": "m1", "messages": [...]}, headers={}, client_host="test")
```

mock provider（确定性内容、可注入故障/延迟/流式块数）覆盖非流式/流式/failover 路径；
`scripts/validate_e2e.py` 展示了真实上游 E2E 的写法。CI 全绿是唯一合并标准。
