# MESSAGE_MODEL.md — 内部消息与身份模型

> 这里描述“概念模型”。Codex 可以用 Pydantic/dataclass 实现，但字段语义不得改变。

## 1. IdentityRef

```python
class IdentityRef:
    platform_user_id: str | None
    display_name: str | None

    status: Literal[
        "known",
        "unknown",
        "unavailable",
    ]

    source: Literal[
        "event",
        "forward_node",
        "resolved_message",
        "unknown",
    ]
```

区别：

- `unknown`：当前无法判断是谁；
- `unavailable`：当前平台数据明确没有给出可用作者信息；
- `known`：有明确来源。

不要用空字符串表示未知。

---

## 2. NormalizedEvent

```python
class NormalizedEvent:
    platform: Literal["qq"]
    protocol: Literal["onebot11"]

    event_type: str
    platform_message_id: str | None
    group_id: str | None

    event_sender: IdentityRef
    event_time: datetime

    raw_message: list[dict]
    raw_event_id: UUID
```

所有 QQ ID 在 domain 层建议使用 `str`，避免未来不同平台 ID 类型差异。

---

## 3. InternalMessage

```python
class InternalMessage:
    internal_id: UUID

    platform: str
    platform_message_id: str | None
    group_id: str | None

    event_sender: IdentityRef
    author: IdentityRef

    timestamp: datetime

    nodes: list["MessageNode"]

    raw_event_id: UUID
    parser_version: str
```

---

## 4. MessageNode

使用树模型：

```python
class MessageNode:
    node_id: UUID

    kind: str
    position: int
    depth: int

    author: IdentityRef | None

    data: dict
    children: list["MessageNode"]
```

`kind` 可包含：

```text
text
at
reply
image
file
record
video
face
mface
json
forward
forward_node
unknown
```

---

## 5. text

```text
kind = text
data = {
  "text": ...
}
```

---

## 6. at

```text
kind = at
data = {
  "target_user_id": ...
}
```

如果 OneBot payload 的当前字段与此概念字段不同，由 parser 映射。

不要让 domain model 使用 OneBot 特定字段命名。

---

## 7. reply

```text
kind = reply
data = {
  "reply_to_platform_message_id": ...,
  "resolution_status": "unresolved" | "resolved" | "invalid_reference" |
                       "fetch_failed" | "invalid_response"
}
children = [
  可选的 resolved quoted message
]
```

quoted message 的作者来自 quoted message，自身不会改变父消息作者。

---

## 8. image

```text
kind = image
data = {
  "platform_file": ...,
  "url": ...,
  "summary": ...,
  "sub_type": ...,
  "file_size": ...,
  "local_media_id": ...
}
```

字段缺失允许为 None。

原始字段完整保存在：

```text
raw_data
```

可以把 `raw_data` 放到 `data["raw"]` 或数据库 payload JSON 中。

---

## 9. file

```text
kind = file
data = {
  "name": ...,
  "platform_file_id": ...,
  "file_size": ...,
  "url": ...,
  "path": ...
}
```

只映射 payload 确实存在的字段。

---

## 10. forward

```text
kind = forward
data = {
  "forward_id": ...,
  "resolution_status": "unresolved" | "embedded" | "fetched" |
                       "invalid_reference" | "fetch_failed" | "invalid_response" |
                       "depth_limit"
}
children = [
  ForwardNode,
  ForwardNode,
  ...
]
```

---

## 11. ForwardNode

```text
kind = forward_node
author = IdentityRef(...)
data = {
  "platform_node_id": ...,
  "timestamp": ...
}
children = [
  text/image/reply/forward/...
]
```

如果 sender 缺失：

```python
author = IdentityRef(
    platform_user_id=None,
    display_name=None,
    status="unavailable",
    source="unknown",
)
```

绝不从父节点复制。

---

## 12. Reference enrichment

`ReplySegment` 可在纯 parser 之后由 `get_msg` 补全为独立的 resolved-message reference；它保留
platform message id、author、timestamp、segments 和来自 action response 的单独 raw data。该 author
只能来自 resolved message 自身明确提供的字段，不能继承当前消息发送者。

`ForwardSegment` 的事件 raw data 与远端 `get_forward_msg` response data 分开保存。已 embedded 的
forward 不重复请求网络。单次 enrichment 对相同 reference id 做内存 cache；不做 nested unresolved
forward 的递归网络获取。

---

## 13. 例子：嵌套转发

群内实际事件：

```text
A 发送：
└─ 合并转发 F1
   ├─ B: "hello"
   └─ 合并转发 F2
      ├─ C: [image]
      └─ 作者缺失: "old message"
```

内部表示：

```text
InternalMessage
author=A
event_sender=A
└── forward F1
    ├── forward_node author=B
    │   └── text "hello"
    │
    └── forward_node author=unknown-or-explicit-node-author
        └── forward F2
            ├── forward_node author=C
            │   └── image
            └── forward_node author=unavailable
                └── text "old message"
```

注意：

最后一条绝对不能成为：

```text
A: old message
```

也不能凭上一节点猜成：

```text
C: old message
```

---

## 13. LLM Renderer 示例

内部树是完整结构。

给 LLM 时可以渲染成：

```text
[A] 转发了一组消息：
  - [B] hello
  - [嵌套转发]
      - [C] [图片]
      - [原作者不可用] old message
```

Renderer 是有损表示。

数据库不是。
