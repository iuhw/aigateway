# Ollama 端到端验证报告

- 时间：2026-10-25 00:51:15
- 上游：本地 Ollama glm4:9b（GLM-4-9B-Chat Q4_K_S，12GB RTX 5070 Ti）
- 结果：**11/11 通过**

| # | 验证项 | 结果 | 详情 |
|---|---|---|---|
| 1 model listing | ✅ | PASS | status=200 models=['failover-demo', 'glm4-limited', 'glm4:9b'] |
| 2 non-streaming completion | ✅ | PASS | reply='我是一个多才多艺的人工智能助手。' usage=10/10tok |
| 3 streaming completion | ✅ | PASS | ttft=206ms chunks-text='1, 2, 3, 4, 5' finish=stop |
| 4 auth (virtual key) | ✅ | PASS | missing=401 invalid=401 |
| 5 keyword filter blocks request | ✅ | PASS | status=400 body={"error":{"message":"E2E: request blocked by keyword filter.","type":"content_filter_rejected","code":"content_filter_re |
| 6 keyword mask on response | ✅ | PASS | reply='这是**文件。' |
| 7 exact-match cache | ✅ | PASS | id1=chatcmpl-415 id2=chatcmpl-415 same=True |
| 8 rate limiting (rpm=2 -> 429) | ✅ | PASS | statuses=[200, 200, 429, 429] |
| 9 failover to healthy fallback | ✅ | PASS | status=200 served_model=glm4:9b |
| 10 Prometheus metrics | ✅ | PASS | missing=none |
| 11 concurrent burst x5 | ✅ | PASS | statuses=[200, 200, 200, 200, 200] |
