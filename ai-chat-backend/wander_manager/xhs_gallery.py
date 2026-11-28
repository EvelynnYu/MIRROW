"""Shared bounded gallery traversal; captured pages are not analyzed pages."""
import re


# Interaction reads have a bounded window. Browse reads pass the observed total.
DEFAULT_PAGE_LIMIT = 12

def position(root):
    found = {tuple(map(int, m.groups())) for n in root.iter('node')
             if (m := re.search(r'图片,第(\d+)张,共(\d+)张', n.get('content-desc', '')))}
    if len(found) != 1:
        return None
    current, total = next(iter(found))
    return (current, total) if 1 <= current <= total else None


async def collect(snapshot, *, get_position, capture, advance, refresh,
                  limit=DEFAULT_PAGE_LIMIT, retreat=None, require_first=False):
    """Capture only consecutively proven pages; never infer a missing page.

    ``require_first`` is for feed/profile reads: a remembered later page is
    first rewound through the same observed ``page/total`` evidence.  A caller
    can return ``False`` from ``capture`` to stop without recording an unsafe
    screenshot (for example after a popup or changed post).
    """
    pos = get_position(snapshot)
    complete = False
    if pos:
        current, total = pos
        if require_first and current != 1:
            while current > 1 and retreat is not None:
                await retreat()
                snapshot = await refresh()
                if get_position(snapshot) != (current - 1, total):
                    break
                current -= 1
            if current != 1:
                return snapshot, False
        first = current == 1
        if await capture(snapshot, f'image_{current}_of_{total}') is False:
            return snapshot, False
        for _ in range(max(0,min(total, limit)-1)):
            if current == total:
                complete = first
                break
            await advance()
            snapshot = await refresh()
            if get_position(snapshot) != (current + 1, total):
                break
            current += 1
            if await capture(snapshot, f'image_{current}_of_{total}') is False:
                return snapshot, False
        complete = first and current == total
    else:
        # Keep the legacy single-screen caller contract when no gallery
        # position is exposed at all.  Gallery-aware callers avoid using this
        # branch so a non-proven screen is never labelled as a gallery page.
        await capture(snapshot, 'initial')
    return snapshot, complete
