package com.mirrow.app;

import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.media.MediaMetadata;
import android.media.Rating;
import android.media.session.MediaController;
import android.media.session.MediaSessionManager;
import android.media.session.PlaybackState;
import android.net.Uri;
import android.util.Base64;

import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.util.List;

/** Controls the real NetEase Cloud Music app; it never proxies or plays audio itself. */
public final class NeteaseMusicController {
    private static final String PACKAGE_NAME = "com.netease.cloudmusic";

    private NeteaseMusicController() {}

    public static JSONObject execute(Context context, JSONObject params) {
        JSONObject result = new JSONObject();
        String action = params == null ? "" : params.optString("action", "");
        try {
            switch (action) {
                case "play_song":
                    return playSong(context, params);
                case "daily_recommend":
                    return dispatchOrpheus(context, new JSONObject()
                            .put("type", "playlist").put("cmd", "daily"),
                            "已在手机网易云打开每日推荐");
                case "play":
                    return transport(context, action, "手机网易云已继续播放", params);
                case "pause":
                    return transport(context, action, "手机网易云已暂停", params);
                case "next":
                    return transport(context, action, "手机网易云已切到下一首", params);
                case "prev":
                    return transport(context, action, "手机网易云已切回上一首", params);
                case "like":
                    return transport(context, action, "已向手机网易云发送收藏操作", params);
                case "now_playing":
                    return nowPlaying(context);
                case "capabilities":
                    MediaController availableController = findController(context);
                    return success("网易云原生播放控制").put("data", new JSONObject()
                            .put("single_stop_supported", true)
                            .put("media_session", availableController != null)
                            .put("notification_access", hasNotificationAccess(context)));
                case "arm_guard":
                    MediaController managed = findController(context);
                    if (managed == null) return failure("网易云媒体会话不可用");
                    JSONObject current = metadata(managed);
                    String expected = params.optString("title", "").trim();
                    if (!NeteaseSongIdentity.matches(expected, titleAliases(params), current.optString("title", "")))
                        return failure("当前歌曲已变化，未接管");
                    NeteasePlaybackGuard.arm(context, params.optString("guard_token", ""),
                            expected, params.optString("artist", ""), titleAliases(params));
                    return success("已建立本地单曲停止保护").put("data", current);
                case "open":
                default:
                    boolean opened = openApp(context);
                    result.put("success", opened);
                    result.put("content", opened ? "已打开手机网易云" : "手机未安装网易云音乐");
                    if (!opened) result.put("error", "网易云音乐未安装或不可启动");
                    return result;
            }
        } catch (Exception e) {
            try {
                result.put("success", false);
                result.put("error", e.getClass().getSimpleName() + ": " + e.getMessage());
                result.put("content", "手机网易云控制失败");
            } catch (Exception ignored) {}
            return result;
        }
    }

    private static JSONObject playSong(Context context, JSONObject params) throws Exception {
        String songId = params.optString("song_id", "").trim();
        if (!songId.matches("\\d+")) return failure("缺少有效的网易云歌曲 ID");
        String title = params.optString("title", "");
        String artist = params.optString("artist", "");
        String content = title.isEmpty()
                ? "已向手机网易云发送点播"
                : "正在手机网易云播放《" + title + "》" + (artist.isEmpty() ? "" : " - " + artist);
        JSONObject result = dispatchUri(context, Uri.parse(songDeepLink(songId)), content);
        if (result.optBoolean("success", false)) {
            JSONObject observed = waitForSong(context, title, artist, titleAliases(params), 4000L);
            JSONObject data = new JSONObject();
            data.put("song_id", songId);
            data.put("title", title);
            data.put("artist", artist);
            data.put("duration_ms", params.optLong("duration_ms", 0));
            data.put("dispatched", true);
            String guardToken = params.optString("guard_token", "");
            if (observed == null) {
                // Launching NetEase backgrounds MIRROW's WebView and some OEMs expose the
                // MediaSession only after that transition has settled.  Dispatch is a real
                // command receipt, but it is not playback evidence: keep the session in
                // `starting` until the background observer sees matching metadata.
                data.put("confirmed", false);
                data.put("available", false);
                data.put("playing", false);
                data.put("notification_access", hasNotificationAccess(context));
                if (!guardToken.isEmpty()) {
                    NeteasePlaybackGuard.armPending(context, guardToken, title, artist, titleAliases(params));
                }
                result.put("data", data);
                result.put("content", hasNotificationAccess(context)
                        ? "点播已交给手机网易云，正在等待播放状态确认"
                        : "点播已交给手机网易云；请为 MIRROW 开启通知访问以同步播放状态");
                return result;
            }
            data.put("confirmed", true);
            data.put("observed", observed);
            if (!guardToken.isEmpty()) NeteasePlaybackGuard.arm(context, guardToken, title, artist, titleAliases(params));
            result.put("data", data);
            result.put("content", content);
        }
        return result;
    }

    // Android RedirectActivity accepts song/<id>; desktop Base64 commands only open its home.
    static String songDeepLink(String songId) {
        if (songId == null || !songId.matches("[0-9]+"))
            throw new IllegalArgumentException("Invalid NetEase song ID");
        return "orpheus://song/" + songId;
    }

    private static JSONObject dispatchOrpheus(Context context, JSONObject command,
                                               String content) throws Exception {
        String encoded = Base64.encodeToString(
                command.toString().getBytes(StandardCharsets.UTF_8), Base64.NO_WRAP);
        return dispatchUri(context, Uri.parse("orpheus://" + encoded), content);
    }

    private static JSONObject dispatchUri(Context context, Uri uri, String content) throws Exception {
        Intent intent = new Intent(Intent.ACTION_VIEW, uri);
        intent.setPackage(PACKAGE_NAME);
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        try {
            context.startActivity(intent);
        } catch (Exception first) {
            if (!openApp(context)) return failure("网易云音乐未安装或不支持播放协议");
            return failure("网易云已打开，但点播协议未被手机端接受");
        }
        JSONObject result = success(content);
        result.put("dispatch", "orpheus");
        return result;
    }

    private static JSONObject transport(Context context, String action, String content, JSONObject params) throws Exception {
        MediaController controller = findController(context);
        if (controller == null) return failure("未找到手机网易云的活动 MediaSession；请先启动网易云并授予通知访问");
        String expected = params == null ? "" : params.optString("title", "").trim();
        if (!expected.isEmpty()) {
            JSONObject current = metadata(controller);
            String expectedArtist = params.optString("artist", "");
            String actualArtist = current.optString("artist", "");
            if (!NeteaseSongIdentity.matches(expected, titleAliases(params), current.optString("title", ""))
                    || (!expectedArtist.isEmpty() && !actualArtist.isEmpty()
                        && !expectedArtist.toLowerCase().contains(actualArtist.toLowerCase())
                        && !actualArtist.toLowerCase().contains(expectedArtist.toLowerCase())))
                return failure("网易云已换歌，未控制手动接管后的歌曲");
        }
        MediaController.TransportControls controls = controller.getTransportControls();
        PlaybackState state = controller.getPlaybackState();
        long supported = state == null ? 0 : state.getActions();
        switch (action) {
            case "play":
                if ((supported & PlaybackState.ACTION_PLAY) == 0) return failure("当前 MediaSession 不支持继续播放");
                controls.play(); break;
            case "pause":
                if ((supported & PlaybackState.ACTION_PAUSE) == 0) return failure("当前 MediaSession 不支持暂停");
                controls.pause(); break;
            case "next":
                if ((supported & PlaybackState.ACTION_SKIP_TO_NEXT) == 0) return failure("当前 MediaSession 不支持下一首");
                controls.skipToNext(); break;
            case "prev":
                if ((supported & PlaybackState.ACTION_SKIP_TO_PREVIOUS) == 0) return failure("当前 MediaSession 不支持上一首");
                controls.skipToPrevious(); break;
            case "like":
                if ((supported & PlaybackState.ACTION_SET_RATING) == 0) {
                    return failure("当前手机网易云 MediaSession 不支持收藏操作");
                }
                controls.setRating(Rating.newHeartRating(true));
                break;
            default: return failure("未知播放操作: " + action);
        }
        if (action.equals("pause") || action.equals("play")) {
            for (int i = 0; i < 12; i++) {
                JSONObject observed = metadata(controller);
                if (observed.has("playing") && observed.getBoolean("playing") == action.equals("play"))
                    return success(content).put("data", observed);
                Thread.sleep(150L);
            }
            return failure("控制指令已发送，但 MediaSession 尚未确认状态");
        }
        return success(content);
    }

    private static JSONObject nowPlaying(Context context) throws Exception {
        MediaController controller = findController(context);
        if (controller == null) return failure("未找到手机网易云的活动 MediaSession");
        JSONObject data = metadata(controller);
        NeteasePlaybackGuard.append(data);
        JSONObject result = success(data.optString("title", "").isEmpty()
                ? "手机网易云当前没有可读的歌曲信息"
                : "手机网易云当前播放：《" + data.optString("title") + "》 - " + data.optString("artist"));
        result.put("data", data);
        return result;
    }

    private static JSONObject metadata(MediaController controller) throws Exception {
        JSONObject data = new JSONObject();
        MediaMetadata metadata = controller.getMetadata();
        PlaybackState state = controller.getPlaybackState();
        if (metadata != null) {
            data.put("title", metadata.getString(MediaMetadata.METADATA_KEY_TITLE));
            data.put("artist", metadata.getString(MediaMetadata.METADATA_KEY_ARTIST));
            data.put("album", metadata.getString(MediaMetadata.METADATA_KEY_ALBUM));
            data.put("duration_ms", metadata.getLong(MediaMetadata.METADATA_KEY_DURATION));
        }
        if (state != null) {
            int playback = state.getState();
            data.put("playing", playback == PlaybackState.STATE_PLAYING);
            data.put("available", playback == PlaybackState.STATE_PLAYING || playback == PlaybackState.STATE_PAUSED
                    || playback == PlaybackState.STATE_STOPPED);
            data.put("position_ms", state.getPosition());
            data.put("playback_state", playback);
        }
        return data;
    }

    private static String[] titleAliases(JSONObject params) {
        org.json.JSONArray values = params.optJSONArray("title_aliases");
        if (values == null) return new String[0];
        String[] aliases = new String[Math.min(values.length(), 10)];
        for (int i = 0; i < aliases.length; i++) aliases[i] = values.optString(i, "");
        return aliases;
    }

    private static JSONObject waitForSong(Context context, String title, String artist, String[] aliases,
                                          long timeoutMs) throws Exception {
        long deadline = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < deadline) {
            MediaController controller = findController(context);
            if (controller != null) {
                JSONObject data = metadata(controller);
                String actualTitle = data.optString("title", "");
                String actualArtist = data.optString("artist", "");
                boolean titleMatches = NeteaseSongIdentity.matches(title, aliases, actualTitle);
                boolean artistMatches = artist.isEmpty() || actualArtist.isEmpty()
                        || actualArtist.toLowerCase().contains(artist.toLowerCase())
                        || artist.toLowerCase().contains(actualArtist.toLowerCase());
                if (titleMatches && artistMatches && data.optBoolean("playing", false)) return data;
            }
            Thread.sleep(250L);
        }
        return null;
    }

    static MediaController findController(Context context) {
        try {
            MediaSessionManager manager = (MediaSessionManager)
                    context.getSystemService(Context.MEDIA_SESSION_SERVICE);
            if (manager == null) return null;
            ComponentName listener = new ComponentName(context, MirrowNotificationListener.class);
            List<MediaController> controllers = manager.getActiveSessions(listener);
            for (MediaController controller : controllers) {
                if (PACKAGE_NAME.equals(controller.getPackageName())) return controller;
            }
        } catch (SecurityException ignored) {
            // Notification access is the user-visible permission boundary for active sessions.
        }
        return null;
    }

    static boolean hasNotificationAccess(Context context) {
        String enabled = android.provider.Settings.Secure.getString(
                context.getContentResolver(), "enabled_notification_listeners");
        if (enabled == null || enabled.trim().isEmpty()) return false;
        ComponentName component = new ComponentName(context, MirrowNotificationListener.class);
        String flat = component.flattenToString();
        String shortFlat = component.flattenToShortString();
        for (String item : enabled.split(":")) {
            if (flat.equals(item) || shortFlat.equals(item)) return true;
        }
        return false;
    }

    private static boolean openApp(Context context) {
        Intent launch = context.getPackageManager().getLaunchIntentForPackage(PACKAGE_NAME);
        if (launch == null) return false;
        launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        context.startActivity(launch);
        return true;
    }

    private static JSONObject success(String content) throws Exception {
        return new JSONObject().put("success", true).put("content", content);
    }

    private static JSONObject failure(String error) throws Exception {
        return new JSONObject().put("success", false).put("error", error).put("content", error);
    }
}
