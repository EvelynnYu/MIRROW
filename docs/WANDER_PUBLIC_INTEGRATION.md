# 漫想公开版接入与升级

这是本地单用户模块，不是完整应用。基础启动方式见 [接入指南](接入指南.md)；本页说明批次5的新增边界，优先于旧五层接口示例。

## 模型与宿主状态

安装 `requirements.txt`，复制 `.env.example` 并填写部署者自己的配置。宿主必须在导入漫想模块前加载环境（例如 `from dotenv import load_dotenv; load_dotenv()`）。示例模型名仅为可替换配置，不保证你的服务商支持；填实际可用的模型与完整 chat/completions URL。

v3 结构化计划/复核调用使用 `mirrow_core.llm_runtime` 的 `DEEPSEEK_FLASH_*` 配置（密钥/地址可回退主模型配置）。高层 `call_llm_func` 并不替代该结构化请求边界。自行组装 `WanderRuntimeRunner` 时，也可分别向决策 adapter 注入 `llm_caller`，接收 `FlashJsonResult`，测试无需访问网络。

通过 `mirrow_core.shared_state` 的 `set_active_session_id`、`set_latest_persona_prompt`、`set_last_private_chat_time` 注入真实主会话、人设及最后私聊时间。未知时间保持 `None`，不要用启动时间冒充用户刚回复。`manager.on_user_reply` 用于真实新回复；恢复旧时间时按接口的恢复参数调用。

## 自主时间节奏

本轮更新适用于默认 v3 运行时，不把旧引擎的随机调度方式称为模型自主计时。宿主重启并加载新代码后，新计划默认使用预计窗口；无需迁移运行数据库。

- 节点复核输出 `next_node_delay_seconds` 和 `timing_reason`，决定继续当前活动前的等待。
- 正常结算输出 `next_run_delay_seconds` 和 `timing_reason`，决定下一轮计划前的休息。它独立于 `continue_next`，后者只表示是否保留下一轮的倾向。
- 规划输出 `horizon_mode="estimate"`（默认）或 `"deadline"`，以及正整数 `horizon_min`。预计窗口到期才增加一次 `TIMING_REVIEW` 模型调用，输出是否继续、`extend_minutes`、下一节点等待及原因；不会生成虚构的活动节点。

模型输出相对时长，系统以可信时钟保存绝对到点。零或很短的等待最低为 5 秒；缺失、负数、非有限或超出时钟表示范围的等待回退 900 秒。不再把正常续想固定为 3 分钟或自然休息固定为 15/30 分钟。执行失败仍退避 15/30/60 分钟，计划技术失败仍等待 5 分钟；失败不会伪装成模型选择。

旧在途 run 缺少时间模式标记时仍按硬截止处理；明确的活动计时、真实播放完成和用户中断仍有效。`plan_horizon_min` 是初始计划值，续延后的当前窗口以 `next_wake_at` 为准。节点、结算和续延的绝对到点写入决策审计，恢复复用而不重新起算；正常尾部 wake 在终态前准备，异步分享或可选情绪收尾不能在用户回来后重新挂上唤醒。

自主时间不等于无限行动权限：模型可以选择可用活动、继续或切换，但仍受宿主真实能力、活动目标模式与数量范围约束。日志以 `model_wait` / `fallback_wait` 区分模型选择和默认休息，并显示下次计划的日期。

## 可选单节点活动

会客和逛淘宝原始集成移植自 AionsHome；公开仓库不包含其实现、服务配置或设备操作。两者目标固定为 `GoalMode.SINGLE`、数值 `1`，一次活动的多步外部工作封装在处理器内，不拆成多次拜访或多次闲逛。

```python
from wander_manager.event_types import EventType
from wander_manager.host_hooks import register_event_handler

# lounge_handler 是宿主自己的真实实现，不是本仓库附带服务。
register_event_handler(
    EventType.VISIT_LOUNGE,
    lounge_handler,
    available=lounge_service.is_ready,  # 同步、只读的可用性探测
)
```

处理器提供 `async visit_once() -> dict`。完成返回 `status="success"` 与真实 `message`；未配置/暂不可用返回 `status="unavailable"`；失败返回 `status="failed"`。可带真实 `visit_id`（会客）及 `notification_id`。后者代表已经完成的宿主交付，运行时会据此避免重复分享；绝不能提前生成一个占位 ID 来表示成功。淘宝可附 `failure_stage`、`public_error` 与已核验活动摘要。异常会被节点边界记为失败。

`register_event_handler(event_type, None)` 断开接入。`available` 缺省仅表示宿主确认已配置，不替代执行结果。有效白名单同时考虑注册状态和失败冷却；直接构造 planner 时还需把对应类型放进 `allowed_event_types`。默认高层管理器已声明这些可选类型，未注册时自动排除。

朋友圈、同伴群聊和书签也要求显式处理器注册，具体方法合同见 `node_execution_adapter.py`，不统一假定为会客/淘宝的 `visit_once` 合同。书签历史仓库可用 `configure_chronicle(repository)` 注入，历史读取者需要 `get_messages_by_date`，其他调用按所接事件提供对应方法。

朋友圈仍只有宿主接口，没有在本仓库发布服务、聊天提醒链路或专属页面。宿主适配时应让界面与模型读取同一条按 `created_at DESC, id DESC` 排序的连续时间线，默认不能只给模型“今天”的内容；日期筛选只用于明确的日期查询。提醒过（announcement）与真正读取正文/评论（read）是两种状态，只有实际内容进入模型的领域读取才能确认已读。这些状态由朋友圈宿主负责，不由漫想 hook 或 UI 通知冒充完成。

## 音乐与其他服务

`configure_music(context=..., record=..., playback=...)` 分别注入已有音乐经历读取、幂等经历写入和真实播放回调。`playback(payload)` 可同步或异步，仅明确 `True` 代表播放已确认；设备失败不会换设备补播。写入回调接收 `payload, source_id=...`，由宿主持久化去重，不直接覆盖自我书。

高层管理器的音乐事件还需要 `music_mcp_client.is_running()` 和既有曲目服务；只注册播放回调不会凭空提供选歌/歌词。也可自行组装 runner 并注入音乐处理器。私有情绪服务不随包提供；需要时显式注入 `runner.affect_service`，否则跳过外部情绪写入。

## 分享回执：升级必改

`on_push_to_user(payload)` 在真实落库并完成宿主规定的交付后必须 `return True`。返回 `None`、`False`、异常或未确认结果不会记为 `sent`。不要沿用旧示例里没有返回值的回调。

以 payload 中稳定的 `event_id`（活动身份，配合 `session_id`）实现宿主消息幂等；数据库内部另有 delivery 身份，不要假定回调载荷包含它。`ConfirmedDeliveryGate` 仅是本进程辅助，不替代数据库唯一约束。勿扰写 `suppressed`，不是发送成功，也不排队补发。正文、工具卡和 UI 通知的持久化由宿主负责。

## 小红书与配置脱敏

公开 `xiaohongshu_adb.py`、读取适配、公共页面来源与设备租约；只读动作仍会在已登录 App 内导航。部署者自行安装 ADB、连接自己的设备并设置 `MIRROW_XHS_ADB_SERIAL`；不附设备序列号、账号 Cookie、截图或抓取结果。公共页面入口可用 `MIRROW_XHS_PUBLIC_FEED_URL` 覆盖。

普通聊天能力授权、前端附件呈现、设备自动化服务不包含在本次发布中。不要把漫想读取组件的存在解释为普通聊天已自动接线。

## 旧标识与运行数据

群组活动使用通用 `HOST_GROUP_ACTIVITY` / `host_group_activity`，仅由宿主显式注册；公开版不包含参与者、房间、传输或续话实现。该活动已改为宿主接口：处理器必须返回 `host_activity_completed=True` 才会被记为完成。旧运行库中的未知活动值不会被静默解释；升级前备份运行数据，并由宿主通过 `MIRROW_LEGACY_HOST_GROUP_EVENT_TYPE` 显式映射后再迁移或重新创建在途计划。

许愿板的新用户角色键为 `user`。若已有数据库的角色约束仍使用旧键，先备份数据库，在本地设置 `MIRROW_LEGACY_USER_ROLE` 为该旧键后显式调用 `WishStore.migrate()`；未提供映射时服务保持只读并报出迁移要求，不会猜测或改写其他角色。

`events/`、`data/` 是运行时本地生成目录，不随源码发布数据库、愿望内容、日志或大脑架构缓存。仓库不包含私人部署的数据迁移。

## 验证范围

在仓库根目录运行 `python -B -m pytest -q`。单元/集成测试使用模拟模型与临时数据库；未覆盖真实第三方账号、设备及公网服务端到端流程。标注跳过的测试依赖未公开的主聊天授权、设备控制或私有服务，不用假实现替代通过。
