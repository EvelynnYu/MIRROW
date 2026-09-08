# 漫想配套前端（批次5补充）

从 MIRROW 生产界面单独提取的漫想日志面板和新版许愿板，保留活动/节点、来源摘要、节点感想、时间与分享状态展示，以及愿望状态、评论增改删、通知定位接口。桌面和手机均可使用。

不是完整聊天前端，不包含会客、淘宝专属页面，聊天气泡、设备设置、登录态、私人样式资源也未复制。会客与逛淘宝的原始集成移植自 AionsHome，本示例只说明未接入边界。

## 本地运行

需要 Python 3.11+ 和满足 Vite 要求的 Node.js（本次验证使用 Node 24.14.1）。从仓库根目录安装 Python 依赖，再构建界面：

```sh
python -m pip install -r requirements.txt
cd wander_frontend
npm ci
npm run build
cd ..
python -m uvicorn examples.wander_ui:app --host 127.0.0.1 --port 8000
```

浏览器打开 http://127.0.0.1:8000。未产生漫想/愿望时显示真实空态，不预填示例记忆。前端构建产物不提交，需要自己构建。

此示例默认打开仓库 `events/wander_runtime.db` 和 `events/wishes.db`，与已开源模块默认路径一致；如已有宿主，应优先在宿主内挂载下述 router，以使用同一存储实例。不要同时挂载与现有 `/api/wander` 冲突的路由。

`MIRROW_UI_DATA_DIR` 可指定另一份本地数据目录。请在连接自己的已有数据前备份；空目录会初始化新库，旧版许愿板若提示需要迁移，应由宿主显式迁移，前端不偷偷迁移。这个示例不启动漫想、不调用模型、不发主动消息，也不控制设备。

开发模式：保持后端运行，在本目录执行 `npm run dev`，打开 http://127.0.0.1:5173。开发代理默认指向本机 8000 端口；需要调整时复制 `.env.example` 为 `.env.local` 修改 `WANDER_API_TARGET`。前端请求始终走同源 `/api`，不要把 API 密钥放进任何 `VITE_*` 环境变量。

## 接入现有宿主

```python
from wander_manager.public_api import create_wander_ui_router

# runtime_store 和 wish_store 为宿主已初始化的真实存储实例。
app.include_router(create_wander_ui_router(runtime_store, wish_store))
```

API 范围只有：

- `GET /api/wander/persona`：仅返回配置的显示称呼，缺失时前端使用中性 AI/用户。
- `GET /api/wander/logs`：日期、分享过滤、统计及已有调度事实。
- `GET /api/wander/wishes`：许愿板与评论线程。
- `PUT /api/wander/wishes/{id}/status`：有限状态修改。
- `POST /api/wander/wishes/{id}/comments`：新增评论。
- `PUT/DELETE /api/wander/wishes/comments/{id}`：修改或删除本地用户自己的评论。

前端接口将评论作者映射为通用 `user`，但不重写上批已经发布的数据库角色别名。若使用自己原有的 API，应返回相同的公开角色合同。

许愿板标题、评论作者和待决定文案通过 `PersonaProvider` 读取后端 `mirrow_core.persona`，配置使用 `PERSONA_AI_NAME` / `PERSONA_USER_NAME`（优先于 settings）。不要把显示名称写回数据库角色键；`k` 与旧的用户角色键仅用于数据兼容。单独复用组件时一并挂载 `PersonaProvider`。

许愿板的 `focus` 属性支持宿主传入 `target_wish_id/change_kind/before/after/title` 以定位真实通知；独立示例不接管聊天通知总线，不伪造 New/+1。可单独复用 `WishBoardModal`、`WanderLogPanel` 及其 hooks、config/api 和样式文件（含组件自己的 `WishBoardModal.css`）；完整 App 只负责打开/关闭面板和键盘关闭。

日志每次完成请求后约十秒刷新，关闭时取消请求和后续刷新；断网显示错误，不把失败画成“没有记录”。分享是否成功以 `delivery_status` 为准，勿扰不算送达。许愿板写请求不自动重试，超时代表结果未确认，需刷新核对后再决定是否重试。

## 验证

```sh
npm run build
npx playwright install chromium
npm test
```

也可在已安装 Edge 的环境设置 `PLAYWRIGHT_CHANNEL=msedge` 运行测试。浏览器测试使用明确的合成 API 数据，覆盖桌面/手机日志与评论增改删、勿扰状态、断网和关闭取消刷新；真实 SQLite 的接口往返由根目录 `pytest` 覆盖。测试截图、浏览器配置、依赖目录和数据库不提交。

仅面向本地单用户，没有互联网认证或多租户权限层。保持 loopback 监听，不把示例服务器直接暴露到公网。界面上的内容属于部署者自己的私人数据，不要将真实运行记录当作测试素材提交。
