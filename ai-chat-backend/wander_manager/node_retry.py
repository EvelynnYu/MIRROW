"""One immediate correction or read retry inside a claimed natural Wander node."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Callable
import sqlite3
import httpx


@dataclass
class Window:
    interrupted: Callable[[], bool]
    receipt: dict = field(default_factory=dict)
    unresolved: bool = False
    closed: bool = False


_window = ContextVar('wander_node_retry', default=None)


@contextmanager
def window(interrupted):
    state = Window(interrupted)
    token = _window.set(state)
    try:
        yield
    finally:
        state.closed = True
        _window.reset(token)


def current():
    state = _window.get()
    return state if state and not state.closed else None


def take(stage, reason):
    state = current()
    if not state or state.receipt or state.interrupted():
        return False
    state.receipt.update(stage=stage, first_reason=reason)
    return True


def unresolved(reason):
    state = current()
    if state and state.receipt:
        state.unresolved = True
        state.receipt['last_reason'] = reason


def cancelled():
    state = current()
    return bool(state and state.interrupted())


async def choose(call, prompt, parse, problem, stage):
    """Repeat just this model choice with the same facts and its validation feedback."""
    if cancelled():
        return None
    value = parse(await call(prompt))
    if cancelled():
        return None
    reason = problem(value)
    if reason and take(stage, reason):
        value = parse(await call(prompt + '\n' + reason))
        if cancelled():
            return None
        if reason := problem(value):
            unresolved(reason)
    return value


def _database_busy(exc):
    return (getattr(exc, 'sqlite_errorcode', 0) & 0xff) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def read_local(call, stage):
    try:
        return call()
    except sqlite3.OperationalError as exc:
        if not _database_busy(exc) or not take(stage, '读取时数据库正忙。'):
            raise
    return call()


async def read(call, stage):
    """Only wrap a repeatable read with its target and parameters already fixed."""
    try:
        return await call()
    except (TimeoutError, ConnectionError, httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError,
            httpx.HTTPStatusError, sqlite3.OperationalError) as exc:
        if isinstance(exc, httpx.HTTPStatusError) and (exc.response.status_code not in (408, 502, 503, 504)
                                                       or 'Retry-After' in exc.response.headers):
            raise
        if isinstance(exc, sqlite3.OperationalError) and not _database_busy(exc):
            raise
        if not take(stage, '读取时连接暂时中断或资源正忙。'):
            raise
    return await call()


def receipt_text(payload):
    entry = payload.get('immediate_retry') or {}
    if not entry.get('stage'):
        return ''
    return f"「{entry['stage']}」当场重试了 1 次。首次回执：{entry['first_reason']}"
