"""Bounded feed selection, keeping unsupported video pages distinct from reads."""

OPEN_ERRORS = {
    'gallery_capture_failed': '打开了图集，但没能核对一张属于原帖的图片。',
    'gallery_read_failed': '已取得原帖图片，这次没有收到完整的逐页读图结果。',
    'post_open_unconfirmed': '尝试点开帖子，但没能确认进入正文，这篇没算读完。',
    'no_readable_image_posts': '这次遇到的视频帖已跳过，有限查找内没有找到可读取的图文，未计入已读。',
    'video_return_failed': '跳过视频时未能返回原列表，本次浏览停下了。',
    'feed_return_unconfirmed': '跳过视频后尚未确认回到原列表，本次浏览停下了。',
}


def video_detail(snapshot):
    """Observed native player + visible author/caption, not words in a post."""
    from .xiaohongshu_adb import _parse_bounds, XIAOHONGSHU_PACKAGE
    if not snapshot.available:
        return False
    nodes = list(snapshot.root.iter('node'))
    def visible(node):
        b = _parse_bounds(node.get('bounds', ''))
        return b and b[2] > b[0] and b[3] > b[1]
    ids = {node.get('resource-id') for node in nodes if visible(node)}
    return (all(XIAOHONGSHU_PACKAGE + ':id/' + name in ids
                for name in ('matrixNickNameView', 'noteContentText'))
            and any(node.get('class') == 'android.view.ViewGroup'
                    and node.get('content-desc') in {'暂停', '播放'}
                    and visible(node) and _parse_bounds(node.get('bounds'))[3] > snapshot.height * .8
                    for node in nodes))


def readable_note(snapshot):
    """Read the native gallery caption even when the app renders no separate title."""
    from .xiaohongshu_adb import _verified_note
    from .xhs_gallery import position
    note = _verified_note(snapshot)
    if note or not snapshot.available:
        return note
    nodes = list(snapshot.root.iter('node'))
    authors = [node.get('text', '').strip() for node in nodes
               if node.get('resource-id') == 'com.xingin.xhs:id/nickNameTV' and node.get('text', '').strip()]
    if len(authors) != 1:
        return None
    captions = []
    for container in nodes:
        if len(container) != 2:
            continue
        media, caption = container
        if (len(media) != 1 or media[0].get('resource-id') != 'com.xingin.xhs:id/noteContentLayout'
                or not position(media[0])):
            continue
        # The caption is the gallery's sibling, behind single-child layout
        # wrappers. A comments list or toolbar elsewhere is not caption text.
        while len(caption) == 1:
            caption = caption[0]
        if not len(caption) and caption.get('class') == 'android.widget.TextView' and caption.get('text', '').strip():
            captions.append(caption.get('text').strip())
    if len(captions) == 1:
        return {'note_author': authors[0], 'note_title': '', 'note_body': captions[0]}
    return None


async def open_readable_post(source, serial, feed, excluded):
    """Try at most three cards from this list under the caller's device lease."""
    from .xiaohongshu_adb import visible_cards
    skipped = 0
    attempted = set(excluded)
    attempted_labels = set()
    origin = {card.text for card in visible_cards(feed)}
    current = feed
    for _ in range(3):
        cards = [card for card in visible_cards(current) if card.fingerprint not in attempted
                 and card.text not in attempted_labels]
        if not cards:
            return None, current, skipped, 'no_readable_image_posts' if skipped else 'no_new_feed_cards'
        card = source._random.choice(cards)
        attempted.add(card.fingerprint)
        attempted_labels.add(card.text)
        tapped = await source._run_device(serial, 'shell', 'input', 'tap', *map(str, card.center))
        if tapped is None or tapped.returncode:
            return card, current, skipped, 'post_open_failed'
        await source._sleeper(source.post_wait_seconds)
        detail = await source._dump_ui(serial)
        for _ in range(2):
            if source._guard_snapshot(detail) or readable_note(detail) or video_detail(detail):
                break
            await source._sleeper(source.post_wait_seconds)
            detail = await source._dump_ui(serial)
        if source._guard_snapshot(detail) or not video_detail(detail):
            return card, detail, skipped, ''
        skipped += 1
        if await source._return_to_feed(serial, detail, force=True) != 'detail_back':
            return None, detail, skipped, 'video_return_failed'
        current = await source._dump_ui(serial)
        guard = source._guard_snapshot(current)
        if guard:
            return None, current, skipped, guard
        # Re-observe rather than reuse stale bounds or wander to another entry.
        if not origin.intersection(card.text for card in visible_cards(current)):
            return None, current, skipped, 'feed_return_unconfirmed'
    return None, current, skipped, 'no_readable_image_posts'
