# 发帖署名素材

可直接使用：

> MIRROW「共享耳蜗」音乐模块：歌曲卡片、手机／电脑网易云控制、一起听、歌单和音乐材料记录。频谱与旋律分析部分改编自 eryu（sebastianevan200-stack/eryu，CC BY-NC-SA 4.0）；分析链路的网易云接口封装改编自 AionsHome（death34018-hue，MIT）；保留可选 netease-music-mcp 服务（原版权作者 luuu-h，MIT）。接口使用 pyncm，音频分析使用 librosa / NumPy / Matplotlib / SoundFile，可选识别使用 ShazamIO。前端 React + Vite + TypeScript，后端 FastAPI + Pydantic + HTTPX。图中使用虚构资料，音乐 UI 由实际开源组件渲染。

## 改编源码／随包分发

1. **eryu** — https://github.com/sebastianevan200-stack/eryu 。采用频谱分析和旋律分析接线；当前核验许可 CC BY-NC-SA 4.0。
2. **netease-music-mcp** — `backend/music_mcp/`，原作者 luuu-h，自带 MIT LICENSE。没有核验到唯一上游仓库，不将同名项目误列为来源。
3. **AionsHome** — https://github.com/death34018-hue/AionsHome ，网易云接口封装，MIT。
4. **Gradle Wrapper** — Android 构建工具，Apache-2.0。

## 安装依赖（与源码改编分开）

pyncm；librosa、NumPy、Matplotlib、SoundFile；可选 ShazamIO；React、React DOM、Vite、TypeScript；FastAPI、Uvicorn、Pydantic、HTTPX；python-qrcode、Pillow；Windows PyWinRT；可选 MCP SDK、Zod、mpv、neteasecli。官方地址见 THIRD_PARTY_NOTICES.md。

## 不应写成已经采用

此前调研的其他网易云 MCP 仓库没有代码采用证据；agent_tools_lib 不随本音乐包分发。
