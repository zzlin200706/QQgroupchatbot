# ARCHITECTURE.md

文档更新时间：`2026-08-11`

## 项目定位

`qqgroupchatbot` 是一个运行在 VMware Ubuntu 上的轻量、单机 `QQ Official Bot` 群聊 AI Bot。

active design 只围绕：

```text
QQ Official inbound transport / OpenAPI
+ Python
+ SQLite
+ local filesystem
+ external LLM API
```

展开，不再维护 QQ 小号 + NapCat + OneBot 双路线架构。

## 当前运行时架构

当前代码已经落地并由测试覆盖的核心数据流是：

```text
QQ Official WebSocket / Webhook
        ↓
QQOfficialInboundEvent
        ↓
QQOfficialEventProcessor
        ↓
QQOfficialRawEventIngestionService
        ↓
raw_events
        ↓
QQOfficialMessageParser
        ↓
InternalMessage
        ↓
MessageRepository
        ↓
SQLite
        ↓
QQOfficialInteractionDispatcher
├── QQOfficialCommandDispatcher
│   ├── #ping
│   └── #总结 → SummaryService
└── GroupAssistantHandler
    ├── #问 → grounded QA
    └── structured @bot → contextual chat
        ↓
GroupAssistantContextBuilder
        ↓
ConversationQueryService + AssistantInteractionRepository
        ↓
MessageRenderer
        ↓
shared LLMProvider
        ↓
QQOfficialGroupMessageSender
        ↓
successful assistant_interactions persistence
```

其中：

- `raw_events`、`messages`、`message_nodes`、`summaries`、`assistant_interactions` 和 assistant trigger claims 都在同一个 SQLite 中
- `FastAPI` 当前提供 `/health`、应用生命周期托管和 QQ Official webhook endpoint
- `SUMMARY_COMMAND_ENABLED=false` 时，不启动 summary handler，但 raw/normalized persistence 仍正常工作
- `GROUP_ASSISTANT_ENABLED=false` 时，`#问` / `@bot` 不触发 LLM，但入站消息仍照常持久化
- 当前 summary reply 只走由入站消息触发的被动回复链路，不把“只有 `group_id` 的主动群发”当作当前 phase 范围
- 当前 inbound transport 由 `QQ_EVENT_TRANSPORT` 选择，默认仍为 `websocket`
- webhook 架构已经通过自动化验证，真实 QQ webhook smoke 仍 pending

## 当前已落地组件

### `app/adapters/qq_official/`

- `auth.py`：AccessToken 获取
- `gateway.py`：`/gateway/bot`、HELLO / IDENTIFY / HEARTBEAT、dispatch、reconnect state
- `inbound.py`：transport-neutral inbound event model
- `message_api.py`：群消息发送
- `redaction.py`：本地 sample 与日志安全脱敏
- `webhook.py`：callback validation、Ed25519 签名校验、webhook event adaptation

### `app/parsers/`

- `qq_official_message_parser.py`：把仓库约定的 raw envelope 解析成 `InternalMessage`

### `app/domain/messages/`

- 严格的内部消息模型、身份模型、provenance 与 segment 树

### `app/storage/`

- `raw_event_repository.py`
- `message_repository.py`
- `summary_repository.py`
- `assistant_interaction_repository.py`
- `message_codec.py` / `summary_codec.py`

### `app/services/`

- `QQOfficialRawEventIngestionService`
- `QQOfficialNormalizedMessageIngestionService`
- `QQOfficialEventProcessor`
- `ConversationQueryService`
- `SummaryService`
- `SummaryCommandHandler`
- `QQOfficialInteractionDispatcher`
- `GroupAssistantContextBuilder`
- `GroupAssistantService`
- `GroupAssistantHandler`

### `app/rendering/`

- `MessageRenderer`
- `SummaryMessageFormatter`

### `app/llm/`

- `LLMRequest` / `LLMResponse`
- `LLMProvider` 协议
- `DeepSeekProvider`
- `OpenAICompatibleProvider`

## 当前组件边界

### Gateway Client

- 负责获取接入点、建立 WebSocket、处理 HELLO / IDENTIFY / HEARTBEAT、收 dispatch、重连。
- 不做 parser。
- 不做数据库业务。
- 不做 summary 或 LLM 调用。
- 当前仍保留并继续支持，未因 webhook phase 删除。

### Webhook Adapter

- 负责 callback validation（`op=13`）
- 负责 Ed25519 签名校验
- 负责把 HTTP body + headers 适配成 `QQOfficialInboundEvent`
- 不访问数据库
- 不调用 parser / summary / LLM / sender

### Event Processor

- 负责 `QQOfficialInboundEvent → raw → normalized → interaction dispatch`
- WebSocket 与 Webhook 共用同一条业务链
- command / assistant interaction 仍以后台 task 形式执行，不让 transport 层等待 LLM

### Raw Event Ingestion

- 负责把 transport-neutral inbound event 写入 `raw_events`。
- `raw_payload` 当前统一保留 QQ 官方 top-level envelope：

```json
{
  "id": "event_id",
  "op": 0,
  "s": 123,
  "t": "GROUP_MESSAGE_CREATE",
  "d": {
    "...": "..."
  }
}
```

- 只复制明确存在的 query/debug index。
- `raw_payload` 永远保留完整事实。

### Parser

- 只消费上面的 raw envelope。
- 不访问网络。
- 不推断缺失身份。
- 只在 payload 或 sample 明确给出时结构化 `mentions`、`attachments` 等字段。

当前 parser 的几个关键边界：

- `message_type == 102`：保存为 unresolved `ForwardSegment`
- `message_type == 103`：保留 opaque `msg_elements` / `message_scene`，并把一致、格式有效的 target `REFIDX` 保存为 `ReplySegment.reference_key`
- mention 只有在 `content` token 与 `mentions[]` 明确对应时才生成 `AtSegment`
- mention 的 `is_self` 只来自明确布尔 `mentions[].is_you`；`GROUP_AT_MESSAGE_CREATE` 由官方事件类型确认 Bot mention
- 未确认结构一律保留为 `UnknownSegment`

parser 不访问 SQLite，也不解析 quoted author。当前 runtime 不做 reply target lookup；未来如果需要 reply / forward 网络补全，仍应通过概念接口实现：

```python
class MessageReferenceResolver:
    async def resolve_reply(...):
        ...

    async def resolve_forward(...):
        ...
```

`reference_key` 是 QQ opaque lookup key，不是 `platform_message_id`。`referenced_message_id` 在没有真实 message ID 时继续为 `None`。

### Storage

- `raw_events` 是 parser replay 和字段追溯的事实源头
- `messages + message_nodes` 当前用于保存可重建的 `InternalMessage` 树
- `summaries` 只保存 validated summary result 与最小必要元数据
- `assistant_interactions` 只保存已经成功发送给 QQ 的 Bot turn，包括可选 `response_message_id`；outbound 不伪造成 raw event 或 `InternalMessage`
- `assistant_trigger_claims` 对 `(platform, group_id, trigger_message_id, trigger_type)` 做轻量持久化去重
- 不保存 prompt、完整 conversation、provider body 或 secret

### Rendering

- `MessageRenderer` 负责把严格的 `InternalMessage[]` 渲染成给 LLM 的有损文本
- renderer 可以为 presentation 做有损折叠
- renderer 不能反过来成为事实源头

### Summary

- `SummaryService` 只依赖：

```text
ConversationQueryService
MessageRenderer
LLMProvider
```

- 不直接依赖 Gateway、QQ HTTP、SQLAlchemy 或具体 provider 实现
- 当前 summary command 的顺序固定为：

```text
generate
→ persist
→ format
→ send
```

### Outbound

- `QQOfficialGroupMessageSender` 只负责 token、HTTP 请求与响应校验
- 不做 prompt、上下文选择、summary 逻辑

### Group Assistant

- `#问` 只使用同 platform、同 group、当前 trigger 之前的 `InternalMessage`；历史 Bot 回答不进入 grounded evidence
- `@bot` 使用最近群消息和同群成功 `assistant_interactions`，按 trigger timestamp 合并为 role-aware timeline
- trigger 优先级是 `#ping → #总结 → #问 → structured @bot → ordinary`
- reply 本身永远不是 assistant trigger；没有 `#问` 或 structured `@bot` 时只入库，不 claim、不调用 LLM
- structured `@bot + reply` 使用 parser 已保留的 quoted content，并以 `untrusted-platform-data` block 进入 Chat
- quoted content 不提供 author identity，不能按正文匹配 Bot 或群成员，也不能覆盖 system prompt
- `#问 + reply` 仍只使用 inbound 群历史作为 grounded evidence，不把历史 Bot answer 当事实
- assistant 顺序固定为 `claim trigger → generate → send → persist successful turn`

## LLM 边界

当前 LLM 架构已经稳定为：

```text
ConversationQueryService
        ↓
InternalMessage[]
        ↓
MessageRenderer
        ↓
SummaryService / GroupAssistantService
        ↓
LLMProvider
        ↓
validated domain result / AssistantResult
```

`DeepSeekProvider` 和 `OpenAICompatibleProvider` 都由配置选择；summary 与 assistant 共享同一个 provider 实例和生命周期。

禁止：

```python
prompt = str(raw_gateway_payload)
```

也禁止让业务 Service 直接面向某个具体平台品牌。

## 未来目标架构

在保持单机轻量部署前提下，后续 phase 的目标架构统一按下面的概念边界扩展：

```text
QQ Official WebSocket / Webhook
        ↓
QQOfficialInboundEvent
        ↓
QQOfficialEventProcessor
        ↓
QQOfficialRawEventIngestionService
        ↓
raw_events
        ↓
QQOfficialMessageParser
        ↓
InternalMessage
        ↓
MessageRepository
        ↓
SQLite
        ↓
ConversationQueryService
        ↓
MessageRenderer
        ↓
AI Processing
        ↓
LLMProvider
        ↓
Summary / Chat / QA
        ↓
Bot Action / Outbound Service
        ↓
QQ Official OpenAPI
```

AI 部分当前形态是：

```text
InternalMessage[]
        ↓
GroupAssistantContextBuilder
        ↓
MessageRenderer
        ↓
LLMProvider
        ├── DeepSeekProvider
        └── OpenAICompatibleProvider
        ↓
validated domain result
```

其中以下组件仍是 `future`：

- `MediaResolver`
- `MultimodalPolicy`
- `MultimodalContentBuilder`

## 触发策略

当前 runtime 的 precedence-aware trigger 是：

```text
1. exact `#ping`
2. exact `#总结`
3. exact-prefix `#问 <问题>`
4. `GROUP_AT_MESSAGE_CREATE` 或结构化 `AtSegment.is_self is True`
5. ordinary message or reply without an explicit trigger: no action
```

一个 message 最多选择一个 handler。显示名称 substring 和 reply reference 都不作为 Bot trigger 证据。

## Storage 路线

本项目的 storage 方向是：

> Internal Message Model 严格，Storage implementation 轻量。

当前表结构：

```text
raw_events
messages
message_nodes
summaries
assistant_trigger_claims
assistant_interactions
```

这是当前有效实现，不要求为了“更规范”继续拆成更多子表。

默认长期部署方案仍然是：

```text
SQLite
+ local filesystem
```

对于未来图片/媒体能力，推荐保留：

```text
data/
├── qqgroupchatbot.db
└── media/
    └── ...
```

SQLite 保存 metadata、relative path、sha256、download status、derived description reference；不要默认把大图片作为 SQLite BLOB 保存。

## FastAPI 定位

FastAPI 不是 Bot 本身，只作为：

```text
health
debug
query
admin
manual API
```

当前已实现 `/health`。未来可以增加 message history、summaries、manual replay，但不需要为了“标准后台”建立大量 CRUD。

## 推荐目录方向

当前仓库实际结构已经接近下面的方向：

```text
app/
├── adapters/
│   └── qq_official/
├── domain/
│   ├── messages/
│   ├── summaries/
│   └── assistant_interactions/
├── parsers/
│   └── qq_official_message_parser.py
├── storage/
├── rendering/
├── services/
│   ├── conversation_query.py
│   ├── summary.py
│   ├── summary_command.py
│   ├── group_assistant_context.py
│   ├── group_assistant.py
│   ├── group_assistant_handler.py
│   └── media.py                     # future
├── llm/
│   ├── models.py
│   └── providers/
│       ├── base.py
│       ├── deepseek.py
│       └── openai_compatible.py
└── bot_actions/
    └── qq_official/                 # future abstraction
```

标注为 `future` 的文件不代表当前应立即创建。
