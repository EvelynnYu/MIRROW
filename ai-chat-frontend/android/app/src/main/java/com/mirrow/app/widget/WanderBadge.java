package com.mirrow.app.widget;

import org.json.JSONArray;
import org.json.JSONObject;

/** The caption and its artwork come from the same public presence state. */
final class WanderBadge {
    final String scene, label, detail;

    private WanderBadge(String scene, String label, String... details) {
        this.scene = scene;
        this.label = label;
        StringBuilder text = new StringBuilder();
        for (String part : details) {
            if (part.isEmpty()) continue;
            if (text.length() > 0) text.append(" · ");
            text.append(part);
        }
        detail = text.toString();
    }

    private static String value(JSONObject object, String key) {
        return object.isNull(key) ? "" : object.optString(key);
    }

    private static String due(JSONObject status, String key) {
        String stamp = value(status, key);
        return stamp.length() >= 16 ? stamp.substring(11, 16) : "";
    }

    private static WanderBadge activity(JSONObject status, JSONObject catalog, JSONObject shelf) {
        String event = value(status, "current_event_type");
        JSONArray info = catalog.optJSONArray(event);
        String scene = info == null ? "thinking" : event;
        if ("browse_bookmarks".equals(event)) {
            JSONArray sources = status.optJSONArray("shelf_sources");
            if (sources != null && sources.length() == 1) {
                String source = sources.optString(0);
                JSONArray entry = shelf.optJSONArray(source);
                if (entry != null) {
                    info = entry;
                    scene = "shelf_" + source;
                }
            }
        }
        String name = info == null ? "漫想活动" : info.optString(1);
        String phase = value(status, "current_activity_state");
        if (phase.isEmpty()) phase = value(status, "active_run_state");
        if ("node_review".equals(phase)) return new WanderBadge("node_review", "在回顾刚才的活动", name);
        if ("settling".equals(phase)) return new WanderBadge("settling", "在整理活动感想", name);
        boolean waiting = "active_wait".equals(phase);
        if ("sleep".equals(event)) {
            String identity = status.optString("current_activity_id", status.optString("timer_ends_at"));
            scene = new String[]{"sleep", "sleep_side", "sleep_sit"}[Math.floorMod(identity.hashCode(), 3)];
            if (waiting) return new WanderBadge(scene, "休息中", due(status, "timer_ends_at"));
        }
        if (waiting) return new WanderBadge("node_wait", "活动间隔", name, due(status, "next_node_at"));
        return new WanderBadge(scene, event.isEmpty() ? "漫想进行中" : "正在" + name);
    }

    static WanderBadge from(JSONObject snapshot, JSONObject catalog, JSONObject shelf) {
        Object replying = snapshot.opt("primary_reply_running");
        if (Boolean.TRUE.equals(replying)) return new WanderBadge("phone_reply", "正在回复你…");
        JSONObject status = snapshot.optJSONObject("wander");
        if (status == null) return new WanderBadge("unavailable", "状态待同步");
        String state = value(status, "runtime_state");
        switch (state) {
            case "blocked": return new WanderBadge("blocked", "漫想暂时受阻");
            case "planning": return new WanderBadge("planning", "在安排接下来的活动");
            case "active": return activity(status, catalog, shelf);
        }
        if ("not_initialized".equals(state)) return new WanderBadge("preparing", "漫想准备中");
        if ("offline".equals(value(status, "mode"))) {
            return Boolean.FALSE.equals(replying)
                    ? new WanderBadge("phone_wait", "在等你的消息…")
                    : new WanderBadge("idle", "此刻未在漫想");
        }
        if ("scheduled".equals(state)) {
            boolean retry = value(status, "wake_reason").matches(".*(error|failed|timeout|cancelled).*");
            String time = due(status, "next_plan_at");
            if (retry) return new WanderBadge("retry", "等待重试", time);
            return new WanderBadge("interval", "发呆中…", time.isEmpty() ? "" : "下次漫想预计 " + time);
        }
        if ("brainstorming".equals(value(status, "mode"))) return new WanderBadge("thinking", "漫想进行中");
        return new WanderBadge("standby", "等待下一次漫想");
    }
}
