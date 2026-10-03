# 第三方来源与许可范围

根目录 MIT 适用于 MIRROW 自有代码。以下派生文件与第三方代码遵循各自许可。

## eryu：派生频谱分析

- 上游：https://github.com/sebastianevan200-stack/eryu
- 分发文件：`backend/music_cochlea/analyze_song.py`、`backend/music_cochlea/enricher.py`。
- 修改：适配材料缓存／子进程协议，分析使用 librosa，可选模型总结与播放事实分开。
- 核验日 2026-10-03：上游当前 LICENSE 为 **CC BY-NC-SA 4.0**。这两份派生文件按同样许可提供，**不适用根目录 MIT 商业授权**。
- 完整许可：[LICENSES/eryu-CC-BY-NC-SA-4.0.txt](LICENSES/eryu-CC-BY-NC-SA-4.0.txt)。原文：https://github.com/sebastianevan200-stack/eryu/blob/main/LICENSE
- 旧分享包误写 MIT 且未附全文，本版已更正；不据此主张上游历史版本许可。

## AionsHome：网易云接口封装

- 上游：https://github.com/death34018-hue/AionsHome
- 原文件：`aion-chat/music.py`；本包派生文件：`backend/netease_api.py`，供音频分析接线使用。
- 修改：适配本地设置、增加 HTTPX 公开资料回退；保留 pyncm 登录刷新与搜索／详情／音频 URL 封装。
- 核验日 2026-10-03：MIT，`Copyright (c) 2026 death34018-hue`；完整许可在 [LICENSES/AionsHome-MIT.txt](LICENSES/AionsHome-MIT.txt)。

## netease-music-mcp：可选独立服务

`backend/music_mcp/` 保留其完整 MIT LICENSE，版权行 `Copyright (c) 2026 luuu-h`。这份源码来自上次已公开分享包，通过 neteasecli / mpv 播放，不是 v2 的网易云 App 控制回退。

没有可验证的唯一上游地址，因此不把同名 GitHub 仓库当作出处；发帖可注明名称及版权作者。

## 构建与安装依赖

Android Gradle Wrapper 的 Apache-2.0 许可在 `LICENSES/gradle-Apache-2.0.txt`。其余依赖通过包管理器安装，不内嵌其源码，各自遵循项目许可：

| 用途 | 项目 | 官方来源 |
| --- | --- | --- |
| 网易云资料／账号 | pyncm | https://github.com/mos9527/pyncm |
| 音频分析 | librosa、NumPy、Matplotlib、SoundFile | https://librosa.org/ 、https://numpy.org/ 、https://matplotlib.org/ 、https://python-soundfile.readthedocs.io/ |
| 可选识别 | ShazamIO | https://github.com/shazamio/ShazamIO |
| Web UI | React、React DOM、Vite、TypeScript | https://react.dev/ 、https://vite.dev/ 、https://www.typescriptlang.org/ |
| 后端 | FastAPI、Uvicorn、Pydantic、HTTPX | https://fastapi.tiangolo.com/ 、https://www.uvicorn.org/ 、https://docs.pydantic.dev/ 、https://www.python-httpx.org/ |
| 二维码 | python-qrcode、Pillow | https://github.com/lincolnloop/python-qrcode 、https://python-pillow.org/ |
| 演示截图与交互验收 | Playwright | https://playwright.dev/python/ |
| Windows 媒体会话 | PyWinRT | https://github.com/pywinrt/pywinrt |
| 可选 MCP | MCP SDK、Zod、mpv、neteasecli | 随服务 README / package.json 说明 |

调研过的其他网易云 MCP 仓库没有确认的代码采用证据，未列为来源。agent_tools_lib 不随本音乐包分发。
