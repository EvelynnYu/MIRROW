"""Strict bounded wire input for the shared GIF wizard."""
import base64
import json
import threading
from .decor_store import DecorError

_PROCESSING = threading.BoundedSemaphore(1)


def generate(frames, settings):
    if not _PROCESSING.acquire(blocking=False):
        raise DecorError('gif_processing_busy')
    try:
        from .decor_gif import build_gif_result
        return build_gif_result(frames,**settings)
    except (ValueError,OSError):
        raise DecorError('gif_generation_failed') from None
    finally:
        _PROCESSING.release()


def decode_request(raw):
    try:
        data = json.loads(raw)
        if not isinstance(data,dict) or set(data)-{'frames','speed','ping_pong','smooth'}:
            raise ValueError()
        frames = data['frames']
        if not isinstance(frames,list) or not 2<=len(frames)<=8:
            raise ValueError()
        if data.get('speed','normal') not in {'slow','normal','fast'}:
            raise ValueError()
        if any(type(data.get(key,True)) is not bool for key in ('ping_pong','smooth')):
            raise ValueError()
        decoded = [base64.b64decode(f,validate=True) for f in frames]
        if sum(map(len,decoded)) > 10*1024*1024:
            raise DecorError('media_too_large')
        return decoded,{'speed':data.get('speed','normal'),'ping_pong':data.get('ping_pong',True),'smooth':data.get('smooth',True)}
    except (ValueError,TypeError,KeyError):
        raise DecorError('invalid_gif_request') from None
