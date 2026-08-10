# IMPLEMENTATION_PLAN.md — Codex 分阶段实施计划

## 使用方式

不要一次完成所有 Phase。

执行某一个 Phase 前：

1. 阅读根目录 `AGENTS.md`；
2. 阅读 `docs/ARCHITECTURE.md`；
3. 阅读 `docs/MESSAGE_MODEL.md`；
4. 检查当前仓库代码；
5. 只实现该 Phase；
6. 运行测试；
7. 输出变更总结。

---

# Phase 0 — VMware Ubuntu 开发环境

## 目标

确认 Ubuntu、Python、Git、NapCat 可用于开发。

## 建议检查

```bash
uname -a
lsb_release -a || cat /etc/os-release
python3 --version
git --version
```

推荐创建 venv：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
```

不要把 `.venv` 加入 Git。

## NapCat

按当前官方 Linux 文档安装。

当前官方文档页面：

https://napneko.github.io/guide/boot/Shell

官方页面目前列出 Ubuntu 20+ 支持，并提供 Shell / Docker 等安装方式。

开发阶段推荐先用 Linux Shell / 官方 Installer，而不是立刻构建复杂 Docker 栈。

## 完成标准

- [ ] Ubuntu 可以运行 Python 3.10+；
- [ ] NapCat 可启动；
- [ ] 独立 Bot QQ 账号登录成功；
- [ ] Bot QQ 已加入测试群；
- [ ] NapCat WebUI/网络配置可访问；
- [ ] OneBot 11 WS server 已启用；
- [ ] Access Token 已设置且未提交仓库。

---

# Phase 1 — Python 项目骨架

## 创建

```text
pyproject.toml
.env.example
.gitignore
app/
tests/
docs/
```

依赖建议：

```text
fastapi
uvicorn
pydantic
pydantic-settings
sqlalchemy
aiosqlite
websockets
pytest
pytest-asyncio
httpx
```

版本策略：

- 不必锁死补丁版本；
- 使用兼容的稳定版本；
- 若库 API 与当前文档冲突，以安装版本官方文档为准。

## 配置

实现：

```python
Settings
```

包含：

```text
ONEBOT_WS_URL
ONEBOT_ACCESS_TOKEN
DATABASE_URL
FORWARD_MAX_DEPTH
FORWARD_MAX_NODES
MESSAGE_MAX_SEGMENTS
API_HOST
API_PORT
```

## 完成标准

```bash
pytest
python -m app.main
```

可正常启动基本 FastAPI `/health`。

---

# Phase 2 — OneBot WebSocket Adapter

## 目标

只打通：

```text
NapCat → Python
```

不要解析业务消息。

## 实现

`app/adapters/onebot/client.py`

功能：

- connect；
- disconnect；
- reconnect；
- receive JSON；
- heartbeat/lifecycle 不当成群消息；
- action request；
- echo/request correlation；
- timeout；
- graceful shutdown。

需要保存真实收到的事件 fixture：

```text
tests/fixtures/onebot/
```

但保存前脱敏：

- bot QQ 可替换；
- 群号可替换；
- 用户 QQ 可替换；
- token/cookie 必须删除。

## 人工测试

测试群发：

```text
hello onebot
```

console 只输出安全摘要：

```text
event=message.group
message_id=...
group_id=...
```

不要完整 print 敏感 raw payload。

## 完成标准

- [x] WS 连通；
- [x] 群普通消息事件可收到；
- [x] 断开 NapCat 后后端不会退出；
- [x] NapCat 恢复后自动重连；
- [x] 可以调用一个无风险 OneBot action，例如 get_status/get_login_info（参数以当前文档为准）。

## 联调记录（2026-08-10）

- 本机 NapCat 正向 WebSocket 已通过 `OneBotClient` 连接；Access Token 只从本地
  `.env` 读取，未打印或写入仓库。
- `get_status` 与 `get_login_info` 均返回 `status=ok`、`retcode=0`，响应的 `echo`
  与请求正确关联。
- 真实群文本事件已进入 adapter 的普通业务事件回调；收到的 meta event 被 adapter
  拦截，未进入该回调。
- `tests/fixtures/onebot/real_group_text_sanitized.json` 保留真实事件的 OneBot JSON
  形状，同时替换账号、群、消息、序列、名称、文本与 URL/路径字段。它只用作后续
  normalizer/parser 的输入 fixture，尚未实现消息解析或持久化。
- 自动重连、断线时 pending action 失败、以及 FastAPI lifespan 启停均由本地 WebSocket
  模拟测试覆盖。
- 已完成真实 NapCat WebSocket 服务端关闭/恢复演练：adapter 检测到远端断开，保持
  FastAPI lifespan 进程存活并按指数退避重试，观察到至少两次真实连接失败。服务端恢复
  后没有重启 qqgroupchatbot 或调用客户端 `disconnect()`，adapter 自动重新连接；
  `get_status` 再次返回 `status=ok`、`retcode=0`，且 `echo` 正常关联。恢复后真实群
  文本事件仍进入业务回调，收到的 heartbeat/lifecycle meta event 继续被拦截。

---

# Phase 3 — Raw Event Storage

## 目标

将每一次由 OneBot adapter 分派的业务事件完整、可追溯地保存到 SQLite；本阶段不解析
消息语义。

## 架构

```text
OneBot adapter business callback
  ↓
RawEventIngestionService
  ↓
RawEventRepository
  ↓
raw_events
```

`Database` 集中管理 async engine、session factory、`create_all` 和 dispose。FastAPI
lifespan 先初始化数据库，再启动 adapter；关闭时先停止 adapter，再 dispose engine。

## raw_events schema

```text
id                 INTEGER primary key
platform           TEXT, currently onebot11
received_at        UTC datetime (local receipt time)
event_time         nullable UTC datetime (only a usable top-level numeric time)
post_type          nullable TEXT
message_type       nullable TEXT
sub_type           nullable TEXT
self_id            nullable TEXT
user_id            nullable TEXT
group_id           nullable TEXT
message_id         nullable TEXT
raw_payload        JSON, complete received payload
payload_hash       SHA-256 canonical-JSON diagnostic index
```

`self_id`、`user_id`、`group_id` 和 `message_id` 均作为可空 `TEXT` 索引副本保存，避免
对未来平台 ID 的数值范围或表示方式作假设。它们不表示内部消息作者身份，更不用于推断
forward node sender。

`raw_payload` 使用 SQLAlchemy JSON 完整保存。读取后与输入 payload 在 JSON 结构意义上
相等；未知字段、数组和任意嵌套树不会被 storage 层删除、改写或解释。

## transaction 与重复策略

- 每个 raw event receipt 使用独立 session 和 transaction；异常 rollback 后上报给
  ingestion service。
- `payload_hash` 是 deterministic JSON SHA-256，仅用于调试与后续重复分析；没有唯一
  约束。
- 即使 payload 完全相同，也会保留两条 receipt，避免重连/重放排查时丢失实际收到的事件。
- 写入失败只记录不含 payload 的 event metadata、hash 与异常，且不会让 OneBot receive
  loop 永久停止；后续事件仍继续处理。

## 初始化与限制

- 当前早期 schema 使用集中化的 `create_all`，尚未引入 Alembic；schema 开始频繁演进时
  再评估 migration。
- 文件型 SQLite URL 的父目录会在 database 初始化前创建；实际 `data/*.db` 已由 Git
  忽略。
- Phase 3 不做 Message Parser、reply/@/file/image/forward 解析、Internal Message Model、
  查询 API 或 AI 功能。

## 测试与真实联调

- fixture raw payload round-trip 和顶层索引提取；
- 未知字段、generic nested JSON tree、缺失索引字段与相同 payload 的双 receipt；
- adapter callback → SQLite 接线，且 heartbeat/lifecycle 不入 raw storage；
- 首次 SQLite 写入失败后，第二条 adapter event 仍可落库；
- FastAPI lifespan 初始化 storage 并保留 Phase 2 adapter lifecycle；
- 2026-08-10 真实 NapCat smoke：测试群文本成功新增一条 `raw_events` receipt，确认
  `post_type=message`、`message_type=group` 与完整 raw payload 已存储，未打印真实 payload
  或身份标识。

## 完成标准

- [x] SQLite async storage 与 raw_events table 正常创建；
- [x] OneBot business event 自动写库；
- [x] raw_payload、未知字段和 nested JSON tree 无损保存；
- [x] 缺失索引字段时可正常保存为 NULL；
- [x] 相同 payload 可保存多次；
- [x] write failure 不会永久杀死 adapter receive loop；
- [x] heartbeat/lifecycle 不进入普通业务 raw storage；
- [x] FastAPI lifespan 正确初始化/关闭 database；
- [x] Phase 1/2 regression 与 Phase 3 tests 通过（17 passed）；
- [x] 真实 NapCat 群消息成功写入 SQLite；
- [x] 未泄露 Access Token，实际数据库文件未加入 Git；
- [x] 文档已更新，git diff --check 通过。

---

# Phase 4 — Internal Message Model + Message Parser v1

## 架构与边界

```text
RawEvent.raw_payload + raw_events.id
  ↓
OneBotMessageParser
  ↓
InternalMessage (in-memory only)
```

Parser 只接收已存储的 raw payload 和可选的 raw event id；它不依赖 WebSocket，不修改
raw event，不写 normalized 表，也不会调用 `get_msg`、`get_forward_msg`、下载媒体或访问
任何 AI 服务。

## 领域模型

- `InternalMessage`：平台消息 ID、`source_raw_event_id`、context、actor、author、时间、
  保序 segments 与 direct-event provenance。
- `IdentityRef`：`platform`、`user_id`、display name、card、`availability` 和明确的
  source。actor 与 author 是独立字段；直接 message event 的 top-level user_id 只用于
  该 direct event，不能传播到 forward node。
- `MessageProvenance`：`direct_event`、`forward_node` 或 `nested_forward_node`，包含 raw
  event id、父节点 ID 和 forward depth。

身份缺失时采用 `availability=unavailable`、`source=unknown`，不填充外层转发者。

## Segment v1

已实现并保留 `raw_data` 的 segment：

- `TextSegment`、`AtSegment`（`qq=all` 明确为 `is_all`）、`ImageSegment`、`ReplySegment`；
- `FileSegment`；
- `ForwardSegment`；
- `UnknownSegment`，保留原 type 与 data。

NapCat 当前文档确认 text、at、reply、image、file 与 forward 的上述基础字段。所有未映射
字段仍保留在 `raw_data`；未识别的 segment 不会让整条消息失败。

## Reply 与 forward

- reply 仅表达当前 payload 明确给出的 referenced message id；不构造被引用作者。
- 只有 forward id 时，`ForwardSegment(resolved=False, resolution_status=unresolved)`；
  本阶段不联网展开。
- 如 payload 已含 `forward.data.content`，parser 保留 content 的递归树；`node` 的 sender
  只从 node 自己明确给出的字段建立。forward depth、node 数和 segment 数为 parser 的
  构造参数（默认值与项目配置一致）；超限时保留原始 data 并标记未解析状态。

## 测试与真实 smoke

- 普通群文本、actor/author、source raw event id；
- text/@/text/image 的顺序、image/file/reply 的 raw data；
- 未知 segment、缺失身份、非 message event 和 raw payload 不变性；
- unresolved forward、内嵌 node tree、深度限制以及 node sender 不继承 outer actor；
- 2026-08-10：从 Phase 3 SQLite 已存群消息 receipt 解析成功，结果为 group +
  `TextSegment`，actor/author 均为 known；未输出群号、昵称、正文或凭据。

## 完成标准

- [x] InternalMessage、Identity、provenance 和 recursive forward node 模型；
- [x] text / at / image / reply / file / forward / unknown segment；
- [x] segment 顺序、未知字段、raw event id 和 raw payload 不变性；
- [x] forward unresolved 与 nested tree，缺失 node sender 不会继承 outer sender；
- [x] 非 message event 安全返回 None；
- [x] Phase 1–3 regression 与 Phase 4 tests 通过；
- [x] 真实普通群消息 parser smoke 通过；
- [x] 本阶段未创建 normalized DB 表、未泄露凭据或群聊内容，diff check 通过。

---

# Phase 5 — Reference Enrichment

## 架构与边界

```text
RawEvent.raw_payload + raw_events.id
  ↓
OneBotMessageParser (pure; no network)
  ↓
InternalMessage
  ↓
ReferenceEnrichmentService (optional, in-memory)
  ├─ ReplySegment → OneBotClient.get_message() → get_msg
  └─ unresolved ForwardSegment → OneBotClient.get_forward_message() → get_forward_msg
```

`ReferenceEnrichmentService` 返回新的 frozen `InternalMessage`；不会修改输入对象、
`raw_events` 或 parser 生成的 `raw_data`。网络失败只更新相应 reference 的 resolution status，
原始引用及原始 payload 始终保留。本阶段不接入 receive loop，因此 action 不会阻塞 WebSocket 收包。

## 实现

- `OneBotClient` 增加 `get_message(message_id)` 与 `get_forward_message(message_id)`；分别封装
  NapCat 文档列出的 `get_msg(message_id: number/string)` 与 `get_forward_msg(message_id: string)`。
- `ReplySegment` 的状态为 `unresolved`、`resolved`、`invalid_reference`、`fetch_failed` 或
  `invalid_response`。成功时使用 `ResolvedMessageReference` 保存 get_msg 数据中明确给出的 message
  id、author、timestamp、保序 segments 和单独的 raw data。
- resolved reply 的 author 只从 get_msg 返回消息自身的 top-level `user_id`/`sender` 提取；缺少
  `user_id` 时为 `availability=unavailable`、`source=unknown`，绝不继承当前消息 actor/author。
- forward 的来源状态区分 `embedded`（事件本身已有 content）与 `fetched`（get_forward_msg 返回）。
  远端内容仅写入 `resolved_raw_data`；原 `ForwardSegment.raw_data` 和各 `ForwardNode.raw_data`
  保持事件中的原始内容。
- 当前实现以实际/测试确认的 response data `messages` list 作为可解析 forward tree；其他形状安全
  标记为 `invalid_response`，不会猜测字段或扁平化树。真实 NapCat forward smoke 用于确认当前运行版本
  的 response shape。
- 单次 `enrich()` 使用仅存活于此次调用的 reply/forward cache；重复 reference id 不重复 action。
  只解析 response 中已嵌入的 nested content；nested unresolved forward 不继续网络请求，完整递归
  网络解析明确留给 Phase 6。

## 失败与隐私

`OneBotClientError`（包括 timeout、断开）、非 `ok`/非零 retcode 及 malformed response 均安全降级为
`fetch_failed` 或 `invalid_response`。日志只含 reference type、resolution 和 exception type，不记录
消息正文、身份字段、URL 或凭据。

## 测试与真实 smoke

- adapter fake-WebSocket 测试验证 helper action 与参数；
- enrichment 单测验证 reply/forward 成功、身份独立性、缺失 sender、失败降级、未知 segment、树及
  segment 顺序、raw-data-first、不变性、每次 enrich 的 cache，以及 nested unresolved forward 不跨入
  Phase 6；
- 真实 reply/forward smoke 仍需在测试群产生相应 reference event 后执行；届时只输出 resolution、
  identity availability 与 node/segment 数量，并在确认后添加严格脱敏的 fixture（如实际 response
  shape 与测试样本不同）。

## 完成标准

- [x] `get_msg` / `get_forward_msg` adapter helper；
- [x] parser 保持无网络，独立 in-memory ReferenceEnrichmentService；
- [x] reply/forward resolution status、raw-data-first 与 immutable enrichment；
- [x] resolved reply author 只来自 resolved message；缺失 author 不继承 outer sender；
- [x] unresolved forward 可获取、embedded forward 不重复请求、fetched/embedded 来源可区分；
- [x] forward tree/node sender 独立，缺失 sender 保持 unavailable；
- [x] failure、安全降级、per-call cache 与 Phase 6 network-recursion boundary；
- [x] Phase 1–4 regression 与 Phase 5 单测通过；
- [ ] 真实 NapCat reply smoke；
- [ ] 真实 NapCat forward smoke / 当前 response shape fixture（如可取得）；
- [ ] 本阶段最终 diff check 与验收。

---

# Phase F — Normalized Message Persistence

## 架构与边界

```text
committed raw_events receipt
  ↓
pure platform parser
  ↓
InternalMessage
  ↓
MessageRepository + explicit message codec
  ↓
messages + message_nodes
```

本阶段只新增 normalized 表，不修改已有 `raw_events` schema。OneBot live callback 在 raw receipt
成功 commit 后执行纯解析和 normalized persistence；parser 返回 `None`、解析失败或 normalized 写入失败
都不会删除 raw receipt，也不会调用 reference enrichment、下载媒体或执行网络补全。

## Schema 与 codec

- `messages` 保存 parser name/version、平台消息索引、顶层 context、actor/author identity snapshot、
  provenance 和 raw receipt foreign key；`(source_raw_event_id, parser_name, parser_version)` 唯一。
- `message_nodes` 使用 `parent_node_id + relation + position + depth` 保存显式树。当前 relation 为
  `segments`、`content`、`nodes`、`resolved_message`，因此 forward 的 loose content、node collection 与
  reply resolved reference 不会混淆。
- node kind 显式覆盖 text、at、image、file、reply、forward、forward_node、resolved_message 和 unknown。
  enum 使用 `.value`；结构中的 datetime 使用 ISO 8601；所有 parser `raw_data`/`resolved_raw_data` 保留在
  JSON payload，不使用 pickle 或 `dataclasses.asdict()`。
- 顶层 actor/author、forward node sender 和 resolved reference author 都按当时的完整 identity snapshot
  保存，包括 platform、source 与 availability。缺失 sender round-trip 后仍为 unavailable/unknown，绝不
  继承外层 author。

## Transaction、幂等与 replay

- 一条 message row 与其全部 node rows 在同一 transaction 中插入；任意 node 失败会整体 rollback。
- 相同 raw receipt + parser name/version 幂等返回既有表示；新 parser version 可为相同 receipt 增加新表示。
- 相同 platform message id 的不同 raw receipts 可以分别 normalized；platform message id 仅有普通索引。
- SQLite connection 启用 foreign key enforcement，确保 normalized message 必须引用真实 raw receipt。

## 已验证范围

- OneBot 真实脱敏文本 fixture 严格 `InternalMessage == reload`；
- nested forward 的 `content`/`nodes` relation、深度、segment position、provenance 和 unavailable sender；
- enriched reply 的 resolved message/segments/author/raw response；
- QQ Official text/@/image/file/103/102 真实脱敏 samples；102 仍为 unresolved、nodes 为空，103 的
  `REFIDX_*` 仍只存在于 raw data；
- idempotency、parser replay、duplicate raw receipts、unknown segment、transaction rollback、非消息事件、
  normalized failure 后下一 receipt 继续处理，以及 live OneBot raw-first pipeline。

---

# Phase G — Conversation Query + Deterministic Message Renderer

## 架构与边界

```text
messages + message_nodes
  ↓
MessageRepository.list_conversation
  ↓
ConversationQueryService
  ↓
Sequence[InternalMessage]
  ↓
MessageRenderer
  ↓
stable readable text
```

本阶段是 normalized storage 的只读 consumer，不是 AI Phase。查询不读取或现场重放
`raw_events.raw_payload`；Renderer 不访问 ORM、数据库、adapter、HTTP 或文件系统，也不接入 LLM、
summary、QA、embedding 或向量存储。

## Conversation Query

- conversation key 必须同时包含 `platform + group_id`，不建立 OneBot 与 QQ Official 的群映射；
- 时间窗口为 timezone-aware 的 `[start_time, end_time)`；naive datetime、反向/空窗口均拒绝；
- `timestamp IS NULL` 不进入历史窗口，且绝不使用 normalized `created_at` 代替发送时间；
- 结果固定按 `timestamp ASC, messages.id ASC` 排序；默认 limit 500，硬上限 2000，返回窗口中最前面的
  limit 条；
- 默认先在每个 `source_raw_event_id` 中选最大 `messages.id`，再应用 conversation/time filter。只传
  `parser_name` 时在该 parser scope 内选最新写入表示；同时传 parser name/version 时精确选择该版本；
  `parser_version` 不允许脱离 `parser_name` 单独使用；
- replay 新旧不根据 parser-version 字符串排序。不同 raw receipts 即使拥有相同
  `platform_message_id` 也保留为两条，不做 heuristic dedup；
- repository 批量读取所选 message 的 node rows，再由既有 `message_codec.decode_message` 返回完整
  `InternalMessage`，ORM 不泄露给 service/renderer。

## Deterministic Renderer

- 顶层格式为 `[ISO 8601 timestamp] author: content`，默认显示 UTC，可显式传展示 timezone；多行正文的
  continuation 与消息边界使用固定缩进；
- KNOWN identity 按 card、display name、`已知用户` 的顺序显示；UNKNOWN 显示 `[作者未知]`；
  UNAVAILABLE 显示 `[原作者不可用]`，且 forward node 永不继承 outer author；
- mention 只在当前 conversation 的 identity snapshots 中按相同 platform 和精确 user id 匹配名称；
  未匹配显示 `@用户`，默认不显示裸 ID；
- text、at、image、file、reply、unknown 按 position（相同 position 保留 tuple 顺序）渲染。图片和文件只
  输出安全占位与可用 summary/name；unresolved reply 默认隐藏 platform id；resolved reply 的 quoted
  author 和内容只来自 resolved reference；
- unresolved forward 显示 `[合并转发：内容未解析]`。resolved forward 保留缩进 node tree；nested
  forward 递归显示；`ForwardSegment.content` 作为 loose content 单独标注，绝不与 `nodes` 合并；
- QQ Official 102 保持 unresolved/empty nodes，不解析展示文本或猜 sender；103 的 `REFIDX_*` 只留在
  raw data，不成为 reply message id；
- 默认输出不包含 raw/resolved raw data、attachment URL/path/platform file id、裸 QQ/OpenID 或 Gateway /
  OneBot JSON；可选 `max_chars` 截断会追加 `[内容已截断]`，不会静默丢内容。

## 已验证范围

- half-open range、timezone validation、NULL timestamp、stable tie-break、limit，以及 cross-group /
  cross-platform isolation；
- default replay latest、parser-name latest、exact parser version，以及不同 raw receipts 的相同 platform
  message id 均保留；
- text/multiline、at、image、file、unknown、unresolved/resolved reply、unresolved/resolved/nested forward、
  content/nodes 分离、unknown/unavailable identity、privacy 和 explicit truncation；
- OneBot 与 QQ Official 的真实脱敏 fixture 均完成 raw fixture → parser → normalized persistence → query →
  renderer 集成链路。

---

# Phase H — LLM Provider + DeepSeek Provider + Group Summary Pipeline

## 架构与边界

```text
ConversationQueryService
  ↓
Sequence[InternalMessage]
  ↓
MessageRenderer (no truncation)
  ↓
SummaryService
  ↓
LLMProvider
  ↓
DeepSeekProvider
  ↓
validated SummaryResult
```

本阶段只提供显式调用的 Python service API 和 in-memory result。没有接入 OneBot live callback、Bot
Actions、定时任务、FastAPI summary endpoint、QA、embedding、vector DB、图片下载或 summary
persistence。LLM 只接收 Renderer 输出，不读取 ORM、raw event、segment raw data 或 adapter response。

## DeepSeek V4 provider

根据 2026-08-10 的 DeepSeek 官方 Chat Completions、Thinking Mode、JSON Output 与 Error Codes 文档：

- OpenAI-compatible base URL 为 `https://api.deepseek.com`，调用 `POST /chat/completions`；
- 默认 model 为 `deepseek-v4-flash`，允许配置覆盖；不使用 legacy `deepseek-chat` /
  `deepseek-reasoner`；
- 请求显式发送 `stream=false`、`thinking={"type":"disabled"}`、`max_tokens` 和
  `response_format={"type":"json_object"}`；prompt 同时明确要求 JSON 并给出 schema 示例；
- 不发送 `user_id`，尤其不把 group id、QQ/OpenID、raw event id 或 message id 当作 provider metadata；
- 使用可复用、可注入的 `httpx.AsyncClient`，每次 request 有明确 timeout；内部创建的 client 可通过
  `aclose()` 释放，测试注入的 client 由 caller 管理；
- `max_retries=N` 表示首次请求后最多再尝试 N 次。429/500/503、timeout、connection error 和 invalid
  response 使用 0.5s、1.0s…… bounded exponential backoff；400/401/402/422 不重试；
- provider 日志只包含 provider、model、HTTP status、attempt、exception type 和 input char count，不输出
  key、Authorization、prompt、conversation、response body/content 或身份/group metadata。

官方资料：

- https://api-docs.deepseek.com/api/create-chat-completion
- https://api-docs.deepseek.com/guides/thinking_mode
- https://api-docs.deepseek.com/guides/json_mode/
- https://api-docs.deepseek.com/quick_start/error_codes/

## Provider abstraction 与结果

- `LLMRequest` 只包含 system prompt、user prompt、max output tokens 与 JSON-output flag；
- `LLMResponse` 只提取 content、provider、response model、finish reason 与明确返回的 usage；不保存
  reasoning content；usage 缺失字段保持 `None`；
- provider exceptions 将 authentication、payment、request、rate limit、server、timeout、connection 和
  invalid response 与 HTTP/transport implementation detail 分离；
- `SummaryResult` 是 immutable business model，包含程序确定的平台/群/时间/message count、严格校验的
  summary/topics/key points/decisions/action items/open questions、provider/model/finish reason、input chars、
  prompt version 和 optional token usage；LLM JSON 无法覆盖程序 metadata；
- `SummaryActionItem.owner/deadline` 接受明确字符串或 `None`，prompt 禁止模型猜测缺失值；
- summary payload 使用 Pydantic strict schema 和 `extra=forbid`，不修复 malformed JSON、不强制转换错误
  字段、不接受额外 metadata。`finish_reason=length`、空 choices/content、invalid JSON/schema 都不能产生
  business result。

## Prompt safety 与完整覆盖

- `SUMMARY_PROMPT_VERSION="1"`；system prompt 把 conversation 明确标记为不可信数据，群聊中的 prompt
  injection 仍保留为事实文本但不会变成 system instruction；
- prompt 禁止猜测 unknown/unavailable author、图片/文件内容、unresolved forward、决定、owner 或
  deadline；`[作者未知]`、`[原作者不可用]`、`[图片]` 和 `[合并转发：内容未解析]` 的不确定性必须保留；
- SummaryService 用 `requested_limit + 1` 查询；多出一条即抛 `SummaryWindowTooLarge`，且不调用 provider；
- Renderer 必须是 `max_chars=None`；完整 rendered text 超过 `SUMMARY_MAX_INPUT_CHARS` 时同样拒绝，不做
  silent truncation；本阶段不实现 chunk/map-reduce；
- 空窗口返回 provider/model 为 `none` 的正常空结果，provider calls 为零；
- user prompt 只加入 UTC time window、message count 与 Renderer safe text，不加入 group id；
- 本阶段 SummaryResult 不入库，不保存完整 prompt。

## 已验证范围

- DeepSeek request URL/header/body、non-thinking JSON mode、model/output-token configuration、optional usage；
- 400/401/402/422 permanent errors，429/500/503 bounded retry，timeout，invalid envelope/choices/content/JSON
  和 length finish reason；retry tests 使用 injected sleep，不真实等待或联网；
- successful strict summary、action item、program metadata、prompt injection、empty window、message overflow、
  char overflow、no renderer truncation、malformed/schema-invalid/extra JSON；
- OneBot nested forward、QQ Official 102 unresolved forward 与 QQ Official 103 reply fixture 均完成 parser →
  normalized DB → ConversationQueryService → MessageRenderer → SummaryService → FakeLLMProvider 集成链，
  不泄漏 forward ids、QQ IDs、Gateway/raw JSON、attachment URL 或 `REFIDX_*`。

---

# Phase I — Summary Persistence + Historical Query

## 架构与边界

```text
SummaryService
  ↓
validated SummaryResult
  ↓ explicit caller boundary
SummaryRepository
  ↓
summaries
  ↓
StoredSummary(id, created_at, result)
```

Generation 与 persistence 保持独立。`SummaryService` 继续只负责 query、render、provider invocation、
validation 和返回 `SummaryResult`；它不依赖 SQLAlchemy、session、ORM 或 repository。调用方只有在获得
成功且已验证的 result 后，才显式调用 `SummaryRepository.persist(result)`。

本阶段没有修改 DeepSeek prompt/retry/privacy，也没有实现 QQ send、bot command、scheduler、QA、RAG、
embedding、vector DB、FastAPI summary endpoint、Web UI、自动触发、chunk/map-reduce summary、图片理解或
新 provider。

## summaries schema

`summaries` 每行代表一次独立的成功生成运行：

```text
id, platform, group_id, start_time, end_time, message_count, created_at
summary, topics, key_points, decisions, action_items, open_questions
provider, model, finish_reason, input_chars, prompt_version
prompt_tokens, completion_tokens, total_tokens
```

- `id` 为本地自增主键；`created_at` 由 repository 的 UTC clock 生成；start/end/created 使用既有
  `UTCDateTime`，SQLite round-trip 后均为 timezone-aware UTC；
- topics/key points/decisions/action items/open questions 使用 JSON array，不拼接为逗号文本；action item
  明确保留 description/owner/deadline，nullable 值仍为 JSON null / Python `None`；
- optional token usage 使用 nullable integer，未知值保持 SQL NULL / Python `None`，不填零；
- 索引为 `(platform, group_id, start_time, end_time)` 与
  `(platform, group_id, created_at)`；window 没有 unique constraint；
- 不保存 raw payload/data、rendered conversation、system/user prompt、provider response body、Authorization、
  API key、attachment URL/path、QQ identity、Gateway/OneBot JSON 或 `REFIDX`。

## Codec、transaction 与历史语义

- `summary_codec` 显式执行 `SummaryResult ↔ SummaryRecord`，并把 database identity 包装在独立 immutable
  `StoredSummary(id, created_at, result)`；`SummaryResult` 本身没有 database id、ORM record 或 session；
- decode 对 JSON array、string item 和 action item exact shape 做防御校验，不静默转换损坏数据；
- persist 在单一 transaction 中 insert/flush/refresh。任何 SQL/JSON serialization failure 自动 rollback 并
  向上传播，不吞异常、不返回伪成功；
- 每次 persist 都 insert 新 row。同一 platform/group/window 的多次运行不会覆盖或去重，便于比较不同
  prompt/model/provider/message representation；
- `get_by_id` 返回单条；`list_for_group(platform, group_id, limit=100)` 严格隔离平台与群，并固定按
  `created_at DESC, id DESC` 返回。非正 limit、空 platform/group 会明确拒绝；
- empty-window `SummaryResult` 与其他已验证 result 一样可由调用方显式保存；repository 不根据
  `message_count` 猜业务意图；
- LLM/validation/overflow failure 不会产生 result，因此 generation failure 路径不会插入 summaries row。

## 已验证范围

- 全字段 persist/get round-trip、structured tuple 顺序、action items（含 nullable owner/deadline）；
- usage 完整缺失、全部存在与部分 nullable，均不伪造零值；
- 同 window 多 run 获得不同 id，created-at/id newest-first 稳定排序，history limit 生效；
- group isolation 与相同 group string 的 cross-platform isolation；
- start/end/created 的 timezone-aware UTC round-trip、transaction rollback 与 privacy-only column allowlist；
- OneBot 脱敏 fixture 完成 raw → parser → normalized DB → query → renderer → SummaryService → FakeLLMProvider
  → SummaryRepository → SQLite → get/list 的完整链路；synthetic provider failure 后 summaries row count 为零；
- schema 继续由现有 `Base.metadata.create_all` 初始化，未引入 Alembic 或新的基础设施。

---

# Phase 6 — 合并转发

这是核心 Phase，不允许跳过测试。

## 6A：单层 forward

处理：

```text
forward segment
  ↓
embedded content?
  ├─ yes → parse
  └─ no  → get_forward_msg
```

当前 NapCat 文档显示：

- `forward` 接收时可有 `id`；
- 已解析时可能有 `content`；
- `get_forward_msg` API 可用。

具体返回字段必须根据当前版本真实返回和官方文档实现。

## 6B：node sender

只使用 node 自己明确提供的发送者字段。

字段缺失：

```text
unavailable
```

## 6C：嵌套 forward

递归 parser。

必须有：

```text
depth
visited_forward_ids
max_depth
max_nodes
```

## 6D：fixture

必须从测试群真实生成至少：

1. 一个普通合并转发；
2. 一个二层嵌套合并转发；
3. 如果能复现，保存一个“内层作者信息消失”的脱敏 fixture。

这些 fixture 是以后防回归最重要的资产。

---

# Phase 7 — Normalized Storage

将 Internal Message Model 保存到数据库。

验收：

数据库读取后可以重建：

```text
message
└─ forward
   ├─ node
   │  └─ text
   └─ node
      └─ forward
         └─ node
            └─ image
```

如果重建时层级、position 或 author source 丢了，就不算完成。

---

# Phase 8 — FastAPI 查询接口

实现：

```text
GET /health
GET /api/v1/messages
GET /api/v1/messages/{internal_id}
GET /api/v1/groups/{group_id}/messages
GET /api/v1/debug/onebot-status
```

返回 Internal Message Model，不返回某个 AI 专用格式。

`/health` 最好包含：

```text
app
database
onebot
```

例如：

```json
{
  "status": "ok",
  "onebot": "connected",
  "database": "ok"
}
```

---

# Phase 9 — Bot 基础指令

普通消息：

```text
只记录，不回复
```

实现少量调试指令，例如：

```text
/help
/status
/count
```

触发规则需避免机器人回复每一条消息。

不要在这一阶段实现 AI。

---

# Phase 10 — 真实群跑测

让 Bot 在测试群连续运行。

人工发送：

```text
普通文字
@人
图片
文件
回复
合并转发
嵌套合并转发
```

抽样比较：

```text
QQ UI
vs
数据库/Internal Message JSON
```

检查：

- 数量；
- 时间；
- sender；
- segment 顺序；
- forward tree；
- unknown author。

达到 `AGENTS.md` 的 AI_READY_GATE 后才继续。

---

# Phase 11 — LLM Provider 抽象

创建：

```text
app/ai/providers/
```

概念接口：

```python
class LLMProvider(Protocol):
    async def chat(self, messages, **kwargs) -> LLMResult:
        ...
```

先实现 DeepSeek provider。

API Key：

```text
DEEPSEEK_API_KEY
```

只从环境变量读取。

DeepSeek API 字段、模型名、限制等必须查看开发时的官方文档：

https://api-docs.deepseek.com/

不要把当前网络文章里的模型名当作永久事实。

---

# Phase 12 — 群聊自动总结

数据流：

```text
DB
 ↓
按 group + time range 查询
 ↓
Message Renderer
 ↓
Chunker
 ↓
LLM
 ↓
Summary
 ↓
send_group_msg
```

第一版支持：

```text
/summary
```

之后再加入定时任务。

## Summary 输出建议

```text
今日重要信息
讨论结论
待办事项
时间/地点/DDL
尚未确定事项
```

不要把群里无意义表情刷屏作为重点。

---

# Phase 13 — 图片理解

消息 ingestion 已经完成之后，才增加：

```text
image node
  ↓
Media Service
  ↓
Vision Provider
  ↓
caption/description
  ↓
derived metadata
```

原始 image node 不被覆盖。

AI 描述是“派生数据”，不是 QQ 原始数据。

---

# Phase 14 — 群聊问答 / RAG

最后再考虑：

```text
message retrieval
embedding
vector index
rerank
LLM QA
```

第一版甚至可以先用：

```text
SQL 时间范围 + 关键词检索
```

不要一开始就引入向量数据库。

---

# 每个 Phase 给 Codex 的通用执行 Prompt

复制下面内容给 Codex，并替换 `{PHASE}`：

```text
请阅读仓库根目录 AGENTS.md、docs/ARCHITECTURE.md、
docs/MESSAGE_MODEL.md 和 docs/IMPLEMENTATION_PLAN.md。

现在只执行 {PHASE}，不要提前实现后续 Phase。

要求：
1. 先检查现有代码，不重复造已有模块。
2. 遇到 NapCat / OneBot 字段或 action 不确定时，查当前官方文档，
   不得凭记忆编造。
3. 严格区分 event sender、message author、forwarder、
   forward node sender、nested forward node sender。
4. 原作者缺失时必须标记 unknown/unavailable，
   不得继承外层转发者。
5. 保留 raw event 和完整消息树，不提前扁平化。
6. 实现后运行全部相关测试。
7. 最后输出：
   - 修改文件
   - 关键设计
   - 测试命令及结果
   - 人工验证步骤
   - 尚未解决的问题
8. 不提交任何真实 Token、Cookie、密码、API Key。
```
