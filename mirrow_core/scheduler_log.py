"""
Shared logger for context scheduler operations.
Provides a single file handle with lock for logs/context_scheduler.log.
Used by both behavior_scheduler and context_scheduler_mcp to avoid interleaving.
"""
import os
import logging
from datetime import datetime
from threading import Lock

logger = logging.getLogger(__name__)

_log_file = None
_log_lock = Lock()


def _get_log_path():
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "context_scheduler.log")


def write_log(message: str, level: str = "INFO"):
    """Write a timestamped log entry to the shared context scheduler log."""
    global _log_file
    try:
        with _log_lock:
            if _log_file is None:
                _log_file = open(_get_log_path(), "a", encoding="utf-8")
            timestamp = datetime.now().isoformat()
            _log_file.write(f"[{timestamp}] [{level}] {message}\n")
            _log_file.flush()
    except Exception as e:
        logger.error(f"Failed to write context scheduler log: {e}")


def close_log():
    """Close the shared log file handle. Called during shutdown."""
    global _log_file
    with _log_lock:
        if _log_file is not None:
            _log_file.close()
            _log_file = None
