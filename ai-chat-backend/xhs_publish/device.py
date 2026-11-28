"""Narrow publication transport; no model-authored shell commands."""
import base64
import hashlib
import re
from android_device_gateway.gateway import AdbDeviceGateway, _as_text

HELPER = 'com.mirrow.xhstexthelper/com.mirrow.xhstexthelper.TextInstrumentation'

class PublishDevice(AdbDeviceGateway):
    @classmethod
    def _allowed_argv(cls, argv):
        p = tuple(argv)
        a = p[3:] if len(p)>3 and p[1]=='-s' else p[1:]
        if a == ('shell','input','keyevent','KEYCODE_BACK'):
            return True
        if a == ('shell','dumpsys','input_method'):
            return True
        if (len(a)==21 and a[:8]==('shell','am','instrument','-w','-r','-e','op','clear')
            and all(a[i:i+2]==('-e',k) and re.fullmatch(r'\d{1,5}',a[i+2])
                    for i,k in ((8,'left'),(11,'top'),(14,'right'),(17,'bottom')))
            and a[20]==HELPER):
            return True
        # Exact helper protocol, with no arbitrary runner/package arguments.
        if (len(a)==24 and a[:7]==('shell','am','instrument','-w','-r','-e','op') and a[7] in {'set','set_empty','search_favorites'}
            and all(a[i:i+2]==('-e',k) and re.fullmatch(r'\d{1,5}',a[i+2])
                    for i,k in ((8,'left'),(11,'top'),(14,'right'),(17,'bottom')))
            and a[20:22]==('-e','text_b64') and re.fullmatch(r'[A-Za-z0-9+/]*={0,2}',a[22])
            and len(a[22])<=24000 and a[23]==HELPER):
            return True
        return super()._allowed_argv(argv)

    async def back(self):
        r=await self._device_invoke('shell','input','keyevent','KEYCODE_BACK')
        return bool(r and r.returncode==0)

    async def xhs_keyboard_visible(self):
        result = await self._device_invoke('shell','dumpsys','input_method')
        state = _as_text(result.stdout) if result and result.returncode==0 else ''
        return bool(re.search(r'\bmInputShown=true\b',state) and
                    re.search(r'\bpackageName=com\.xingin\.xhs\b',state))

    async def set_text(self, bounds, text, *, require_empty=False):
        return await self._editor_action(bounds, text, 'set_empty' if require_empty else 'set')

    async def search_favorites(self, bounds, query):
        if not isinstance(query, str) or not query.strip() or len(query)>200:
            return False
        return await self._editor_action(bounds, query, 'search_favorites')

    async def _editor_action(self, bounds, text, op):
        if not isinstance(text,str) or len(text)>10000:
            return False
        encoded=base64.b64encode(text.encode()).decode('ascii')
        args=['shell','am','instrument','-w','-r','-e','op',op]
        for key,value in zip(('left','top','right','bottom'),bounds):
            args.extend(['-e',key,str(value)])
        args.extend(['-e','text_b64',encoded,HELPER])
        r=await self._device_invoke(*args)
        out=_as_text(r.stdout) if r else ''
        return bool(r and r.returncode==0 and 'status=ok' in out
            and 'INSTRUMENTATION_CODE: -1' in out
            and 'text_sha256='+hashlib.sha256(text.encode()).hexdigest() in out)

    async def clear_text(self, bounds):
        # Empty shell arguments can be dropped by adb shell. This fixed helper
        # operation has no text argument and proves the actual editor is empty.
        args=['shell','am','instrument','-w','-r','-e','op','clear']
        for key,value in zip(('left','top','right','bottom'),bounds):
            args.extend(['-e',key,str(value)])
        args.append(HELPER)
        r=await self._device_invoke(*args)
        out=_as_text(r.stdout) if r else ''
        return bool(r and r.returncode==0 and 'status=ok' in out
            and 'INSTRUMENTATION_CODE: -1' in out
            and 'text_sha256='+hashlib.sha256(b'').hexdigest() in out)
