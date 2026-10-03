"""Typed coupling points. A missing host callback never means a successful write."""
from typing import Protocol, Any, Awaitable, Callable

class DeviceAdapter(Protocol):
    async def __call__(self, action: str, device: str, song: dict | None = None) -> dict: ...

class EventSink(Protocol):
    async def __call__(self, fact: dict[str, Any]) -> bool: ...

class MemorySearch(Protocol):
    async def __call__(self, query: str, *, limit: int = 3) -> list[dict]: ...

memory_search: MemorySearch | None = None
evidence_writer: Callable[..., Awaitable[Any]] | None = None

def configure(*, search: MemorySearch | None = None, write_evidence=None):
    global memory_search, evidence_writer
    memory_search, evidence_writer = search, write_evidence
