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

先保证“一条消息都不丢”。

实现：

```text
raw_events
```

字段至少：

```text
id
event_type
platform_message_id nullable
received_at
dedup_key
raw_json
parse_status
parse_error nullable
```

流程：

```text
WS event
  ↓
dedup
  ↓
raw DB save
  ↓
后续解析
```

## 测试

- 同一 fixture 输入两次；
- raw event 只保存一次；
- parser 尚未实现也不影响保存。

---

# Phase 4 — Normalizer + 基础 Parser

先支持：

- text
- at
- image
- file
- face/mface
- json
- unknown

不要做 forward。

## 关键验收

消息：

```text
hello @B [image]
```

内部 nodes 顺序必须保持：

```text
text
at
image
```

不能只提取成一个纯文本字符串。

---

# Phase 5 — Reply

实现 reply segment。

第一步只保存引用 ID。

第二步再实现可配置的 `get_msg` resolution。

如果 get_msg 失败：

- 当前消息仍然成功；
- reply 标记 unavailable/error。

测试“当前作者”和“被引用消息作者”不会混淆。

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
