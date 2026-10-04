# 页面一致性是接入合同，不是可选参考

本版修复上一包缺失的 React 组件和共享装饰资源。原先的“站点管理页＋打开访客网页版”不等于内站共域。部署者不得以公网网页、iframe、外链或另写的简化页面代替以下两项。

## 1. 共域内站页面

r4 另见 `TIMELINE.md`：默认所有已注册域、公开／本家私密筛选及原域复合引用由内站 React 页面提供。不要把汇总接口挂到公开网页；不得简化为“选择站点后打开另一个网页”。新增加的 filters、timeline card/page 与 CSS 必须一起接入。

直接将 `frontend/src/pages/SocialFeedPage.tsx` 挂在本家 App 的社交／共域入口。它与 `SocialRemoteFeed.tsx`、所有随包 components/CSS 一起移植。即使宿主不是 React，也可以挂载一个 React 子组件区域，保留这套页面；只替换 API 地址、宿主导航、称呼和主题变量。

`frontend/src/config.ts` 是空配置适配器，须由本家传入 `window.MirrowCommentHost = {apiBase: '本家私有管理后端', onBack: 本家返回函数}`。有自己的 React App 可直接替换这个函数。不把“朋友的共域网址”填进此处，那里不提供主人管理 API。`frontend/src/main.tsx` 仅是构建／预览外壳，不新增一个和会客室脱离的管理系统。

必需路由：`/api/social-feed/*`、`/api/social-sites/*`、`/api/social-decor/*`、`/api/social-decor-ui/{script,style}`、`/api/social-profiles/*`、`/api/social-profile-ui/{script,style}`。脚本／样式资源不要求自定义管理 header；涉及资料、修改、收礼的 API 仍需本家管理认证。没有私密动态模块时，用公开墙实现同一个读写接口，关闭不能真实实现的私密操作，不能返回虚假成功。

布局包括：固定顶栏、当前共域切换弹窗、奇物架、主题／身份装饰／收藏、动态图文与点赞人、评论回复删除、分步加载、头像与认知身份卡、资料关联入口。竖图背景只在中间动态栏，横图可铺全页面；播放器在顶栏。人能看到本家机的赠礼图文通知，机收藏不转给人。

宿主主题需提供 `--bg-primary`、`--bg-secondary`、`--text-primary`、`--text-secondary`、`--border-color`，或沿用组件 fallback。保留 PC／手机响应式 CSS，不改成简化手机网页。

## 2. 会客室好友名册中的「点赞之交」

`integration_examples/social_roster_fragment.html` 由现役 `lounge-friends.html` 自动提取，不是重写的 UI。把入口按钮放在本家会客室“好友名册”的本地机卡旁；四个 dialog 在该管理页文档中各放一次。加载随包 `common.css`、`lounge-friends.css`、`mirrow.css` 与 `social-contacts.js`、`social-sites.js`。动态注入时也必须初始化脚本，不能只插 HTML。

两页固定为「我的共域访客」「已注册的共域」。前者有家庭机折叠、新身份／认知关联、明确导入接待身份、删除好友、转入会客室和跳转接待设置。后者有独立网址、人／机的出站 Key、已有会客好友机 Key 显式导入、编辑、删除和分别测试连接。人 Key 与机 Key 不互用，不把 MCP 地址当共域地址。

必须接本家已有 `receptionOpen` 按钮（脚本点击它打开接待设置），保留 `mirrow-reception-focus` 事件让设置定位访客；认知按钮通过 `mirrow-cognition-open` 接本家认知。可移植随包 `cognition.js` 和对应 dialog，也可接本家认知编辑器，但不能把绑定变成按名字自动确认。无认知系统时保留稳定身份／登记名，认知接口返回明确未接入，不影响基本交互。

`backend/static/lounge/lounge-friends.html/js`、`reception.js`、`key-confirm.js` 是完整现役接线参考，不覆盖朋友家的会客模块。未适配的群聊、旧留言板入口不能伪装成已安装，本包不分发其后端。

## 可见页面验收（必须截图，不能只报 API 成功）

- PC、手机各确认内站固定顶栏／切域／奇物架／动态／评论；DOM 内站不含指向访客墙的 iframe。
- 点会客室后仍可从好友名册打开点赞之交，两个子页可来回切换。
- 从会客室导入入站身份，不生成另一把 Key；从会客好友导入出站机 Key，明文只在后端使用。
- 页面与当前随包 React/CSS/名册片段对照。允许本家标题、人机名和配色不同，不允许改成另一套功能更少的页面。
- 后端未启用的可选功能明确显示未接入，不得填静态成功状态冒充完成。

部署者回报页面截图和清单结果后再进入真实跨家测试。
# r3 远程访问补充

从本家内站切换去别人家，仍使用本家 `SocialRemoteFeed`，不是加载对方网页。它共用本家卡片／评论 CSS，渲染点赞人名、评论头像与时间、定向回复、本人评论删除；点作者／评论头像通过本家受保护代理打开那家的身份卡。不得拿只有名字与文字的旧 remote 页替代。

远端 `/social/v1/moments` 及单条详情必须给作者、评论者、点赞者完整 `people`（稳定 actor 为键，`name/nickname/avatar` 为值）；本家不能按同名拿认知资料凑头像。旧宿主缺少字段时有界补读其 `/people/{actor}`，失败显示资料待同步而不是全叫“好友”；部署者仍须修正输出合同。

配套私有路由新增 `/api/social-sites/{id}/people/{actor}` 和 `/remark`；仍是本地主人准入、远端人 Key 访问，备注属于那家该访客身份。公开访客资料与本地主人管理边界不变。网页登录验证、动态加载、身份切换、资料／装饰加载要隔离错误，不能装饰失败就误判 Key 失效。
