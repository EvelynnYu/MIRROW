"""Navigate only to the verified native profile and its personal conversation."""
import asyncio
from types import SimpleNamespace
from urllib.parse import quote

from wander_manager.xhs_profile_navigation import normalized, identity
from xhs_interactions.native import InteractionBlocked, NativeInbox


class DirectChatNavigation(NativeInbox):
    def matching_rows(self, label):
        return [r for r in self.rows if label in (r.get('text'), r.get('content-desc'))]

    async def refresh_after_back(self):
        if not await self.device.back():
            raise InteractionBlocked('back_failed')
        await asyncio.sleep(.6)
        await self.observe()

    async def open_author(self, account, locator):
        await self.home(account)
        uri = 'xhsdiscover://search/result?keyword=' + quote(locator['profile_id'], safe='')
        if not await self.device.launch('com.xingin.xhs', uri=uri):
            raise InteractionBlocked('author_search_failed')
        await self.observe()
        await self.click('用户')
        authors = [row for row in self.rows if row['box'][1] > 380 and
                   normalized(row.get('text', '')) == normalized(locator['note_author'])]
        if len(authors) != 1:
            raise InteractionBlocked('post_author_ambiguous')
        await self.tap(authors[0])
        if identity(SimpleNamespace(text='\n'.join(row['text'] for row in self.rows))) != locator['profile_id']:
            raise InteractionBlocked('post_author_changed')

    async def leave_verified_chat(self):
        # Only our verified recipient chat; leave a human's draft in place.
        edits = [r for r in self.rows if r['class'] == 'android.widget.EditText']
        if any(r['text'] not in ('', '发消息…', '发消息...', '发消息… ', '发消息') and
               r.get('hint') != r['text'] for r in edits):
            raise InteractionBlocked('existing_editor_requires_attention')
        header = {r['text'] for r in self.rows if r['text'] and r['box'][1]<250}
        await self.refresh_after_back()
        if (header & {r['text'] for r in self.rows if r['text'] and r['box'][1]<250} and
                any(r['class']=='android.widget.EditText' and r['box'][1]>1400 for r in self.rows)):
            await self.refresh_after_back()

    async def recipient_chat(self, account, recipient):
        await self.observe()
        if (any(r['class'] == 'android.widget.EditText' and r['box'][1] > 600 for r in self.rows)
                and any(normalized(r['text']) == normalized(recipient['note_author']) and
                        r['box'][1] < 250 for r in self.rows)):
            await self.leave_verified_chat()
        await self.open_author(account, recipient)
        await self.click('发私信')
        headers = [r for r in self.rows if r['box'][1] < 250 and
                   normalized(r['text']) == normalized(recipient['note_author'])]
        if len(headers) != 1:
            raise InteractionBlocked('recipient_chat_unverified')
