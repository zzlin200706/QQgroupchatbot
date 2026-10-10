# 验收记录

建立日期：2026-10-07；最近更新：2026-10-10。本记录区分代码测试、真实接口预检和群内端到端验收。

## 最终交付范围（2026-10-10）

- 保留已实现的 QQ Official 入库、ping、总结、问答/@、图片理解，以及转发展示中明确图片链接的安全读取。
- parser v3 保留平台实际给出的引用/转发树；图片经过限额下载、解码校验、请求内去重，并把描述放回对应上下文，不猜作者、不下载任意聊天链接。
- 保留 outbox 失败语义、关闭排空、健康检查、日志脱敏、长总结预算修复和离线重建工具；raw event 永久保留。
- 用户已取消“自动补取 QQ 未下发的嵌套记录”。单独转发的对照样本包含 5 张图片和 1 条文字，不能据此宣称外层消息已提供它们。NapCat/OneBot 未接入；研究文档与离线脚本仅作归档。
- 当前本机持久配置已启用问答和 vision，文本/vision 均使用用户指定的 `gpt-6-luna` 兼容接口。此次整理不修改 `.env`，不重启进程。下方早期“持久配置关闭”描述仅为历史记录。
- Webhook 实群验收因无公网 HTTPS 已延期；继续使用现有 Gateway 配置。
- `.env`、数据库及 WAL/SHM、日志和原始采集样本不提交。仓库 fixture 使用脱敏替身，本轮补齐带回车正文的脱敏；本地原始事实不变。

新增文件范围还包括：`app/domain/messages/forward_display.py`、
`app/services/forward_reparse.py`、`scripts/reparse_qq_official_forwards.py`、
转发/图片回归测试，以及归档的 NapCat 离线审计脚本及测试；其余文件分组见下方清单。

人工复核：保持单实例，测试群发送 `#ping` 应收到 pong；转发一份明确包含图片的记录，
引用并 @机器人询问文字与图片。仅对 QQ 实际提供的内容核对答案；缺失内层不应被编造。
本次发布前不重复发送真实群消息或调用模型；使用离线完整回归进行验证。

整理后完整离线回归：`pytest -q -W error::pytest.PytestUnhandledThreadExceptionWarning`
结果为 **656 passed in 28.79s**；`git diff --check` 通过。当前待提交文件未检出
本机配置的凭证值，fixture URL 均为测试域名。

发布检查发现本地 31 个未推送提交中曾跟踪 SQLite WAL/SHM。仅在新提交
取消跟踪不能消除历史副本，因此暂缓推送；须先经用户确认保留备份并清理
待推送历史，或改为从远端基线导出干净提交。不得把数据库历史直接上传。

以下按阶段保留历史验收证据；未完成事项以本节最终范围为准。

## 2026-10-10 新入站复核：内层内容仍未由 QQ 提供

用户确认已发送后，读取新 raw 58–60。raw 59（09:56:56，UTC+8）为
439 字符合并转发展示；raw 60（09:57:00）为引用 @ 问“里面的内容”，
包含一个仅有 content 的 MsgElement（493 字符）。59/60 均已存为 parser
v3，故先前的旧进程问题已不再是本次阻碍。outbox 15/source raw 60
为 completed，无 last_error；interaction 21 已保存模型回答。

逐项核对 raw：第 1 条只有发送者，无正文、附件或子 msg_elements；第
2 条提供一张 1264×2780 图片及下载链接；第 3 条提供一句文字。59 的
顶层无 msg_elements；60 的唯一元素也无子 msg_elements，parallel_message
只给“[聊天记录]”标题。没有合并转发类型标签或可供恢复第一条内容的
内层树。机器人回复描述了现有图片/群聊说法，但不能以此证明内层读取。

再次只读取得腾讯官方 GROUP_MESSAGE_CREATE 文档（HTTP 200），消息
导航仅发现收发概述、发送、撤回、入站全量/@事件；搜索未找到已验证的
嵌套聊天记录补取入口。此证据不等于断言平台绝对没有该能力；未猜测
API 路径、复用 auth_token 探测、引入 OneBot 或按标题关联历史内容。

结论：新代码已加载并成功回复，但本次仍未证明目标完成。需要核对
客户端第一条是否为内层转发，并将该内层单独转发作对照，确定其内容
是否仅在嵌套时被平台省略。不可再要求用户重复发送同样的外层消息并
期待本地解析生成不存在的内容。此次只读诊断与文档记录，未改业务代码。

## 2026-10-09 嵌套转发阅读修复（真实新样本待验收）

目标：阅读转发里面的转发，包括内层文字和图片。本轮未缩小为只读取
最外层，也未把离线测试视作真实 QQ 端到端验收。

现有证据：最新 raw 53 为展示形式的三条记录，第一条只提供发送者而
无正文/附件；raw 54–57 是同份记录的引用。没有可据以恢复该空条目内层
内容的结构化子节点。`parallel_message.msg_nodes` 仅有“[聊天记录]”标题，
不猜它指向什么，也不将外层转发者用作内层作者。已请用户发送新的嵌套
转发样本并引用 @ 询问，当前尚未收到 raw 57 之后的新入站。

已修复两个代码缺口：

- `_resolved_reply_message` 原先只对 102 做递归，103 内还有 103/102 时
  只留下本层文字/附件。现在使用递归解析，保留 Reply/Forward 树、原作者
  缺失状态、原始 JSON。真实样本中无子列表的 103 继续按已给出的引用正文
  处理，不额外制造空引用。
- 引用展示中更深的合并转发可能在第 3 行之后，旧识别入口会跳过图片。
  现在同时接受已观察的转发标题＋合并转发类型＋关联消息标签组合；仍只
  在引用/转发上下文提取明确附件图片链接，不下载任意聊天 URL。

引用/转发共用节点预算，深度限制同时计算引用与转发层。达到上限有
明确标记，未展开部分保留 raw；parser version 升为 3，原离线重建工具
可从 v1/v2 追加 v3，保留旧树且不重放命令。未对用户数据库执行重建。

官方依据：2026-10-09 通过 HTTP 只读再次核对
[QQ 群消息 MsgElement](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_message_create.html)
中的 102/103 与递归 msg_elements；网页浏览工具不可达后用本地 HTTP
获取官方页面（200），未参考个人号/OneBot 路线。

测试：**641 passed in 33.38s**，启用线程异常警告转错误，无警告。
覆盖真实脱敏二层/三层展示正文存储后到达问答 prompt；二层图片行使用
明确标注的合成 URL/mock 图片验证安全入口；混合四层转发/引用结构的
文字、图片、身份、入库往返、vision 描述和 prompt；递归深度/节点预算；
v2 丢失的内层内容重建为 v3，raw/旧版本保留且不发送消息。
三层真实样本中的附件是视频，测试未将其伪称为可识别的图片。

修改文件：`app/parsers/qq_official_message_parser.py`、
`app/domain/messages/segments.py`、`app/domain/messages/forward_display.py`、
`app/rendering/message_renderer.py`、`app/services/normalized_message_ingestion.py`、
`tests/unit/test_qq_official_forwards.py`、`tests/unit/test_forward_display_images.py`、
新增 `tests/integration/test_nested_forward_reading.py`、
`tests/integration/test_forward_reparse.py`、`README.md`、`docs/ARCHITECTURE.md`、本文件。

剩余验收：启动新代码，发送包含明确内层文字和图片的真正嵌套转发，
确认 raw 提供哪些内容、内部树保留了哪些内容，以及群内回答是否覆盖
内层事实。若当前平台不提供内层正文，需要以新实包为证据定位平台边界；
不得以“标题可见”宣称已经读取缺失的内层内容。目标仍 active。

## 2026-10-09 图片阅读优化：请求内复用与重点图片优先

在下面 JPEG/签名修复基础上，继续优化转发展示图片处理。模型和接口、
raw-first、作者身份与树结构均保持不变。

- ImageEnrichmentService 提供请求作用域；handler 在一次 answer 的
  当前引用和历史上下文生成阶段共用缓存和 10 资源限额，不再分别下载
  识别同一张转发图片。独立 summary 仍使用自己的批次。
- 当前引用先处理；QA/chat 的剩余历史图片从近到远处理，返回结果恢复
  原始顺序，因此只改变图片预算分配，不改变聊天时间线或命令语义。
- 缓存绑定 service、asyncio task、platform、group。不同群、不同
  请求、继承上下文的子任务不共享结果；嵌套作用域退出恢复外层缓存。
  成功、异常和取消均通过 finally 清理，未新增数据库缓存或后台服务。

修改文件：`app/services/image_enrichment.py`、
`app/services/group_assistant_handler.py`、
`app/services/group_assistant_context.py`、新增
`tests/unit/test_image_request_scope.py`、
`tests/integration/test_qq_official_assistant_pipeline.py`、`README.md`、本文件。

验证：完整离线套件启用线程异常警告转错误，**632 passed in 21.08s**，
无警告。包含同一图片跨引用/历史只下载和识别一次、共享名额、混合群
输入隔离、并发子任务隔离、异常/取消清理、嵌套恢复，以及 quoted/QA/chat
三种完整链路的图片优先级与时间线顺序。没有调用真实模型、发送群消息
或重启用户进程；此前 JPEG 修复的五张真实图片本地校验结果仍见下一节。

人工验收：正常停止旧机器人再启动 `.venv/bin/python -m app.main`。
发送图片合并转发，再引用它并 @ 请求逐图描述；同一次问答中，相同
资源不应重复出现 vision 成功调用。发送较多历史图片后询问最新图片，
应优先识别最新内容；超过名额仍明确提示，不猜测未读取图片。

## 2026-10-09 JPEG 尾随数据误判与签名去重修复

用户真实测试确认可识别转发图片，但有一张失败。诊断 raw 51 引用中的
第 1 张 JPEG：170544 字节，尺寸 2006×1280，结束标记后有 24 字节尾随
数据；本机可解码，但旧 `endswith(FFD9)` 规则误拒绝，图片未送入 vision。
此外 raw 47–51 中 17 条不同签名 URL 实际对应 6 个资源，旧 URL 去重
会耗尽 10 张额度。

修复：

- 新增 Pillow 依赖，保留 MIME/签名检查并进行 verify/load 解码；首帧
  2000 万像素、10 MiB 限制。不启用截断容错。JPEG 尾随数据经解码后
  以 EXIF 正确方向重新编码为干净 JPEG，编码输出同样限额，不覆盖源。
  参考 [Pillow Image 文档](https://pillow.readthedocs.io/en/stable/reference/Image.html)。
- 同批资源键仅忽略 rkey，保留 appid/fileid/spec 和未知参数；拒绝含
  重复参数或空必要参数的 URL。不同尺寸变体不合并，不扩大 URL 白名单。
- 资源成功缓存＋校验后字节哈希复用；失败不污染成功缓存。同资源最多
  尝试两个不同 URL，允许新签名恢复；总计最多 10 资源/20 次下载尝试。
  缓存仅属于单次 enrichment，引用与历史仍是独立批次，不跨群共享。
- 失败提示区分下载、格式/解码、大小/像素、安全地址及模型调用。日志
  含阶段、附件所在行、segment position 和资源短哈希，不含签名链接
  或异常正文。

本次修改文件：`pyproject.toml`、`app/services/image_resources.py`、
`app/services/image_enrichment.py`、`app/domain/messages/forward_display.py`、
`tests/unit/test_forward_display_images.py`、新增
`tests/unit/test_display_image_regressions.py`、`README.md`、本文件。
项目 `.venv` 已安装 Pillow 12.3.0，`pip check` 通过。

真实本地验证：raw 51 五张图片全部下载/校验通过，第 1 张规范化后为
278817 字节，其余四张无需改写；17 条 URL 的资源键归并为 6 个。
未把图片发送到真实模型，未修改数据库、模型配置或重启运行中机器人。
首次完整回归 622 passed，出现一条 aiosqlite 测试线程退出警告。增加
JPEG 尾随数据经过 enrichment 到达 mock vision 的回归用例后，以
`-W error::pytest.PytestUnhandledThreadExceptionWarning` 复跑完整套件：
**623 passed in 19.79s**，无警告。第一次警告未在严格复跑中复现，未
改动无关 SQLite 代码。`git diff --check` 通过。

人工验收：正常停止旧机器人后，用 `.venv/bin/python -m app.main` 启动；
重新引用含五张图片的转发并 @机器人逐图描述。确认原第 1 张不再因为
本地校验被拒绝，重复引用不再占额；如 QQ 链接过期，重新转发后测试。

## 2026-10-09 转发展示图片理解

按用户要求新增受限的展示图片识别入口，沿用现有 vision provider，
不修改 `.env`、parser version、数据库或运行中进程。

- 只在 Forward 内容或已解析的转发展示引用中匹配明确图片附件行。
  URL 仅接受已观察的 QQ HTTPS 下载端点；普通正文链接不触发下载。
- 复用安全 HTTP 下载器的地址、DNS、重定向、大小、超时、MIME 检查；
  新入口另做 PNG/JPEG/GIF/WebP 文件签名匹配（不是完整图像解码验证）。
- 图片字节经 VisionProvider 识别，描述放回对应附件行旁的临时文本副本。
  保留原文/raw/树和身份来源，不把展示姓名升级为已确认作者。
- 每批最多 10 个不同链接，含失败结果的同批去重；逐图失败隔离。
  引用与历史是独立批次，不提供跨请求持久缓存。
- renderer 与日志过滤器隐藏已观察的 QQ 签名图片链接；URL 不进入
  模型 prompt，只有图片字节和内部渲染内容进入对应模型请求。

修改文件：
`app/domain/messages/forward_display.py`（新增）、
`app/services/image_enrichment.py`、`app/services/image_resources.py`、
`app/rendering/message_renderer.py`、`app/log_redaction.py`、
`tests/unit/test_forward_display_images.py`（新增）、
`tests/integration/test_qq_official_assistant_pipeline.py`、`README.md`、本文件。

验证：完整离线测试 **603 passed in 19.25s**。覆盖图片行严格匹配、
拒绝任意链接/私网/私网重定向、体积限制、伪图片、vision 失败、预算、
去重、源对象不变、引用缩进、日志与 prompt 脱敏，以及 QA/chat/summary。
本地 raw 47/48/49/50 只读提取结果依次为 1/1/5/5 个候选；未下载这些
图片，未请求真实模型或发送 QQ 回复。真实 gpt-6-luna 图片调用及群内
端到端验收仍待验证，不把离线通过等同于中转站 vision 已确认可用。

人工验证：正常重启已有进程；发送含图片的合并转发，引用它并真实
@机器人询问逐图内容，或用 `#问` / `#总结` 查询窗口内的转发。
确认描述贴合图片、不同附件没有串位、原作者不被猜测；单独转发应静默。
若过期图片显示“无法解析”，重新转发获取新链接后重试。

## 2026-10-09 合并转发阅读实现

用户报告合并转发无法识别，随后明确要求实现阅读功能。继续当前工作区
已有解析改动并补齐收尾，不改模型配置、不重启用户进程、不请求真实模型。

- parser v2 将平台提供的展示正文放入 `ForwardSegment.content`；没有
  `msg_elements` 的实包仍 unresolved，不从正文猜节点、作者或时间。
- 显式 `msg_elements` 的作者与嵌套树按复核的官方字段保留；缺身份仍
  unavailable，深度/节点限额在正常入库、worker fallback、重解析中一致。
- renderer 标注平台展示未核验，隐藏展示 HTTP(S) 链接；只有内部消息
  内容进入 QA、chat、summary prompt。转发内部的命令与 @ 不触发回复。
- summary prompt v3、QA v2、chat v3 允许阅读展示正文，禁止把姓名/身份
  归给转发者、执行转发指令或补造未提供的内容。
- 新增可预览、可限定 raw ID 的离线重解析工具。只追加新 parser version，
  保留旧树和 raw；不生成 outbox、摘要、claims 或模型调用。

验证：针对性单元测试 127 项通过；完整离线套件 **575 passed in 18.70s**。
覆盖 captured 展示正文、结构化/嵌套合成样本、独立身份、存储往返、
跨群隔离、签名链接/鉴权信息不进 prompt、QA/chat/summary、quoted
prompt escaping、预算上限，以及旧记录预览/应用/幂等/失败保留。

真实本地修复：`--raw-event-id 45` 预览候选 1，应用新增 v2 记录 1。
原 409 字符展示正文通过正式 repository/renderer 验证可读；原 raw 的
SHA256 前后一致，v1 仍保留。raw_events=45 不变、messages=45→46，
summaries=3、assistant_interactions=13、outbox=7 均未变化，quick_check=ok。
未将此离线修复冒充群客户端已完成新功能验收。

修改文件：`app/parsers/qq_official_message_parser.py`、
`app/rendering/message_renderer.py`、`app/services/group_assistant_trigger.py`、
`app/services/group_assistant.py`、`app/services/summary.py`、
`app/services/normalized_message_ingestion.py`、`app/main.py`、
`app/services/interaction_outbox_worker.py`；新增
`app/services/forward_reparse.py` 与 `scripts/reparse_qq_official_forwards.py`。
测试位于 `tests/unit/test_qq_official_forwards.py`、
`test_qq_official_message_parser.py`、`test_message_renderer.py`、
`tests/integration/test_qq_official_assistant_pipeline.py`、
`test_forward_reparse.py`；同步 README、架构、计划、运维、参考与本记录。

人工验证：正常重启机器人，发送含具体事实的合并转发，再真实 @ 询问
“刚才转发主要讲了什么？”或发送 `#问 转发中的具体事实是什么？`；也可
发 `#总结`。单独转发应静默入库。已修复历史记录仍受时间窗口约束，超时
请重发。若平台只给标题/预览，机器人只能读到这些，不能保证完整记录。

## 2026-10-09 永久启用 assistant 与 vision

在保留原配置的决定之后，用户明确要求正常启动命令也启用问答、@ 和
图片理解。已在本地 `.env` 新增 `GROUP_ASSISTANT_ENABLED=true`、
`VISION_ENABLED=true`、`VISION_PROVIDER=openai_compatible`、
`VISION_MODEL=gpt-5.6-luna`。保留 WebSocket、已启用的总结、现有文本
provider/model/interface 和所有凭证，不将此前临时 DeepSeek 覆盖写回。

因此以下历史章节中的 assistant/vision 关闭状态只描述当时的配置，
不再代表当前状态。现在文本与 vision 均用现有兼容接口的
`gpt-5.6-luna`。配置加载及 provider 构造校验通过，确认二者复用同一
vision-capable provider；没有发送外部请求。配置、provider factory、
vision 单元测试共 47 项通过。修改文件仅 `.env` 和本记录；未启动机器人。

人工使用：在项目目录运行 `.venv/bin/python -m app.main`，连接就绪后
发送 `#ping`、`#问 问题`、真实 @ 对话，或普通图片后 @ 请求描述。
已有进程需先正常停止再启动，保持单实例；普通消息/图片仍只入库，
明确的 AI 请求才会按需将上下文与图片发送至已配置接口。

## 2026-10-09 Webhook 延期与配置保留

用户明确说明目前没有公网 HTTPS，暂不进行 Webhook 验收，仍使用之前
配置。因此 Webhook 真实回调验收按用户决定延期，不作为当前 WebSocket
运行方式的阻塞项，也不标记为已通过。

只读核对持久配置：transport=websocket，llm_provider=openai_compatible，
summary=true，assistant=false，vision=false。先前 DeepSeek/assistant/
vision 验收均为临时覆盖，未写回 `.env`；本次不改配置、不重新启动临时
进程。只更新本记录及实施计划，并执行 `git diff --check`；无代码改动，
未重复运行测试或发送模型/QQ 请求。

## 2026-10-09 图片实群验收

用户明确提出用已配置的 `gpt-5.6-luna` 测试 vision，并已允许测试所需
授权。先通过 `https://relay.example.test/v1/chat/completions` 发送内存生成的
653-byte 红蓝色块 PNG，提示中未给出颜色答案；模型正确描述“左半边
红色，右半边蓝色”。这证明该接口/模型配置在本次请求中能理解图片，
不是从模型名称推测能力，也不表示所有图片任务均已验证。

随后临时开启 vision/assistant，vision 使用该配置，文本回复仍使用
DeepSeek；关闭 summary，限制原测试群一次有输入的 mention_chat。
保留 30 分钟/80 条消息及 20 条成功机器人回答的 chat 上限。抑制 HTTP
请求日志，避免图片签名 URL 出现在日志中；未修改 `.env` 或应用代码。

人工步骤：用户先发普通图片（不 @，应不回复），再通过 QQ 真实 @ 请求
描述刚发图片及可见文字；最后明确确认“收到回复 描述正确”。

| 证据 | 结果 |
| --- | --- |
| 普通图片 raw 41 | GROUP_MESSAGE_CREATE；normalized 1 条，image/jpeg segment，outbox 0 条 |
| @ 请求 raw 42 | GROUP_MESSAGE_CREATE；normalized 1 条，mention_chat，outbox 5 |
| 图片 enrichment | 请求到来后才开始；下载并识别成功，117297 bytes |
| 出站及持久化 | assistant interaction 11，outbox 5 completed，attempt_count=1，last_error=null；两者的同群/触发消息/类型/response_message_id 精确匹配 |
| 完成时间 | 2026-10-09 01:26:51.427690 UTC |
| 最终文本元数据 | provider=deepseek，model=deepseek-flash，finish_reason=stop；input_chars=966，prompt_version=group-context-chat-v2 |
| 最终文本 usage | prompt=849，completion=129，total=978；仅文本请求，不冒充 vision usage |
| 原始事实 | 持久化 image segment 没有写入 AI description；描述仅临时派生，raw 保留 |
| 数据库 | raw_events=41、messages=41、summaries=3、assistant_interactions=11、outbox=5；quick_check=ok |
| 客户端 | 用户确认收到回复且描述正确 |
| 退出 | SIGTERM 后 worker 与应用正常 shutdown，临时进程已停止 |

结论：QQ 普通图片入库静默 → 后续显式 @ → 历史图片下载/vision 识别 →
文本回答 → QQ 被动回复的实群链路通过。没有因此宣称同条图片+@、图片
总结、图片 QA 或所有 MIME 类型均完成真实验收。配置名为 vision 请求
模型，不另外推断服务端实际模型身份。此次仅更新验收、计划与参考文档，
以真实测试及 `git diff --check` 验证；未重复执行此前 546 项离线测试。

## 2026-10-09 文本 assistant 实群验收

完成总结后，用户要求继续其他验收。在列明 `#问` / `@bot` 的 DeepSeek
目的地、上下文范围和次数后，用户明确允许测试所需授权。临时使用同一
DeepSeek 请求模型，开启 assistant、关闭 summary/vision，限制原测试群、
每种有输入的交互一次；未修改 `.env` 或应用代码。

人工步骤：先发送一条含负责人、截止时间和待定事项的普通测试计划，再
发送 `#问 合成任务由谁负责？`；收到回复后间隔至少 5 秒，
用 QQ 真实 @ 选中机器人并询问计划中还需确认什么。用户实际替换了示例
负责人的姓名，质量核对使用真实测试文本，不以建议示例姓名作为标准。

| 证据 | 结果 |
| --- | --- |
| 普通文本 raw 38 | normalized 1 条，未触发模型或回复 |
| `#问` raw 39 | GROUP_MESSAGE_CREATE，grounded_qa；interaction 9，outbox 3 |
| `@bot` raw 40 | GROUP_MESSAGE_CREATE 中的结构化 mention，mention_chat；interaction 10，outbox 4 |
| 外部接口 | 两次 DeepSeek HTTP 200，两次 QQ 被动回复 HTTP 200 |
| 发送记录 | 两条 outbox 均 completed、attempt_count=1、last_error=null；response_message_id 与对应同群、同触发消息、同类型 interaction 精确匹配 |
| QA 内容 | 用户核对负责人及截止时间正确；不保留实际消息内容 |
| Chat 内容 | 用户核对待确认事项正确；不保留实际回答内容 |
| 模型元数据 | provider=deepseek，返回 model=deepseek-flash，均 finish_reason=stop |
| QA usage | prompt=365，completion=17，total=382；input_chars=181，prompt_version=group-grounded-qa-v1 |
| Chat usage | prompt=449，completion=192，total=641；input_chars=314，prompt_version=group-context-chat-v2 |
| 完成时间 UTC | outbox 3：2026-10-09 01:16:13.227154；outbox 4：01:16:36.021125 |
| 客户端 | 用户明确确认“已发 并看到两条回复” |
| 数据库 | raw_events=39、messages=39、summaries=3、assistant_interactions=10、outbox=4；quick_check=ok |
| 退出 | SIGTERM 后 worker 和应用正常 shutdown；临时进程已停止 |

结论：本次 DeepSeek 文本 grounded QA 与结构化 @ chat 实群验收通过。
不将其扩展为图片、reply-only、GROUP_AT_MESSAGE_CREATE 独立事件或
Webhook 的真实验收。上下文上限：QA 120 分钟/150 条；Chat 30 分钟/
80 条及 20 条成功机器人回答。未为此次文档更新重复运行此前通过的
546 项离线测试；更新文件为本记录、实施计划与参考核对记录。

## 2026-10-09 授权后的 DeepSeek 实群总结

用户先允许发送聊天记录给模型，随后明确确认：测试群最近 120 分钟的
内部渲染聊天文本发送至 `https://api.deepseek.com`，配置请求模型
`deepseek-v4-flash`，生成摘要并被动回复测试群。不发送 Gateway raw
JSON 或 QQ 凭证到模型。此次授权不扩展为图片、自动聊天或 Webhook 验收。

临时进程覆盖 `LLM_PROVIDER=deepseek`、`SUMMARY_COMMAND_ENABLED=true`、
`SUMMARY_COMMAND_LOOKBACK_MINUTES=120`，assistant/vision 保持 false，
关闭 outbox 启动清理。启动包装只允许此前 raw 35 对应测试群，且最多
执行一次有效总结；底层仍使用原有 handler/provider/storage/sender。
未修改 `.env` 或应用代码。

2026-10-09 09:09（Asia/Shanghai）的后台验收结果：

| 证据 | 结果 |
| --- | --- |
| 接收准备 | 单实例，Gateway READY，health ready；无 pending/running outbox |
| 普通文本 raw 36 | normalized 1 条，outbox 0 条 |
| 总结命令 raw 37 | normalized 1 条，outbox 2 关联该 receipt |
| DeepSeek 接口 | `/chat/completions` HTTP 200，validated summary generated |
| summary 3 | 与入站同群，120 分钟窗口，message_count=1，input_chars=66，prompt_version=2 |
| provider 元数据 | provider=deepseek；响应 model=deepseek-flash，按接口返回原样保存，与请求配置名分别记录，不推断二者别名关系 |
| usage | prompt=551，completion=103，total=654，finish_reason=stop |
| 顺序 | generated → persisted → QQ HTTP 200 → send succeeded |
| outbox 2 | completed，attempt_count=1，last_error=null；完成于 2026-10-09 01:09:15.476864 UTC |
| 数据库 | raw_events=36、messages=36、summaries=3、outbox=2；quick_check=ok |
| 客户端显示 | 用户于 2026-10-09 明确确认“已看到总结”；与后台 HTTP/数据库证据分别记录 |

后台真实生成、入库和被动回复链路通过，且用户确认客户端已收到摘要，
本次 DeepSeek 实群文本总结闭环验收通过。成功后已
关闭临时进程，保留所有数据。只修改本验收记录和实施计划；本次运行的
真实验收结果如上，未为文档更新重复执行此前通过的 546 项离线测试。

## 2026-10-08 验收范围更新

用户明确指定本轮只测试入库和 `#ping`，不将聊天内容发送给模型。
启动时临时覆盖三个 AI 开关，不修改本地 `.env`：

```bash
SUMMARY_COMMAND_ENABLED=false GROUP_ASSISTANT_ENABLED=false VISION_ENABLED=false \
  .venv/bin/python -m app.main
```

先确认没有其他实例运行。该模式不构造 text/vision provider，不下载
图片供模型理解；普通消息、`#总结`、`#问` 和 `@bot` 均只持久化，
只有有效的 `#ping` 会通过 durable outbox 被动回复 `pong`。
`tests/test_health.py` 对这些触发方式及图片输入设置 provider/resolver
构造陷阱，并验证全部入库、仅 `#ping` 发送。
2026-10-08 运行该测试文件：`10 passed in 2.37s`。测试使用 fake QQ
sender 和临时数据库，没有连接模型或向真实群发送消息。

本轮真实进程已收到 QQ Gateway READY；只读 `/health` 检查返回
`status=ready`，database=ok，qq_adapter/outbox_worker=ready，
llm_provider/vision_provider=disabled。这证明接收服务和关闭模型的
运行配置生效。随后收到本轮两条真实消息，结果如下。

本轮人工步骤为：发送普通文本“入库验收测试”，再单独发送 `#ping`；
核对两条 raw/normalized receipt、普通消息没有 outbox、ping outbox
完成，以及 QQ 端看到 `pong`。2026-10-08 开始前数据库仍为
raw_events=32（最大 id=33）、messages=32、summaries=2、outbox=0。
本轮不执行下方历史完整流程中的模型步骤，也不将 AI 实群验收标为通过。

### 本轮真实入库与 ping 结果

2026-10-08 14:48（Asia/Shanghai）收到用户测试消息：

| 证据 | 结果 |
| --- | --- |
| 普通文本，raw_event_id=34 | 完整 payload 和入站 message ID 存在；normalized message 1 条；outbox 0 条；未触发回复 |
| `#ping`，raw_event_id=35 | 完整 payload 和入站 message ID 存在；normalized message 1 条；outbox 1 条 |
| QQ 被动回复接口 | `POST /v2/groups/{group_openid}/messages` 返回 HTTP 200；handler 记录 `ping command send succeeded` |
| 群客户端显示 | 用户于 2026-10-08 明确确认已看到机器人回复 `pong` |
| outbox_id=1 | interaction=ping，status=completed，attempt_count=1，last_error=null |
| outbox 完成时间 | 2026-10-08 06:48:40.654889 UTC |
| 数据库数量 | raw_events=34、messages=34、summaries=2、outbox=1 |
| 数据库完整性 | `PRAGMA quick_check` 返回 `ok` |
| 模型调用范围 | summary/assistant/vision 全部关闭，本轮没有新增 summary，也未创建模型客户端 |
| 临时进程退出 | SIGTERM 后 Gateway/worker 与应用正常完成 shutdown；未修改 `.env` |

结论：本轮真实普通消息入库、`#ping` 被动回复 API、durable outbox
以及用户侧收到 `pong` 的完整人工验收通过。客户端显示证据来自用户
确认，与 QQ API 和数据库证据分别记录；本轮未执行任何实群 AI 验收。

## 已验证

| 检查 | 本次结果 |
| --- | --- |
| `.venv/bin/python -m pytest -q` | 2026-10-08 出站响应修正后：`546 passed in 18.43s` |
| 不含 `.env` / 数据库 / 本地采集目录的交付副本 | 此前版本 Python 3.10.12，`521 passed in 22.30s`；确认导入的是副本代码；该结果不冒充此次新增出站校验的副本测试 |
| `git diff --check` | 通过 |
| 真实 QQ 鉴权 | 成功，凭证有效期返回 7200 秒；未输出 token |
| 真实 QQ Gateway | `/gateway/bot`、WebSocket、HELLO、IDENTIFY、READY 成功；限时诊断正常退出 |
| 真实 DeepSeek 文本请求 | 合成测试文本经 `InternalMessage → Renderer → SummaryService`，返回通过 schema 校验的摘要、决定、待办和未决问题 |
| 已配置兼容模型文本请求 | 同一合成测试文本通过上述链路和 schema 校验 |
| 实际应用启动 | 4 个 outbox worker 启动，QQ Gateway READY |
| 实际 `GET /health` | `status=ready`，database=ok，qq_adapter/outbox_worker/llm_provider=ready，vision_provider=disabled |
| 运行后数据库检查 | 四张表数量与运行前一致，`PRAGMA quick_check` 返回 `ok` |

真实模型预检没有发送群消息，也没有把合成测试消息写入生产事实表。
接口预检不能替代从群入站到群回复的完整验收。此前部署历史中的成功
`#总结` 不算本次改动的发送验证。

交付副本按 Git 跟踪文件和未忽略的新文件生成。副本在沙箱内曾停在
aiosqlite fixture 初始化，限时诊断后终止；相同副本在获准的沙箱外执行中
全部通过。远端 GitHub Actions 及 Python 3.12 矩阵尚未实际执行。

## 本次实现及回归范围

- 长总结在消息边界处分段，完整 user prompt 不超过配置预算。单条消息超限时在文本模型调用前失败。
- 合并传递全部 validated 字段；合并输入超预算时确定性去重合并。无效、截断模型输出终止生成，不以占位文本替代未总结的内容。
- 生成失败不保存摘要、不发送回复；集成测试确认 raw receipts 保留。`input_chars` 记录原始渲染长度，token usage 累计全部调用，缺失则保留 unknown。
- durable outbox 对失败、重复、冷却及未知结果不误标成功；可能已送达时不盲目重试。AI 关闭仍消费 `#ping`；过期任务不开始执行。
- worker 排空、重启恢复、Gateway/worker 健康状态及日志脱敏已有回归覆盖；raw 不自动清理。
- QQ 官方文档与固定 GitHub 参考核对见 `REFERENCE_REVIEW.md`。AppID 不作为群成员身份；reply-only 保持关闭。
- 2026-10-08 重新核对腾讯官方出站响应文档后，发送器补充 `err_code` 校验：2xx 携带业务错误不算成功，畸形错误码不被静默接受，错误码与描述均脱敏。
- 201/202 异步受理按未确认送达处理，不误标 outbox completed。新增真实 sender + MockTransport 集成回归：已生成摘要/raw 保留、outbox failed、重复处理不再生成或发送；无外部网络/模型调用。

本次新增出站校验前，针对性测试复现了错误回执被接受及错误码丢失；修正后
单元与链路测试通过，最终完整套件为上表的 546 项。最终完整测试在获准的
沙箱外执行，使用临时 SQLite 和 fake/mock 外部边界，未启动实际机器人。

## 修改文件清单

以下包含当前工作区中此前推进的未提交修改，交付时一起保留：

| 范围 | 文件 |
| --- | --- |
| 总结 | `app/services/summary.py` |
| QQ 出站响应 | `app/adapters/qq_official/message_api.py`、`app/adapters/qq_official/__init__.py`、`tests/unit/test_qq_official_message_api.py`；链路回归在下列 `test_manual_summary_command_pipeline.py` |
| runtime、配置、日志 | `app/main.py`、`app/config.py`、`app/log_redaction.py`、`app/observability.py`、`.env.example` |
| outbox 与交互 | `app/services/interaction_outbox_worker.py`、`app/storage/interaction_outbox_repository.py`、`app/storage/assistant_interaction_repository.py`、`app/services/group_assistant_handler.py`、`app/services/group_assistant_trigger.py`、`app/domain/assistant_interactions/models.py` |
| 数据库注释 | `app/storage/database.py` |
| 运行数据排除 | `.gitignore`；取消跟踪 `data/qqgroupchatbot.db-shm` 和 `data/qqgroupchatbot.db-wal`，保留本地文件，未改写 Git 历史 |
| 单元测试 | `tests/unit/test_summary_service.py`、`tests/unit/test_group_assistant_trigger.py`、`tests/unit/test_log_redaction.py` |
| 独立测试数据 | `tests/fixtures/qq_official/captured/` 中的 16 份脱敏采集快照、manifest 和来源说明；`tests/unit/test_qq_official_message_parser.py`、`tests/unit/test_message_renderer.py`、`tests/integration/test_message_repository.py`、`tests/integration/test_summary_persistence_pipeline.py`、`tests/integration/test_conversation_rendering.py` 切换到仓库 fixture |
| 集成测试 | `tests/integration/test_manual_summary_command_pipeline.py`、`tests/integration/test_adversarial_failures.py`、`tests/integration/test_outbox_state_transitions.py`、`tests/integration/test_qq_official_assistant_pipeline.py`、`tests/integration/test_qq_official_concurrency_lifecycle.py`、`tests/integration/test_qq_official_webhook_pipeline.py`、`tests/integration/test_vision_concurrency.py` |
| 生命周期与入口测试 | `tests/test_health.py`、`tests/test_qq_official_webhook_endpoint.py` |
| CI | `.github/workflows/tests.yml`（Python 3.10 / 3.12；远端 CI 尚未在本次会话执行） |
| 文档 | `README.md`、`docs/ARCHITECTURE.md`、`docs/CONCURRENCY.md`、`docs/IMPLEMENTATION_PLAN.md`、`docs/OPERATIONS.md`、`docs/REFERENCE_REVIEW.md`、`docs/ACCEPTANCE.md` |

## 人工群内验收

2026-10-08 的授权仅涵盖入库与 `#ping`，且已经通过；2026-10-09 新增
一次 DeepSeek 实群总结授权，已执行，见本文开头。下列方法不表示继续
授权其他接口或无限次重放。重新测试入库/ping 时使用三个 AI 禁用开关。

1. 在运行主机执行 `.venv/bin/python -m app.main`，确认 Gateway READY 和 `/health` 就绪。保持单实例。
2. 测试群先发一条普通文本，例如“测试项目周五发布，小李周四前整理文档，回归负责人待定”。确认 raw/normalized 入库，不回复。
3. 发送精确文本 `#ping`，确认 `pong` 和对应 outbox completed。
4. 发送精确文本 `#总结`，确认日志顺序为 generated → persisted → send succeeded，并在群中看到摘要。确认同群 `summaries` 有新的 validated 结果、outbox completed，且入站 raw 仍存在。
5. 验证 DeepSeek 闭环时，仅在该次运行设置 `LLM_PROVIDER=deepseek`，重新发送新的总结命令。不要复用过期 msg_id，不手动重放已送达任务。
6. 退出进程后确认正常关闭，下一次启动重复入站不会再次生成或发送。离线失败/重复测试由上述 pytest 覆盖。

可在本机用只读 SQLite 检查数量与状态，不需要输出消息正文或群 ID：

```sql
SELECT COUNT(*) FROM raw_events;
SELECT COUNT(*) FROM messages;
SELECT COUNT(*) FROM summaries;
SELECT status, COUNT(*) FROM interaction_outbox GROUP BY status;
```

2026-10-07 首次限时运行前基线：raw_events=32、messages=32、summaries=2、outbox=0。
当时五分钟运行期间未收到新的群入站消息，限时结束后 Gateway、worker 和
应用均正常关闭。2026-10-08 后续成功的入库与 ping 验收见本文开头。

SQLite 数据库及其 WAL/SHM 旁文件只在本地保留，不作为交付代码提交。
取消跟踪不会删除本地数据库或 raw receipt，也不会清除已有提交历史。

出站失败判定可先人工运行离线命令，不需要群消息或模型授权：

```bash
.venv/bin/python -m pytest -q tests/unit/test_qq_official_message_api.py \
  tests/integration/test_manual_summary_command_pipeline.py
```

其中模拟 `HTTP 200 + err_code=40034005` 和 `HTTP 202`，验证 raw 与摘要
保留、outbox failed、发送和生成各一次。不为此向真实平台故意发送违规
消息，也不手动重放真实已发送请求。

## 尚待真实验收

- DeepSeek `#总结` 完整实群闭环已通过（包括用户确认）；不计入下列待验收项。
- Webhook 真实平台回调：用户于 2026-10-09 明确延期（暂无公网 HTTPS）；继续使用 WebSocket，不作为本轮验收阻塞项，仍未实测。
- QQ 图片经 `gpt-5.6-luna` vision 识别并由 DeepSeek 回答的历史图片 + @ 场景已通过；持久配置仍关闭 vision，临时测试配置不等同于常驻启用。
- 文本 assistant 的真实 `@bot` / `#问` 已通过；持久配置仍为 `GROUP_ASSISTANT_ENABLED=false`，验收使用临时覆盖，不等同于常驻启用。

当前已通过 WebSocket 下的入库、ping、DeepSeek 总结、文本 QA/@ 和
历史图片 + @ 的指定实群案例；不宣称所有实现变体均已实测。Webhook
按用户决定延期；reply-only 和结构化 forward 扩展另需字段证据，不属于
当前默认启用的能力。
