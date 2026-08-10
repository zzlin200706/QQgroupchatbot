# ARCHITECTURE.md — qqgroupchatbot 架构设计

## 1. 总体数据流

```text
┌──────────────────────────────┐
│ 独立 QQ Bot 账号             │
└───────────────┬──────────────┘
                │ NTQQ
                ▼
┌──────────────────────────────┐
│ NapCatQQ                     │
│ OneBot 11                    │
└───────────────┬──────────────┘
                │ WebSocket
                ▼
┌──────────────────────────────┐
│ OneBot Adapter               │
│ connect / reconnect / action │
└───────────────┬──────────────┘
                │ raw JSON
                ▼
┌──────────────────────────────┐
│ Event Normalizer             │
└───────────────┬──────────────┘
                ▼
┌──────────────────────────────┐
│ Message Parser               │
│ pure raw-data parsing        │
└───────────────┬──────────────┘
                ▼
┌──────────────────────────────┐
│ Internal Message Model       │
└──────────┬───────────┬───────┘
           │           │
           ▼           ▼
      ┌─────────┐  ┌──────────┐
      │ Storage │  │ Bot Cmds │
      └────┬────┘  └────┬─────┘
           │             │
      ┌────▼─────┐       │
      │ FastAPI  │       │
      │ Query    │       │
      └──────────┘       │
                         ▼
                  OneBot Actions
```

未来 AI：

```text
Storage
  ↓
Conversation Query
  ↓
Message Renderer
  ↓
Summary / QA
  ↓
LLM Provider
  ↓
Bot Actions
```

---

## 2. 为什么不用“一个 FastAPI webhook + 一个 messages 表”

因为这个项目不是纯文本聊天机器人。

QQ 消息可能包含：

```text
message
├── text
├── at
├── image
├── reply
└── forward
    ├── node(sender=B)
    │   └── text
    └── node(sender=C)
        └── forward
            └── node(sender=unknown)
                └── image
```

如果最开始转换成：

```text
sender=A
content="..."
```

会永久丢失：

- 消息段顺序；
- reply 关系；
- image/file 元数据；
- forward 层级；
- node sender；
- nested forward sender；
- 作者是否未知。

因此内部模型必须是树。

---

## 3. Adapter 与业务分离

`OneBotAdapter` 对上层暴露概念接口：

```python
async def run_event_loop(...)
async def call_action(action: str, params: dict) -> dict
async def get_message(message_id: str) -> dict
async def get_forward_message(forward_id: str) -> dict
async def send_group_message(group_id: str, message: object) -> dict
```

这里是概念接口，不表示 OneBot 当前 action 参数一定完全如此。

实现 action 前必须依据当前 NapCat / OneBot 文档确认参数字段。

---

## 4. Event Normalizer

输入：

```python
raw_event: dict
```

输出：

```python
NormalizedEvent
```

Normalizer 不应该递归请求 forward 内容。

它只确定：

- platform；
- event kind；
- top-level group；
- top-level sender；
- timestamp；
- platform message id；
- raw message segments；
- raw event reference。

复杂解析交给 Parser。

---

## 5. Message Parser and Reference Enrichment

建议主接口：

```python
def parse_message(
    raw_event: dict,
    *,
    raw_event_id: int | None,
) -> InternalMessage | None
```

Parser 是 raw-data-first 的纯解析层：不访问 WebSocket、不调用 OneBot action、不写数据库，输入也不
会被修改。它可保留 embedded forward 的递归树，但只有 forward id 时保持 unresolved。

可选的后续 enrichment 才通过 adapter action 补全 reference：

```text
InternalMessage
  ↓
ReferenceEnrichmentService
  ├─ reply id   → get_msg
  └─ forward id → get_forward_msg
```

enrichment 返回新对象，并保留原对象的 raw data。它不在收包 callback 中执行。本阶段只补全直接
unresolved reference；nested unresolved forward 的网络递归、循环检测及 traversal 留给后续 Phase 6。

```text
forward event content present → parser marks embedded
forward id only             → parser marks unresolved
optional get_forward_msg    → enrichment marks fetched or a safe failure status
```

---

## 6. 身份传播规则

只允许“明确来源”传播。

### 顶层群消息

```text
event_sender = event.sender
message_author = event.sender
```

这是因为当前 message 就是该 event_sender 在群内发送的消息。

### forward segment

```text
forwarder = 当前包含 forward segment 的 message_author
```

但是：

```text
forward_node.author
```

必须来自 node 自身返回数据。

如果 node 没有作者：

```text
unknown
```

绝不继承 forwarder。

### nested forward

同样逐层处理。

---

## 7. 存储建议

### raw_event

任何 parser 崩溃都不能导致 raw event 丢失。

推荐 ingest 顺序：

```text
receive raw event
      ↓
dedup
      ↓
save raw event
      ↓
commit
      ↓
normalize / parse
      ↓
save normalized form
```

如果 parse 失败：

```text
raw_event.status = parse_failed
error = ...
```

以后可以 replay。

### normalized tree

可以选择两种方式：

A. 多表关系模型；
B. messages + generic message_nodes 表。

第一版推荐 B，减少 schema 频繁变化。

例如概念上：

```text
messages
message_nodes
identities
raw_events
```

`message_nodes`：

```text
id
message_id
parent_node_id
node_kind
position
depth
author_id
author_name
author_status
author_source
payload_json
```

这样 text/image/forward/forward_node 都可以表达成树节点。

如果 Codex 判断多表更清晰，也可以实现，但必须保证完整树结构可重建。

---

## 8. 消息重放

因为保存了 raw event，可以提供 parser 升级后的重放能力：

```text
raw_events
    ↓
replay
    ↓
new parser version
    ↓
normalized representation
```

建议保留：

```text
parser_version
normalizer_version
```

第一版可以是常量字符串。

---

## 9. 并发

消息进入时不要让长时间的 forward resolution 阻塞所有事件。

但是第一阶段也不要上复杂队列。

可以：

```text
WS receive loop
  ↓
bounded asyncio.Queue
  ↓
1~N ingest worker
```

要求：

- queue 有容量上限；
- shutdown 可优雅等待；
- 同一 event 去重；
- SQLite 写并发保持保守。

默认 1 个 DB ingest worker 完全可接受。

---

## 10. 故障策略

### NapCat 断线

- 指数退避；
- 有上限；
- 成功后重置退避；
- `/health` 显示 disconnected。

### OneBot action timeout

- action 有 timeout；
- forward resolution 失败时保留 unresolved forward；
- 不让整条消息丢失。

### DB 写失败

- 记录 error；
- 不把事件标记为成功；
- 不伪造成功。

---

## 11. AI 层边界

未来增加：

```text
app/ai/
├── providers/
├── renderer.py
├── summarizer.py
└── qa.py
```

AI 看见的内容是 Renderer 输出，而不是 QQ raw JSON。

Renderer 可以按需求把树“渲染”为文本，但存储层仍保持完整树。

这就是：

```text
storage representation != LLM prompt representation
```

二者不要混为一谈。
