# qqgroupchatbot

`qqgroupchatbot` 是一个运行在 VMware Ubuntu 上的轻量、单机 `QQ Official Bot` 群聊 AI Bot。

当前 active design 只支持 `QQ Official Bot`。`NapCat`、`OneBot 11`、个人 QQ 小号 Bot、OneBot WebSocket transport、`send_group_msg` 和相关兼容层均视为 `legacy / deprecated / no longer maintained`。历史代码和历史 Git commit 可以保留，但它们不再是当前设计依据。

## 当前基线

- 默认运行环境是 `VMware Ubuntu + Python + QQ Official Gateway/OpenAPI + SQLite + local filesystem + external LLM API`
- 核心原则是 `raw-first`：先保存 raw event，再做 parser 和上层处理
- 领域边界是 `internal model first`：LLM、renderer、repository、summary command 只依赖 `InternalMessage`
- 默认长期部署方案是 `SQLite + ./data/ + 单进程 asyncio`，不是多服务重基础设施
- AI 功能包括群聊 `#总结`、grounded `#问`、结构化 `@bot` 对话，以及可选的图片理解
- reply reference 保真解析；单独 reply 有意不触发 AI，只有 `#问` 或 structured `@bot` 可以触发
- 普通群消息和普通图片只入库，不触发 LLM；文件理解、RAG 和工具调用未实现

## 当前实现

- QQ Official AccessToken 获取
- QQ Official `/gateway/bot` 接入点获取
- Gateway HELLO / IDENTIFY / RESUME / HEARTBEAT / reconnect loop
- raw event persistence 到 `raw_events`
- `QQOfficialMessageParser` 到 `InternalMessage`
- `MessageRepository` / `ConversationQueryService` / `MessageRenderer`
- `SummaryService` / `SummaryRepository` / `SummaryMessageFormatter`
- `DeepSeekProvider`
- `OpenAICompatibleProvider`
- QQ Official group passive reply sender
- `#ping` / `#总结` command dispatch
- `#问 <问题>` 当前群历史 grounded QA
- `GROUP_AT_MESSAGE_CREATE` 或真实 sample 中 `mentions[].is_you=true` 的 `@bot` 对话
- 成功 Bot 回答独立持久化到 `assistant_interactions`
- reply 的 opaque reference 与 quoted content 保真解析、存储
- 结构化 `@bot + reply` 可把平台引用正文作为不可信上下文使用，不推断 quoted author
- 最近群消息与成功 assistant turns 的 group-scoped 多轮上下文
- QQ JPEG / PNG / GIF attachment metadata 到 `ImageSegment` 的原样持久化
- 有超时、大小、MIME、redirect 和 public-network 检查的图片资源解析器
- 独立 `VisionProvider` capability 与 OpenAI-compatible data-URL multimodal request
- summary、grounded QA 和 structured `@bot` 路径中的按需图片 enrichment
- `assistant_trigger_claims` 与 `command_trigger_claims` 持久化幂等保护
- inbound transport selector: `QQ_EVENT_TRANSPORT=websocket|webhook` (default `websocket`)
- FastAPI `/health` 和应用生命周期管理

## 当前运行链路

```text
QQ Official WebSocket / Webhook
→ QQOfficialInboundEvent
→ QQOfficialEventProcessor
→ QQOfficialRawEventIngestionService
→ raw_events
→ QQOfficialMessageParser
→ InternalMessage
→ MessageRepository
→ SQLite
→ QQOfficialInteractionDispatcher
├── #ping → QQOfficialGroupMessageSender
├── #总结 → SummaryService → SummaryRepository → QQOfficialGroupMessageSender
└── #问 / structured @bot
    → ConversationQueryService
    → optional ImageEnrichmentService
    → MessageRenderer
    → GroupAssistantService
    → configured LLMProvider
    → QQOfficialGroupMessageSender
    → AssistantInteractionRepository
```

图片只在明确 AI interaction 需要时解析：

```text
stored ImageSegment metadata
→ HttpImageResourceResolver
→ VisionProvider
→ immutable enriched message copy
→ MessageRenderer
→ SummaryService / GroupAssistantService
```

AI 描述是未独立验证的派生数据，不覆盖 `raw_events`、原始 URL 或数据库中的 source `ImageSegment`。

当 `SUMMARY_COMMAND_ENABLED=false` 且 `GROUP_ASSISTANT_ENABLED=false` 时，runtime 仍会继续做：

```text
inbound transport
→ raw_events
→ parser
→ normalized messages
```

只是不会启动对应 LLM 闭环。`.env.example` 推荐启用 assistant；代码默认关闭，避免无凭证环境启动即失败。

当前默认 transport 仍是已真实 smoke 过的 `websocket`。Webhook 入站架构已完成代码与自动化验证，但真实 QQ webhook smoke 仍 pending。

## 当前约束

- summary 命令只接受顶层直接群消息中的精确文本 `#总结`
- summary 顺序固定为 `generate → persist → format → send`
- 当前项目的实际回复链路只做由入站消息触发的被动回复，不把“只有 `group_id` 的主动群发”作为当前 phase 范围
- parser 和 summary 失败都不能导致 raw event 丢失
- `message_type == 102` 当前仍按 unresolved forward 保存
- `message_type == 103` 将 opaque target `REFIDX` 保存为独立 `ReplySegment.reference_key`，绝不写入 `referenced_message_id`
- reply 本身不是 assistant trigger；没有 `#问` 或 structured `@bot` 时只做 raw-first 入库，不 claim、不调用 LLM、不回复
- structured `@bot + reply` 会把可取得的 quoted content 标记为 untrusted platform data；不会从正文推断被引用者身份
- `#问` 不使用历史 Bot 回答作为事实；chat 才会把成功 Bot turn 标记为 assistant-generated context
- 图片 URL 不在 parser 中下载；vision 关闭或单张图片失败时仍保存消息并显示中性占位
- QQ payload 将 `content` 与 `attachments` 分开提供时，parser 保留 content segments 后接 attachment list 的可证实顺序，不猜测更细的视觉交错位置
- 不允许把 forward tree 提前 flatten 成 `sender + text`
- 不主动引入 `PostgreSQL`、`MySQL`、`Redis`、`Kafka`、`RabbitMQ`、`Celery`、`Elasticsearch`、独立向量数据库、`Kubernetes`、微服务

## 官方文档说明

`2026-08-11` 复核的腾讯官方文档将 AccessToken 和 OpenAPI 统一地址写为 `https://api.bot.qq.com`；当前 auth、Gateway 和 group-message sender 均使用该地址。

涉及 QQ Official 字段、事件体、发送接口、被动回复规则时，优先级固定为：

1. 腾讯当前官方文档
2. 仓库内真实脱敏 sample：`data/qq_official_samples/`
3. 第三方 reference implementation

Reply policy：项目有意不支持“仅回复 Bot 消息就自动触发 Assistant”。QQ reply 仍按通用消息解析、保存；只有 `#问` 或 structured `@bot` 能触发 AI。

## 安装与运行

要求 Python `>=3.10`：

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
.venv/bin/python -m app.main
```

`.env` 至少需要 QQ App ID/Secret。启用 summary 或 assistant 时还需要选择并配置 text provider：

```dotenv
QQ_EVENT_TRANSPORT=websocket
SUMMARY_COMMAND_ENABLED=true
GROUP_ASSISTANT_ENABLED=true
LLM_PROVIDER=openai_compatible
OPENAI_COMPATIBLE_BASE_URL=https://provider.example/v1
OPENAI_COMPATIBLE_API_KEY=
OPENAI_COMPATIBLE_MODEL=
```

启用图片理解时追加：

```dotenv
VISION_ENABLED=true
VISION_PROVIDER=openai_compatible
VISION_MODEL=
IMAGE_DOWNLOAD_TIMEOUT_SECONDS=15
IMAGE_MAX_BYTES=10485760
```

Vision 复用 OpenAI-compatible endpoint 和 API key。Text 可以使用 DeepSeek，同时由单独的 OpenAI-compatible provider 处理 vision；缺少可选 vision 配置时应保持 `VISION_ENABLED=false`，文本链路不受影响。

## 测试

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/test_image_vision.py --help
```

使用本地 JPEG / PNG / GIF 对已配置 relay 做一次人工 vision 验证：

```bash
.venv/bin/python scripts/test_image_vision.py path/to/test.png
```

脚本只输出简短描述，不输出凭证或 base64 数据。

## QQ Smoke

在测试群按顺序验证：

1. 普通消息：入库，不回复。
2. `#ping`：被动回复 `pong`，不调用 LLM。
3. structured `@Bot 你好`：调用 chat 并被动回复。
4. 单独 reply：保留 reply 数据，不回复。
5. reply + structured `@Bot`：引用正文作为 author unavailable 的不可信上下文。
6. `#问 ...`：只基于当前群历史回答。
7. `#总结`：总结当前群配置窗口。
8. image + structured `@Bot`：vision 成功时将派生描述加入 chat；失败时安全降级。

真实 Webhook 和 vision smoke 尚未由自动化测试替代；默认 transport 保持已经真实验证过的 `websocket`。

## 设计文档

- `AGENTS.md`
- `docs/ARCHITECTURE.md`
- `docs/IMPLEMENTATION_PLAN.md`
- `docs/MESSAGE_MODEL.md`

后续开发默认先读这几份文档，再读 `app/` 与测试。
