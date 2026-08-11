# AGENTS.md — qqgroupchatbot 开发规则

文档更新时间：`2026-08-11`

本仓库当前只支持 `QQ Official Bot`。Codex 在修改本项目时必须优先遵守本文件。

## 1. 项目基线

项目定位已经固定为：

> 一个运行在 VMware Ubuntu 上的轻量、单机 QQ Official 群聊 AI Bot。

当前 active design 只支持：

```text
QQ Official Bot
```

以下路线均为 `legacy / deprecated / no longer maintained`：

```text
NapCat
OneBot 11
个人 QQ 小号 Bot
OneBot WebSocket transport
send_group_msg
OneBot action
OneBot compatibility layer
OneBot runtime
```

除非用户明确要求，否则不要继续修改、扩展或重新设计这些旧路线。

## 2. 核心原则

### 2.1 raw-first

- 先保存 raw event，再做 parser 和上层处理。
- parser、summary、provider 或 outbound 失败都不能导致 raw event 丢失。

### 2.2 internal model first

- LLM、renderer、repository、summary command 都依赖内部模型。
- 禁止直接把 Gateway raw JSON 拼成 prompt。

### 2.3 不猜字段

- QQ Official 字段只有在官方文档或仓库真实 sample 明确存在时才可使用。
- 无法确认就保留在 `raw_payload`，上层按 `None` / `unknown` / `unavailable` 处理。

### 2.4 简单、单机、可替换

- 默认方案是 `Python + asyncio + SQLite + local filesystem`。
- 如果想新增 `Redis`、消息队列、worker service、微服务、向量数据库、分布式锁或复杂事件总线，必须先说明为什么当前单机方案做不到。

## 3. 官方资料与证据优先级

涉及 QQ Official 当前行为时，优先级固定为：

1. 腾讯 QQ Official 最新官方文档
2. 仓库真实脱敏 sample：`data/qq_official_samples/`
3. 第三方 reference implementation，例如 `zhinjs/qq-official-bot`

涉及 DeepSeek 当前 API 时，只使用 DeepSeek 官方文档。

### 3.1 当前官方文档基线

`2026-08-11` 复核的腾讯官方文档当前写明：

- AccessToken：`POST https://api.bot.qq.com/app/getAppAccessToken`
- OpenAPI 统一地址：`https://api.bot.qq.com`
- 群消息发送路径：`POST /v2/groups/{group_openid}/messages`
- 群被动回复有效期：`5 分钟`
- 每条群消息最多回复：`5 次`

仓库现有 adapter 代码与测试中仍存在较早 host 常量。这是实现漂移问题，不是架构方向问题。后续如果需要改代码，必须重新核对官方文档后再改，不要把老 host 继续扩散到新逻辑里。

说明：

- 官方文档当前仍描述了主动消息相关规则
- 但本项目当前 phase 只围绕“由入站消息触发的被动回复”构建
- 不要把“只有 `group_id` 也主动发群消息”当作默认开发任务

### 3.2 第三方实现参考原则

可以将 `https://github.com/zhinjs/qq-official-bot` 作为 QQ Official adapter / event / parser 的交叉验证参考，但只能用于：

- 验证 `GROUP_MESSAGE_CREATE` / `GROUP_AT_MESSAGE_CREATE` 的字段映射
- 验证 `content`、`mentions`、`message_reference`、`attachments` 的常见处理方式
- 帮助发现本仓库 sample 尚未覆盖的字段

禁止：

- 直接依赖该 npm SDK
- 将项目改为 TypeScript
- 直接搬用其领域模型
- 因第三方实现缺少某能力而删除本项目的 `ForwardSegment`
- 把第三方行为当作腾讯官方规范

在当前 DeepSeek 文本闭环真实验收完成前，不要因为第三方参考仓库而重构现有 `QQOfficialMessageParser`。之后再安排独立的 `QQ Official Parser Cross-check` 阶段。

## 4. 当前运行链路

当前代码与测试已经证明的主链路是：

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
→ LLMProvider
→ SummaryRepository
→ SummaryMessageFormatter
→ QQOfficialGroupMessageSender
→ QQ Official passive reply
```

当前唯一运行内 AI 功能是 `#总结`。

## 5. 代码边界

### Gateway client

- 只负责 `/gateway/bot`、WebSocket connect、HELLO / IDENTIFY / HEARTBEAT、dispatch、reconnect。
- 不做 parser。
- 不做 summary。
- 不直接写数据库。

### Raw event ingestion

- 只负责把 `QQGatewayDispatch` 变成 raw receipt。
- 保留完整 payload。
- 只提取明确存在的 query/debug index。

### Parser

- 只做纯解析。
- 不访问网络。
- 不推断缺失身份。
- `message_type == 102` 当前按 unresolved forward 保存。
- `message_type == 103` 当前只保留 opaque reply 引用信息。

### Storage

- `raw_events` 是事实源头。
- `messages` / `message_nodes` 当前用于保存可重建的 `InternalMessage` 树。
- `summaries` 只保存 validated domain result，不保存 prompt、完整 conversation 或 provider body。

### Outbound sender

- 只负责拿 token、调 QQ Official HTTP API、校验响应。
- 不做 summary 逻辑。
- 不做上下文选择。
- 当前 summary / planned chat reply 都默认走由入站消息触发的被动回复链路。

### Summary / Chat / QA service

- 只能依赖 `LLMProvider` 抽象。
- 禁止直接依赖 `DeepSeekProvider`、某个中转站品牌或某个具体模型。

## 6. Summary command 规则

当前只允许以下消息触发 `#总结`：

```text
platform == "qq_official"
provenance == direct_event
sub_type == GROUP_MESSAGE_CREATE
top-level exact text == #总结
group_id is present
platform_message_id is present
timestamp is timezone-aware
```

不会触发：

- reply 引用内容
- forward 展示文本
- attachment metadata
- unknown raw fields

处理顺序必须固定：

```text
generate
→ persist
→ format
→ send
```

失败处理：

- generate 失败：不 persist，不 send
- persist 失败：不 send
- send 失败：保留 summary row，不删除，不再次调用 LLM，不盲目自动重试

## 7. InternalMessage 与存储规则

### 7.1 InternalMessage 要严格，Storage 可以轻量

`InternalMessage` 必须继续完整表达：

```text
sender
author
reply
image
file
forward
nested forward
unknown
provenance
```

但存储实现的第一原则是：

```text
原始事实不丢失
树结构能重建
身份来源能重建
```

第二原则才是：

```text
schema 简单
部署简单
查询简单
```

当前已经存在的 `messages + message_nodes` 是有效实现，不要求为了“更范式化”继续无限拆表。

### 7.2 raw event 永远保留

必须保持：

```text
Gateway dispatch
→ raw_events
→ parser
→ InternalMessage
```

不要退化成只留 `sender + text` 的 memory bot。

### 7.3 Forward / Nested Forward 不能 flatten

必须继续严格区分：

- event sender
- message author
- forwarder
- forward node sender
- nested forward node sender
- original sender

缺失作者时只能是：

```text
unknown / unavailable
```

禁止把外层转发者当成原作者。

`ForwardSegment` 的树结构必须在 `InternalMessage` 中保留。只有 `MessageRenderer` 才允许为 LLM 输入做有损展示。

未来如果做 reply / forward 补全，应使用概念化边界，例如：

```python
class MessageReferenceResolver:
    async def resolve_reply(...):
        ...

    async def resolve_forward(...):
        ...
```

是否真的能 resolve，取决于当前 QQ Official API、当前 Gateway payload 和当前真实 sample；无法确认就继续保留 `unresolved / unavailable`。

## 8. LLM 路线

当前开发策略固定为三步：

1. 先用 `DeepSeekProvider` 完成真实 QQ Official `#总结` 闭环验收
2. 再实现 `OpenAICompatibleProvider`
3. 再开始图片多模态、聊天回复、QA

业务 Service 只能面向：

```text
LLMRequest
→ LLMProvider
→ LLMResponse
→ domain validation
```

不要让 `SummaryService`、未来的 `ChatReplyService`、`QAService` 知道 provider-specific 细节。

## 9. FastAPI 定位

FastAPI 不是 Bot 本身，只作为：

```text
health
debug
query
admin
manual API
```

当前已实现的是 `/health` 和 runtime lifespan 管理。

## 10. 交付要求

每次修改后至少完成：

1. 列出修改文件
2. 说明核心设计
3. 运行测试
4. 给出测试结果
5. 给出人工验证方法

如果某项无法完成，要明确说明阻塞点。
