# MIRROW 开源模块

MIRROW 是一个单用户、本地部署的 AI 伴侣系统。本仓库开源其中可独立复用的核心模块,按批次逐步发布。

**这里没有完整的产品**——你拿到的是三块经过实战验证的"器官",以及把它们接进你自己系统的说明书。所有模块通过回调注入解耦:不依赖特定 LLM 厂商、不依赖特定存储、缺失的依赖一律优雅降级。

## 已发布模块

| 模块 | 一句话 | 亮点 |
|------|--------|------|
| [silicon_perception/](silicon_perception/) | 硅基感知——AI 的"感官系统" | 9 数据源 60s 监控循环、行为基线(6 层数据架构+7 维度基线)、三级触发器体系、数据时效模型(陈旧数据不伪装成正常) |
| [wander_manager/](wander_manager/) | 漫想模式——AI 的"潜意识" | 五层主动推送引擎 + **v3 持久化运行时**(run→activity→node 可追溯身份链,重启可恢复) + **大脑架构生成器** + 10 事件目录 + 愿望板 |
| [behavior_scheduler/](behavior_scheduler/) | 工具调用调度器——AI 的"行动力" | 三通道工具检测(Pre-tool 关键词 → 主模型 TOOL_CALL → 轻模型兜底提取)、防幻觉机制、防重复推送 |
| [mirrow_core/](mirrow_core/) | 共享基础件 | 截断参数配置中心、时间工具、全局状态注册、称呼配置层 |
| [context_builder/](context_builder/) | 上下文组装框架——AI 的"长期记忆"方法论 | Recipe 声明式分层语言(14 场景)、Builder 组装管道、注意力五层模型 |
| [memory_provider.py](memory_provider.py) | 记忆检索抽象协议 | MemoryProvider 标准接口(已附 SQLite 参考实现),不注入=优雅降级 |
| [wander_frontend/](wander_frontend/) | 漫想配套前端 | 独立 React 日志面板与许愿板，附本地 API 示例，不包含完整聊天应用 |

## 本批更新（批次5：漫想运行时与独立部署边界）

- **批次5补充：漫想配套前端** — 开放活动/节点日志、执行与分享状态、许愿板状态和评论线程；含独立样式、构建配置、本地 API 适配与浏览器测试。启动方法见 [前端接入说明](wander_frontend/README.md)。
- **补丁：许愿板信息一致性** — 自省保留完整愿望/评论快照，不再从 4000 字符处截断；许愿板显示称呼读取宿主配置。朋友圈领域实现仍不在公开范围，仅补充宿主读取/提醒/已读的接口语义。

- 同步区间计划、节点复核、中断恢复、自省结算与分享回执；计划和复核共用可执行事件白名单。
- 分享回调必须在真实持久化/交付完成后返回 `True`；未确认不记为已发送，勿扰会留下 `suppressed` 状态。
- 会客、逛淘宝保留 `single / 1` 活动接口，不发布实现；原始集成移植自 AionsHome。
- 小红书开放只读组件与设备租约代码，设备序列号、登录态、截图、数据库和私人配置不随代码发布。
- 新增宿主历史、音乐与可选事件接入边界。升级接线及标识兼容说明见 [漫想公开版接入](docs/WANDER_PUBLIC_INTEGRATION.md)。

## 上批新增（批次4：漫想 v3 重构）

- **漫想 v3 持久化运行时** — `runtime_runner` 消费状态机动作,计划/节点/结算全部落 SQLite,重启可恢复;`plan_decision_adapter` 的 `allowed_event_types` 是宿主控制"哪些事件能跑"的开关
- **大脑架构生成器** — `wander_manager/brain_architecture.py` 从当前权威事实源(架构基线/事件目录/能力目录/工具注册表)动态生成 AI 的第一人称自述,不再依赖过时手写 prompt。接线:
  ```python
  from wander_manager.brain_architecture import generate_brain_architecture
  # flash_llm_func 接收 OpenAI 风格 messages 列表
  await generate_brain_architecture(flash_llm_func)  # 可传 force=True 强制刷新
  ```
  缓存写 `data/brain_architecture.md`(已 gitignore)。事实源: `docs/ARCHITECTURE_BASELINE.md` + `event_catalog.py`(10 事件) + `capability_catalog.py`。
- **小红书只读浏览(Android 真机)** — `xiaohongshu_adb.py` 白名单 ADB 只读,不点赞/收藏/评论/发布;需宿主接入 USB 真机并配置 `MIRROW_XHS_ADB_SERIAL`,否则该事件自动降级
- **愿望板** — `wish_board_service.py` 结构化 wish 生命周期 + 评论线程,SQLite 事务保证崩溃重试不重复计数

> 事件目录不等于已配置能力。可选事件需要注册真实处理器，并由 `allowed_event_types` 启用；音乐另需宿主播放服务。会客、购物域实现未开源，缺失接入不会产生成功结果。

## 待发布

- **完整聊天前端 / 群聊 / 语音 / 记忆库(OB_Rev)** — 视社区反馈决定。漫想日志和许愿板前端已单独开放。

## 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env   # 填入你的 LLM API key
```

三个模块都是"库",不自带入口进程——你需要一个宿主(FastAPI 应用或任意 asyncio 程序)在启动时注入回调。完整接线示例见 **[docs/接入指南.md](docs/接入指南.md)**。

最小心智模型:

```
你的宿主进程
  ├─ init_sentinel(on_push=..., call_llm_func=..., call_pro_llm=...)      # 感知(眼睛+伴侣本能)
  ├─ init_wander_manager(call_llm_func=..., on_push_to_user=..., ...)    # 意志(主动来找你)
  ├─ init_global_scheduler(call_llm_func=..., llm_api_key=..., ...)      # 手(想到了就真的做到)
  └─ ContextBuilder.build(recipe=..., persona=..., messages=...)          # 记忆(把以上拼成不乱的一段话)
```

- **LLM 无关**:所有模块通过 `call_llm_func` 类回调消费 LLM,任意 OpenAI 兼容接口均可
- **记忆无关**:漫想的记忆抓取走 `get_memories_func` 注入,协议见 [docs/记忆接口协议.md](docs/记忆接口协议.md),接你自己的向量库/数据库即可;不注入则该事件类型自动降级
- **设备无关**:手机中继、蓝牙心率、摄像头都是可选数据源,不配置则对应采集静默跳过

## 设计哲学

详见 [docs/架构文章/](docs/架构文章/):

- 《漫想模式运行逻辑与推送策略》——AI 主动性的分层设计
- 《用户状态系统——当AI学会了不打扰》——打扰判断与状态机

## 目录约定

- 运行时数据默认写入 cwd 下 `data/`、`logs/`、`settings.json`(均已 gitignore)
- 环境变量见 [.env.example](.env.example),全部可留空——留空即降级
- 测试: `pip install pytest pytest-asyncio` 后运行 `pytest` 即可(漫想/生成器/愿望板/小红书适配器的纯单元测试,不依赖外部服务;依赖宿主生态的用例自动跳过)

## 灵感致谢

MIRROW 的功能设计曾受以下同类项目启发,在此致谢:

- [AionsHome](https://github.com/death34018-hue/AionsHome) — 自托管 AI 伴侣(长期记忆/语音/摄像头视觉)
- [Operit](https://github.com/AAswordman/Operit) — 安卓端 AI 助手(工具调用能力)

除已在 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 中标注的派生内容(app 包名映射表,源自 AionsHome,MIT)外,本仓库代码为独立实现。

## License

[MIT](LICENSE)。第三方派生内容的归属见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 背景

这些模块面向单用户本地 AI 伴侣部署。参考实现以 Windows 为主（键鼠空闲检测、截图和本地蓝牙均可由宿主按需接入），不以云服务、多租户或无头运行作为设计前提。重点是长期交互场景的工程可靠性：数据源可降级，推送经过打扰判断，提示词不把未知事实当作已知。
