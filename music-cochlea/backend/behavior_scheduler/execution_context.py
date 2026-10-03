"""Request-scoped facts shared by behavior tools.

This module deliberately has no dependency on ``main.py`` so tools can read the
originating client platform without importing the application entrypoint.
"""

from contextvars import ContextVar, Token


_request_platform: ContextVar[str] = ContextVar(
    "behavior_request_platform", default="pc"
)
_request_user_message: ContextVar[str] = ContextVar(
    "behavior_request_user_message", default=""
)
_request_session_id: ContextVar[str] = ContextVar(
    "behavior_request_session_id", default=""
)
_request_message_id: ContextVar[str] = ContextVar(
    "behavior_request_message_id", default=""
)
_request_turn_id: ContextVar[str] = ContextVar(
    "behavior_request_turn_id", default=""
)
_request_protected_texts: ContextVar[tuple[str, ...]] = ContextVar(
    'behavior_request_protected_texts', default=()
)


def set_request_protected_texts(texts) -> Token:
    return _request_protected_texts.set(tuple(text for text in texts if isinstance(text, str) and text))


def reset_request_protected_texts(token: Token) -> None:
    _request_protected_texts.reset(token)


def get_request_protected_texts() -> tuple[str, ...]:
    return _request_protected_texts.get()


def set_request_platform(platform: str) -> Token:
    normalized = "mobile" if str(platform).lower() == "mobile" else "pc"
    return _request_platform.set(normalized)


def reset_request_platform(token: Token) -> None:
    _request_platform.reset(token)


def get_request_platform() -> str:
    return _request_platform.get()


def set_request_user_message(message: str) -> Token:
    """Bind the unmodified human message for deterministic tool gates."""
    return _request_user_message.set(str(message or ""))


def reset_request_user_message(token: Token) -> None:
    _request_user_message.reset(token)


def get_request_user_message() -> str:
    return _request_user_message.get()


def set_request_identity(session_id: str, message_id: str, turn_id: str) -> tuple[Token, Token, Token]:
    clean_message_id = str(message_id or "")
    return (
        _request_session_id.set(str(session_id or "")),
        _request_message_id.set(clean_message_id),
        _request_turn_id.set(str(turn_id or clean_message_id)),
    )


def reset_request_identity(tokens: tuple[Token, Token, Token]) -> None:
    session_token, message_token, turn_token = tokens
    _request_turn_id.reset(turn_token)
    _request_message_id.reset(message_token)
    _request_session_id.reset(session_token)


def get_request_identity() -> tuple[str, str, str]:
    return (
        _request_session_id.get(),
        _request_message_id.get(),
        _request_turn_id.get(),
    )
