# MIRROW：本地 AI 伴侣核心与公开应用

MIRROW 是一个单用户、本地部署的 AI 伴侣系统。本仓库提供可复用模块，以及连接聊天、记忆、认知和漫想的独立本地前后端。每位部署者使用自己的身份设定、模型和数据。

**这不是私人生产环境的原样镜像。** `local_app/` 是公开模块的独立宿主：聊天与漫想分享共用本地 SQLite，Memory V2 校验真实消息来源；前端含聊天、设定、日常状态、日结与日记、记忆检索、漫想日志和许愿板。漫想默认接通关键词联想、记忆回看、自省；哨兵默认可读声明状态与页面可见性。其他已公开模块仍按各自文档接入。共享耳蜗和共域各有独立界面，不会被默认宿主暗中启动。[部署与功能边界](docs/LOCAL_APP.md)。

## 已发布模块

| 模块 | 一句话 | 亮点 |
|------|--------|------|
| [local_app/](local_app/) | 可启动的本地后端 | 固定主会话、模型接线、来源校验记忆、认知书接口、真实漫想运行时 |
| [memory-cognition/](memory-cognition/) | Memory V2 与三域认知书 | 不可变事件、来源追溯、事件延续和可修订认知 |
| [comment/](comment/) | 共域／朋友圈 | 本家动态、评论互动、资料与跨站接口；独立接入包 |
| [music-cochlea/](music-cochlea/) | 共享耳蜗——AI 与你的音乐经历 | 后端、React UI、Windows／Android 网易云控制、歌单／卡片／材料与宿主接口；分析派生文件为非商业许可 |
| [silicon_perception/](silicon_perception/) | 硅基感知——AI 的"感官系统" | 可选设备源、行为基线、分层触发、移动段去重与事实时效合同（没有接入的数据保持未知） |
| [wander_manager/](wander_manager/) | 漫想模式——AI 的"潜意识" | 五层主动推送引擎 + **v3 持久化运行时**(run→activity→node 可追溯身份链,重启可恢复) + **大脑架构生成器** + 10 事件目录 + 愿望板 |
| [behavior_scheduler/](behavior_scheduler/) | 工具调用调度器——AI 的"行动力" | 三通道工具检测(Pre-tool 关键词 → 主模型 TOOL_CALL → 轻模型兜底提取)、防幻觉机制、防重复推送 |
| [mirrow_core/](mirrow_core/) | 共享基础件 | 截断参数配置中心、时间工具、全局状态注册、称呼配置层 |
| [context_builder/](context_builder/) | 上下文组装框架——AI 的"长期记忆"方法论 | 公开配方、显式宿主事实接口与四区消息组装、来源与执行合同分离 |
| [memory_provider.py](memory_provider.py) | 记忆检索抽象协议 | MemoryProvider 标准接口(已附 SQLite 参考实现),不注入=优雅降级 |
| [wander_frontend/](wander_frontend/) | 本地 React 前端 | 聊天、身份、日常状态、自动日结与日记、记忆、漫想日志和许愿板；兼容独立面板宿主 |

## 最新更新：日常闭环、上下文与主动能力

- 新增[模型来源与手动主聊天切换](docs/MODEL_PROVENANCE.md)：历史型号附属记录、当前调用信息、切换事件，以及日记/认知/Memory V2 的来源解释。只标型号，不把不同模型拆成不同人格；公开宿主提供同 endpoint 的模型 ID 切换 API。

- 新增「日常」页：状态、日结关键词、生效时段、倒计时、跨午夜活跃日、自动整理、日记与进度重试。
- 当前日/上一会话日完整原话保留；已完成的较早日以日记、记忆检索和日结交接延续，无需另开会话。失败日不提前移出上下文。
- 工具引导、真实执行回执和四区上下文配套更新；静态首 system 与后端来源材料分离，不承诺固定 token 数或缓存命中率。
- Wander V3 计划、节点复核和自省使用完整来源消息；听歌结束只认真实播放回执，不再用预计时长假定完成。
- 哨兵同步 GPS 移动段去重、来源引文核验、位置变化复核和聊天抢占；默认设备仍须部署者自行接入。

[完整更新、验收边界与宿主接口](docs/RELEASE_20261013.md) · [独立应用部署](docs/LOCAL_APP.md)

## 不随公开应用提供

私人身份、对话、记忆数据库、账号登录态、设备标识、模型密钥均不提供。会客和淘宝仅保留接口，原始集成移植自 AionsHome。群聊传输、完整语音链路、私人设备服务和其他生产专属功能不是默认应用的能力；缺失接入不返回模拟成功。

## 快速开始

```bash
python -m venv .venv
# 激活虚拟环境后：
python -m pip install -r local_app/requirements.txt
cp .env.example .env   # PowerShell 使用 Copy-Item；填写 MIRROW_* 模型配置
cd wander_frontend
npm ci
npm run build
cd ..
python -m local_app
```

打开 `http://127.0.0.1:8765`，先在「设定」填写自己的身份。首次启动为空库，不导入私人项目，也不自动启动模型、传感器或后台漫想。安装、费用、停止语义及兼容边界见 **[本地应用指南](docs/LOCAL_APP.md)**。

只想移植模块时，仍可使用自己的 FastAPI / asyncio 宿主；接线示例见 **[docs/接入指南.md](docs/接入指南.md)**。

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

第三方派生内容与各模块的归属、许可范围见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)，共享耳蜗还有模块内的详细声明。

## 许可补充

共享耳蜗包含歌曲卡片、歌单、持续一起听、真实播放回执、歌词/音频材料及 Windows/Android 接入；部分 eryu 派生分析代码使用 **CC BY-NC-SA 4.0（署名、非商业、相同方式共享）**，不适用根目录 MIT 商业许可。具体文件见 [耳蜗第三方声明](music-cochlea/THIRD_PARTY_NOTICES.md)。公开演示不含私人账号、记录或歌曲音频。

## License

MIRROW 自有代码使用 [MIT](LICENSE)，第三方代码遵循各自许可。**共享耳蜗的 eryu 派生文件使用 CC BY-NC-SA 4.0，完整仓库不能一概视为纯 MIT／可自由商用。** 归属及文件范围见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 背景

这些模块面向单用户本地 AI 伴侣部署。参考实现以 Windows 为主（键鼠空闲检测、截图和本地蓝牙均可由宿主按需接入），不以云服务、多租户或无头运行作为设计前提。重点是长期交互场景的工程可靠性：数据源可降级，推送经过打扰判断，提示词不把未知事实当作已知。
