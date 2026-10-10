# NapCat 嵌套转发可行性验证（已归档，未接入）

**2026-10-10 用户决定停止该功能。** 本文仅保留此前研究记录，以下部署、
登录与适配方案均不再推进，也不表示当前授权。离线审计脚本仅为研究附件，
不属于 Bot 运行链路；未安装 NapCat、未登录个人 QQ、未启用 OneBot。

日期：2026-10-10。用户明确允许研究 NapCat/OneBot，授权边界为：不登录
QQ、不启动替代机器人、不修改现有 QQ Official 运行配置。生产架构仍以
AGENTS.md 的 QQ Official 为准，本报告和离线检查工具不构成第二套 runtime。

## 结论

**具备源码层面的可行路径，值得做独立实机验证；尚未证明用户这份嵌套
记录能被完整获取。** 不应现在迁移，也不应承诺换接入端就一定成功。

此次以 `git ls-remote` 固定 NapCat main 提交：
`abda6a96de9efef7528ba3f07958418b0291291a`。GitHub API 因未认证速率限制
返回 403，改用只读 git 引用和固定 SHA 的 raw 文件核对，未克隆或执行源码。
以下结论仅对应该提交，不代表所有版本。

## 已验证的实现边界

1. [GetForwardMsg.ts](https://github.com/NapNeko/NapCatQQ/blob/abda6a96de9efef7528ba3f07958418b0291291a/packages/napcat-onebot/action/go-cqhttp/GetForwardMsg.ts)
   接受 `message_id` 或 `id`，返回 `messages`。普通数字消息 ID 依赖
   客户端本地消息映射；另一分支使用转发资源 ID 获取内容。不能把官方
   Bot 的 opaque `REFIDX` 或事件 ID 当作这里的 ID。
2. [消息解析实现](https://github.com/NapNeko/NapCatQQ/blob/abda6a96de9efef7528ba3f07958418b0291291a/packages/napcat-onebot/api/msg.ts)
   的 getMultiMessages 保留 root/parent 上下文获取子消息；
   parseMultiMessageContent 再以展开模式解析子消息，嵌套数据位于
   `forward.data.content`。图片段通过 FileApi.getImageUrl 获取 URL。
3. **修正先前过于笼统的方案：不能假定每个内层 ID 都可重复 get_forward_msg。**
   子消息 ID 在源码中被说明为展示用途；获取接口也会拒绝过期或无法
   定位的内层消息。应优先获取外层并检查已经展开的 content，而不是
   编写“遇到 ID 一律递归 HTTP 请求”的逻辑。
4. 图片 URL 获取失败、身份转换失败或转发获取失败的分支可能跳过节点。
   因此 HTTP 成功不等于完整；必须逐项对照 QQ 客户端的文字/图片数量。
5. 获取接口的回退模板包含占位 peer/sender 值。返回 group_id 不能直接
   用作可信群绑定，也不能据它映射 QQ Official group_openid。将来如接入，
   必须以实际接收事件建立隔离上下文，不根据昵称、文字相似度自动拼接。

[NapCat 官方配置文档](https://napneko.github.io/config/basic) 包含 array 消息
格式、parseMultMsg 和服务端 token 配置。**这些都是 NapCat 配置，不是
给当前 QQ Official `.env` 加一个开关。** 当前未创建或启用这些配置。

## 独立验证方案（需后续授权登录与部署）

1. 在独立测试环境准备用户自有测试账号；不接生产事件处理器、不配置
   群消息发送动作。只监听本机地址并配置鉴权；不要将控制面/API 裸露公网。
2. 同一测试群只发送一次用户原来的外层嵌套转发，不另外发送内层。
3. 保存测试接入端的外层事件和 get_forward_msg 原始响应，保留原始事实，
   本地限制访问权限；不得公开带签名 URL、凭证或身份的原始样本。
4. 检查外层响应中的 `forward.data.content`，对照客户端，确认目标内层
   的 5 张图片及 1 条文字确实存在。不能用旧官方 raw 61 冒充新获取结果。
5. 对真实图片 URL 使用现有安全下载/解码限制，确认字节可取；得到用户
   同意的模型测试范围后再测试 vision。只出现路径或 URL 不算图片读取成功。
6. 测试两层、三层、重复引用、缺作者、缺节点、失效链接，并验证失败可见、
   限额、原作者来源及缓存隔离。若任一关键层缺失，判为未通过，不先迁移。

通过后可另行设计独立 adapter，把原始事实和递归树送入 InternalMessage；
复用 renderer/vision/service，而不让业务依赖 NapCat。官方账号与客户端
账号的群/消息/用户 ID 不可直接混用。该适配器本次**未实现或启用**。

## 已交付的离线工具

新增 `scripts/audit_napcat_forward.py`，只读取本地导出的 NapCat 响应。
不请求任何地址、不下载图片、不调用模型、不读 `.env`、不访问数据库。
报告仅输出每层文字/图片/转发数量和缺失项，不输出正文、昵称、ID 或 URL。
输入上限 4 MiB，深度 10，消息及消息段合计预算 500。

```bash
.venv/bin/python -m scripts.audit_napcat_forward /path/to/exported-response.json
```

退出码 0 仅代表该结构检查没有发现需审阅项，**不是实机验收通过**。
仍需检查 has_nested_text / has_nested_image_url_candidate 和逐层数量。
URL candidate 不表示已通过 DNS/SSRF 校验或已可下载。报告中的 live_fetch、
image_download、vision 验证字段固定为 false，防止混淆证据层级。
退出码 1 表示结构/缺失/限额问题需审阅；2 表示输入不可用或超限。

测试使用完全合成数据，不包含真实聊天，不执行上游源码。覆盖三层结构、
未展开 ID、无图片 URL、本地路径、无效响应、循环、深度/节点限额和脱敏。
定向测试 **15 passed**；完整离线套件以线程异常警告转错误运行，
**656 passed in 30.40s**，无警告；`git diff --check` 通过。

修改文件：本文件、`scripts/audit_napcat_forward.py`、
`tests/unit/test_napcat_forward_audit.py`。未修改运行时或安装 NapCat。
