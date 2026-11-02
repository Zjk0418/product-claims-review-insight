"""按需启动独立采集浏览器；只关闭本应用创建的进程。"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

try:  # 桌面包会一并打包；命令行环境缺少时降级为只按父进程关闭。
    import psutil
except Exception:  # noqa: BLE001
    psutil = None

from cdp_endpoint import configure_cdp_endpoint, get_cdp_host

LAUNCH_TIMEOUT = 45.0


def cdp_available(port: int | None = None, browser_path: str = "") -> bool:
    try:
        host = f"http://127.0.0.1:{port}" if port else get_cdp_host()
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(host + "/json/version", timeout=1) as response:
            data = json.load(response)
        if not isinstance(data, dict) or not data.get("Browser"):
            return False
        if port:
            ws = urlsplit(data.get("webSocketDebuggerUrl") or "")
            return (ws.scheme == "ws" and ws.hostname in {"127.0.0.1", "localhost"}
                    and ws.port == port and ws.path == browser_path)
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def find_browser() -> Path:
    bases = [os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
             os.environ.get("ProgramFiles", r"C:\Program Files"),
             os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))]
    for relative in ("Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe"):
        for base in bases:
            path = Path(base) / relative
            if path.is_file():
                return path
    raise RuntimeError("未找到 Edge 或 Chrome，请安装其中一个浏览器后再采集")


def _profile_processes(profile: Path) -> list:
    """找出命令行里带这个专属配置目录的浏览器进程；不会误伤用户日常浏览器。"""
    if psutil is None:
        return []
    target = str(profile).lower()
    found = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if (proc.info.get("name") or "").lower() not in {"msedge.exe", "chrome.exe"}:
                continue
            cmdline = proc.info.get("cmdline") or []
            if any(target in str(part).lower() for part in cmdline):
                found.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


def _terminate_profile_browsers(profile: Path) -> None:
    """Edge 兼容层重启后父进程会退出，仅靠 Popen 句柄关不掉，需要按配置目录兜底清理。"""
    if psutil is None:
        return
    procs = _profile_processes(profile)
    if not procs:
        return
    for proc in procs:
        try:
            for child in proc.children(recursive=True):
                child.terminate()
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    try:
        _, alive = psutil.wait_procs(procs, timeout=3)
    except Exception:  # noqa: BLE001
        alive = []
    for proc in alive:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def persistent_profile() -> Path:
    """采集浏览器使用的持久化配置目录。

    登录态（Cookie）保存在自己的配置目录里，重启应用后仍然有效，从而避免每次扫码；
    本模块不读取、不解密任何账号密码，登录仍由用户在浏览器里完成。
    需要退出登录时删除该目录即可（或设置 REVIEW_COLLECTION_PROFILE 指到别处）。
    """
    override = os.getenv("REVIEW_COLLECTION_PROFILE", "").strip()
    if override:
        return Path(override).expanduser()
    base = os.getenv("LOCALAPPDATA") or os.getenv("XDG_DATA_HOME")
    if base:
        return Path(base) / "ReviewInsight" / "collection-profile"
    return Path.home() / ".reviewinsight" / "collection-profile"


class CollectionBrowser:
    def __init__(self):
        self.process: subprocess.Popen | None = None
        self.profile: Path | None = None
        self.port: int | None = None
        self.browser_path = ""
        self._lock = threading.RLock()

    def ensure(self, session_root: Path, url: str = "about:blank",
               profile_dir: Path | None = None) -> None:
        with self._lock:
            profile = Path(profile_dir).expanduser().resolve() if profile_dir else (
                session_root / "collection-browser"
            ).resolve()
            if self.process is not None and self.process.poll() is None:
                if self.profile == profile and self.port and cdp_available(self.port, self.browser_path):
                    return  # 只复用本对象启动且通过本会话端口核验的浏览器。
                # 进程还在但端口不通：清理后重建，而不是把错误直接抛给用户。
                self.close()
            self.process = None
            configure_cdp_endpoint(None)
            executable = find_browser()
            profile.mkdir(parents=True, exist_ok=True)
            # 上一次失败可能留下仍占用配置目录的 Edge；先按配置目录清理，
            # 否则新进程会把命令交给旧进程，自己退出且不写调试端口。
            _terminate_profile_browsers(profile)
            port_file = profile / "DevToolsActivePort"
            port_file.unlink(missing_ok=True)
            self.profile = profile
            self.port, self.browser_path = None, ""
            last_exit: int | None = None
            try:
                self.process = subprocess.Popen([
                    str(executable), "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
                    f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", "--start-minimized",
                    "--disable-extensions", "--disable-component-extensions-with-background-pages",
                    "--disable-default-apps", "--disable-sync",
                    "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding", url,
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                deadline = time.monotonic() + LAUNCH_TIMEOUT
                while time.monotonic() < deadline:
                    try:
                        lines = port_file.read_text(encoding="utf-8")[:1024].splitlines()
                        port, browser_path = int(lines[0]), lines[1]
                        valid = 1 <= port <= 65535 and browser_path.startswith("/devtools/browser/")
                    except (OSError, ValueError, IndexError):
                        valid = False
                    if valid and cdp_available(port, browser_path):
                        self.port, self.browser_path = port, browser_path
                        configure_cdp_endpoint(port)
                        return
                    if self.process.poll() is not None:
                        # Edge 可能为了跳过兼容层而重启：原进程退出不代表新进程失败。
                        # 只记录退出码，继续等端口文件，不能立即 break。
                        last_exit = self.process.returncode
                    time.sleep(0.2)
                detail = f"，启动进程退出码 {last_exit}" if last_exit is not None else ""
                raise RuntimeError(
                    f"采集浏览器未能就绪（启动超时 {int(LAUNCH_TIMEOUT)} 秒{detail}）。"
                    "已清理旧的采集浏览器进程，请重试；若仍失败，请重启应用。"
                )
            except Exception:
                self.close()
                raise

    def close(self) -> None:
        with self._lock:
            process, self.process = self.process, None
            profile = self.profile
            self.profile, self.port, self.browser_path = None, None, ""
            configure_cdp_endpoint(None)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        if profile is not None:
            _terminate_profile_browsers(profile)


COLLECTION_BROWSER = CollectionBrowser()
