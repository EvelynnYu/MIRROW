# 部署条件与启动方式

这是当前整合源码的启动说明，不是已经完成新机端到端验收的承诺。主后端含 Windows 桌面和音乐控制依赖，现行整合链以 Windows 为前提；其他平台需要适配这些工具。前端与独立模块可按自身条件移植。

## 独立目录启动

不要覆盖旧部署或把私人数据库复制进源码仓库。后端进入 `ai-chat-backend/` 后，在自己的虚拟环境中执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

后端默认 `127.0.0.1:8005`。日志目录在后端源码目录下首次创建。模型、人格、账号、设备、语音权重等均由部署者自行配置，本包不包含这些私人资料。

另开终端进入 `ai-chat-frontend/`：

```powershell
npm ci --ignore-scripts
npm run dev
```

本地前端默认 `http://localhost:5173`。若改变后端 `PORT`，前端构建配置 `VITE_BACKEND_PORT` 必须一致。发布构建可运行 `npm run build`；这不等于后端、设备或模型已联通。

## 手机及远程个人设备

主后端默认不对外监听。确需手机访问时，自行配置后端 `HOST`、精确可信客户端 `ALLOWED_IPS`（或既有局域网设备校验），再配置前端 `.env.example` 中的后端地址。

浏览器来源另外受 `ALLOWED_ORIGINS` 约束，按完整 `scheme://host:port` 逗号分隔登记实际前端地址。内置本地开发/预览来源和 Capacitor localhost 来源；不接受 `*`、`null` 或 `CORS_ALLOW_ALL` 放开全部来源。主应用所有 HTTP/WebSocket 都走同一网络边界；没有 Origin 的非浏览器客户端仍必须通过可信网络准入。公共共域的宿主鉴权是另一套显式接入条件，不能用它把主设置接口暴露到公网。

## 依赖层次

`requirements.txt` 是当前主运行清单，包含消息/认知 Markdown、模型 SDK、网页工具、Windows 控制、语音和耳蜗分析的直接依赖；不递归引入内嵌工具包的格式化、类型检查或测试依赖。

`requirements-optional.txt` 提供可选增强：摄像头需要 OpenCV；ImageHash、rapidfuzz 和日期解析缺省各有既有回退；winotify 缺省使用既有 Windows 通知回退。节假日同步需要 chinesecalendar，未安装时相应同步不可用。pyautogui 仅供手动启用内嵌 ComputerTool，默认不注册该工具。外部浏览器、ADB、媒体程序及音频系统仍需按各模块实际使用条件安装。

RVC 不包含私人音色和适配器。可选设置 `RVC_PYTHON` 与 `RVC_INFER_SCRIPT` 为部署者自己的可执行程序和脚本；脚本参数合同是输入音频、输出 WAV、音高整数，工作目录为脚本所在目录。未配置或转换失败时使用原始 TTS 音频，不冒称完成音色转换。

Android 构建需自己的 SDK、签名和构建环境；摄像头、手环、手机、音乐账户与模型调用均未在公开干净环境实测。完整日结生命周期也仍需部署者按真实环境验收。

## Android 桌面小组件

准备 Node.js 22、JDK 21、Android SDK Platform 36、Build Tools 35.0.0 和 Gradle 8.14.3，设置 JAVA_HOME、ANDROID_HOME。在手机可达的地址启动后端并按前文登记设备。进入 ai-chat-frontend 后构建。

```powershell
npm ci --ignore-scripts
npm run build
npx cap sync android
cd android
gradle :app:assembleDebug
```

用自己生成的 app/build/outputs/apk/debug/app-debug.apk 安装主应用。先打开应用，选好后端地址并确认连通，再到启动器的小组件列表添加「MIRROW 漫想」。覆盖升级使用与原应用相同的签名。

## 小红书私信

1. 在自己控制的专用 Android 手机上登录准备给 MIRROW 使用的小红书账号，授权本机 ADB。浏览路径使用 `MIRROW_XHS_ADB_SERIAL`，互动、私信及发帖的设备网关使用 `MIRROW_ANDROID_DEVICE_SERIAL`。有多台设备时，将两项在本机设为同一台专用设备的序列号。
2. 文字输入辅助程序位于 `android-input-helper`。首次以本地调试方式构建时，在该目录创建自己的签名；已有部署沿用原签名。下面是 PowerShell 示例。将生成的文字辅助 APK 安装到那台专用手机。桌面小组件安装在日常使用的手机，两者按各自用途配置。

```powershell
New-Item -ItemType Directory -Force build | Out-Null
# 仅首次构建执行下一行。Android 本地调试口令为 android。
keytool -genkeypair -keystore build/helper-debug.keystore -alias xhstexthelper -keyalg RSA -keysize 2048 -validity 3650 -storepass android -keypass android -dname "CN=MIRROW Local Debug"
./build.ps1 -Platform android-36
```

3. 在 `ai-chat-backend/events/xhs_publish_config.json` 填写登录账号的资料。这两个示例文件应由部署者在自己电脑上创建。

```json
{"account_id":"替换为登录账号的小红书号","account_name":"替换为登录账号的昵称"}
```

4. 确认设备、账号及原生界面一致后，在 `ai-chat-backend/events/xhs_interactions_config.json` 启用互动。`account` 与上一步相同。`owner_account_id` 可选，用于识别伙伴的小红书号；`external_reply_guidance` 可选，用于补充对外回复时的约定。

```json
{
  "ready": true,
  "ui_contract": "xhs-native-interactions-v1",
  "account": {"account_id":"替换为登录账号的小红书号","account_name":"替换为登录账号的昵称"}
}
```

5. 在「收藏 → 🍠 私信」管理本机待审来信及联系人。未知身份、草稿冲突、控件布局无法核验或提交结果不明都会留下相应状态。私信、批准、黑名单和发送回执存于部署者本机。模型处理已获准的来信时，相关文字会进入部署者配置的模型请求；使用图集阅读时，可见页面也会进入所配置的视觉模型。
