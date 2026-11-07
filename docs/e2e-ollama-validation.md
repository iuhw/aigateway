# Ollama 端到端验证报告

- 时间：2026-11-08 00:28:35
- 上游：本地 Ollama glm4:9b（GLM-4-9B-Chat Q4_K_S，12GB RTX 5070 Ti）
- 结果：**21/21 通过**

| # | 验证项 | 结果 | 详情 |
|---|---|---|---|
| 1 model listing | ✅ | PASS | status=200 models=['failover-demo', 'glm4-canary', 'glm4-limited', 'glm4:9b'] |
| 2 non-streaming completion | ✅ | PASS | reply='我是一个专注于人工智能和信息技术领域的专家。 [[E2E-M5-e2e-test' usage=46/21tok |
| 3 streaming completion | ✅ | PASS | ttft=368ms chunks-text='1, 2, 3, 4, 5 [[E2E-M5-e2e-tes' finish=stop |
| 4 auth (virtual key) | ✅ | PASS | missing=401 invalid=401 |
| 5 keyword filter blocks request | ✅ | PASS | status=400 body={"error":{"message":"E2E: request blocked by keyword filter.","type":"content_filter_rejected","code":"content_filter_re |
| 6 keyword mask on response | ✅ | PASS | reply='这是**文件。 [[E2E-M5-e2e-tester]].' |
| 7 exact-match cache | ✅ | PASS | id1=chatcmpl-340 id2=chatcmpl-340 same=True |
| 8 rate limiting (rpm=2 -> 429) | ✅ | PASS | statuses=[200, 200, 429, 429] |
| 9 failover to healthy fallback | ✅ | PASS | status=200 served_model=glm4:9b |
| 10 Prometheus metrics | ✅ | PASS | missing=none |
| 11 concurrent burst x5 | ✅ | PASS | statuses=[200, 200, 200, 200, 200] |
| 12 Anthropic /v1/messages non-stream | ✅ | PASS | reply='长城是世界上最长的军事防御工程。 [[E2E-M5-e2e-' stop=end_turn |
| 13 Anthropic streaming events | ✅ | PASS | events=6 text='1,2,3 [[E2E-M5-e2e-tester]].' |
| 14 PII mask roundtrip | ✅ | PASS | reply='我的手机号是*********** [[E2E-M5-e2e-tester]].' |
| 15 prompt-guard blocks injection | ✅ | PASS | status=400 body={"error":{"message":"Prompt injection attempt detected and blocked.","type":"prompt_injection_detected","code":"prompt_i |
| 16 webhook-review allow path | ✅ | PASS | status=200 counter_allowed=True |
| 17 OTLP traces received | ✅ | PASS | exports=2 spans=19 chats=18 model_ok=True with_ttft=1 failover_spans=1 upstream_children=1 |
| 18 usage report reconciles | ✅ | PASS | statuses=[200, 200, 200] request_delta=3 key_delta=3 prompt_token_delta=92 bucket=hour |
| 19 collector down -> requests unaffected | ✅ | PASS | statuses=[200, 200, 200] (collector stopped before these requests) |
| 20 prompt template injection | ✅ | PASS | reply='好[[E2E-M5-e2e-tester]].' token=True unsubstituted=False |
| 21 canary split by identity | ✅ | PASS | stable-served=glm4:9b canary-served=glm4:9b-canary metrics(canary)=True metrics(stable)=True |
