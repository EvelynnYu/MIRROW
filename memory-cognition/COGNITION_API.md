# 认知书模块接口

这一包公开三域认知书的完整写入链路，而不只是 Markdown 文件：按日取证、世界事实核验、自我认识修订、他者认识分析、版本冲突保护、维护记录、人工编辑与只读注入投影。宿主只需接入自己的权威对话、模型、数据根和触发时机。代码默认面向本地单用户固定主会话；HTTP 路由必须由宿主加鉴权，不能直接暴露在公网。

## 最小程序接线

```python
from pathlib import Path
from cognition import books
from cognition.daily import run_daily

books.ROOT = Path("/private/app-data")  # 不要指向公开仓库

# rows 来自同一固定会话、同一活跃日的权威消息库。
# llm 是 async def llm(prompt: str) -> str，返回模型生成的 JSON 文本。
result = await run_daily(rows, llm, source_date="2026-09-24", session_id="private-session")
```

`rows` 至少包含稳定的 `message_id`（或 `id`）、`role`、`content`、`timestamp`、`session_id`。普通私聊角色是 `user`／`assistant`；主动消息可带 `is_wander`／`is_sentinel`／`is_reminder` 和相应 `event_type`。已结算的漫想活动用 `role="system", event_type="wander_activity"`，并携带结算文字 `content`、可选 `tool_calls`；它只进入 self 路，不进入世界／他者事实路，也不被认作人类伙伴自述。未结算的漫想、工具原始载荷、群聊和访客材料不应伪装成这类消息。

日结串行执行 world → self → other。失败路保留已完成路的结果和维护记录，再次运行仍按来源摘要复用同一运行 ID；`run_daily` 会抛出 `BookError`，不要将部分成功显示成全部完成。运行状态与候选分别由 `cognition.maintenance.status()`、`record_report()` 查询。

可选 `memory_lookup(evidence)` 是异步只读回调，返回带 `id`、`text`、`date`、`source` 的历史摘要列表；不接时为空，不依赖 OB_Rev。可选 `other_evidence` 是宿主已核实归属的其他场景材料，供他者路使用；不能把任意 HTTP 文本当成权威证据。旧主会话历史原文搜索若需要跨日检索，可由宿主在私有数据根提供 `events/event_chronicle.db`，否则 `other_history` 返回空结果。

## 可移植 HTTP 路由

```python
from fastapi import FastAPI, Header, HTTPException
from cognition.public_router import create_router

def authorize(x_local_token: str = Header("")):
    if not valid_private_token(x_local_token):
        raise HTTPException(401, "unauthorized")

async def load_day(source_date: str, session_id: str) -> list[dict]:
    return await my_canonical_store.get_messages_by_day(source_date, session_id)

app = FastAPI()
app.include_router(create_router(
    authorize=authorize,
    load_day=load_day,
    llm=my_async_model,
    session_id=lambda: my_fixed_private_session_id,
    # memory_lookup=my_async_memory_lookup,
    # load_other_evidence=my_async_other_evidence_loader,
))
```

可移植路由只接受日期来启动日结；原始消息由宿主内部读取，不通过 HTTP 上传。挂载前必须先把 `books.ROOT` 配到私有数据目录；仍指向源码目录时路由会拒绝创建。四个必需回调缺一不可，并且所有端点都使用宿主鉴权依赖。

| 用途 | 路径／方法 |
| --- | --- |
| 三域认知书读取、手动写入与删除 | `GET /api/cognition/books/{domain}`；`PUT/DELETE /api/cognition/books/{domain}/{name}` |
| 标记已读、恢复可恢复删除 | `POST /api/cognition/books/{domain}/{name}/read`；`POST /api/cognition/deleted/{rid}/restore` |
| 主体与人物概览 | `GET /api/cognition/identity`；`GET /api/cognition/people/{subject}` |
| 他者实体列表、登记与改名 | `GET /api/cognition/other`；`POST /api/cognition/other/entities`；`PUT /api/cognition/other/entities/{identifier}/names` |
| 日结执行与维护状态 | `POST /api/cognition/maintenance/run`；`GET /api/cognition/maintenance` |
| 候选预览与人工决定 | `POST /api/cognition/maintenance/{rid}/preview`；`POST /api/cognition/maintenance/{rid}/decide` |

手动更新要带当前词条 `revision`；冲突会拒绝覆盖。待审候选必须先 preview，再携带返回的签名与版本决定 apply/reject。`cognition.router`／`maintenance_router` 中 MIRROW 专属的上下文预览、旧世界书建议、重试和旧画像预览路由没有挂进这个可移植接口；迁移这些产品功能时应各自接自己的宿主适配器，不应让它们悄悄引用 MIRROW 私有服务。

只读注入可直接调用 `books.core_context()`、`books.person_projection(subject)`、`other_book.context(...)`。`self_book`／`world_book` 的检索类也可复用，但初始化时应显式把 `entries_dir` 指向 `books.folder(domain)`；这些旧检索类默认路径仍是源码目录，不会自动跟随 `books.ROOT`，写入后也要重新加载其缓存。认知书文件、实体登记、阅读状态、维护账本与删除备份都写入 `books.ROOT`，请只放在私有数据目录并纳入自己的备份策略。
