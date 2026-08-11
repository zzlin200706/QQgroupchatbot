# IMPLEMENTATION_PLAN.md

文档更新时间：`2026-08-11`

## 计划总原则

本项目的实施路线已经固定为：

- `QQ Official only`
- `single process`
- `SQLite + local filesystem`
- `raw-first`
- `InternalMessage first`
- `LLMProvider abstraction`
- `先完成真实文本闭环，再扩展 provider，再做多模态`

不再把 `NapCat`、`OneBot 11` 或兼容层当作未来 phase。

## Historical / Completed Foundation

下面这些能力已经由当前代码和现有测试证明存在：

- QQ Official AccessToken 获取
- QQ Official `/gateway/bot` 接入点获取
- Gateway HELLO / IDENTIFY / HEARTBEAT
- reconnect loop 与 `session_start_limit` 感知退避
- QQ Official raw event capture 与 `raw_events` 落库
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

## Current Phase — DeepSeek Real End-to-End Validation

### 目标

当前立即目标不是再扩展新能力，而是完成真实环境下的文本 summary 闭环验收：

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

## Current Phase — OpenAICompatibleProvider + Relay Integration

### 目标

在保留现有 `DeepSeekProvider` 的前提下，新增第二个 provider：

```text
SummaryService
→ LLMProvider
→ OpenAICompatibleProvider
→ 中转站 API
```

当前实际部署目标：

- `LLM_PROVIDER=openai_compatible`
- `OPENAI_COMPATIBLE_BASE_URL=https://4router.net/v1`
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

## Current Phase — QQ Official Bot Actions + Command E2E

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
- real QQ smoke pending
- 尚未做真实群 `#ping`
- 尚未做真实群 `#总结`

## Current Phase — QQ Official Webhook-ready Inbound Architecture

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
- real QQ webhook smoke pending
- `GROUP_MESSAGE_CREATE` automated code support: yes
- `GROUP_MESSAGE_CREATE` real webhook verification: pending
- 若真实 webhook 只收到 `GROUP_AT_MESSAGE_CREATE`，则保留 WebSocket 为当前正式 transport，不伪造“全量普通群消息能力”

## Next Phase — Provider Configuration / Selection

在 provider 扩展后，加入最小可用的 provider 选择配置：

```text
LLM_PROVIDER
    ├── deepseek
    └── openai_compatible
```

第一版只需要简单显式配置，不需要复杂 registry 或 plugin framework。

## Next Phase — QQ Official Parser Cross-check

DeepSeek 文本闭环稳定后，安排独立的 parser cross-check 阶段。

目标不是立刻重构 parser，而是逐项核对：

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

只有在三方交叉验证后，才决定是否需要调整 parser 细节。

如果未来需要 reply / forward 补全，统一走概念化的 `MessageReferenceResolver` 边界，而不是把历史平台 action 名称带回当前架构。

## Next Phase — Multimodal Foundation

文本 summary 闭环和 provider 抽象稳定后，再开始图片多模态基础：

```text
InternalMessage
        ↓
MediaResolver
        ↓
Image / Media metadata
        ↓
MultimodalPolicy
        ↓
是否需要理解图片？
        ├── No  → 普通文本 LLM
        └── Yes → vision-capable provider/model
                    ↓
             ImageUnderstandingResult
```

当前不要把图片理解混进 `SummaryService`。

## Next Phase — Mention / Reply Chat

后续聊天回复方向固定为：

```text
QQ Official incoming message
        ↓
TriggerPolicy
        ↓
ConversationContextService
        ↓
MessageRenderer
        ↓
ChatReplyService
        ↓
LLMProvider
        ↓
QQ Official passive reply
```

`ChatReplyService` 不负责：

- QQ HTTP
- token 获取
- Gateway
- 数据库 SQL

QQ outbound service 不负责：

- prompt
- context selection
- summary
- AI reasoning

## Later — Context Expansion

未来群聊上下文不应等同于“每次直接把最近 100 条消息全部塞给模型”。

需要引入：

```text
ConversationContextService
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
ConversationContextService 查询更多历史
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

## Later — QA

第一版检索 / QA 路线应保持轻量：

```text
SQLite time-range query
↓
SQLite keyword search / FTS5
↓
ConversationContextService
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
