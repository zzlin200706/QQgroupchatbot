# qqgroupchatbot 开发文档包

这是给 Codex 使用的项目指导文件。

推荐把这些文件放到仓库：

```text
qqgroupchatbot/
├── AGENTS.md
└── docs/
    ├── ARCHITECTURE.md
    ├── MESSAGE_MODEL.md
    └── IMPLEMENTATION_PLAN.md
```

使用顺序：

1. 首先把全部文件复制到仓库；
2. 在 Codex 中让它阅读 `AGENTS.md`；
3. 然后从 `Phase 0` / `Phase 1` 开始逐阶段实现；
4. 不要让 Codex 一次性完成整个项目；
5. 第一目标是完整、可靠地保存 QQ 群消息及其来源结构；
6. AI 总结在 `AI_READY_GATE` 之后再做。

当前推荐开发路线：

```text
独立 QQ 小号
→ NapCatQQ
→ OneBot 11 WebSocket
→ Python Adapter
→ Event Normalizer
→ Message Parser
→ Internal Message Model
→ SQLite
→ FastAPI Query
→ AI（后续）
```

注意：NapCat 是基于 NTQQ 的协议端实现，不是 QQ 开放平台官方 Bot API。
建议使用独立 QQ 小号测试，不使用主 QQ 作为机器人账号。
