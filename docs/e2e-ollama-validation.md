# Ollama 端到端验证报告

- 时间：2026-10-31 07:04:09
- 上游：本地 Ollama glm4:9b（GLM-4-9B-Chat Q4_K_S，12GB RTX 5070 Ti）
- 结果：**16/16 通过**

| # | 验证项 | 结果 | 详情 |
|---|---|---|---|
| 1 model listing | ✅ | PASS | status=200 models=['failover-demo', 'glm4-limited', 'glm4:9b'] |
| 2 non-streaming completion | ✅ | PASS | reply='我是一个致力于提供丰富知识和多元回答的人工智能助手。' usage=10/12tok |
| 3 streaming completion | ✅ | PASS | ttft=209ms chunks-text='1, 2, 3, 4, 5' finish=stop |
| 4 auth (virtual key) | ✅ | PASS | missing=401 invalid=401 |
| 5 keyword filter blocks request | ✅ | PASS | status=400 body={"error":{"message":"E2E: request blocked by keyword filter.","type":"content_filter_rejected","code":"content_filter_re |
| 6 keyword mask on response | ✅ | PASS | reply='这是**文件。' |
| 7 exact-match cache | ✅ | PASS | id1=chatcmpl-654 id2=chatcmpl-654 same=True |
| 8 rate limiting (rpm=2 -> 429) | ✅ | PASS | statuses=[200, 200, 429, 429] |
| 9 failover to healthy fallback | ✅ | PASS | status=200 served_model=glm4:9b |
| 10 Prometheus metrics | ✅ | PASS | missing=none |
| 11 concurrent burst x5 | ✅ | PASS | statuses=[200, 200, 200, 200, 200] |
| 12 Anthropic /v1/messages non-stream | ✅ | PASS | reply='长城，世界文化遗产，中国悠久历史的见证。' stop=end_turn |
| 13 Anthropic streaming events | ✅ | PASS | events=6 text='1、2、3' |
| 14 PII mask roundtrip | ✅ | PASS | reply='我的手机号是***********' |
| 15 prompt-guard blocks injection | ✅ | PASS | status=400 body={"error":{"message":"Prompt injection attempt detected and blocked.","type":"prompt_injection_detected","code":"prompt_i |
| 16 webhook-review allow path | ✅ | PASS | status=200 counter_allowed=True |
