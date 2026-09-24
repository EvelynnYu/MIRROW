# 宿主接线边界

这份公开包包含 Memory V2 与认知书模块，不包含整个聊天宿主。认知书的可移植接口见 [COGNITION_API.md](COGNITION_API.md)；下面列出运行真实资料时由宿主负责的接口，合成测试无需这些接口。

1. **原始对话权威**：`memory_v2.conversation_source.ConversationSource` 从宿主的 `conversation_messages` SQLite 表读取稳定消息 ID、时间、角色、来源类型与正文。公开包不提供对话库，也不从摘要反向制造原话。
2. **模型调用**：编码、日结、认知维护接收宿主提供的模型回调。公开包不携带模型密钥或默认云端连接；`memory_v2.experiment` 中保留的旧调用适配器只是 MIRROW 宿主接口示例，独立使用时应替换。
3. **认知书存储与日结**：`cognition.books` 默认在源码根下的四个 `entries/` 目录保存 Markdown 词条。生产集成应将 `books.ROOT` 指向私有数据根，给 `cognition.daily.run_daily` 提供有稳定 ID 的当天消息和异步模型回调。公开 HTTP 适配器 `cognition.public_router.create_router` 还要求宿主注入鉴权、按日读取与固定主会话 ID；任何原始消息和真实词条都不属于公开源码。
4. **主动深搜**：`behavior_scheduler.bookshelf_tool.BookshelfTool` 的 `source=memory` 分区调用同一 V2 索引，并可接受显式的 `stage_queries`；其日记、日历、收藏等其他分区仍是 MIRROW 宿主适配器，不是这份包的独立 API。
5. **旧系统兼容**：公开认知日结默认不读取 OB_Rev；需要历史摘要时显式注入 `memory_lookup`。原 MIRROW 专属的 `maintenance_router.py`、`maintenance.run_for_date`、`other_legacy.py` 和部分重试／预览入口仍保留为宿主适配参考，不由可移植路由挂载。它们不能被当作已经独立运行或旧系统全面退役的证据。

权威顺序是「原始消息 → 有来源的事件／线程 → 可修订的认知」。记忆检索可以成为认知维护的参考材料，但不能仅凭召回摘要改写人物立场；认知词条也不能覆盖或伪装原始消息。
