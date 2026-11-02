"""The active loopback CDP endpoint, shared by all collection code paths."""
from __future__ import annotations

import os
import threading

_port: int | None = None
_lock = threading.RLock()


def configure_cdp_endpoint(port: int | None) -> None:
    """Publish an owned browser's port, or clear it when that browser closes."""
    if port is not None and (type(port) is not int or not 1 <= port <= 65535):
        raise ValueError("采集浏览器端口必须是 1–65535 的整数")
    global _port
    with _lock:
        _port = port


def get_cdp_host() -> str:
    with _lock:
        port = _port
    if port is None:
        if os.environ.get("REVIEW_DESKTOP_BROWSER") == "1":
            raise RuntimeError("采集浏览器尚未启动")
        # Preserve the traditional CLI launcher's explicitly managed browser.
        port = 9222
    return f"http://127.0.0.1:{port}"
