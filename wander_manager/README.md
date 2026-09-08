# Wander Manager - 思维漫想管理器

## 当前入口：v3 持久化运行时

新部署请先阅读 [公开版接入与升级说明](../docs/WANDER_PUBLIC_INTEGRATION.md)。当前运行路径由 `runtime_runner` 编排 `run → activity → node`，SQLite 保存计划、复核、结算和分享回执；下文五层结构是保留的旧接口说明，不是当前默认的随机事件调度路径。

会客、逛淘宝均为一次活动一个节点（`single / 1`），仅保留接入接口，原始集成移植自 AionsHome。小红书提供只读代码，设备与模型需部署者配置。实际事件类型与目标范围以 `event_catalog.py` 为准，不以本文旧列表的数量为准。

AI 伴侣的主动行为调度系统：在用户长时间无回复时自动进入"胡思乱想"模式，产生各种行为事件，经过打扰判断后可能主动推送消息给用户。

## 架构概览

```
┌─────────────────────────────────────────────────────────┐
│                    模式开关层 (ModeSwitch)                │
│     管理漫想模式开启/关闭状态，检测用户回复时间阈值        │
└─────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────┐
│                    漫想创建层 (WanderCreator)            │
│     按概率随机创建行为事件（关键词扩写/记忆抓取/看新闻等） │
└─────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────┐
│                    漫想日志层 (WanderLog)                │
│     记录每次事件类型、时间、过程，支持持久化存储          │
└─────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────┐
│                    主动打扰判断层 (DisturbJudgment)       │
│     LLM评分 + 公式计算 + 推送决策                        │
└─────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────┐
│                    消息生成器 (MessageGenerator)          │
│     生成主动消息内容                                      │
└─────────────────────────────────────────────────────────┘
```

## 核心功能

### 1. 模式开关层
- 检测用户空闲时间
- 三种状态：OFFLINE（离线）、IDLE（发呆）、BRAINSTORMING（头脑风暴）
- 用户回复后自动关闭漫想模式

### 2. 漫想创建层
支持的事件类型（7种）：
- `KEYWORD_EXPANSION` - 关键词生成+主题扩写
- `MEMORY_FETCH` - 记忆抓取（需注入 get_memories_func）
- `USER_TRACKING` - 用户追踪（截屏/摄像头分析，需注入 vision_llm_func）
- `SLEEP` - 休眠
- `BROWSE_NEWS` - 看新闻（需注入 web_search_func）
- `SELF_REFLECTION` - 自省（大脑架构+愿望清单）
- `BROWSE_BOOKMARKS` - 看收藏夹（需宿主提供书签数据源）

动态概率调整：某类事件长时间未触发时概率逐步提升，触发后重置。

### 3. 主动打扰判断层
- LLM 评分：重要度、分享欲、情绪强度（high/medium/low）
- 总分超过阈值则推送

### 4. 用户追踪
- 截取屏幕（mss）+ 按需摄像头（cv2），交由注入的视觉 LLM 分析
- 识别用户活动类型：工作/游戏/浏览/看视频/聊天/空闲
- 与用户声明状态做一致性判断，生成个性化关心消息

### 5. 配置与持久化
- 所有阈值可配置，支持运行时修改
- 漫想事件持久化到 SQLite（`MIRROW_WANDER_DB_PATH` 可配，默认 `data/wander_log.db`）

## 快速开始

```python
from wander_manager import init_wander_manager, WanderConfig

config = WanderConfig(
    idle_threshold=30 * 60,  # 30分钟
    event_interval=15 * 60,  # 15分钟
    push_threshold=0.5
)

manager = init_wander_manager(
    config=config,
    call_llm_func=your_llm_function,       # async (messages) -> {"content": str}
    on_push_to_user=lambda msg: print(f"推送: {msg}"),
    persistence_enabled=True
)

await manager.start()

# 用户回复时调用
manager.on_user_reply()

await manager.stop()
```

### 用户追踪服务（视觉 LLM 注入）

```python
from wander_manager.user_tracking_service import init_user_tracking_service

# vision_llm_func 签名: async (messages, model) -> {"content": str}
# 未注入时视觉分析路径优雅跳过（返回不确定结果），不崩溃
tracking_service = init_user_tracking_service(vision_llm_func=your_vision_llm)
```

### 大脑架构文档（自省事件的源文档）

自省事件会读取"大脑架构文档"作为自我认知素材。通过环境变量
`MIRROW_BRAIN_DOC_PATH` 指定你自己的架构描述 markdown 文件；
未配置时使用内置中性模板（描述漫想/感知/工具调用三模块）。

## 配置项说明

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `idle_threshold` | 600 (10分钟) | 用户空闲阈值（秒） |
| `event_interval` | 300 (5分钟) | 事件创建间隔（秒） |
| `push_threshold` | 0.6 | 推送阈值（0-1） |
| `score_weight_high` | 0.3 | high级别权重 |
| `score_weight_medium` | 0.2 | medium级别权重 |
| `score_weight_low` | 0.1 | low级别权重 |
| `log_retention_hours` | 24 | 日志保留时间（小时） |
| `log_persistence_enabled` | True | 是否启用日志持久化 |
| `save_screenshots` | False | 是否保存追踪截图 |
| `tracking_confidence_threshold` | 0.3 | 追踪置信度阈值 |

## 环境变量

| 变量 | 说明 |
|------|------|
| `MIRROW_WANDER_DB_PATH` | 漫想日志 SQLite 路径（默认 `data/wander_log.db`） |
| `MIRROW_BRAIN_DOC_PATH` | 大脑架构描述文档路径（默认使用内置中性模板） |
| `PERSONA_USER_NAME` / `PERSONA_AI_NAME` | 用户/AI 称呼（见 `mirrow_core/persona.py`） |

## 可选集成

以下模块通过运行时 import 探测，不存在时自动降级（不崩溃）：
情绪引擎（mood_engine）、记账本（ledger_manager）、健康追踪（health_tracker）、
对话档案（event_chronicle，收藏夹事件依赖）。

## 依赖

- Python 3.10+
- `mss` - 屏幕截图（用户追踪功能，可选）
- `opencv-python` - 摄像头（可选）
- `httpx` - HTTP客户端
- `Pillow` - 图像处理
- `rapidfuzz` - 模糊匹配（许愿去重，可选）

## 许可证

MIT License
