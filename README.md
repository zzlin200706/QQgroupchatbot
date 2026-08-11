# qqgroupchatbot

`qqgroupchatbot` 是一个运行在 VMware Ubuntu 上的轻量、单机 `QQ Official Bot` 群聊 AI Bot。

当前 active design 只支持 `QQ Official Bot`。`NapCat`、`OneBot 11`、个人 QQ 小号 Bot、OneBot WebSocket transport、`send_group_msg` 和相关兼容层均视为 `legacy / deprecated / no longer maintained`。历史代码和历史 Git commit 可以保留，但它们不再是当前设计依据。

## 当前基线

- 默认运行环境是 `VMware Ubuntu + Python + QQ Official Gateway/OpenAPI + SQLite + local filesystem + external LLM API`
- 核心原则是 `raw-first`：先保存 raw event，再做 parser 和上层处理
- 领域边界是 `internal model first`：LLM、renderer、repository、summary command 只依赖 `InternalMessage`
- 默认长期部署方案是 `SQLite + ./data/ + 单进程 asyncio`，不是多服务重基础设施
- 当前唯一运行内 AI 功能是群聊 `#总结`
- 当前 AI 路线固定为：
  1. 先完成 `DeepSeekProvider` 的真实闭环验收
  2. 再实现 `OpenAICompatibleProvider`
  3. 最后再做图片多模态、聊天回复、QA

## 当前实现

- QQ Official AccessToken 获取
- QQ Official `/gateway/bot` 接入点获取
- Gateway HELLO / IDENTIFY / HEARTBEAT / reconnect loop
- raw event persistence 到 `raw_events`
- `QQOfficialMessageParser` 到 `InternalMessage`
- `MessageRepository` / `ConversationQueryService` / `MessageRenderer`
- `SummaryService` / `SummaryRepository` / `SummaryMessageFormatter`
- `DeepSeekProvider`
- QQ Official group passive reply sender
- FastAPI `/health` 和应用生命周期管理

## 当前运行链路

```text
QQ Official Gateway
→ QQOfficialGatewayClient
→ QQGatewayDispatch
→ QQOfficialRawEventIngestionService
→ raw_events
→ QQOfficialMessageParser
→ InternalMessage
→ MessageRepository
→ SQLite
→ ConversationQueryService
→ MessageRenderer
→ SummaryService
→ LLMProvider (current: DeepSeekProvider)
→ validated SummaryResult
→ SummaryRepository
→ SummaryMessageFormatter
→ QQOfficialGroupMessageSender
→ QQ Official passive reply
```

当 `SUMMARY_COMMAND_ENABLED=false` 时，当前 runtime 仍会继续做：

```text
Gateway
→ raw_events
→ parser
→ normalized messages
```

只是不会启动 `#总结` 的 LLM 闭环。

## 当前约束

- summary 命令只接受顶层直接群消息中的精确文本 `#总结`
- summary 顺序固定为 `generate → persist → format → send`
- 当前项目的实际回复链路只做由入站消息触发的被动回复，不把“只有 `group_id` 的主动群发”作为当前 phase 范围
- parser 和 summary 失败都不能导致 raw event 丢失
- `message_type == 102` 当前仍按 unresolved forward 保存
- `message_type == 103` 当前只保留 reply 的 opaque 引用信息，不把 `ref_msg_idx` 伪装成真实消息 ID
- 不允许把 forward tree 提前 flatten 成 `sender + text`
- 不主动引入 `PostgreSQL`、`MySQL`、`Redis`、`Kafka`、`RabbitMQ`、`Celery`、`Elasticsearch`、独立向量数据库、`Kubernetes`、微服务

## 官方文档说明

`2026-08-11` 复核的腾讯官方文档当前将 AccessToken 和 OpenAPI 统一地址写为 `https://api.bot.qq.com` 体系；仓库现有 adapter 代码与部分测试仍保留较早的 host 常量。这是一个需要单独验证和对齐的实现漂移问题，不改变本项目 `QQ Official only` 的设计方向。

涉及 QQ Official 字段、事件体、发送接口、被动回复规则时，优先级固定为：

1. 腾讯当前官方文档
2. 仓库内真实脱敏 sample：`data/qq_official_samples/`
3. 第三方 reference implementation

## 设计文档

- `AGENTS.md`
- `docs/ARCHITECTURE.md`
- `docs/IMPLEMENTATION_PLAN.md`
- `docs/MESSAGE_MODEL.md`

后续开发默认先读这几份文档，再读 `app/` 与测试。
