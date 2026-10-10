# QQ Official 参考实现核对

核对日期：2026-10-02。结论来自当日读取的腾讯官方文档、仓库真实脱敏样本及参考源码；文档能力与真实平台验收分别记录。本文件不表示尚未完成的真实场景已经通过。

## 官方资料基线

| 官方资料 | 页面标注更新时间 | 本项目相关结论 |
| --- | --- | --- |
| [获取访问凭证](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html) | 2026-09-16 | `POST https://api.bot.qq.com/app/getAppAccessToken`；参数 `appId` / `clientSecret`；HTTP 200 仍可能通过 `code` 返回认证错误。 |
| [API 调用指南](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/api-call-guide.html) | 2026-09-17 | OpenAPI 统一地址 `https://api.bot.qq.com`；鉴权 `Authorization: QQBot …`；业务错误字段 `err_code`；201/202 也可能携带 error body。 |
| [发送群聊消息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html) | 2026-09-03 | `POST /v2/groups/{group_openid}/messages`；群被动回复有效期 5 分钟，每条入站消息最多回复 5 次；`msg_id` 使用入站 `d.id`，`msg_seq` 与它共同去重。 |
| [消息收发概述](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html) | 2026-07-21 | 再次确认群被动回复 5 分钟 / 5 次；主动消息是另一能力，本项目不据此开启主动群发。 |
| [群消息（全量模式）](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_message_create.html) | 2026-09-16 | `GROUP_MESSAGE_CREATE`，Intent `1 << 25`；列出 `author`、`mentions`、`attachments`、`message_scene`、`msg_elements`。 |
| [群 @ 机器人消息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_at_message_create.html) | 2026-09-16 | `GROUP_AT_MESSAGE_CREATE`，相同 Intent；文档描述 content 已去机器人 @ 前缀，mentions 不含机器人自身；以事件类型识别显式 @ 有依据。 |

群发送文档登记 `304103` / `40034005` 为消息引用过期，`40034128` 为被动回复时间或次数超限。发送前应检查当前入站消息的时间预算，失败后保留已持久化结果；不得重跑 LLM 或盲目重发。OpenAPI 返回的 `id` 与引用索引 `ext_info.ref_idx` 是不同用途的字段。

### 2026-10-08 出站响应复核

重新读取上述腾讯 API 调用指南与群发送文档，确认 `err_code=0` 表示业务
成功，非零表示业务错误；不能依据错误描述文本判断。指南将 HTTP 201/202
描述为异步操作成功，并提示需要特别处理返回正文。

据此修正发送器：2xx 中的非零 `err_code` 不能作为发送成功回执；现有
`code` / `error.code` 兼容解析继续保留。对 201/202，本项目尚无异步
送达确认机制，因此保守地记为 delivery unconfirmed，不把异步受理
推断成群消息已送达，也不重发。后者是本项目的保守处理策略，不表示
腾讯规定所有 201/202 都是业务失败。成功的同步回执仍要求有效 `id`。

自动化测试覆盖正文业务错误、异步受理、畸形错误码及脱敏；使用真实
发送器加 MockTransport 的总结/outbox 集成测试确认已生成摘要保留、
raw 不丢失、失败不误标 completed、重复处理不再生成或发送。

## 固定版本参考

`git ls-remote` 读取到 `zhinjs/qq-official-bot` HEAD 为 `048172e4a6faf27c57b70f5321d30373666882bf`，该版本 `package.json` 声明版本 `1.3.0`。实际读取了该提交的 [message/parser.ts](https://github.com/zhinjs/qq-official-bot/blob/048172e4a6faf27c57b70f5321d30373666882bf/src/message/parser.ts) 与 [package.json](https://github.com/zhinjs/qq-official-bot/blob/048172e4a6faf27c57b70f5321d30373666882bf/package.json)。

其通用 parser 处理 `message_reference.message_id`、`<@!id>`、mentions 及附件 MIME 类型，同时会 `trimStart()` 并删除原 payload 的 attachments。它覆盖多个 QQ 场景，不是群聊字段规范，也没有提供本项目所需的完整 Forward 树证据。不得把这些变换或模型直接移植到 raw-first 管线。

Python 交叉参考为腾讯官方文档链接的 [tencent-connect/botpy](https://github.com/tencent-connect/botpy)。当日读取的 [botpy/http.py](https://github.com/tencent-connect/botpy/blob/master/botpy/http.py) 在登录时请求 `/users/@me`，独立获取机器人资料，而不是把认证 AppID 当作用户资料。该文件仍含旧 `api.sgroup.qq.com` host，因此其地址常量不作为当前官方基线；此处是浮动分支参考，没有将其冒充固定提交验收。

## 身份与引用边界

[启动接入](https://bot.q.qq.com/wiki/develop/api-v2/) 将 AppID / AppSecret 定义为接入票据；API 调用指南说明用户与群 OpenID 随应用隔离。群事件 User.id 是 OpenID，author.member_openid 是群成员身份。没有官方文档或本仓库真实样本证明 `AppID == 引用消息作者的群 OpenID`。

因此 runtime 不应将 `qq_bot_app_id` 自动传给 `bot_user_id`。当前保持仅凭引用的聊天触发关闭；没有新增要求用户猜测 ID 的配置。未来需要身份触发时，应先证明所使用的身份与群事件作者处于同一标识空间，不能仅因 `/users/@me` 或 Gateway READY 返回了某个 ID 就推断它等于群 OpenID。

`017_reply_human_smoke_sanitized.json` 与 `018_reply_bot_smoke_sanitized.json` 都只有引用正文和 opaque `msg_idx`，均没有嵌套 author。因此无法用作者判断二者，也不能用引用正文相似度猜测 bot 身份。官方 MsgElement 表允许 author，但官方 103 示例也展示了缺失 author 的情况；可选字段不构成所有实包都包含该字段的保证。

群发送文档提供了另一条有证据的未来路线：发送成功响应 `ext_info.ref_idx` 对应消息事件 `message_scene.ext` 中的 `msg_idx` / `ref_msg_idx`，机器人引用自身消息可用此值。若以后实现 reply-only，应先保存实际成功发送回执的引用索引，再按同平台、同群精确关联，取得真实双向样本后验收；不能把 opaque 索引当作 Gateway message ID，也不能靠文本或 outer sender 补作者。

## Parser 差异与当前决定

| 场景 | 官方 / 参考 / 样本证据 | 当前处理及未直接扩展的原因 |
| --- | --- | --- |
| mentions | 本仓库 `002_at_bot.json` 使用 `<@id>` 与 `mentions[].is_you=true`；参考 parser 识别 `<@!id>`。 | 保留真实群样本映射，不因通用 SDK 扩大文本语法；`is_you` 有样本证据，`bot=true` 不能替代“是本机器人”。 |
| `message_reference` | 参考 parser 使用通用 message_reference；当前群入站官方表使用 msg_elements/message_scene。群发送请求另有 message_reference。 | 出站字段存在不能证明同形入站字段存在；未知入站字段仍留 raw，不跨场景移植。 |
| `103` 引用 | 官方允许 MsgElement.author、content、attachments 和递归 msg_elements；仓库真实引用样本常缺作者。 | 保留引用内容和 opaque key，缺作者 unavailable。当前仅提取已覆盖的文本/附件；不宣称引用已完整解析。 |
| opaque 引用索引 | 样本是 REFIDX，官方 103 示例还包含 `ref_msg_idx=TMP_…`。 | 当前 REFIDX 校验是样本子集；TMP 值留 raw、不伪造 message ID。扩大结构化索引范围须独立验证语义。 |
| `102` / nested forward | 2026-10-09 再读官方 MsgElement 表，确认 author、message_type、content、attachments、递归 msg_elements；真实 raw 45 仅有 409 字符展示文本，无 msg_elements。 | 用户明确要求后补齐阅读：展示正文存入 ForwardSegment.content，无节点仍 unresolved，不拆展示姓名；已存在的结构化解析按官方字段保留作者/树与预算，通过合成测试，但真实结构化转发仍单独验收。 |
| attachments | 官方列出 image/jpeg、image/png、image/gif、file，以及 voice/video 与语音参考字段。 | Image/File 映射已有样本依据；未知类型保留 UnknownSegment。第三方 URL 补协议、filename 改名和删除附件不能替代原始事实。 |

此次转发阅读按用户请求、真实 content 样本与腾讯官方字段实现，不是照搬
第三方 SDK。没有引入 npm SDK、移除 ForwardSegment、开启主动消息或
扩展旧 OneBot 路线。缺失节点仍不推断，缺失作者仍 unavailable；不将
渲染后的正文当成可信命令。原始消息及签名链接保留于 raw/storage，模型
只收到内部 segments 的展示内容，HTTP(S) 链接在转发展示中隐藏。

## 仍需真实验收的证据

1. DeepSeek 的真实 `#总结`：已于 2026-10-09 补齐 raw 36/37、内部消息、summary 3、outbox 2 和成功群被动回复证据，用户确认已看到摘要；此项已通过，详见 `ACCEPTANCE.md`。
2. @ 文本 / @ 图片、`#问` 与长上下文：2026-10-09 文本 `#问` 和结构化 @ 已通过（raw 39/40、interaction 9/10、outbox 3/4）；同日普通 JPEG 后续显式 @ 的 vision 链路亦通过（raw 41/42、interaction 11、outbox 5），用户确认描述正确。真实长上下文和其他图片交互场景仍须分别验证，不从这些成功案例推断通过。
3. 引用身份：分别采集引用 bot、引用成员、缺作者和冲突索引；验证发送回执 ref_idx 的平台行为。未验收前维持 reply-only 关闭。
4. 结构化 forward：取得包含真实作者与递归 msg_elements 的实包再验证树与来源重建；展示文本不能算结构化证据。
5. 被动回复时效与次数：验证队列等待和生成耗时后的到期拒发、成功后的回执持久化，以及重复入站不导致第二次生成或发送。

复核时可运行对应 parser、trigger、sender 与 command pipeline 的离线测试；人工验证使用现有捕获/验收工具，所有对外文档与样本保持脱敏。
