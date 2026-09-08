"""Cross-process lease for a local Android read operation; no device controls."""
from __future__ import annotations
import asyncio
import hashlib
import os
import threading
import time
import tempfile
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

class DeviceBusyError(RuntimeError):
    """The dedicated phone is currently owned by another operation."""


class LeaseUnavailableError(DeviceBusyError):
    """Compatibility alias with a more descriptive name."""


@dataclass(frozen=True)
class DeviceLease:
    """An opaque ownership token returned by :class:`DeviceLeaseManager`."""

    owner: str
    lease_id: str
    acquired_at: float
    _manager: "DeviceLeaseManager" = field(repr=False, compare=False)

    def release(self) -> None:
        self._manager.release(self)

    def __enter__(self) -> "DeviceLease":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


class DeviceLeaseManager:
    """Cross-process, non-reentrant lease for the dedicated Android phone.

    A small OS file lock is the production authority.  The lock is released by
    the OS when a process exits unexpectedly, while the in-memory lock only
    prevents same-process re-entry.  ``memory_only`` exists solely for tests.
    """

    def __init__(self, *, lock_path: str | os.PathLike[str] | None = None, device_key: str = "dedicated_android_phone", memory_only: bool = False) -> None:
        self._lock = threading.Lock()
        self._active: DeviceLease | None = None
        self._counter = 0
        self._memory_only = bool(memory_only)
        safe_key = hashlib.sha256(str(device_key or "dedicated_android_phone").encode("utf-8", "ignore")).hexdigest()[:20]
        configured_dir = os.getenv("MIRROW_ANDROID_LEASE_DIR", "")
        self._lock_path = Path(lock_path) if lock_path else Path(configured_dir or tempfile.gettempdir()) / f"mirrow_android_{safe_key}.lock"
        self._file_handle: Any = None

    @property
    def busy(self) -> bool:
        return self._active is not None

    def acquire(self, owner: str, *, timeout: float = 0.0) -> DeviceLease:
        owner = str(owner or "").strip()
        if not owner or len(owner) > 100:
            raise ValueError("owner_required")
        timeout = max(0.0, float(timeout))
        deadline = time.monotonic() + timeout
        acquired = self._lock.acquire(False)
        if not acquired:
            raise LeaseUnavailableError("android_device_busy")
        try:
            if self._active is not None:
                raise LeaseUnavailableError("android_device_busy")
            lock_handle = None
            if not self._memory_only:
                self._lock_path.parent.mkdir(parents=True, exist_ok=True)
                lock_handle = open(self._lock_path, "a+b")
                while True:
                    try:
                        lock_handle.seek(0)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except (BlockingIOError, OSError):
                        if time.monotonic() >= deadline:
                            lock_handle.close()
                            raise LeaseUnavailableError("android_device_busy")
                        time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))
                self._file_handle = lock_handle
            self._counter += 1
            # The id is only for local ownership checks.  It never appears in
            # a domain result or a logger call.
            lease = DeviceLease(owner, f"lease-{self._counter}", time.monotonic(), self)
            self._active = lease
            return lease
        except Exception:
            if self._file_handle is not None:
                try:
                    self._unlock_file(self._file_handle)
                finally:
                    self._file_handle = None
            self._lock.release()
            raise

    async def acquire_async(self, owner: str, *, timeout: float = 0.0) -> DeviceLease:
        # The critical section is tiny and only protects in-process state.
        # Avoid blocking the event loop if a caller requests a bounded wait.
        if timeout <= 0:
            return self.acquire(owner, timeout=0)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.acquire(owner, timeout=timeout))

    def release(self, lease: DeviceLease) -> None:
        if self._active is not lease:
            # Releasing an old/double token must not unlock the active owner.
            return
        self._active = None
        handle = self._file_handle
        self._file_handle = None
        if handle is not None:
            self._unlock_file(handle)
        self._lock.release()

    @staticmethod
    def _unlock_file(handle: Any) -> None:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError):
            pass
        try:
            handle.close()
        except (OSError, ValueError):
            pass
