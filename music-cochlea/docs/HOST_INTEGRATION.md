# 耳蜗宿主接口

`music_system` 是播放与歌单收据的权威。宿主不按歌曲时长伪造播放，不从模型文本推断操作成功。以下代码都随包分发，可以复用或通过同名 adapter 替换。

| 边界 | 随包实现 | 宿主接入方式 |
| --- | --- | --- |
| 工具协议 | `behavior_scheduler/base_tool.py`、`cloud_music_tool.py` | schema 与 ToolResult；失败回模型，独立成功可仅 UI，中间依赖传 continue_after |
| 请求来源 | `behavior_scheduler/execution_context.py` | `set_request_platform` / `reset_request_platform`，来源来自请求 |
| 当前会话／手机回调 | `mirrow_core/shared_state.py` | `set_latest_session_id`、`set_mobile_relay_callback` |
| 附件定位 | `event_chronicle.py` → LocalChronicle | find_tool_attachment_messages、update_message_tool_calls；只补已有占位，不复活删除消息 |
| UI 投影 | `session_manager.py` → LocalSessionProjection | async update_message_tool_calls；本示例单 SQLite，整机可另更新缓存 |
| 歌单事件 | `integration.start(..., on_library_event=...)` | 事件 ID 幂等持久写入成功返回 True，失败停止推进水位 |
| 音乐体验 | `cognition/music.py` → local_host | records / record；主观感想与实际设备播放分开 |
| 世界书整理 | `cognition/maintenance.py` → host_ports | configure(write_evidence=callback)，不提供完整认知引擎 |
| 历史场景 | `scene_manager/scene.py` | get_scene_for_ts，默认 None，发生日期真实回退 |
| 记忆召回 | `memory_v2/retrieval.py` → host_ports | configure(search=callback)，默认空来源记录；无私有记忆 |
| 共同上下文 | `music_system/context.py`、`cards.py`、`long_text_cards.py` | build_context / 卡片首轮和历史投影；不调用模型 |
| 自主音乐 | catalog / selection_catalog / library | 候选目录、选择来源与 apply_wander_action；宿主提供完整调度与实际模型决策 |

## 设备协议

```python
async def adapter(action, device, song=None) -> dict:
    # action: play / pause / resume / next / end / observe
    # device: mobile / computer
    return {"available": False}  # 缺观察时如实不可用
```

真实媒体观察：`available, playing, title, artist, position_ms, duration_ms`。已派发命令可返回 `dispatched=True, confirmed=False`；不能凭这个状态标 heard。曲尾保护确认后附 `end_of_track=True, end_token, ended_title, ended_artist`。保留的旧收据不遮蔽当前媒体观察。

手机宿主合同：`async relay(request_id, 'mobile_music_control', params)->dict`。`params.action` 可为 capabilities / now_playing / play_song / pause / play / next / prev / like / arm_guard；点播包含 song_id、title、artist、title_aliases、duration_ms、guard_token。回应 `{success, data}` 或 `{success:false,error}`。

本包的 `relay_port.py` 与 `MusicRelayService.java` 提供音乐专用认证长轮询示例。若宿主已有 Relay，可直接复用 NeteaseMusicController.execute 并替换传输；不需要把整个手机控制服务带入。

## 前端挂载

```tsx
<MusicHost onAttach={() => refreshYourComposer()} />
<ConversationQuickTools onSearch={() => openYourSearch()} />
<MusicCard card={message.longTextCard} />
<AsyncMusicCard attachment={toolCall.extra_data} />
```

`useMusicDraft` / `setMusicDraft` 是本次待发送卡片；发送后由宿主随消息保存，删除／撤回同属消息。`shared/config.ts` 的 getApiBase / getPlatform / getAccessHeaders 和 `shared/types.ts` 的 LongTextCard 是适配边界。`MusicExperiences`、`MusicLibraryEventCard`、`WanderMusicReceipt` 是独立消费视图。

## HTTP

`/api/music/v2`：status、resolve、search、play、control、session、playlists、shared-songs、material、cards、history、account；完整请求 schema 在后端 `/docs` 与 router.py。

`/api/music-host`：示例消息 GET/POST/DELETE、tool POST、context GET。这个示例没有聊天生成模型。

`/api/music-relay`：带令牌的 next / result；只有音乐操作。

`/api/cognition/music`：示例体验 GET/POST；手动资料不标实际播放。

## 生命周期

先创建 MusicService，再注册 adapter / 当前会话 / Relay，再 `integration.start(llm, on_library_event)`。停止调用 integration.stop。默认 app.py 不设置 LLM；有活动日的日整理须在宿主显式配置，并为世界书提供真实消费回执。`cognition.autonomous` 是未接自我书的明确失败接口，不是完整兴趣偏好系统。

模块加载路径需确保解析到本副本。旧宿主的 `owner` 标识可在边界映射到自己的角色 ID。退役的全局音乐模式、捕获链和 commanded_playback 不作为 v2 接入依赖。
