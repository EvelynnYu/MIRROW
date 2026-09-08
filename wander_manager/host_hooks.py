"""Optional host integrations. No private implementation or device configuration.

The original lounge and shopping integrations were ported from AionsHome;
this release contains only their callback boundary, not those implementations.
"""
from contextlib import asynccontextmanager
from functools import wraps

_handlers = {}
_availability = {}
_work_scope = None
_chronicle = None
_music_context = None
_music_record = None
_music_playback = None


def configure_chronicle(provider=None):
    """Optional host history/bookmark repository; no database is opened here."""
    global _chronicle
    _chronicle = provider


def get_chronicle():
    if _chronicle is None:
        raise RuntimeError('history_provider_not_configured')
    return _chronicle


def configure_music(*, context=None, record=None, playback=None):
    global _music_context, _music_record, _music_playback
    _music_context, _music_record, _music_playback = context, record, playback


def music_context(subject):
    return str(_music_context(subject) or '') if _music_context else ''


def record_music_experience(payload, *, source_id):
    return _music_record(payload, source_id=source_id) if _music_record else False


async def play_music(payload):
    import inspect
    if _music_playback is None:
        return False
    result = _music_playback(payload)
    if inspect.isawaitable(result):
        result = await result
    return result is True


def register_event_handler(event_type, handler, *, available=None):
    """Register a bounded handler (visit_once for optional outing events).

    Pass None to disconnect it. Availability is a synchronous, read-only probe.
    Handler results must describe actual work; missing configuration is not success.
    """
    key = getattr(event_type, 'value', str(event_type))
    if handler is None:
        _handlers.pop(key, None)
        _availability.pop(key, None)
    else:
        _handlers[key] = handler
        _availability[key] = available


def get_event_handler(event_type):
    return _handlers.get(getattr(event_type, 'value', str(event_type)))


def event_available(event_type):
    key = getattr(event_type, 'value', str(event_type))
    if key not in _handlers:
        return False
    probe = _availability.get(key)
    try:
        return probe is None or bool(probe())
    except Exception:
        return False


def configure_work_scope(scope_factory=None):
    """Optional async context manager for a host-owned encounter/work lease."""
    global _work_scope
    _work_scope = scope_factory


@asynccontextmanager
async def _unrestricted_local_work():
    yield


def ordinary(fn):
    @wraps(fn)
    async def wrapped(*args, **kwargs):
        async with (_work_scope or _unrestricted_local_work)():
            return await fn(*args, **kwargs)
    return wrapped
