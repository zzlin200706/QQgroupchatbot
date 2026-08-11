# ARCHITECTURE.md

文档更新时间：`2026-08-11`

## 项目定位

`qqgroupchatbot` 是一个运行在 VMware Ubuntu 上的轻量、单机 `QQ Official Bot` 群聊 AI Bot。

active design 只围绕：

```text
QQ Official Gateway / OpenAPI
+ Python
+ SQLite
+ local filesystem
+ external LLM API
```

展开，不再维护 QQ 小号 + NapCat + OneBot 双路线架构。

## 当前运行时架构

当前代码已经落地并由测试覆盖的核心数据流是：

```text
QQ Official Gateway
        ↓
QQOfficialGatewayClient
        ↓
QQGatewayDispatch
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
SummaryService
        ↓
LLMProvider
        ↓
validated SummaryResult
        ↓
SummaryRepository
        ↓
SummaryMessageFormatter
        ↓
QQOfficialGroupMessageSender
        ↓
QQ Official passive reply
```

其中：

- `raw_events`、`messages`、`message_nodes`、`summaries` 都在同一个 SQLite 中
- `FastAPI` 当前只提供 `/health` 和应用生命周期托管
- `SUMMARY_COMMAND_ENABLED=false` 时，不启动 summary handler，但 raw/normalized persistence 仍正常工作
- 当前 summary reply 只走由入站消息触发的被动回复链路，不把“只有 `group_id` 的主动群发”当作当前 phase 范围

## 当前已落地组件

### `app/adapters/qq_official/`

- `auth.py`：AccessToken 获取
- `gateway.py`：`/gateway/bot`、HELLO / IDENTIFY / HEARTBEAT、dispatch、reconnect state
- `message_api.py`：群消息发送
- `redaction.py`：本地 sample 与日志安全脱敏

### `app/parsers/`

- `qq_official_message_parser.py`：把仓库约定的 raw envelope 解析成 `InternalMessage`

### `app/domain/messages/`

- 严格的内部消息模型、身份模型、provenance 与 segment 树

### `app/storage/`

- `raw_event_repository.py`
- `message_repository.py`
- `summary_repository.py`
- `message_codec.py` / `summary_codec.py`

### `app/services/`

- `QQOfficialRawEventIngestionService`
- `QQOfficialNormalizedMessageIngestionService`
- `ConversationQueryService`
- `SummaryService`
- `SummaryCommandHandler`

### `app/rendering/`

- `MessageRenderer`
- `SummaryMessageFormatter`

### `app/llm/`

- `LLMRequest` / `LLMResponse`
- `LLMProvider` 协议
- `DeepSeekProvider`

## 当前组件边界

### Gateway Client

- 负责获取接入点、建立 WebSocket、处理 HELLO / IDENTIFY / HEARTBEAT、收 dispatch、重连。
- 不做 parser。
- 不做数据库业务。
- 不做 summary 或 LLM 调用。

### Raw Event Ingestion

- 负责把 `QQGatewayDispatch` 写入 `raw_events`。
- 存储的 envelope 统一为：

```json
{
  "gateway": {
    "op": 0,
    "s": 123,
    "t": "GROUP_MESSAGE_CREATE"
  },
  "data": {
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
- `message_type == 103`：reply 只保存 opaque `msg_elements` / `message_scene`
- mention 只有在 `content` token 与 `mentions[]` 明确对应时才生成 `AtSegment`
- 未确认结构一律保留为 `UnknownSegment`

未来如果需要 reply / forward 补全，应该通过概念接口实现，而不是把某个平台 action 名称写死成领域边界。例如：

```python
class MessageReferenceResolver:
    async def resolve_reply(...):
        ...

    async def resolve_forward(...):
        ...
```

当前 runtime 尚未实现这一层。

### Storage

- `raw_events` 是 parser replay 和字段追溯的事实源头
- `messages + message_nodes` 当前用于保存可重建的 `InternalMessage` 树
- `summaries` 只保存 validated summary result 与最小必要元数据
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

## LLM 边界

当前 LLM 架构已经稳定为：

```text
ConversationQueryService
        ↓
InternalMessage[]
        ↓
MessageRenderer
        ↓
SummaryService
        ↓
LLMProvider
        ↓
validated domain result
```

`DeepSeekProvider` 只是当前第一个真实 provider，不是业务层依赖对象。

禁止：

```python
prompt = str(raw_gateway_payload)
```

也禁止让业务 Service 直接面向某个具体平台品牌。

## 未来目标架构

在保持单机轻量部署前提下，后续 phase 的目标架构统一按下面的概念边界扩展：

```text
QQ Official Gateway
        ↓
QQOfficialGatewayClient
        ↓
QQGatewayDispatch
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

AI 部分的 future 形态是：

```text
InternalMessage[]
        ↓
ConversationContextService          # future
        ↓
MessageRenderer / MultimodalContentBuilder   # future
        ↓
LLMProvider
        ├── DeepSeekProvider
        └── OpenAICompatibleProvider         # future
        ↓
validated domain result
```

其中以下组件是 `future`，本次不是已实现功能：

- `ConversationContextService`
- `TriggerPolicy`
- `ChatReplyService`
- `QAService`
- `MediaResolver`
- `MultimodalPolicy`
- `MultimodalContentBuilder`
- `OpenAICompatibleProvider`

## 触发策略

当前 runtime 只实现严格的 `#总结`：

```text
platform == "qq_official"
provenance == direct_event
sub_type == GROUP_MESSAGE_CREATE
top-level exact text == #总结
group_id exists
platform_message_id exists
timestamp is timezone-aware
```

未来扩展的触发边界应该通过 `TriggerPolicy` 表达，例如：

- `SummaryCommandTrigger`
- `MentionTrigger`
- `ReplyTrigger`
- `PendingInteractionTrigger`

但这些都是 future，不要在当前业务逻辑里提前写死。

## Storage 路线

本项目的 storage 方向是：

> Internal Message Model 严格，Storage implementation 轻量。

当前表结构：

```text
raw_events
messages
message_nodes
summaries
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
│   └── summaries/
├── parsers/
│   └── qq_official_message_parser.py
├── storage/
├── rendering/
├── services/
│   ├── conversation_query.py
│   ├── summary.py
│   ├── summary_command.py
│   ├── conversation_context.py      # future
│   ├── chat_reply.py                # future
│   └── media.py                     # future
├── llm/
│   ├── models.py
│   └── providers/
│       ├── base.py
│       ├── deepseek.py
│       └── openai_compatible.py     # future
└── bot_actions/
    └── qq_official/                 # future abstraction
```

标注为 `future` 的文件不代表当前应立即创建。
