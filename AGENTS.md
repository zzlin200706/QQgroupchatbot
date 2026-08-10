# AGENTS.md — qqgroupchatbot Codex 开发规则

> 本文件放在仓库根目录。Codex 在修改本项目时必须优先遵守本文件。
>
> 项目阶段：先保证 QQ 群消息接收、身份来源、消息树结构、持久化正确，再实现 AI 总结/问答。
>
> 文档校验日期：2026-08-10。

---

## 1. 项目目标

实现一个长期运行的 QQ 群消息机器人：

1. 使用一个**独立的 QQ 小号作为机器人账号**加入目标群。
2. 通过 NapCatQQ + OneBot 11 接收该账号所在群的消息事件。
3. 对所有收到的群消息进行可靠记录。
4. 正确处理：
   - 普通文本
   - 图片
   - @
   - 回复
   - 文件
   - JSON/卡片
   - 合并转发
   - 嵌套合并转发
5. 不丢失、不混淆消息来源和发送者。
6. 在消息存储正确后，再接入：
   - DeepSeek 等 LLM API
   - 自动群聊总结
   - 图片多模态处理
   - 群聊检索与问答
7. FastAPI 用于健康检查、调试、查询、管理接口，不应成为 QQ 原始消息业务逻辑的耦合中心。

---

## 2. 当前推荐技术路线

开发环境：

- VMware Ubuntu
- Python 3.10+
- NapCatQQ（Linux）
- OneBot 11
- FastAPI
- Uvicorn
- Pydantic v2
- SQLAlchemy 2.x
- SQLite + aiosqlite（开发阶段）
- pytest
- pytest-asyncio
- websockets 或经过验证的等价异步 WebSocket 客户端

第一阶段不要引入：

- Redis
- Kafka
- Celery
- Elasticsearch
- 向量数据库
- Docker Compose 多服务编排
- 微服务拆分

除非已有明确需求，否则不要为了“以后可能需要”提前加入复杂基础设施。

---

## 3. QQ 接入方式

### 3.1 账号模型

本项目不是让 AI 接管用户的主 QQ。

推荐：

```text
用户自己的 QQ
    │
    ├── 正常使用
    │
目标 QQ 群
    │
    └── 独立 QQ 小号（机器人账号）
             │
             ▼
          NapCat
             │
          OneBot 11
             │
             ▼
        Python Backend
```

不要把主 QQ 的 Cookie、Token、密码或私钥写入仓库。

NapCat 属于基于 NTQQ 的协议端实现，不等同于 QQ 开放平台官方 Bot API。项目 README 中必须明确这一点。

### 3.2 开发期推荐连接方式

优先使用：

```text
NapCat OneBot 11 WebSocket Server
              │
              │ forward WebSocket
              ▼
Python OneBot Adapter（客户端）
```

原因：

- VMware Ubuntu 内部可以全部走 localhost；
- 后端主动连接 NapCat，结构简单；
- 同一连接可接收事件并执行 OneBot action；
- 易于做断线重连；
- FastAPI 可以独立承担管理 API，不需要让 NapCat 的生命周期依赖 FastAPI 路由。

如果实际验证发现当前 NapCat 版本的正向 WS 与项目需求不匹配，可以切换为反向 WS，但只能修改 `adapters/onebot/` 层，不得影响内部消息模型和上层业务。

### 3.3 文档优先原则

NapCat / OneBot 字段不可凭记忆编造。

Codex 遇到以下情况必须先查看当前官方文档或本项目保存的真实事件 fixture：

- 不确定事件字段名；
- 不确定 API action 参数；
- 不确定合并转发返回格式；
- 不确定文件、图片 URL 生命周期；
- 不确定 reply / forward / node 的结构；
- 不确定某字段是否在当前 NapCat 版本存在。

官方资料：

- NapCat 文档：
  https://napneko.github.io/
- NapCat Linux / Shell：
  https://napneko.github.io/guide/boot/Shell
- NapCat API 兼容情况：
  https://napneko.github.io/develop/api
- NapCat 事件兼容情况：
  https://napneko.github.io/develop/event
- NapCat 消息格式：
  https://napneko.github.io/develop/msg
- NapCat OneBot 消息段：
  https://napneko.github.io/onebot/segment
- OneBot 11 标准：
  https://11.onebot.dev/
- NapCat GitHub：
  https://github.com/NapNeko/NapCatQQ

如果文档没有说明，应在代码中按 `unknown / unavailable / None` 处理，并记录原始数据，不得猜测。

---

## 4. 强制架构

整个项目必须遵循以下分层：

```text
QQ / NapCat / OneBot
        │
        ▼
OneBot Adapter
        │
        ▼
Event Normalizer
        │
        ▼
Message Parser
        │
        ▼
Internal Message Model
        │
        ▼
Storage
        │
        ├─────────────┐
        ▼             ▼
AI Processing      FastAPI Query/Admin
        │
        ▼
Bot Actions
```

各层职责如下。

### 4.1 OneBot Adapter

只负责：

- WebSocket 连接；
- 重连；
- 接收原始 JSON；
- 调用 OneBot action；
- action request / response correlation；
- heartbeat / lifecycle；
- 将原始事件交给 Normalizer。

禁止：

- 在这里生成 AI prompt；
- 在这里拼群聊总结；
- 在这里直接写复杂数据库 SQL；
- 在这里把合并转发扁平化。

### 4.2 Event Normalizer

负责：

- 判断事件类别；
- 从 OneBot raw event 提取稳定的顶层信息；
- 保留完整 `raw_event`；
- 将外部字段转换为内部模型字段；
- 不解释复杂消息内容。

### 4.3 Message Parser

负责解析消息段：

- text
- at
- reply
- image
- file
- record
- video
- face / mface
- json
- forward
- 其他未知 segment

未知消息段必须保存为：

```text
type = "unknown"
raw_data = 原始 data
```

不得丢弃。

### 4.4 Internal Message Model

这是整个系统的核心。

AI、数据库查询、FastAPI、总结模块都应依赖内部模型，而不是直接依赖 OneBot raw JSON。

### 4.5 Storage

负责：

- 原始事件持久化；
- 标准化消息持久化；
- 消息段和树结构持久化；
- 幂等 / 去重；
- 查询。

### 4.6 AI Processing

第一阶段不要实现。

只有达到本文定义的 `AI_READY_GATE` 后才能开始。

### 4.7 Bot Actions

对 OneBot action 做统一封装，例如：

- send_group_msg
- get_msg
- get_forward_msg
- get_image

所有 action 都必须从 adapter 走统一请求机制。

---

## 5. 身份与来源：最高优先级规则

这是本项目最重要的规则。

必须严格区分：

1. **event_sender**
   - 当前 QQ 群里触发这个 OneBot 事件的人。

2. **message_author**
   - 当前消息对象实际能确认的作者。

3. **forwarder**
   - 把某个合并转发发送到当前群里的人。

4. **forward_node_sender**
   - 合并转发某一个 node 中明确给出的发送者。

5. **nested_forward_node_sender**
   - 嵌套合并转发下一层 node 的发送者。

这几个概念绝对不能自动互相替代。

### 5.1 禁止错误推断

场景：

```text
A 在群中发送一个合并转发
合并转发中有若干历史消息
其中某一层 QQ 没有提供原作者
```

禁止：

```text
原作者 = A
```

正确做法：

```text
event_sender = A
forwarder = A
original_author = unknown
author_source = unavailable
```

只有原始 payload / `get_forward_msg` 返回结果明确提供发送者时，才能填入对应作者。

### 5.2 必须记录来源可信度

建议内部字段：

```python
author_id: str | None
author_name: str | None

author_status:
    "known"
    "unknown"
    "unavailable"

author_source:
    "event"
    "forward_node"
    "resolved_message"
    "unknown"
```

不要写“猜测发送者”。

---

## 6. 合并转发和嵌套合并转发

NapCat 当前文档表明 `forward` 消息段可包含 `id`，接收时在已解析情况下可能带 `content`；NapCat 也支持 `get_forward_msg`。

实现策略：

```text
收到 forward segment
      │
      ├─ 保存 forward_id
      │
      ├─ 如果 segment 已带 content
      │      └─ 解析 content
      │
      └─ 如果没有 content
             └─ 调 get_forward_msg(forward_id)
                    │
                    ▼
               解析 nodes
```

### 6.1 绝不提前扁平化

禁止直接转换成：

```python
[
    {"sender": "...", "text": "..."},
    {"sender": "...", "text": "..."}
]
```

必须保留树。

推荐概念模型：

```text
Message
└── Segment(forward)
    └── ForwardBundle
        ├── ForwardNode
        │   ├── sender
        │   └── message
        │       ├── text
        │       └── image
        │
        └── ForwardNode
            ├── sender
            └── message
                └── Segment(forward)
                    └── ForwardBundle
                        └── ForwardNode
                            └── ...
```

### 6.2 递归保护

必须实现：

- `max_forward_depth`，默认建议 10，可配置；
- visited forward id 集合，防止循环；
- 单次最大 node 数；
- 单条消息最大解析 segment 数；
- 超过限制时保留 unresolved 节点，而不是崩溃。

例如：

```python
resolution_status = "depth_limit"
```

### 6.3 缺少发送者

某层 node 未提供 sender：

```python
sender_id = None
sender_name = None
sender_status = "unavailable"
```

不得继承父层 forwarder。

---

## 7. 回复消息

`reply` segment 首先保存：

```text
reply_to_platform_message_id
```

之后可以选择调用 `get_msg` 补充引用消息。

但要注意：

```text
“引用的消息作者”
!=
“当前回复消息发送者”
```

两个必须分开存。

引用解析失败时：

```text
reply_resolution_status = "unavailable"
```

当前消息仍正常入库。

---

## 8. 图片 / 文件

第一阶段：

图片先保存元数据，不做 AI 理解：

```text
segment_type
file
url
summary（如果平台提供）
sub_type
file_size
raw_data
```

文件保存：

```text
name / file
file_id
file_size
url/path（如果当前 payload 提供）
raw_data
```

不要假设 URL 永久有效。

后续如需要长期保存图片：

```text
QQ/NapCat
   ↓
Media Downloader
   ↓
本地对象目录
   ↓
sha256
   ↓
media 表
```

AI 多模态处理是独立的 `MediaUnderstandingService`，不得侵入 parser。

---

## 9. 建议仓库结构

```text
qqgroupchatbot/
├── AGENTS.md
├── README.md
├── pyproject.toml
├── .env.example
├── .gitignore
│
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   │
│   ├── adapters/
│   │   └── onebot/
│   │       ├── __init__.py
│   │       ├── client.py
│   │       ├── actions.py
│   │       ├── protocol.py
│   │       └── reconnect.py
│   │
│   ├── normalizers/
│   │   ├── __init__.py
│   │   └── onebot_event.py
│   │
│   ├── parsers/
│   │   ├── __init__.py
│   │   ├── message_parser.py
│   │   ├── segment_parser.py
│   │   └── forward_parser.py
│   │
│   ├── domain/
│   │   ├── __init__.py
│   │   ├── events.py
│   │   ├── messages.py
│   │   ├── segments.py
│   │   └── identity.py
│   │
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── database.py
│   │   ├── orm.py
│   │   └── repositories/
│   │       ├── raw_event_repository.py
│   │       └── message_repository.py
│   │
│   ├── services/
│   │   ├── __init__.py
│   │   ├── ingest_service.py
│   │   ├── message_query_service.py
│   │   └── forward_resolution_service.py
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   ├── health.py
│   │   └── messages.py
│   │
│   └── bot_actions/
│       ├── __init__.py
│       └── sender.py
│
├── tests/
│   ├── fixtures/
│   │   └── onebot/
│   ├── unit/
│   └── integration/
│
├── data/
│   └── .gitkeep
│
└── docs/
    ├── ARCHITECTURE.md
    ├── IMPLEMENTATION_PLAN.md
    └── MESSAGE_MODEL.md
```

不要为了结构漂亮创建完全没用的空抽象；但核心分层必须保留。

---

## 10. 内部消息模型原则

具体模型见 `docs/MESSAGE_MODEL.md`。

核心要求：

### NormalizedMessage

至少表达：

```text
internal_id
platform
platform_message_id
group_id
event_sender
author
timestamp
segments
reply
forward_context
raw_event_id
```

### Segment

至少表达：

```text
segment_type
position
data
children
```

消息必须允许递归树结构。

---

## 11. 数据库存储原则

开发阶段 SQLite 足够。

需要至少保存两层数据：

### raw_events

完整 OneBot JSON：

```text
id
platform
event_type
received_at
dedup_key
raw_json
```

### normalized messages

用于查询/AI：

```text
messages
message_segments
forward_nodes
```

具体 schema 可以由 SQLAlchemy 设计，但必须满足：

1. 原始事件永远可追溯；
2. 消息树可重建；
3. segment 顺序不丢；
4. forward 层级不丢；
5. sender source 不丢；
6. message_id 可去重；
7. parser 升级后可以重放 raw event。

第一版可以使用 SQLite JSON 字段/JSON 字符串辅助保存，但不能只保存一个 `content TEXT` 就结束。

---

## 12. 幂等与重复消息

WebSocket 重连、事件重放都可能导致重复。

必须建立去重策略。

优先使用平台明确提供的稳定 message/event 标识。

若某事件没有明确唯一 ID：

- 生成内部 fingerprint；
- fingerprint 只用于去重，不得伪装为平台 message_id。

数据库必须有唯一约束或等价机制防止重复入库。

---

## 13. FastAPI 的定位

FastAPI 在本项目中不是“QQ Bot 本身”。

第一阶段只提供：

```text
GET /health
GET /api/v1/messages
GET /api/v1/messages/{id}
GET /api/v1/groups/{group_id}/messages
GET /api/v1/debug/onebot-status
```

可选：

```text
POST /api/v1/debug/replay/{raw_event_id}
```

用于把历史 raw event 再次走 normalizer/parser，但必须防止产生重复正式记录。

FastAPI 不直接依赖 NapCat 原始字段。

---

## 14. 日志要求

日志至少分清：

```text
onebot.connection
onebot.event
normalize
parse
forward.resolve
storage
api
```

禁止在日志中输出：

- Access Token
- QQ Cookie
- 密码
- API Key
- 完整 Authorization Header

生产/开发默认也不要把整条包含敏感字段的 raw event 打到 console。

需要 raw event 时写数据库或经过脱敏的 debug 文件。

---

## 15. 配置

`.env.example` 建议包含：

```dotenv
APP_ENV=development
LOG_LEVEL=INFO

ONEBOT_WS_URL=ws://127.0.0.1:3001
ONEBOT_ACCESS_TOKEN=

DATABASE_URL=sqlite+aiosqlite:///./data/qqgroupchatbot.db

FORWARD_MAX_DEPTH=10
FORWARD_MAX_NODES=500
MESSAGE_MAX_SEGMENTS=1000

API_HOST=127.0.0.1
API_PORT=8000
```

端口只是项目默认值，不代表 NapCat 官方默认端口。

Codex 不得把示例端口误写成“NapCat 默认端口”。

Token 的具体 WS 携带方式必须根据当前 NapCat 网络配置文档和真实环境配置。

---

## 16. 第一阶段必须实现的测试

### 16.1 普通文本

验证：

- group_id
- event_sender
- message_author
- text
- timestamp
- raw_event

### 16.2 文本 + @ + 图片

验证 segment 顺序：

```text
text
at
text
image
```

### 16.3 reply

验证：

- 当前发送者不变；
- reply_to_message_id 正确；
- 引用解析失败不影响入库。

### 16.4 单层合并转发

验证：

- 外层 event_sender 是外层发送者；
- node sender 独立；
- 不把外层发送者复制给 node。

### 16.5 嵌套合并转发

必须至少有一个 fixture：

```text
A
└── forward
    ├── B: hello
    └── forward
        ├── C: world
        └── unknown: image
```

验证树结构可完整重建。

### 16.6 sender 缺失

必须测试：

```text
node sender missing
```

期望：

```text
author_status = unavailable
```

绝对不能变成外层 A。

### 16.7 重复事件

同一 raw event 输入两遍：

数据库只存在一份有效记录。

### 16.8 未知 segment

输入：

```json
{"type": "future_type", "data": {"x": 1}}
```

必须保存，不得报错丢弃。

---

## 17. AI_READY_GATE

只有以下条件全部完成，才能开始 AI 总结：

- [ ] NapCat ↔ Python WS 稳定连接；
- [ ] 断线自动重连；
- [ ] 群普通消息可持续入库；
- [ ] raw event 100% 保存；
- [ ] text/@/reply/image/file 可解析；
- [ ] 单层 forward 可解析；
- [ ] 嵌套 forward 可解析；
- [ ] 缺失发送者不会被错误归因；
- [ ] 消息树可从 DB 重建；
- [ ] 事件去重有效；
- [ ] 单元测试通过；
- [ ] 至少在真实测试群跑过一段时间并人工抽查消息。

在 Gate 前不要实现 RAG、Embedding、自动总结。

---

## 18. 后续 AI 架构

Gate 通过后再增加：

```text
Storage
   ↓
Conversation Selector
   ↓
Message Renderer
   ↓
LLM Provider Interface
   ↓
DeepSeek / Other LLM
   ↓
Summary
   ↓
Bot Actions
```

LLM 输入必须来自 `Internal Message Model`。

不得直接：

```python
prompt = str(onebot_raw_json)
```

### Message Renderer

负责把内部树转成适合 LLM 的文本，例如：

```text
[20:14] 张三: 明天下午开会
[20:15] 李四: [回复 张三] 几点？
[20:16] 张三: 3点

[20:20] 王五转发了一组消息：
  ├─ [原作者未知] ...
  └─ 小明: ...
```

未知作者必须明确渲染为：

```text
[原作者未知]
```

不能显示成转发者。

---

## 19. 图片 AI 处理原则

DeepSeek 或其他文本 LLM 是否支持多模态必须以当时官方 API 文档为准。

建议抽象：

```python
class VisionProvider(Protocol):
    async def describe_image(...) -> ImageUnderstandingResult:
        ...
```

这样后续可以替换：

- OpenAI vision-capable model
- Gemini
- 其他视觉 API

parser 只识别“这是图片”，不负责视觉理解。

---

## 20. Codex 工作方式

每次只完成一个可验证阶段。

每完成一个阶段必须：

1. 列出修改文件；
2. 说明核心设计；
3. 运行测试；
4. 给出测试结果；
5. 给出人工验证方法；
6. 若遇到 NapCat 字段不确定，指出查阅的官方文档；
7. 不要擅自进入下一阶段的大功能。

不要一次生成几千行代码然后声称“项目完成”。

优先形成短小、可测试、可回滚的 commit。

---

## 21. 禁止事项

- 禁止提交 `.env`；
- 禁止真实 QQ Token/Cookie/API Key；
- 禁止将外层 forwarder 当成缺失的原作者；
- 禁止把嵌套转发提前扁平化；
- 禁止只存 `sender + text`；
- 禁止未知 segment 直接丢弃；
- 禁止 AI 功能直接依赖 OneBot raw JSON；
- 禁止在未验证文档时臆造 NapCat 字段；
- 禁止为了“高级”引入不必要的微服务；
- 禁止修改已稳定的内部模型来迁就某一个 AI Provider。

---

## 22. 当前第一目标

Codex 首先只实现到：

```text
NapCat
  ↓
OneBot WS Adapter
  ↓
Raw event 保存
  ↓
Normalizer
  ↓
Parser
  ↓
Internal Message Model
  ↓
SQLite
  ↓
FastAPI 查询验证
```

完成后，用户在测试群连续发送：

- 文本
- 图片
- @
- 回复
- 文件
- 合并转发
- 嵌套合并转发

项目应能在数据库/API 中正确查看完整结构和身份来源。

达到这一点，第一阶段才算完成。
