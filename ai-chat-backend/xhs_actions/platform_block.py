"""Synchronize one explicit local block against the verified native profile."""
import asyncio
import re

from xhs_interactions.service import InteractionBlocked
from xhs_interactions.native import box
from xhs_publish.store import account_config
from .dm_navigation import DirectChatNavigation


class BlockPhone(DirectChatNavigation):
    def control(self, labels, *, top=False):
        hits = [row for row in self.rows if row.get('enabled') != 'false'
                and (row.get('text') in labels or row.get('content-desc') in labels)
                and (not top or row['box'][1] < 320)]
        if len(hits) == 1:
            return hits[0]
        # The sheet exposes one outer accessibility label plus its nested text.
        for candidate in hits:
            x1,y1,x2,y2 = candidate['box']
            if (candidate.get('content-desc') in labels and
                    all(x1<=r['box'][0] and y1<=r['box'][1] and x2>=r['box'][2] and y2>=r['box'][3]
                        for r in hits)):
                return candidate
        return None

    async def profile_menu(self, account, profile_id, name):
        await self.open_author(account, {'profile_id': profile_id, 'note_author': name})
        menu = self.control({'更多','更多操作','更多选项','更多按钮','分享用户'}, top=True)
        if not menu:
            raise InteractionBlocked('profile_menu_unverified')
        await self.tap(menu)

    async def block_actions(self):
        undo = {'取消拉黑','解除拉黑','移出黑名单'}
        add = {'拉黑','加入黑名单'}
        # "更多" is a share sheet. Only scroll the row containing native
        # management actions. The separate row of recipients must stay untouched.
        bounds = None
        for _ in range(4):
            remove, block = self.control(undo), self.control(add)
            if remove or block:
                return remove, block
            rows = [node for node in self.root.iter('node') if node.get('scrollable')=='true'
                    and (box(node)==bounds or any(n.get('content-desc') in {'举报','不看他','不让他看','复制链接'}
                                                  for n in node.iter('node')))]
            if len(rows)!=1:
                break
            bounds = box(rows[0])
            left, top, right, bottom = bounds
            center = (top+bottom)//2
            if not await self.device.swipe(right-(right-left)//8, center, left+(right-left)//8, center, 450):
                break
            await self.observe()
        return None, None

    async def ensure_blocked(self, account, profile_id, name):
        await self.profile_menu(account, profile_id, name)
        undo, block = await self.block_actions()
        if undo:
            return True
        if not block:
            return False
        await self.tap(block)
        # Confirmation may have a generic label; only admit it on a block dialog.
        wording = [row.get('text', '') for row in self.rows]
        if any('拉黑' in line or '黑名单' in line for line in wording if len(line) > 4):
            confirm = self.control({'确定拉黑','确认拉黑','确定','确认','拉黑'})
            if confirm and self.control({'取消'}):
                await self.tap(confirm)
        await self.profile_menu(account, profile_id, name)
        # Never tap the reverse action. Re-observation handles interrupted writes.
        undo, _ = await self.block_actions()
        return undo is not None


async def sync_block(profile_id, name, *, phone=None):
    account = account_config()
    if (not isinstance(profile_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', profile_id)
            or not name or profile_id == account['account_id']):
        return 'pending'
    phone = phone or BlockPhone()
    try:
        with await phone.device.lease_async('xhs_explicit_block', timeout=5):
            if await phone.ensure_blocked(account, profile_id, name):
                return 'verified'
    except asyncio.CancelledError:
        raise
    except Exception:
        # Offline phone, unknown layout or an unverified submit stays retryable
        # through the explicit management action. Only local blocking is certain.
        pass
    return 'pending'
