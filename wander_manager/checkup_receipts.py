"""Pure checkup receipt helpers; deliberately no runtime/DB imports."""


def is_checkup_request_text(text):
    """Recognise direct checkup wording, including common spoken variants."""
    import re

    value = str(text or "")
    return bool(
        re.search(r"查(?:个|一下|下)?岗", value)
        or any(word in value for word in ("睡了没", "午睡没", "在干嘛"))
    )

def tracking_items(details, activity):
    d = details or {}; out = []
    if d.get('route') == 'away':
        out.append({'tool':'behavior_state','description':'哨兵行为状态','status':'success','result':'判定离席，未调用视觉'})
    elif d.get('screen_attempted'):
        out.append({'tool':'pc_screen','description':'电脑屏幕/活动分析','status':'success' if d.get('screen_available') else 'failure','result':activity[:500], 'error':d.get('screen_error','')})
    if d.get('phone_screen_attempted'):
        out.append({'tool':'phone_screen','description':'手机屏幕','status':'success' if d.get('phone_screen_available') else 'failure','result':'已取得手机画面' if d.get('phone_screen_available') else '', 'error':d.get('phone_screen_error','')})
    if d.get('camera_attempted'):
        out.append({'tool':'camera','description':'电脑摄像头','status':'success' if d.get('camera_available') else 'failure','result':'已取得画面' if d.get('camera_available') else '', 'error':d.get('camera_error','')})
    if d.get('mobile_activity_attempted'):
        m=d.get('mobile_activity') or {}; out.append({'tool':'mobile_activity','description':'手机活动','status':'success' if m else 'failure','result':f"屏幕{'亮' if m.get('screen_on') else '灭'}；{m.get('foreground_app') or '前台应用未知'}" if m else '', 'error':d.get('mobile_activity_error','')})
    return out

def tracking_conclusion(details, status_consistent):
    d=details or {}; items=tracking_items(d, '')
    if d.get('error') or not any(x.get('status') == 'success' for x in items): return 'uncertain'
    return 'confirmed' if status_consistent else 'contradicted'

def scheduled_conclusion(items):
    return 'uncertain' if (not items or any(x.get('status') != 'success' for x in items)) else 'confirmed'


def sensory_tool_items(tool_calls):
    """Flatten executed sensory calls, including Eyes' physical sources."""
    import json

    sensory = {"eyes", "check_phone", "band", "capture_camera", "take_screenshot"}
    items = []
    for call in tool_calls or []:
        if not isinstance(call, dict) or call.get("tool") not in sensory:
            continue
        error = str(call.get("error") or "")
        result = str(call.get("result") or "")
        parent_item = {
            "tool": call.get("tool"),
            "description": call.get("description") or call.get("tool"),
            "status": "failure" if (error or call.get("success") is False) else ("success" if result else "partial"),
            "result": result[:500],
            "error": error[:300],
        }
        if call.get("tool") != "eyes":
            items.append(parent_item)
            continue
        extra = call.get("extra_data") or {}
        child_items = []
        for source in extra.get("sources") or []:
            if not isinstance(source, dict):
                continue
            analysis = source.get("analysis") or ""
            if isinstance(analysis, dict):
                analysis = analysis.get("summary") or json.dumps(analysis, ensure_ascii=False)
            child_items.append({
                "tool": str(source.get("source") or "eyes_source"),
                "description": str(source.get("source") or "视觉来源"),
                "status": "success",
                "result": str(analysis)[:500],
                "error": "",
            })
        for source_error in extra.get("errors") or []:
            label, _, detail = str(source_error).partition(":")
            child_items.append({
                "tool": label.strip() or "eyes_source",
                "description": label.strip() or "视觉来源",
                "status": "failure",
                "result": "",
                "error": (detail.strip() or str(source_error))[:300],
            })
        # Eyes is one aggregate tool whose sources are the actual camera captures.
        # Showing both the aggregate and its children makes one execution look like
        # several duplicate calls, so prefer the physical-source rows when present.
        items.extend(child_items or [parent_item])
    return items


def visible_chat_tool_calls(tool_calls, *, hide_sensory=False):
    """Keep sensory evidence on the checkup receipt instead of duplicating it on chat."""
    calls = list(tool_calls or [])
    if not hide_sensory:
        return calls
    sensory = {"eyes", "check_phone", "band", "capture_camera", "take_screenshot"}
    return [call for call in calls if not isinstance(call, dict) or call.get("tool") not in sensory]
