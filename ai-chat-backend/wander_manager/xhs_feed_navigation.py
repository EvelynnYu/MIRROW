"""Verify an explicit destination before selecting any native XHS card."""
from .xhs_profile_navigation import ProfileNavigator, elements
from .xiaohongshu_adb import _parse_bounds


def bottom_tab(snapshot, label):
    return [node for node in elements(snapshot)
            if node.get('content-desc') == label
            and (_parse_bounds(node.get('bounds', '')) or (0, 0, 0, 0))[1] > snapshot.height * .8]


def selected(node):
    return node.get('selected') == 'true' or node.get('checked') == 'true'


async def enter_tab(nav, snapshot, label):
    """Back out of retained detail/inbox pages, never dismiss an editor."""
    for step in range(6):
        nodes = elements(snapshot)
        if any(node.get('class') == 'android.widget.EditText' for node in nodes) or any(
                node.get('text') in {'预览', '下一步', '发笔记', '存草稿'} for node in nodes):
            raise ValueError('existing_editor_requires_attention')
        tabs = bottom_tab(snapshot, label)
        if len(tabs) == 1:
            if not selected(tabs[0]):
                await nav.tap(tabs[0])
                snapshot = await nav.snapshot()
            confirmed = bottom_tab(snapshot, label)
            if len(confirmed) != 1 or not selected(confirmed[0]):
                raise ValueError('navigation_target_unconfirmed')
            return snapshot
        if step < 5:
            await nav.command('shell', 'input', 'keyevent', 'KEYCODE_BACK')
            snapshot = await nav.snapshot()
    raise ValueError('navigation_target_unconfirmed')


async def enter_recommend(source, serial, snapshot):
    nav = ProfileNavigator(source, serial)
    snapshot = await enter_tab(nav, snapshot, '首页')
    discover = [node for node in elements(snapshot) if node.get('text') == '发现'
                and (_parse_bounds(node.get('bounds', '')) or (0, 0, 0, 0))[3] < snapshot.height * .15]
    if len(discover) != 1:
        raise ValueError('recommend_feed_unconfirmed')
    if not selected(discover[0]):
        await nav.tap(discover[0])
        snapshot = await nav.snapshot()
    discover = [node for node in elements(snapshot) if node.get('text') == '发现'
                and (_parse_bounds(node.get('bounds', '')) or (0, 0, 0, 0))[3] < snapshot.height * .15]
    home = bottom_tab(snapshot, '首页')
    if len(home) != 1 or not selected(home[0]) or len(discover) != 1 or not selected(discover[0]):
        raise ValueError('recommend_feed_unconfirmed')
    return snapshot
