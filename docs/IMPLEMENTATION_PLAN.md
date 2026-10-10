# IMPLEMENTATION_PLAN.md

文档更新时间：`2026-10-10`

## 计划总原则

本项目的实施路线已经固定为：

- `QQ Official only`
- `single process`
- `SQLite + local filesystem`
- `raw-first`
- `InternalMessage first`
- `LLMProvider abstraction`
- `文本、图片、provider 都保持独立边界并由显式 trigger 驱动`

不再把 `NapCat`、`OneBot 11` 或兼容层当作未来 phase。

## Current Completion Status

- QQ Official WebSocket runtime、raw-first persistence、parser/storage、commands、assistant、summary 和 multimodal implementation 已通过自动化验证
- current full suite: `575 passed in 18.70s`（2026-10-09，合并转发阅读与历史重解析回归）
- reproducible test inputs: sanitized captured fixtures are included; the earlier 521-test revision also passed in a copy without `.env`, runtime database or local captures
- default transport: `websocket`
- real OpenAI-compatible text / `#总结` smoke: passed in the existing deployment history
- real QQ webhook smoke: deferred by user on 2026-10-09 (no public HTTPS); keep existing WebSocket configuration, not a blocker for the current transport
- real QQ image + configured vision model smoke: passed on 2026-10-09 for an ordinary JPEG followed by a structured mention; vision `gpt-5.6-luna` via configured compatible endpoint, text answer via DeepSeek; user confirmed accurate description
- reply-only automatic continuation: intentionally unsupported
- durable outbox worker: completed; command/assistant failures are not blindly retried after possible delivery
- outbound response validation: nonzero business codes override 2xx/id; asynchronous 201/202 acceptance remains unconfirmed, with summary/raw retained and no blind retry (offline integration covered)
- long-window summary: complete structured chunk inputs, bounded user prompts, strict failure handling; prompt version `3` adds readable/unverified forwarded-content rules
- forward reading: parser v3 retains provided display text and recursively preserves explicit reply/forward MsgElement trees, provenance and shared depth/node limits. Renderer hides signed image URLs; QA/chat/summary read domain data only. Offline replay appends v3 without executing interactions. Safe display-image reading remains supported. Real samples omit some nested content; on 2026-10-10 the user canceled automatic fetching of that missing content. NapCat/OneBot is not integrated; related research is archived, not an active phase.
- 2026-10-07 real preflight: QQ auth / Gateway READY / runtime health passed; DeepSeek and configured compatible provider both produced validated summaries from synthetic text
- 2026-10-08 real ingestion + `#ping`: passed, including user-confirmed receipt of `pong`. Raw 34/35 and normalized messages persisted; QQ passive reply API returned 200; outbox 1 completed with one attempt. See `docs/ACCEPTANCE.md`.
- 2026-10-08 user constraint: live acceptance is ingestion + `#ping` only; summary/assistant/vision are disabled for this run and no conversation data may be sent to a model. Live AI acceptance remains unverified.
- 2026-10-09 authorization update: user explicitly approved one test-group summary of the last 120 minutes via api.deepseek.com. Real raw 36/37 → normalized messages → validated summary 3 → QQ HTTP 200 → outbox 2 completed, one attempt. Requested model `deepseek-v4-flash`, returned model `deepseek-flash`; user explicitly confirmed seeing the summary. DeepSeek text end-to-end acceptance passed. Temporary process stopped; `.env` unchanged. This supersedes the prior no-egress restriction only for the confirmed test.
- 2026-10-09 assistant acceptance: after user approved the disclosed test scopes, real grounded QA (raw 39/interaction 9/outbox 3) and structured mention chat (raw 40/interaction 10/outbox 4) passed through DeepSeek and QQ. Both receipts match persisted interactions; one attempt each. Answers matched the actual test plan, and the user confirmed both replies. Temporary process stopped; persistent feature flags unchanged.

## Historical / Completed Foundation

下面这些能力已经由当前代码和现有测试证明存在：

- QQ Official AccessToken 获取
- QQ Official `/gateway/bot` 接入点获取
- Gateway HELLO / IDENTIFY / RESUME / HEARTBEAT
- reconnect loop 与 `session_start_limit` 感知退避
- QQ Official raw event capture 与 `raw_events` 落库
- durable `interaction_outbox` 与有界 worker；`#ping` 在 AI 功能关闭时仍可消费
- `QQOfficialMessageParser`
- `InternalMessage` / segment tree / provenance / identity 模型
- `MessageRepository` 与 `message_nodes` 树持久化
- `ConversationQueryService`
- `MessageRenderer`
- `DeepSeekProvider`
- `SummaryService`
- `SummaryRepository`
- `SummaryMessageFormatter`
- QQ Official group passive reply sender
- `#总结` 命令的启停、冷却和并发保护
- `/health`

这些基础已经足以支撑后续真实环境 smoke test 和 provider 扩展。

## Completed Phase — DeepSeek Real End-to-End Validation

2026-10-09 完成真实入站、模型生成、摘要持久化、QQ 被动回复和用户侧
收到摘要的完整验收，证据见 `docs/ACCEPTANCE.md`。以下保留该阶段目标
与验收标准，文本基础链路进入冻结维护阶段，不代表其他功能已经验收。

### 目标

该阶段目标是在扩展新能力前，完成真实环境下的文本 summary 闭环验收：

```text
真实 QQ Official 群消息
→ raw event
→ QQOfficialMessageParser
→ InternalMessage
→ SQLite
→ ConversationQueryService
→ MessageRenderer
→ SummaryService
→ DeepSeekProvider
→ 真实 DeepSeek API
→ validated SummaryResult
→ SummaryRepository
→ QQ Official passive reply
```

### 为什么先做这一步

当前仓库已经具备完整的离线测试闭环，但自动测试使用的是 fake / mocked provider 与 fake sender。

先用真实 DeepSeek API 验证这条链路成立，可以把问题边界固定在：

- QQ Official 入站
- raw / normalized persistence
- conversation query
- renderer
- summary domain validation
- QQ Official passive reply

一旦这条文本 summary 闭环真实跑通，就冻结这条基础链路。

### 验收标准

需要在真实测试群中完成：

1. 产生真实聊天消息
2. 发送精确 `#总结`
3. raw event 成功保存
4. command 本身正常 normalized
5. 查询窗口不包含 command 自身
6. renderer 输出正常
7. DeepSeek 真请求成功
8. JSON validation 成功
9. `SummaryResult` 成功生成
10. `summaries` row 成功落库
11. QQ Official passive reply 成功
12. 日志不泄漏 API key / secret / raw conversation

完成后冻结这条文本 summary 闭环，不在这一步混入多模态或聊天回复。

说明：

- 即使官方文档当前仍描述主动消息规则，本项目当前验证目标仍只覆盖由入站消息触发的被动回复
- 不把“只有 `group_id` 的主动群发”纳入当前 phase

## Completed Phase — OpenAICompatibleProvider + Relay Integration

### 目标

在保留现有 `DeepSeekProvider` 的前提下，新增第二个 provider：

```text
SummaryService
→ LLMProvider
→ OpenAICompatibleProvider
→ 中转站 API
```

部署配置示例（接口域名已脱敏，不是可直接使用的真实配置）：

- `LLM_PROVIDER=openai_compatible`
- `OPENAI_COMPATIBLE_BASE_URL=https://relay.example.test/v1`
- `OPENAI_COMPATIBLE_MODEL=gpt-5.6-luna`
- relay token 侧已选择 `GptPro` 分组，但应用程序不处理任何 group / pool / channel / multiplier 概念
- `long_context` 当前禁用，程序不发送相关参数

### 要求

- `base_url` 可配置
- `api_key` 只从环境变量读取
- `model` 可配置
- `timeout`
- 有界 retry policy
- provider error mapping
- optional JSON mode
- HTTP tests 使用 mocked transport
- 不能破坏现有 `DeepSeekProvider` 测试
- `SummaryService` 不应加入 provider-specific 逻辑

### 预期配置

```dotenv
LLM_PROVIDER=openai_compatible
OPENAI_COMPATIBLE_BASE_URL=
OPENAI_COMPATIBLE_API_KEY=
OPENAI_COMPATIBLE_MODEL=gpt-5.6-luna
```

说明：

- `OPENAI_COMPATIBLE_BASE_URL` 表示服务商给出的完整 OpenAI-compatible base URL，可直接是 `.../v1`
- 最终请求应为 `{base_url}/chat/completions`
- 不把 relay group / GptPro / long_context / model routing 写进业务层或 request payload

## Completed Phase — QQ Official Bot Actions + Command E2E

### 目标

把当前 QQ Official 入站消息链路和 Bot Actions 接通，但范围只限明确命令：

```text
#ping
#总结
```

普通群消息在本阶段仍然不触发 LLM：

```text
测试bot
你好
帮我解释一下 xxx
```

### 目标链路

`#ping`

```text
QQ Official Gateway
→ raw event
→ QQOfficialMessageParser
→ InternalMessage
→ SQLite
→ exact command match
→ QQOfficialGroupMessageSender
→ passive reply: pong
```

`#总结`

```text
QQ Official Gateway
→ raw event
→ QQOfficialMessageParser
→ InternalMessage
→ SQLite
→ exact command match
→ ConversationQueryService
→ MessageRenderer
→ SummaryService
→ LLMProvider
→ OpenAICompatibleProvider
→ validated SummaryResult
→ SummaryRepository
→ SummaryMessageFormatter
→ QQOfficialGroupMessageSender
→ passive group reply
```

### 约束

- 不改 `InternalMessage` 的 sender / author attribution 规则
- 不把 `group_openid`、`msg_id` 塞进 `IdentityRef`
- 不让 `SummaryService` 直接调用 QQ API
- 不让 QQ adapter 直接调用 LLM
- `#总结` 继续使用当前 `SUMMARY_COMMAND_LOOKBACK_MINUTES`
- `#总结` 继续使用当前 cooldown / in-progress 保护

### 当前状态

- implementation validated
- 2026-10-08 真实普通消息入库与 `#ping` 完整闭环通过，用户确认客户端已看到 `pong`
- 2026-10-09 当前改动后的 DeepSeek 实群 `#总结` 生成/持久化/QQ 回复及客户端可见性均通过，用户明确确认已看到总结，详见验收记录

## Completed Implementation — QQ Official Webhook-ready Inbound Architecture

### 目标

在保持当前已真实工作的 QQ Official Gateway WebSocket 链路可用前提下，引入 transport-neutral inbound processing，并新增符合腾讯当前公开文档的 webhook transport：

```text
QQ Official WebSocket / Webhook
→ QQOfficialInboundEvent
→ QQOfficialEventProcessor
→ raw_events
→ QQOfficialMessageParser
→ InternalMessage
→ MessageRepository
→ QQOfficialCommandDispatcher
```

### 当前实现边界

- `QQ_EVENT_TRANSPORT=websocket|webhook`
- 默认仍为 `websocket`
- `gateway.py` 保留
- 新增 webhook callback validation（`op=13`）
- 新增 Ed25519 签名校验
- `main.py` 只做 transport wiring
- raw-first 保持不变
- command / summary / provider 边界不变

### 当前状态

- implementation validated
- automated tests passed
- real QQ webhook smoke deferred by user on 2026-10-09; no public HTTPS, keep WebSocket
- `GROUP_MESSAGE_CREATE` automated code support: yes
- `GROUP_MESSAGE_CREATE` real webhook verification: unverified, deferred with the webhook smoke
- 若真实 webhook 只收到 `GROUP_AT_MESSAGE_CREATE`，则保留 WebSocket 为当前正式 transport，不伪造“全量普通群消息能力”

## Phase N — Complete Group Assistant Text Interaction Layer

### 目标与架构

在 Phase M transport-neutral inbound pipeline 上增加完整文本交互层：

```text
QQOfficialEventProcessor
→ raw_events
→ QQOfficialMessageParser
→ InternalMessage / MessageRepository
→ QQOfficialInteractionDispatcher
├── #ping / #总结
└── GroupAssistantHandler
    ├── #问 → grounded current-group QA
    └── structured @bot → contextual chat
        → GroupAssistantContextBuilder
        → shared LLMProvider
        → QQ passive reply
        → assistant_interactions
```

### 已实现行为

- `#问 <问题>`：120 分钟、最多 150 条当前群 inbound message；不使用 Bot 历史回答作为事实
- bare `#问`：只回复用法，不调用 LLM
- `@bot`：支持 `GROUP_AT_MESSAGE_CREATE`，以及真实 sample 中 token 对应 `mentions[].is_you=true` 的 `GROUP_MESSAGE_CREATE`
- chat：30 分钟、最多 80 条群消息和 20 个成功 assistant turns，按 timestamp 合并
- `assistant_interactions`：只在 LLM 成功且 QQ send 成功后写入，不伪造 `raw_events/messages`
- `assistant_trigger_claims`：同一 logical trigger 只处理一次
- cooldown：按 platform + group + requester 隔离
- provider：summary 与 assistant 共享同一个 configured `LLMProvider` 实例
- prompts：grounded/chat 分离；conversation data 做 XML escaping，并明确标为不可信数据
- text-only output：`json_output=false`；空内容和 `finish_reason=length` 拒绝；内部字符上限 4500

### Reply context 结论

当前真实 reply sample 的 `message_type=103` 提供 opaque reference information 和可选引用展示内容，但不提供可安全推断 quoted author 的身份依据。

最终策略固定为：reply event 继续 raw-first 入库并保留 `ReplySegment` / quoted content；单独 reply 不创建 assistant claim、不调用 LLM、不回复。`#问 + reply` 仍走 grounded QA；structured `@bot + reply` 才走 Chat，并将 quoted content 放入 `untrusted-platform-data` prompt block。Automatic reply-to-bot continuation intentionally unsupported。

### 配置

```dotenv
GROUP_ASSISTANT_ENABLED=true
QA_LOOKBACK_MINUTES=120
QA_MAX_MESSAGES=150
CHAT_LOOKBACK_MINUTES=30
CHAT_MAX_MESSAGES=80
CHAT_MAX_ASSISTANT_TURNS=20
ASSISTANT_MAX_INPUT_CHARS=40000
ASSISTANT_MAX_OUTPUT_TOKENS=1200
ASSISTANT_MAX_OUTPUT_CHARS=4500
ASSISTANT_COOLDOWN_SECONDS=3
```

代码默认 `GROUP_ASSISTANT_ENABLED=false`，避免未配置 LLM 凭证的环境启动失败；`.env.example` 给出显式启用配置。

### 当前状态

- implementation validated
- automated unit/integration tests remain covered by the current full suite
- real QQ text assistant smoke passed on 2026-10-09: grounded QA and structured mention chat in GROUP_MESSAGE_CREATE, including user-confirmed receipt; images and other event variants remain separate gates
- default inbound transport remains `websocket`
- Phase M webhook automated support remains validated; real webhook smoke is deferred by user, and WebSocket remains the chosen transport

## Completed — Provider Configuration / Selection

在 provider 扩展后，加入最小可用的 provider 选择配置：

```text
LLM_PROVIDER
    ├── deepseek
    └── openai_compatible
```

第一版只需要简单显式配置，不需要复杂 registry 或 plugin framework。

## QQ Official Parser Evidence Policy

Parser 新字段继续逐项核对：

1. 腾讯当前官方文档
2. 仓库真实脱敏 samples
3. `zhinjs/qq-official-bot` 等第三方参考实现

重点检查：

- `GROUP_MESSAGE_CREATE` / `GROUP_AT_MESSAGE_CREATE`
- `content`
- `mentions`
- `message_reference`
- `attachments`
- `message_type` 的已知语义
- `author` / `sender` / `member_openid` 的可用字段

只有在证据充分后才调整 parser 细节。当前 image attachment 字段已按腾讯官方文档和仓库真实脱敏 samples 交叉核对；无法确认的 attachment 继续成为 `UnknownSegment`。

如果未来需要 reply / forward 补全，统一走概念化的 `MessageReferenceResolver` 边界，而不是把历史平台 action 名称带回当前架构。

## Completed — Image / Multimodal Support

图片能力采用按需 enrichment，不改变 text provider contract：

```text
QQ attachment
→ ImageSegment source metadata
→ SQLite message_nodes
→ HttpImageResourceResolver
→ VisionProvider
→ validated ImageDescription
→ immutable enriched message copy
→ MessageRenderer
→ SummaryService / GroupAssistantService
```

已实现：

- JPEG / PNG / GIF metadata parsing 与树形存储 round-trip
- public HTTP(S)、redirect、timeout、MIME、empty body 和最大 bytes 防护
- OpenAI-compatible multimodal data-URL request，model 可配置且不复制 secret 配置
- summary、grounded QA、structured `@bot` 当前图片与历史图片按需 enrichment
- 单图失败降级，不影响 raw/normalized persistence 和其他 segment
- 普通图片不触发下载、不调用 vision/text LLM、不回复
- 本地人工脚本 `scripts/test_image_vision.py`

AI description 是 transient derived data，不作为 source truth 持久化。
2026-10-09 真实 JPEG 普通入库后显式 @ 场景通过：raw 41/42、interaction
11、outbox 5；117297 bytes 图片成功识别，用户确认回复描述正确。源码
image segment 未持久化 AI description。vision 临时配置为兼容接口上的
`gpt-5.6-luna`，文本仍用 DeepSeek；其他图片交互/MIME 场景不据此声称
实测完成。自动化测试仍使用 HTTP/fake provider boundary。

## Completed — Reliability Hardening

- `assistant_trigger_claims` 防止 assistant logical trigger 重复调用 LLM/发送
- `command_trigger_claims` 防止重复投递的 `#ping/#总结` 再次发送或生成
- duplicate raw receipts 仍保留，便于追踪平台重投
- conversation query 对同 platform/group 的稳定 QQ message ID 返回最新 normalized representation，避免重复历史污染 QA/summary；无 ID 时不猜测
- Gateway 在同一进程重连时使用官方 `op=6` session resume；无效 session fail closed 并回退 IDENTIFY
- restart 后 SQLite 历史、claims、summaries 和 assistant interactions 保持可用

## Completed in Phase N — Mention Chat

当前聊天回复方向已经落地为：

```text
QQ Official incoming message
        ↓
QQOfficialInteractionDispatcher
        ↓
GroupAssistantContextBuilder
        ↓
MessageRenderer
        ↓
GroupAssistantService
        ↓
LLMProvider
        ↓
QQ Official passive reply
```

`GroupAssistantService` 不负责：

- QQ HTTP
- token 获取
- Gateway
- 数据库 SQL

QQ outbound service 不负责：

- prompt
- context selection
- summary
- AI reasoning

Reply reference 不参与 AssistantInteraction lookup。无 `@` reply 有意保持 storage-only；显式 structured `@bot` 才可使用作者不可用的引用正文。

## Later — Context Expansion

未来群聊上下文不应等同于“每次直接把最近 100 条消息全部塞给模型”。

需要在当前 builder 之上扩展：

```text
GroupAssistantContextBuilder / future retrieval service
```

职责：

- `RecentContext`
- `RelevantContext`
- `ReplyContext`
- `ExpandedContext`

未来可支持：

```text
model first pass
        ↓
context insufficient
        ↓
ContextExpansionRequest
        ↓
context service 查询更多历史
        ↓
second pass
```

实现必须用结构化内部状态，不要把 `[[qq_context_more]]` 这种字符串协议写死成领域模型。

## Later — Image-follow-up Interaction

后续可以支持类似：

```text
用户：@bot 看一下
用户下一条：图片
```

短时间内记录：

```text
PendingInteraction
```

至少按：

- group
- sender
- TTL

隔离，避免把 A 的意图关联到 B 的图片。

## Later — Retrieval / RAG

未来超出时间窗口的检索 / RAG 路线应保持轻量：

```text
SQLite time-range query
↓
SQLite keyword search / FTS5
↓
retrieval-aware context service
↓
LLM QA
```

只有在规模和质量都证明需要时，再评估：

- `sqlite-vec`
- 或独立向量数据库

当前不要把 `Pinecone`、`Milvus`、`Qdrant`、`Weaviate` 设计成必需依赖。

## Later — Persona / Memory

`PersonaService`、`MemoryService` 可以是远期能力，但它们只能属于：

```text
derived data
```

不能覆盖：

- `raw_events`
- `InternalMessage`

也禁止把 AI 推断出的身份或偏好回写成原始消息事实。

## 测试策略

每个阶段都要求：

- unit test
- integration test
- real smoke test when necessary

当前测试路线已经证明：

- provider 侧使用 HTTP mock
- parser 侧依赖真实脱敏 fixture
- storage / rendering / summary pipeline 有完整单元和集成测试

后续 Gateway 相关扩展优先增加：

- fake WS / protocol-level tests
- 真实 QQ 测试群 smoke

## 不要过度工程化

除非有明确证据证明现有方案不足，否则不要主动引入：

- PostgreSQL
- MySQL
- Redis
- Kafka
- RabbitMQ
- Celery
- Elasticsearch
- 独立向量数据库
- Kubernetes
- 多服务 Docker Compose
- 微服务
