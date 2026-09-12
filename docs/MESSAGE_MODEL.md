# MESSAGE_MODEL.md

文档更新时间：`2026-08-12`

## 目标

`InternalMessage` 是本项目对 QQ Official 消息事实的稳定内部表示。

边界固定为：

- `raw event`：事实源头，完整保留
- `InternalMessage`：严格、可重建、可供业务使用的内部模型
- `MessageRenderer`：给 LLM 和展示层使用的有损 presentation

禁止把 renderer 文本或 AI 推断结果回写成消息事实。

## Identity

当前代码中的身份相关枚举和值是：

```python
class IdentityAvailability(str, Enum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class IdentitySource(str, Enum):
    EVENT = "event"
    FORWARD_NODE = "forward_node"
    RESOLVED_MESSAGE = "resolved_message"
    UNKNOWN = "unknown"
```

```python
@dataclass(frozen=True)
class IdentityRef:
    platform: str
    user_id: str | None
    display_name: str | None
    card: str | None
    source: IdentitySource
    availability: IdentityAvailability
```

语义：

- `known`：平台明确提供了可用身份
- `unknown`：当前无法判断是谁
- `unavailable`：当前 payload 明确没有提供可用身份

禁止把外层转发者、reply 发送者或 AI 猜测结果替代为原作者。

## Provenance

当前代码中的 provenance 相关枚举和值是：

```python
class ProvenanceSource(str, Enum):
    DIRECT_EVENT = "direct_event"
    RESOLVED_REFERENCE = "resolved_reference"
    FORWARD_NODE = "forward_node"
    NESTED_FORWARD_NODE = "nested_forward_node"
```

```python
@dataclass(frozen=True)
class MessageProvenance:
    source_type: ProvenanceSource
    raw_event_id: int | None
    parent_message_id: str | None = None
    forward_depth: int = 0
```

当前 summary command 只接受：

```text
source_type == direct_event
```

## MessageContext

```python
@dataclass(frozen=True)
class MessageContext:
    message_type: str | None
    sub_type: str | None
    group_id: str | None
```

当前 QQ Official runtime 中：

- `message_type`：来自 payload 的明确字段，如 `0`、`102`、`103`
- `sub_type`：Gateway 事件类型，如 `GROUP_MESSAGE_CREATE`
- `group_id`：当前 parser 直接从仓库 sample 中的 `data.group_id` 读取

如果 payload 里还有 `group_openid` 等字段，但当前 parser 没有安全归一化规则，就继续保留在 `raw_payload` 中，不要猜测性迁移。

## InternalMessage

```python
@dataclass(frozen=True)
class InternalMessage:
    platform: str
    source_raw_event_id: int | None
    platform_message_id: str | None
    context: MessageContext
    actor: IdentityRef
    author: IdentityRef
    timestamp: datetime | None
    segments: tuple[MessageSegment, ...]
    provenance: MessageProvenance
```

当前 runtime 固定：

```text
platform == "qq_official"
```

当前 parser 中：

- `actor` 与 `author` 都来自 `data.author`
- 两者目前相同，不代表未来永远相同
- 缺失身份时必须显式标成 `unknown` 或 `unavailable`

## Segment Hierarchy

当前代码中的基础 segment 是：

```python
@dataclass(frozen=True)
class Segment:
    position: int
    raw_data: Any
```

当前 `MessageSegment` 联合类型是：

```text
TextSegment
AtSegment
ImageSegment
ReplySegment
FileSegment
ForwardSegment
UnknownSegment
```

### TextSegment

```python
@dataclass(frozen=True)
class TextSegment(Segment):
    text: str | None
```

### AtSegment

```python
@dataclass(frozen=True)
class AtSegment(Segment):
    target: str | None
    is_all: bool
    display_name: str | None = None
    is_self: bool | None = None
```

当前 QQ Official parser 只有在 payload 的 `mentions[]` 明确确认目标时，才会生成结构化 `AtSegment`。`display_name` 来自明确的 mention nickname/username；`is_self` 只接受 payload 的布尔 `is_you`，缺失时保持 `None`，不根据显示名称猜测 Bot mention。

### ImageSegment

```python
@dataclass(frozen=True)
class ImageSegment(Segment):
    file: str | None
    url: str | None
    summary: str | None
    sub_type: str | None
    file_size: int | None
    content_type: str | None
    width: int | None
    height: int | None
    description: str | None
```

字段来源严格区分：

- `file/url/file_size/content_type/width/height` 只映射 QQ attachment 明确提供的值，缺失即 `None`
- `summary/sub_type` 保留内部模型已有的平台字段位，不凭空生成
- `description` 是 vision provider 产生的 derived enrichment，不是 QQ 原始事实

当前 parser 创建的 source `ImageSegment.description` 永远为 `None`。`ImageEnrichmentService` 只通过 `dataclasses.replace` 生成不可变运行时副本；不会覆盖 `raw_events`、原始 URL 或已存 `message_nodes`。失败时运行时副本使用中性 unavailable 描述，其余 segment 继续处理。

### FileSegment

```python
@dataclass(frozen=True)
class FileSegment(Segment):
    file: str | None
    name: str | None
    file_id: str | None
    file_size: int | None
    url: str | None
    path: str | None
```

### ReplySegment

```python
class ReplyResolutionStatus(str, Enum):
    UNRESOLVED = "unresolved"
    RESOLVED = "resolved"
    INVALID_REFERENCE = "invalid_reference"
    FETCH_FAILED = "fetch_failed"
    INVALID_RESPONSE = "invalid_response"
```

```python
@dataclass(frozen=True)
class ReplySegment(Segment):
    referenced_message_id: str | None
    reference_key: str | None = None
    resolution_status: ReplyResolutionStatus = ReplyResolutionStatus.UNRESOLVED
    resolved_message: ResolvedMessageReference | None = None
    resolved_raw_data: Any = None
```

当前 QQ Official parser 对 reply 的边界是：

- 保留原始 `msg_elements` / `message_scene` 等 opaque 数据
- 真实 `103` samples 中 reply element `msg_idx` 与 `message_scene.ext.ref_msg_idx` 一致时，保存为 `reference_key`
- `message_scene.ext.msg_idx` 是当前消息自己的 opaque index，不作为 reply target
- 不把 `ref_msg_idx` / `reference_key` 伪装成真实 `platform_message_id`
- 当前 runtime 不做网络补全

`reference_key` 只表达 QQ payload 中的 opaque reply reference，不参与 assistant trigger。它不是 `platform_message_id`，也不会用于推断 quoted author。

quoted element 的正文作为通用 reply 数据保留，其 `ResolvedMessageReference.author` 继续为 `unavailable`。只有消息同时具有 `#问` 或 structured `@bot` trigger 时业务层才会处理 AI；structured `@bot` 可把正文作为不可信平台上下文，单独 reply 不触发机器人。

### ResolvedMessageReference

```python
@dataclass(frozen=True)
class ResolvedMessageReference:
    platform_message_id: str | None
    author: IdentityRef
    timestamp: datetime | None
    segments: tuple[MessageSegment, ...]
    raw_data: Any
```

这是当前代码真实存在的模型，即使 runtime 还没有启用 reply 网络补全逻辑，文档也应保留它。

### ForwardNode

```python
@dataclass(frozen=True)
class ForwardNode:
    sender: IdentityRef
    timestamp: datetime | None
    content: tuple[MessageSegment, ...]
    provenance: MessageProvenance
    raw_data: Any
```

### ForwardSegment

```python
class ForwardResolutionStatus(str, Enum):
    UNRESOLVED = "unresolved"
    EMBEDDED = "embedded"
    FETCHED = "fetched"
    INVALID_CONTENT = "invalid_content"
    DEPTH_LIMIT = "depth_limit"
    NODE_LIMIT = "node_limit"
    INVALID_REFERENCE = "invalid_reference"
    FETCH_FAILED = "fetch_failed"
    INVALID_RESPONSE = "invalid_response"
```

```python
@dataclass(frozen=True)
class ForwardSegment(Segment):
    reference_id: str | None
    resolved: bool
    resolution_status: ForwardResolutionStatus
    content: tuple[MessageSegment, ...]
    nodes: tuple[ForwardNode, ...]
    resolved_raw_data: Any = None
```

当前 QQ Official parser 的关键边界：

- `message_type == 102` 当前按 unresolved forward 保存
- 不从展示文本里猜 node sender
- 不从展示文本里重建 nested tree
- 不从展示文本里反推图片/文件内容

也就是说，当前 runtime 保留：

```text
存在一个 forward 事实
```

但不伪造：

```text
forward node 作者
nested tree 结构
媒体内容
```

### UnknownSegment

```python
@dataclass(frozen=True)
class UnknownSegment(Segment):
    original_type: str | None
```

未知内容必须保留，不得静默丢弃。

## 当前 QQ Official parser 语义

当前 parser 的已证实行为包括：

- 接受 top-level 或 legacy wrapper 中的 `GROUP_MESSAGE_CREATE` / `GROUP_AT_MESSAGE_CREATE` group envelope
- `author` 直接来自 payload `author`
- 文本、mention、图片、文件按当前 sample 和官方文档明确字段解析
- QQ 将 `content` 与 `attachments` 分开提供时，只能证明 content segments 在前、attachment list 顺序在后；parser 不猜测不可取得的 text/image 视觉交错位置
- 不访问网络
- 不做 reply / forward 的远程补全
- reply reference 与 quoted content 保真保存，不改变 inbound author/actor，也不单独触发 Assistant
- 不猜测身份

这意味着：

- 当前 `InternalMessage` 是对仓库 sample 契约的严格表示
- 不是对所有可能 QQ Official payload 的“完整猜测实现”

## Storage 与 renderer 的边界

当前数据库实现使用：

```text
messages
+ message_nodes
```

保存 `InternalMessage` 树。

`MessageRenderer` 的职责是：

- 解析 `IdentityRef` 的展示标签
- 在 reply / forward / image / file / unknown 场景下生成安全文本
- 为 LLM 提供有损、可控、不会泄漏 raw payload 的文本表示
- 把 enriched image 渲染为 `[图片: ...]`，未 enriched image 保持 `[图片]`

它可以：

- 把 unresolved forward 渲染成 `[合并转发：内容未解析]`
- 把不可用作者渲染成 `[原作者不可用]`

但它不能：

- 成为新的事实源
- 替代 `InternalMessage`
- 覆盖 `raw_events`

## Image Enrichment Boundary

```text
stored InternalMessage / ImageSegment
→ ImageResourceResolver
→ ResolvedImage(bytes, content_type)
→ VisionProvider
→ validated ImageDescription
→ immutable enriched InternalMessage
→ MessageRenderer
```

下载和 provider request 都不属于 parser/domain model。当前 HTTP resolver 只接受 public HTTP(S) destination，限制 redirect、timeout、MIME 和最大 bytes；只支持腾讯文档/样本覆盖的 JPEG、PNG、GIF。Vision 描述始终标记为 AI-generated、未独立验证且不可信，不能覆盖 system instruction，也不能用于猜测 sender。

## Future Extension Rules

未来允许增加 file/audio understanding、retrieval、persona 或 memory 等 derived service，但不得把它们伪装成平台事实。

但这些都必须是 derived data，不能覆盖：

- `raw_payload`
- `InternalMessage`
- `IdentityRef`
- `ForwardNode`

本项目宁可保留 `unknown / unavailable`，也不接受推断出来的假事实。
