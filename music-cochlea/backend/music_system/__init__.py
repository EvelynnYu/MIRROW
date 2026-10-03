"""Music centre domain layer; ``main.py`` owns router and device registration."""
from .router import router
from .service import MusicService, configure, get_service
from .store import MusicStore

__all__ = ["router", "MusicService", "MusicStore", "configure", "get_service"]
