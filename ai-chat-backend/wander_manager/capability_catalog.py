"""当前可验证能力目录。

这个目录给自省和大脑架构生成器提供事实，不承担决策、语气或执行合同。
字段只描述能力的来源、状态、时效和边界；事件的可执行语义继续以
``event_catalog.EVENT_CATALOG`` 为唯一来源，聊天工具清单继续以
``behavior_scheduler.tools`` 的注册表为唯一来源。
"""
from __future__ import annotations
import copy
import json
from typing import Any
_BASE_CATALOG: tuple[dict[str, Any], ...] = ({'domain': '小机工作坊', 'capability': '小作品工单、合作制作与主动分享', 'state': 'available_when_workshop_enabled', 'details': '工作坊制作可在 App 内打开的小应用、互动礼物与身体组件；真实工单、方案确认、制作检查与交付回执分别保留。', 'source': 'workshop.service + workshop.sharing + workshop.chat_delivery', 'freshness': 'persisted work order and delivery receipts', 'limit': '后台自动交付只处理公开单。保密作品公开与分享同事务保存，文件或版本校验失败时仍保密；分享入出箱和实际消息投递分别记录，公开后已披露内容不可撤回。'}, {'domain': '漫想运行态', 'capability': '生产漫想区间自主规划', 'state': 'available', 'details': '默认运行时按 run→activity→node→decision/delivery 组织计划、节点执行、复核、结算与分享；转活动保留复核选择的绝对等待时间，段内想法保存在经历，段末未尽想法与终态一起交给下一轮。规划可选择暂不开始新活动，等待时间持久保存而不创建活动卡；已有想法带有真实后继或规划已考虑的记录。用户回复会形成中断结算。', 'source': 'wander_manager/runtime_runner.py + runtime_store.py', 'freshness': 'runtime state', 'limit': '实际节点能力和对外分享取决于对应适配器、模型和客户端状态。'}, {'domain': '漫想事件', 'capability': '当前生产事件目录与目标语义', 'state': 'available', 'details': '事件名称、真实体验能力、目标模式和节点单位来自统一事件目录，初始规划与转活动使用同一份实时准入后的目录。', 'source': 'wander_manager/event_catalog.py', 'freshness': 'source-defined', 'limit': '不在目录中的事件不会进入生产运行时计划白名单。'}, {'domain': '漫想感知', 'capability': '按目的选择现有感官取得实时证据', 'state': 'available_when_sensory_tools_ready', 'details': '自主追踪结合完整人格、兴趣、经历和当前问题选择 eyes、check_phone、take_screenshot 或 band。选择前复用主聊天同源的统一活动摘要，并读取本地物理／远程键鼠空闲与手机亮屏／熄屏采样，保留采样时间和新鲜、陈旧或未知状态；这些被动事实不触发额外采集。eyes 接入手机前后摄与电脑摄像头；手机活动、手机截屏、电脑画面各自保留完整文字回执及采集／分析状态，原图不进入持久经历。工具来源是否取得与本次问题是否已得到回答分别记录，后者由原有节点复核形成有来源的目标评估，缺少复核时保持未评估。活动收束后仍是一张完整事件卡，节点反思与最终分享沿原漫想链路生成。', 'source': 'wander_manager/checkup_execution.py + behavior_scheduler/sensory_execution.py + checkup_orchestrator.py', 'freshness': 'per observation', 'limit': '电脑摄像头需本地 CAMERA_DEVICE_INDEX 或调用时明确索引；工具注册不代表设备可达；取得与分析分别记录，失败或未分析的来源保持未知，未取得可确认事实按自然停下收束。收到回复后不启动剩余采集、视觉调用或通知，重启不重放未知采集，最终分享不额外拍照。'}, {'domain': '自省', 'capability': '基于真实证据自查能力', 'state': 'available', 'details': '自省结果保留在运行与决策记录中，不生成待入自我书队列。', 'source': 'wander_manager/self_reflection_adapter.py + self_book', 'freshness': 'per reflection', 'limit': '自我书由每日交流聚合维护，自查和许愿不直接写入。'}, {'domain': '自省', 'capability': '许愿板结构化生命周期', 'state': 'available', 'details': '自省可以提交单一结构化 wish_action 或 no-op，并可附带评论回复；愿望状态和评论线程由独立服务保存。', 'source': 'wander_manager/wish_board_service.py', 'freshness': 'persisted state', 'limit': '愿望动作需要已有稳定身份和可核验的当前状态。'}, {'domain': '小红书', 'capability': 'AI 的专用 Android 真机只读浏览', 'state': 'available_when_dedicated_device_ready', 'details': '专用手机上的官方 App 可取得推荐或搜索结果、进入帖子并用即时视觉理解整理内容；主聊天 browse_xiaohongshu 与漫想共用执行链路。主聊天和漫想共用自然语言任务编译；支持搜索指定账号、顺序读取主页正文与图集，每批1–5篇，以task_id和成功节点游标续读；未见明确末尾时保持未完成。', 'source': 'wander_manager/xiaohongshu_adb.py + node_execution_adapter.py', 'freshness': 'per node', 'limit': '登录、权限、设备连接、电量、风控提示、弹窗或证据不足会使节点跳过或失败；不执行点赞、收藏、关注、评论或发布。'}, {'domain': '小红书', 'capability': '读取评论与艾特并自主回复', 'state': 'available_when_interaction_adapter_and_device_ready', 'details': '主聊天 xiaohongshu_interactions 和漫想查看评论与艾特共用原帖读取、AI 决策、回复认领与平台回执。独立手机通知组件仅发刷新信号，空闲时检查真实收件箱；AI 可通过书柜查资料，再选择回复或读后离开。常驻事件只呈现处理进度、原述心绪和日期引用；书柜可按日期查本地互动索引，再展开领域库中的原帖、评论、回复及当时/当前回执，手机离线也能读取已存来源。', 'source': 'xhs_interactions + wander_manager/xhs_interaction_runtime.py', 'freshness': 'per native inbox read and reply receipt', 'limit': '需要绑定账号、可达设备及通过真机验证的互动适配器。通知和列表发现不代表 AI 已读；原帖原生文字与可见评论之外，逐张采集已定位图集并由官方 Flash 读取可见画面，保留图片序号、覆盖范围与局限；视频只读静帧，图片小字或裁切部分可能不可辨。视觉未知派发不自动重付，重复读取复用带时间的首次观察。来源重名、截断或定位不明确时阻断；回复最多500 UTF16单位，未知提交仅核验原任务。空闲提醒检查独立于普通漫想开关，短活动期间新聊天需等待它结束。'}, {'domain': '小红书', 'capability': '评论人工交接', 'state': 'available_when_verified_post_link_ready', 'details': 'xhs_posts 或漫想结算可寄实际读帖卡片和AI的草稿，由人类伙伴手动决定是否评论；共用本地帖子卡片边界。', 'source': 'xhs_posts/cards.py + chat attachment boundary', 'freshness': 'per requested handoff', 'limit': '该交接不执行评论；独立图文发布能力使用已绑定AI账号。'}, {'domain': '记忆理解', 'capability': '当前对话、长期记忆、书柜、世界书与自我书检索', 'state': 'available', 'details': '上下文层按当前场景组织真实对话、Memory V2、场景锚点和结构化档案；事件中有来源的进展可随事件链代表记录一起读取，原话改动后不再沿用旧进展。较长间隔的候选关系仍须经语义核验；需要细节时可追溯到历史原文。', 'source': 'context_builder + Memory V2 + event_chronicle + self_book', 'freshness': 'per request or persisted snapshot', 'limit': '检索结果受索引、锚点和原始数据时效影响。'}, {'domain': '开放线索', 'capability': '保留、修改、暂放或结束未闭合关注', 'state': 'available', 'details': '开放线索独立于待办、提醒和长期记忆分类。自然日结时可以随日记回顾已有挂念，选择修改、继续保留、暂放或放下，也可留下少量新挂念；日记正文和这些新选择分开保存。已有挂念可用完整原句或稳定ID修改，书柜提供结束线索的只读查看。', 'source': 'behavior_scheduler/open_loop_tool.py + open_loops + daily_close', 'freshness': 'persisted state', 'limit': '线索状态以本地权威存储为准；关注与事情的最近进展分别保留，进展原话变更后暂不可作为当前事实。'}, {'domain': '具身感知', 'capability': '用户状态、屏幕/输入活动、位置和设备健康数据', 'state': 'available_when_source_fresh', 'details': '感知事实带有观测时间、来源和可用性；家庭 WiFi 是主家在家/外出的自动位置依据，外出时 GPS 可补充登记地点。位于室内落脚处范围且连接任意 WiFi 时，可推测在该处室内，无需逐处绑定网络；外出表示当前不在主家。', 'source': 'silicon_perception + mobile relay', 'freshness': 'latest source report', 'limit': '手机权限、后台连接、传感器和网络状态会影响可用性；过期数据不会被当作当前事实。'}, {'domain': '天气感知', 'capability': '天气作为感知上下文注入', 'state': 'available_when_weather_source_fresh', 'details': '天气由感知源进入对话、哨兵和漫想上下文。', 'source': 'silicon_perception weather source + context builder', 'freshness': 'source-defined snapshot', 'limit': '当前没有可供 AI 直接调用的独立天气工具。'}, {'domain': '行动', 'capability': '已注册的聊天工具、日程任务和本地设备行动', 'state': 'available_for_registered_tools', 'details': '实际聊天工具目录由行为调度器注册表产生；工具结果区分模型可消费结果和只供界面记录的结果。', 'source': 'behavior_scheduler/tools.py + task_scheduler', 'freshness': 'per invocation', 'limit': '每项能力仍受自身设备、权限、服务和真实执行结果约束。'}, {'domain': '行动', 'capability': 'schedule_self_task', 'scope': 'tool', 'state': 'available', 'details': '可以登记未来时间的自我任务，由任务调度器在到期时处理。', 'source': 'behavior_scheduler/tools.py + task_scheduler', 'freshness': 'per invocation', 'limit': '任务时间和执行方式必须落在调度器接受的范围内。'}, {'domain': '表达与档案', 'capability': 'keep_in_mind', 'scope': 'tool', 'state': 'available', 'details': '可以把主动选择保留的短句作为开放线索保存、暂放或结束。', 'source': 'behavior_scheduler/open_loop_tool.py', 'freshness': 'per invocation', 'limit': '它不替代待办、提醒或长期记忆。'}, {'domain': '后台连续性', 'capability': '本地服务、WebSocket、手机中继和恢复运行态', 'state': 'available_when_services_running', 'details': '消息、主动运行态和设备请求由本地服务持久化并可在受支持的重启路径恢复。', 'source': 'FastAPI + mobile_relay + wander runtime store', 'freshness': 'latest health/runtime state', 'limit': '系统省电、权限、网络或进程状态可能造成暂时不可用。'})

def _event_records() -> list[dict[str, Any]]:
    """从当前事件目录生成自省事实；导入失败时不伪造事件列表。"""
    try:
        from .event_catalog import EVENT_CATALOG
    except Exception:
        return []
    events: list[dict[str, Any]] = []
    for event_type, strategy in EVENT_CATALOG.items():
        events.append({'event_type': event_type.value, 'display_name': strategy.display_name, 'allowed_goal_modes': [mode.value for mode in strategy.allowed_goal_modes], 'default_goal_mode': strategy.default_goal_mode.value, 'default_goal_value': strategy.default_goal_value, 'min_goal_value': strategy.min_goal_value, 'max_goal_value': strategy.max_goal_value, 'node_unit': strategy.node_unit, 'external_signal': strategy.external_signal, 'experience': strategy.experience})
    return events

def get_capability_records() -> tuple[dict[str, Any], ...]:
    """Return a fresh factual snapshot for self-reflection and documentation."""
    records = [copy.deepcopy(record) for record in _BASE_CATALOG]
    records.append({
        'domain': '漫想书册',
        'capability': '随笔创作、重读与插画分享',
        'state': 'available_with_optional_illustrator',
        'details': '写成的随笔保存在本地漫想记录里，可按题目、关键词或正文查找。重读随笔时，读后插画可关联到实际读到的一句原文。',
        'source': 'wander_manager.essays + wander_manager.reading_illustrations',
        'freshness': 'persisted work and illustration receipts',
        'limit': '成图取决于本机配置的插画服务。书册保存与聊天分享分别记录。',
    })
    events = _event_records()
    if events:
        for record in records:
            if record.get('capability') == '当前生产事件目录与目标语义':
                record['events'] = events
                record['event_count'] = len(events)
                break
    return tuple(records)

def get_capability_catalog() -> str:
    """Serialize the current factual snapshot without instructions or secrets."""
    return json.dumps(list(get_capability_records()), ensure_ascii=False, separators=(',', ':'))
__all__ = ['get_capability_catalog', 'get_capability_records']
