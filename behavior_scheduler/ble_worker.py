"""BLE worker — 在独立线程运行 bleak，避开 FastAPI WinRT 冲突"""
import asyncio
import threading
import logging

logger = logging.getLogger(__name__)

_worker_loop = None
_worker_thread = None


def _start_worker():
    global _worker_loop
    _worker_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_worker_loop)
    _worker_loop.run_forever()


def _ensure_worker():
    global _worker_thread, _worker_loop
    if _worker_thread is None or not _worker_thread.is_alive():
        _worker_thread = threading.Thread(target=_start_worker, daemon=True)
        _worker_thread.start()
        # Wait for loop to be ready
        import time
        time.sleep(0.3)


async def run_ble(coro):
    """在 BLE 专用线程的事件循环中运行协程，返回结果"""
    _ensure_worker()
    fut = asyncio.run_coroutine_threadsafe(coro, _worker_loop)
    # Poll with timeout
    timeout = 20
    start = asyncio.get_event_loop().time()
    while not fut.done():
        await asyncio.sleep(0.05)
        if asyncio.get_event_loop().time() - start > timeout:
            fut.cancel()
            raise TimeoutError("BLE operation timed out")
    return fut.result()
