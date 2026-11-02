"""淘宝/天猫低星评价采集 · 一键启动器。

双击 `启动低星采集.bat` 后，本脚本会依次完成：
1. 检查/创建 Python 虚拟环境并安装依赖；
2. 启动独立的调试浏览器（Edge/Chrome，仅本机 9222）；
3. 启动本地 Web 服务（127.0.0.1:8787）；
4. 用默认浏览器打开采集页面。

不会修改或复用日常浏览器的配置与 Cookie。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
REQ_FILE = (
    ROOT
    / "taobao-jd-review-cdp"
    / "taobao-tmall"
    / "taobao-tmall-review-exporter"
    / "scripts"
    / "requirements.txt"
)
SERVER_SCRIPT = ROOT / "app" / "server.py"
APP_REQ_FILE = ROOT / "app" / "requirements.txt"

HOST = "127.0.0.1"
CDP_PORT = 9222
WEB_PORT = int(os.getenv("PORT", "8787"))
PRODUCT_URL = "https://www.taobao.com/"

FLAGS = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0


def log(message: str) -> None:
    print(message, flush=True)


def port_open(port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((HOST, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_json(url: str, timeout: float = 2.0):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={"Cache-Control": "no-store"})
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def cdp_info() -> dict | None:
    try:
        data = http_json(f"http://{HOST}:{CDP_PORT}/json/version")
        return data if data.get("Browser") else None
    except Exception:
        return None


def server_ready() -> bool:
    try:
        data = http_json(f"http://{HOST}:{WEB_PORT}/api/health")
        return "cdp" in data
    except Exception:
        return False


def browser_pages() -> list[dict]:
    try:
        pages = http_json(f"http://{HOST}:{CDP_PORT}/json/list")
        return [page for page in pages if page.get("type") == "page"]
    except Exception:
        return []


def find_browser() -> tuple[str, Path, Path]:
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    program_files_x86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    local_app_data = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))

    candidates = [
        ("Edge", program_files_x86 / "Microsoft/Edge/Application/msedge.exe", ROOT / ".cdp-edge-profile"),
        ("Edge", program_files / "Microsoft/Edge/Application/msedge.exe", ROOT / ".cdp-edge-profile"),
        ("Chrome", program_files / "Google/Chrome/Application/chrome.exe", ROOT / ".cdp-chrome-profile"),
        ("Chrome", program_files_x86 / "Google/Chrome/Application/chrome.exe", ROOT / ".cdp-chrome-profile"),
        ("Chrome", local_app_data / "Google/Chrome/Application/chrome.exe", ROOT / ".cdp-chrome-profile"),
    ]
    for label, path, profile in candidates:
        if path.exists():
            return label, path, profile
    raise RuntimeError("没有找到 Edge 或 Chrome。请先安装 Microsoft Edge（Windows 自带）或 Google Chrome。")


def python_works(command: list[str]) -> bool:
    try:
        result = subprocess.run(
            command + ["-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)"],
            capture_output=True,
            timeout=15,
        )
        return result.returncode == 0
    except Exception:
        return False


def find_base_python() -> list[str]:
    candidates: list[list[str]] = []
    if os.name == "nt":
        candidates.extend([["py", "-3.10"], ["py", "-3.11"], ["py", "-3.12"], ["py", "-3"]])
    candidates.append([sys.executable])
    for command in candidates:
        if python_works(command):
            return command
    raise RuntimeError("没有找到可用的 Python 3.9+。请安装 Python 3.10 或更高版本。")


def venv_deps_ok() -> bool:
    if not VENV_PY.exists():
        return False
    result = subprocess.run(
        [str(VENV_PY), "-c", "import pandas, openpyxl, requests, websockets"],
        capture_output=True,
    )
    return result.returncode == 0


def ocr_available() -> bool:
    if not VENV_PY.exists():
        return False
    result = subprocess.run(
        [str(VENV_PY), "-c", "import rapidocr_onnxruntime"],
        capture_output=True,
    )
    return result.returncode == 0


def ensure_environment() -> None:
    if venv_deps_ok():
        log(f"      虚拟环境就绪：{VENV_PY}")
    else:
        if not VENV_PY.exists():
            base = find_base_python()
            log(f"      首次运行，正在创建虚拟环境（{' '.join(base)}）…")
            result = subprocess.run(base + ["-m", "venv", str(ROOT / ".venv")])
            if result.returncode != 0 or not VENV_PY.exists():
                raise RuntimeError("创建虚拟环境失败。")

        if not REQ_FILE.exists():
            raise RuntimeError(f"依赖清单不存在：{REQ_FILE}")

        log("      正在安装基础依赖（首次运行需要几分钟）…")
        subprocess.run([str(VENV_PY), "-m", "pip", "install", "--upgrade", "pip"], check=False)
        result = subprocess.run([str(VENV_PY), "-m", "pip", "install", "-r", str(REQ_FILE)])
        if result.returncode != 0:
            raise RuntimeError("基础依赖安装失败。请检查网络后重试。")
        if not venv_deps_ok():
            raise RuntimeError("基础依赖安装完成但仍无法导入，请查看上方日志。")

    if ocr_available():
        log("      本地 OCR 就绪")
        return
    if not APP_REQ_FILE.exists():
        log("      警告：未找到 OCR 依赖清单，商品图片将不做文字识别。")
        return
    log("      正在安装 OCR 依赖（首次运行需要几分钟）…")
    result = subprocess.run([str(VENV_PY), "-m", "pip", "install", "-r", str(APP_REQ_FILE)])
    if result.returncode != 0 or not ocr_available():
        log("      警告：OCR 依赖安装失败，商品图片仍会下载，但不会提取文字。")


def start_browser() -> subprocess.Popen | None:
    info = cdp_info()
    if info:
        log(f"      复用已开启的调试浏览器：{info.get('Browser')}")
        return None

    if port_open(CDP_PORT):
        raise RuntimeError(
            f"端口 {CDP_PORT} 已被其他程序占用，但不是可用的浏览器调试端口。请关闭占用程序后重试。"
        )

    label, executable, profile = find_browser()
    profile.mkdir(parents=True, exist_ok=True)
    log(f"      启动 {label}（独立配置：{profile.name}）…")

    args = [
        str(executable),
        f"--remote-debugging-port={CDP_PORT}",
        f"--remote-debugging-address={HOST}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        # 采集在后台标签页里进行，避免被浏览器节流/挂起
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        PRODUCT_URL,
    ]
    process = subprocess.Popen(
        args,
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=FLAGS,
    )

    for _ in range(60):
        time.sleep(0.5)
        info = cdp_info()
        if info:
            log(f"      浏览器已就绪：{info.get('Browser')}")
            return process
        if process.poll() is not None:
            break
    raise RuntimeError("调试浏览器启动失败。请检查是否被安全软件拦截。")


def page_hint() -> None:
    urls = [str(page.get("url") or "") for page in browser_pages()]
    if any("login.taobao.com" in url for url in urls):
        log("      [提示] 浏览器当前在淘宝登录页，请扫码登录；登录一次后会记住。")
    elif any("detail.tmall.com" in url for url in urls):
        log("      [提示] 商品页已打开。")
    else:
        log("      [提示] 浏览器已启动，采集时会自动打开商品页。")


def start_server() -> subprocess.Popen | None:
    if server_ready():
        log(f"      复用已运行的服务：http://{HOST}:{WEB_PORT}/")
        return None

    if port_open(WEB_PORT):
        raise RuntimeError(
            f"端口 {WEB_PORT} 已被其他程序占用。请关闭占用程序，或设置环境变量 PORT 使用其他端口。"
        )

    log("      启动本地服务…")
    process = subprocess.Popen(
        [str(VENV_PY), "-u", str(SERVER_SCRIPT)],
        cwd=str(ROOT),
        creationflags=FLAGS,
    )

    for _ in range(60):
        time.sleep(0.5)
        if server_ready():
            log(f"      服务已就绪：http://{HOST}:{WEB_PORT}/")
            return process
        if process.poll() is not None:
            raise RuntimeError("本地服务启动失败，请查看上方日志。")
    raise RuntimeError("本地服务启动超时。")


def open_page() -> None:
    url = f"http://{HOST}:{WEB_PORT}/"
    try:
        if os.name == "nt":
            os.startfile(url)  # type: ignore[attr-defined]
        else:
            webbrowser.open(url)
    except Exception:
        log(f"      请手动打开：{url}")


def stop_process(process: subprocess.Popen | None, name: str) -> None:
    if process is None or process.poll() is not None:
        return
    log(f"      正在停止{name}…")
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def main() -> int:
    log("=" * 56)
    log("  淘宝 / 天猫 低星评价采集 · 一键启动")
    log("=" * 56)

    server_process: subprocess.Popen | None = None
    browser_process: subprocess.Popen | None = None

    try:
        log("[1/4] 检查运行环境…")
        ensure_environment()

        log("[2/4] 检查调试浏览器…")
        browser_process = start_browser()
        page_hint()

        log("[3/4] 启动本地服务…")
        server_process = start_server()

        log("[4/4] 打开采集页面…")
        open_page()

        log("")
        log(f"  页面地址：http://{HOST}:{WEB_PORT}/")
        log("  保持此窗口打开；按 Ctrl+C 停止本地服务。")
        log("  调试浏览器会保持运行，下次启动直接复用。")
        log("")

        while True:
            time.sleep(1)
            if server_process is not None and server_process.poll() is not None:
                log("[错误] 本地服务已退出，请查看上方日志。")
                break
    except KeyboardInterrupt:
        log("\n收到停止信号。")
    except Exception as exc:  # noqa: BLE001
        log(f"\n[错误] {exc}")
        return 1
    finally:
        stop_process(server_process, "本地服务")
        if browser_process is not None:
            log("      调试浏览器保持运行（关闭浏览器窗口即可退出）。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
