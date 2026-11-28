"""One bounded visual recovery step for read-only XHS navigation.

UIAutomator remains the normal navigation source.  This helper only selects
among already observed, safe navigation controls when that source cannot
confirm a route after an app layout change.  It never supplies coordinates,
text input, or a social action.
"""
from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Mapping
from typing import Any, Awaitable, Callable


BLOCKED_MARKERS = frozenset({'验证码', '安全验证', '人机验证', '账号异常', '风控','隐私政策','用户协议','个人信息保护'})
BLOCKED_CONTROLS = frozenset({
    '登录', '登陆', '授权', '权限', '允许', '同意', '发送', '存草稿', '确认发布',
})
SAFE_LABELS = frozenset({'首页', '发现', '推荐', '搜索', '用户', '我', '消息', '返回',
                         '关闭', '取消', '以后再说', '知道了', '我知道了'})


def _text(row: Mapping[str, Any]) -> str:
    return str(row.get('text') or row.get('content-desc') or '').strip()


def is_blocked(rows: Iterable[Mapping[str, Any]]) -> bool:
    for row in rows:
        text = _text(row)
        if any(marker in text for marker in BLOCKED_MARKERS) or text in BLOCKED_CONTROLS:
            return True
        if str(row.get('class') or '') == 'android.widget.EditText':
            return True
    return False


def candidates(rows: Iterable[Mapping[str, Any]], *, allowed: Iterable[str] = SAFE_LABELS) -> list[dict[str, Any]]:
    """Make stable, observed-only candidate records for a visual chooser."""
    allowed = frozenset(allowed) & SAFE_LABELS
    result: list[dict[str, Any]] = []
    for row in rows:
        label = _text(row)
        if not label and str(row.get('resource-id','')).lower().split('/')[-1] in (
                'close','close_btn','closebutton','iv_close','dialog_close','closeicon'):
            label = '关闭'
        bounds = row.get('box') or row.get('bounds')
        if label not in allowed or not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
            continue
        if str(row.get('enabled', 'true')) == 'false':
            continue
        result.append({
            'id': str(len(result) + 1),
            'label': label,
            'bounds': [int(value) for value in bounds],
            'resource_id': str(row.get('resource-id') or '')[-120:],
        })
    return result


async def select_read_navigation(
    rows: Iterable[Mapping[str, Any]], screenshot: bytes, *,
    allowed: Iterable[str] = SAFE_LABELS,
    vision: Callable[..., Awaitable[Mapping[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    """Return one observed navigation candidate, or ``None`` without acting.

    The response contract accepts only a candidate ID.  It deliberately has no
    free-form coordinates or action names, which keeps an app redesign from
    widening this recovery path into a general device controller.
    """
    rows = list(rows)
    if is_blocked(rows) or not screenshot:
        return None
    options = candidates(rows, allowed=allowed)
    if not options:
        return None
    if vision is None:
        from llm_client import call_vision_api, official_flash_vision_ready
        try:
            official_flash_vision_ready()
        except ValueError:
            return None
        vision = call_vision_api
    prompt = (
        '这是小红书官方 App 的当前 Android 截图。普通控件树导航没有确认目标，'
        '请只从下列已观察到的只读导航或关闭普通提示的候选中选择一个，帮助回到首页推荐、'
        '搜索结果或用户结果。不得选择发送、发布、评论、收藏、点赞、关注、登录、授权或输入。'
        '若没有把握或页面是验证/登录/编辑页面，返回 {"candidate_id":""}。'
        '只输出 JSON。候选=' + json.dumps(options, ensure_ascii=False)
    )
    try:
        result = await vision(
            base64.b64encode(screenshot).decode('ascii'), prompt,
            model='flash-vision', max_tokens=1500, mime_type='image/png', official_only=True,
        )
        value = json.loads(str(result.get('content') or '').strip().removeprefix('```json').removesuffix('```').strip())
    except Exception:
        return None
    chosen = str(value.get('candidate_id') or '') if isinstance(value, Mapping) else ''
    return next((item for item in options if item['id'] == chosen), None)
