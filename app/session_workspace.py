"""应用会话临时处理目录；只清理本模块创建并标记的目录，不触碰用户文件。"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import tempfile
import threading
import uuid
import time
from pathlib import Path


class SessionWorkspace:
    def __init__(self, base: Path | None = None, *, reap_abandoned: bool = True):
        self.base = (base or Path(tempfile.gettempdir()) / "ReviewGapSessions").resolve()
        self.base.mkdir(parents=True, exist_ok=True)
        if reap_abandoned:
            self._remove_abandoned()
        self._lock = threading.RLock()
        self.token = uuid.uuid4().hex
        self.root = Path(tempfile.mkdtemp(prefix="session-", dir=str(self.base))).resolve()
        self.marker = self.root / ".review-session.json"
        self.marker.write_text(json.dumps({"token": self.token, "pid": os.getpid()}), encoding="utf-8")
        self.closed = False
        atexit.register(self.cleanup)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return True
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.GetExitCodeProcess.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return ctypes.get_last_error() == 5  # 权限不足时保守保留。
            try:
                code = wintypes.DWORD()
                return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
            finally:
                kernel.CloseHandle(handle)
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _remove_abandoned(self) -> None:
        for folder in self.base.glob("session-*"):
            try:
                if folder.is_symlink() or folder.resolve().parent != self.base:
                    continue
                marker = folder / ".review-session.json"
                data = json.loads(marker.read_text(encoding="utf-8"))
                token, pid = data.get("token"), int(data.get("pid") or 0)
                if not isinstance(token, str) or len(token) != 32 or self._pid_alive(pid):
                    continue
                self._delete_contents(folder, marker)
            except (OSError, ValueError, TypeError):
                continue

    @staticmethod
    def _delete_contents(target: Path, marker: Path) -> None:
        # 先清内容，最后移除所有权标记；遇文件锁时仍可在退出/下次启动重试。
        for child in target.iterdir():
            if child == marker:
                continue
            if child.is_symlink() or not child.is_dir():
                child.unlink()
            else:
                shutil.rmtree(child)
        saved = marker.read_bytes()
        marker.unlink()
        try:
            target.rmdir()
        except OSError:
            marker.write_bytes(saved)
            raise

    def cleanup(self) -> None:
        with self._lock:
            if self.closed or not self.root.exists():
                self.closed = True
                return
            # 删除前再次解析最终绝对路径；只接受标记和令牌都吻合的直属临时目录。
            target = self.root.resolve()
            if target.parent != self.base or not target.name.startswith("session-"):
                raise ValueError("拒绝清理临时目录边界之外的路径")
            if json.loads(self.marker.read_text(encoding="utf-8")).get("token") != self.token:
                raise ValueError("临时目录标记不匹配，拒绝删除")
            for attempt in range(3):
                try:
                    self._delete_contents(target, self.marker)
                    self.closed = True
                    return
                except OSError:
                    if attempt < 2:
                        time.sleep(0.15)
            # WebView2 等组件的文件锁未释放时保留标记，下次启动清理孤立会话。
