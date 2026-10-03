# netease-music-mcp

一个用于控制本机音乐播放的 MCP Server。它通过 `neteasecli` 和本地 `mpv` 播放音乐，并提供一个本地歌词播放器 Web UI。

## 免责声明 / Disclaimer

本项目不是网易云音乐官方 MCP，也不隶属于、关联于或受网易云音乐官方认可。本项目仅用于个人学习、研究和本机自动化体验。使用时请合理遵守网易云音乐及相关服务的服务条款、版权规则和账号使用规范。

This project is not an official NetEase Cloud Music MCP and is not affiliated with, associated with, or endorsed by NetEase Cloud Music. It is intended for personal learning, research, and local automation experiments. Please use it responsibly and comply with the applicable NetEase Cloud Music terms of service, copyright rules, and account usage policies.

你可以让 Claude Desktop 这类 MCP 客户端帮你：

- 搜索并播放网易云音乐歌曲
- 暂停、继续、停止、切歌
- 打开本地 Web 播放器：`http://127.0.0.1:8765/`
- 显示歌词、歌单、喜欢的音乐和黑胶播放页
- 在 AI 回复前读取当前歌曲、曲风、歌手和当前歌词上下文
- 结束听歌时一键停止 `mpv` 并关闭本次 Web 播放器

## 环境要求

本项目目前主要面向 Windows 使用。

| 依赖 | 要求 | 说明 |
| --- | --- | --- |
| Windows | 推荐 Windows 10/11 | 当前主要在 Windows 上开发和测试 |
| Node.js | 24 或更新版本 | `neteasecli` 当前版本自身要求 Node.js 24+ |
| npm | 随 Node.js 安装 | 用于安装本项目依赖和全局 CLI |
| 网易云音乐账号 | 可正常登录 | `neteasecli` 会从浏览器导入 Cookie |
| `neteasecli` | 全局安装 | 用于访问网易云音乐账号、搜索、歌单和歌曲信息 |
| `mpv` | 系统可执行文件 | 推荐加入系统 `PATH`，确保能运行 `mpv --version` |

需要上游项目：[wangwalk/neteasecli](https://github.com/wangwalk/neteasecli)。代码默认从 `%APPDATA%\npm\node_modules` 查找全局安装的 `neteasecli`。

`mpv` 是前置依赖，需要提前安装，并确保在 PowerShell 里可以直接运行 `mpv --version`。

`neteasecli` 和 `mpv` 都属于系统/全局依赖，不会被 `npm install` 自动安装。
`neteasecli` 当前版本自身要求 Node.js 24 或更新版本，因此推荐直接使用 Node.js 24+ 跑整个项目。
如果你使用的是 Windows 便携版 `mpv.exe`，也可以临时放在项目根目录。

## 安装

克隆项目：

```powershell
git clone https://github.com/luuu-h/netease-music-mcp.git
cd netease-music-mcp
```

安装项目依赖：

```powershell
npm install
```

全局安装音乐 CLI：

```powershell
npm install -g neteasecli
```

先在浏览器里登录网易云音乐网页版。`neteasecli` 的登录方式是从浏览器导入 Cookie，不会在 MCP 里保存你的账号密码。

```text
https://music.163.com/
```

然后在 PowerShell 里导入登录状态：

```powershell
neteasecli auth login
```

如果你有多个 Chrome/Edge 浏览器 Profile，可以指定 Profile：

```powershell
neteasecli auth login --profile "Profile 1"
```

确认登录状态：

```powershell
neteasecli --pretty auth check
```

安装 `mpv`。任选一种你常用的方式即可：

```powershell
# Chocolatey
choco install mpv -y

# Scoop
scoop install mpv
```

如果 PowerShell 提示找不到 `neteasecli` 命令，请确认 npm 全局命令目录已经加入 `PATH`。Windows 上通常是 `%APPDATA%\npm`。

## 检查安装

语法检查：

```powershell
npm run check
```

Smoke test：

```powershell
npm run smoke
```

`smoke` 会检查：

- 系统 `PATH` 或项目根目录里是否能找到 `mpv`
- 是否全局安装了 `neteasecli`

也可以在 MCP 客户端里让模型调用：

```text
netease-music-mcp.check_environment
```

## 配置 Claude Desktop

Claude Desktop 通过 `claude_desktop_config.json` 注册本地 MCP Server。配置完成后，Claude 才能看到 `netease-music-mcp` 这些工具。

### 1. 确认项目绝对路径

在项目目录里运行：

```powershell
pwd
```

假设输出是：

```text
D:\projects\netease-music-mcp
```

那么 MCP Server 文件路径就是：

```text
D:\projects\netease-music-mcp\src\server.js
```

### 2. 打开 Claude Desktop 配置文件

在 Windows 上打开 Claude Desktop 配置文件：

```powershell
notepad "$env:APPDATA\Claude\claude_desktop_config.json"
```

如果提示找不到路径，先创建目录：

```powershell
New-Item -ItemType Directory -Force "$env:APPDATA\Claude"
notepad "$env:APPDATA\Claude\claude_desktop_config.json"
```

### 3. 写入 MCP 配置

如果文件是空的，直接填入下面内容。注意把路径换成你自己的项目路径，并使用双反斜杠：

```json
{
  "mcpServers": {
    "netease-music-mcp": {
      "command": "node",
      "args": [
        "C:\\path\\to\\netease-music-mcp\\src\\server.js"
      ]
    }
  }
}
```

如果文件里已经有其他 MCP Server，就只把 `"netease-music-mcp"` 这一项加进已有的 `"mcpServers"` 里：

```json
{
  "mcpServers": {
    "existing-server": {
      "command": "..."
    },
    "netease-music-mcp": {
      "command": "node",
      "args": [
        "C:\\path\\to\\netease-music-mcp\\src\\server.js"
      ]
    }
  }
}
```

### 4. 重启并验证

保存配置后，完全退出并重新打开 Claude Desktop。

然后在 Claude 里发送：

```text
请调用 netease-music-mcp.check_environment 检查我的本机音乐环境
```

如果返回里 `neteaseCliInstalled`、`mpvAvailable` 和登录状态都正常，就可以开始点歌。

如果还没登录网易云，可以发送：

```text
请调用 netease-music-mcp.setup_netease_login 带我完成网易云登录
```

### 5. 常见配置错误

- JSON 里 Windows 路径要写双反斜杠，例如 `C:\\path\\to\\file.js`
- `args` 必须指向 `src\\server.js`，不是项目文件夹
- 修改配置后必须重启 Claude Desktop
- 如果 Claude 看不到工具，先确认 `node` 可以在 PowerShell 里直接运行：

```powershell
node -v
```

## 单独启动 Web 播放器

如果你想不通过 Claude，直接预览 Web UI：

```powershell
node .\src\server.js --web-player --port 8765
```

然后在浏览器打开：

```text
http://127.0.0.1:8765/
```

Web 播放器包含：

- 收藏歌单
- 创建歌单
- 我喜欢的音乐
- 歌曲搜索
- 歌单详情页
- 底部播放器
- 播放队列
- 黑胶歌词播放页
- CLI 支持时显示双语歌词

## MCP 工具列表

| 工具名 | 作用 |
| --- | --- |
| `check_environment` | 检查 `neteasecli`、`mpv` 和登录状态 |
| `setup_netease_login` | 引导用户安装/登录 `neteasecli`，并返回下一步命令 |
| `search_song` | 搜索网易云歌曲 |
| `play_song` | 按关键词搜索并播放最匹配的歌曲 |
| `play_track` | 按网易云歌曲 ID 播放 |
| `next_song` | 搜索并切换到另一首歌 |
| `pause` | 暂停播放 |
| `resume` | 继续播放 |
| `stop` | 停止播放并清理当前听歌状态 |
| `shutdown` | 结束本次听歌会话：停止播放、停止 `mpv`、清理状态，并关闭本次 Web 播放器，但保留 MCP 工具进程 |
| `get_status` | 获取播放器状态和缓存的歌曲信息 |
| `get_listening_context` | 获取当前歌曲、曲风、歌手和歌词上下文 |
| `open_web_player` | 启动本地 Web 播放器并返回 localhost URL |

## 推荐 Claude 指令

建议把下面这段放进 Claude 的项目指令或自定义指令里：

```text
你可以使用 netease-music-mcp MCP 控制本机音乐。

当用户要配置、登录、安装、修复或检查 neteasecli 时，调用 netease-music-mcp.setup_netease_login，并按工具返回的 steps 带用户完成登录。用户执行完命令后，再调用一次 netease-music-mcp.setup_netease_login 或 netease-music-mcp.check_environment 验证。

当用户第一次要求播放音乐、点歌、听歌、打开播放器、查看歌词播放器，或当前对话还没有打开过播放器界面时，你必须先调用 netease-music-mcp.open_web_player，并把返回的 localhost URL 告诉用户。

当用户要求播放音乐时，调用 netease-music-mcp.play_song 或 netease-music-mcp.play_track。
当用户要求切歌时，调用 netease-music-mcp.next_song。
当用户要求暂停、继续、停止时，调用 netease-music-mcp.pause、netease-music-mcp.resume、netease-music-mcp.stop。

音乐播放期间，每次回复用户前，都必须先调用 netease-music-mcp.get_listening_context。
把返回的 ai_context 当作当前对话上下文使用。

点歌或切歌成功后，也要使用工具返回的 ai_context。
playback.style 字段已经优先来自网易云歌曲百科，可直接作为曲风/风格使用。

如果 netease-music-mcp.open_web_player 已经返回过 URL，不要重复打开，除非用户明确要求重新打开播放器。
如果用户问播放器在哪里，直接给出上次的 URL；如果不知道 URL，再调用 netease-music-mcp.open_web_player。

当用户说“结束听歌”、“不听了”、“关闭播放器”、“停止整个程序”，或任何表示要结束音乐/听歌会话的请求时，调用 netease-music-mcp.shutdown。这个工具只结束本次听歌和 Web 播放器，不会关闭 MCP 工具进程，因此之后仍然可以继续调用 netease-music-mcp。
```

## 使用示例

在 Claude Desktop 里可以这样说：

```text
一起听歌吧，听歌过程中每次回复我之前请先看 listening_context
打开音乐播放器
播放 布拉格广场 蔡依林
切到 编号89757 林俊杰
暂停
继续
结束听歌
```

## 听歌上下文是怎么工作的

每次开始播放歌曲后，服务会把当前歌曲信息缓存到 `.listening-state.json`：

- 歌曲 ID
- 歌名
- 歌手
- 专辑
- 封面 URL
- 歌曲时长
- 曲风，优先来自网易云歌曲百科
- 带时间戳的歌词
- CLI 提供时的翻译歌词

`get_listening_context` 会返回类似下面的 `ai_context`，其中歌词是当前播放时间之后的 6 句：

```text
我们正在一起听歌，你现在跟我一起听xxx，曲风是xxx，歌手是xxx，当前的6句歌词是xxx
```

注意：MCP Server 本身不能 100% 强制 Claude 在每次回复前调用某个工具。上面的推荐指令会强约束 Claude 主动调用 `get_listening_context`。如果你需要硬性保证，需要自己做一个代理层或自定义客户端，在每次发给模型前自动注入听歌上下文。

## 常见问题

### Claude 看不到工具

修改 `claude_desktop_config.json` 后需要重启 Claude Desktop。

同时确认配置里的 `src/server.js` 是正确的绝对路径。

### PowerShell 找不到 `neteasecli`

先确认安装：

```powershell
npm install -g neteasecli
```

然后确认 npm 全局命令目录在 `PATH` 里。Windows 通常需要包含：

```text
%APPDATA%\npm
```

如果命令暂时不可用，也可以直接用 Node 运行全局安装的 CLI：

```powershell
node "$env:APPDATA\npm\node_modules\neteasecli\dist\index.js" auth login
node "$env:APPDATA\npm\node_modules\neteasecli\dist\index.js" --pretty auth check
```

### 如何登录网易云

`neteasecli` 不走短信验证码登录，而是从浏览器导入网易云音乐 Cookie：

1. 先在 Chrome 或 Edge 里打开 `https://music.163.com/` 并登录网易云账号。
2. 回到 PowerShell 运行：

```powershell
neteasecli auth login
```

3. 检查登录状态：

```powershell
neteasecli --pretty auth check
```

你也可以直接让 Claude 调用：

```text
netease-music-mcp.setup_netease_login
```

它会检查当前缺哪一步，并返回下一步应该运行的命令。

### neteasecli 找不到登录 Cookies

如果 `neteasecli auth login` 一直找不到浏览器里的登录 Cookies，可以手动写入 `neteasecli` 的 session 文件。这通常比继续处理浏览器锁文件更直接。

默认 profile 的 session 文件路径是：

```text
<用户配置目录>\neteasecli\profiles\default\session.json
```

文件内容最少只需要 `MUSIC_U`：

```json
{"MUSIC_U":"这里填你从浏览器里拿到的值"}
```

获取 `MUSIC_U` 的方法：

1. 在 Edge、Chrome 或其他浏览器里打开 `https://music.163.com/` 并登录。
2. 按 `F12` 打开开发者工具。
3. 进入 `Application` / `应用程序`。
4. 在左侧找到 `Cookies` -> `https://music.163.com`。
5. 找到名为 `MUSIC_U` 的 Cookie，复制它的 `Value`。

然后在 PowerShell 里执行：

```powershell
New-Item -ItemType Directory -Force "$HOME\.config\neteasecli\profiles\default"
Set-Content -Encoding UTF8 "$HOME\.config\neteasecli\profiles\default\session.json" '{"MUSIC_U":"把这里替换成你的MUSIC_U"}'
neteasecli --pretty auth check
```

### Smoke test 提示找不到 `mpv`

先确认 PowerShell 里能直接运行：

```powershell
mpv --version
```

如果不能，请先安装 `mpv`，或者把 `mpv.exe` 所在目录加入系统 `PATH`。如果你只是本机开发，也可以把便携版 `mpv.exe` 放在项目根目录；项目里的 `.gitignore` 已经忽略了本机 mpv 二进制和 DLL，避免误上传到 GitHub。

### 关闭网页后音乐还在播放

网页只是播放器界面，真正播放音频的是后台 `mpv`。

只停止播放：

```text
netease-music-mcp.stop
```

结束整个听歌会话并关闭本次 Web 播放器：

```text
netease-music-mcp.shutdown
```

### 手动停止后台 mpv

如果你想手动停掉后台播放进程，可以在 Windows PowerShell 里运行：

```powershell
Get-Process mpv,mpv.com -ErrorAction SilentlyContinue | Stop-Process -Force
```

## 开发

先进入项目目录：

```powershell
cd netease-music-mcp
```

运行检查：

```powershell
npm run check
npm run smoke
```

启动 Web UI：

```powershell
node .\src\server.js --web-player --port 8765
```

启动 MCP Server：

```powershell
npm start
```

