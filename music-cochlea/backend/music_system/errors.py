class MusicSystemError(RuntimeError):
    """A safe, user-displayable error from the music boundary."""

    status_code = 400


class MusicNotFound(MusicSystemError):
    status_code = 404


class AccountRequired(MusicSystemError):
    status_code = 401


class PlaybackUnavailable(MusicSystemError):
    status_code = 503


class SessionConflict(MusicSystemError):
    status_code = 409

