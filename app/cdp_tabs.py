"""在调试浏览器里创建“后台标签页”。

为什么需要这个模块：
- 打开普通标签页（PUT /json/new）会把标签切到前台，窗口可能被拉到最前，
  用户正在做别的事情就会被打断；
- CDP 的 Target.createTarget 支持 background=True，新建的标签页不会激活，
  页面照常加载和执行脚本，但不抢焦点。

采集结束后由调用方关闭该标签页，避免越积越多。
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

import requests
import websockets
from cdp_endpoint import get_cdp_host


def _session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    return session


def _browser_ws_url() -> str:
    data = _session().get(f"{get_cdp_host()}/json/version", timeout=5).json()
    url = data.get("webSocketDebuggerUrl")
    if not url:
        raise RuntimeError("调试浏览器没有返回 webSocketDebuggerUrl")
    return str(url)


def _find_page(target_id: str) -> dict[str, Any] | None:
    pages = _session().get(f"{get_cdp_host()}/json/list", timeout=5).json()
    for page in pages:
        if page.get("id") == target_id and page.get("webSocketDebuggerUrl"):
            return page
    return None


def find_tab(tab_id: str) -> dict[str, Any] | None:
    """查找仍存活的标签页；浏览器断开或标签被关时返回 None。"""
    if not tab_id:
        return None
    try:
        return _find_page(tab_id)
    except Exception:  # noqa: BLE001 - 浏览器不可用时按“不存在”处理
        return None


def list_tabs() -> list[dict[str, Any]]:
    try:
        pages = _session().get(f"{get_cdp_host()}/json/list", timeout=5).json()
    except Exception:  # noqa: BLE001
        return []
    return [page for page in pages if page.get("type") == "page"]


def focus_tab(tab_id: str) -> bool:
    """把指定标签页切到前台（会同时激活浏览器窗口）。"""
    if not tab_id:
        return False
    try:
        response = _session().get(f"{get_cdp_host()}/json/activate/{tab_id}", timeout=5)
        return response.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def _browser_ws() -> str:
    try:
        info = _session().get(f"{get_cdp_host()}/json/version", timeout=5).json()
    except Exception:  # noqa: BLE001
        return ""
    return str(info.get("webSocketDebuggerUrl") or "")


async def _set_window(state: str, left: int | None = None, top: int | None = None) -> bool:
    """调整浏览器窗口状态；窗口级操作只能在浏览器端点上做。"""
    ws_url = await asyncio.to_thread(_browser_ws)
    if not ws_url:
        return False
    pages = await asyncio.to_thread(list_tabs)
    if not pages:
        return False
    async with websockets.connect(ws_url, max_size=1_000_000, proxy=None) as ws:
        await ws.send(
            json.dumps(
                {"id": 1, "method": "Browser.getWindowForTarget", "params": {"targetId": pages[0]["id"]}}
            )
        )
        window_id = None
        while True:
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=6))
            if message.get("id") == 1:
                window_id = (message.get("result") or {}).get("windowId")
                break
        if not window_id:
            return False
        bounds: dict[str, Any] = {"windowState": state}
        if left is not None:
            bounds["left"] = left
        if top is not None:
            bounds["top"] = top
        await ws.send(
            json.dumps({"id": 2, "method": "Browser.setWindowBounds",
                        "params": {"windowId": window_id, "bounds": bounds}})
        )
        await asyncio.wait_for(ws.recv(), timeout=6)
        return True


def hide_window() -> bool:
    """把采集浏览器移出屏幕并最小化，避免采集过程中反复抢焦点。"""
    try:
        return asyncio.run(_set_window("minimized", -32000, -32000))
    except Exception:  # noqa: BLE001
        return False


def show_window() -> bool:
    """需要人工登录/验证时，把窗口放回屏幕中央附近。"""
    try:
        return asyncio.run(_set_window("normal", 140, 90))
    except Exception:  # noqa: BLE001
        return False


LOGIN_PAGE_MARKERS = ("login.taobao.com", "login.tmall.com", "passport.", "punish", "x5sec")


async def _login_state() -> dict[str, Any]:
    """只判断登录态是否存在，不读取任何账号、密码或页面内容。"""
    ws_url = await asyncio.to_thread(_browser_ws)
    if not ws_url:
        return {"available": False, "logged_in": False, "on_login_page": False}
    names: set[str] = set()
    async with websockets.connect(ws_url, max_size=5_000_000, proxy=None) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Storage.getCookies", "params": {}}))
        while True:
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=6))
            if message.get("id") == 1:
                for cookie in (message.get("result") or {}).get("cookies") or []:
                    names.add(str(cookie.get("name") or ""))
                break
    pages = await asyncio.to_thread(list_tabs)
    urls = [str(page.get("url") or "") for page in pages]
    on_login_page = any(marker in url for url in urls for marker in LOGIN_PAGE_MARKERS)
    logged_in = "unb" in names or {"cookie2", "_tb_token_"} <= names
    return {"available": True, "logged_in": logged_in, "on_login_page": on_login_page}


def login_state() -> dict[str, Any]:
    try:
        return asyncio.run(_login_state())
    except Exception:  # noqa: BLE001
        return {"available": False, "logged_in": False, "on_login_page": False}


async def _open_foreground(url: str) -> dict[str, Any]:
    """新建前台标签并导航到指定地址。

    不用 PUT /json/new?url=…：Edge 155 会忽略 url 参数，返回 about:blank。
    """
    target_id = await _create_target("about:blank", False)
    page: dict[str, Any] | None = None
    for _ in range(25):
        page = await asyncio.to_thread(_find_page, target_id)
        if page:
            break
        await asyncio.sleep(0.2)
    if page is None:
        raise RuntimeError("前台标签页创建后未能取得调试地址")
    if url and url != "about:blank":
        async with websockets.connect(
            page["webSocketDebuggerUrl"], max_size=10_000_000, proxy=None
        ) as ws:
            await ws.send(
                json.dumps({"id": 1, "method": "Page.navigate", "params": {"url": url}})
            )
            try:
                await asyncio.wait_for(ws.recv(), timeout=5)
            except Exception:  # noqa: BLE001 - 只要发出导航即可，不阻塞页面加载
                pass
    return await asyncio.to_thread(_find_page, target_id) or page


def open_foreground_tab(url: str) -> dict[str, Any] | None:
    """没有可复用的采集标签时，打开一个前台标签页供用户操作。"""
    try:
        return asyncio.run(_open_foreground(url))
    except Exception:  # noqa: BLE001
        try:
            return _legacy_new_tab(url)
        except Exception:  # noqa: BLE001
            return None


def _legacy_new_tab(url: str = "about:blank") -> dict[str, Any]:
    response = _session().put(f"{get_cdp_host()}/json/new", params={"url": url}, timeout=8)
    response.raise_for_status()
    return response.json()


async def _create_target(url: str, background: bool) -> str:
    async with websockets.connect(
        _browser_ws_url(),
        max_size=10_000_000,
        proxy=None,
        ping_interval=None,
        ping_timeout=None,
    ) as ws:
        await ws.send(
            json.dumps(
                {
                    "id": 1,
                    "method": "Target.createTarget",
                    "params": {"url": url, "background": background},
                }
            )
        )
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            message = json.loads(raw)
            if message.get("id") != 1:
                continue
            if "error" in message:
                error = message["error"]
                raise RuntimeError(str(error.get("message") or error))
            return str(message["result"]["targetId"])


def _close(tab_id: str) -> None:
    try:
        _session().get(f"{get_cdp_host()}/json/close/{tab_id}", timeout=5)
    except Exception:  # noqa: BLE001
        pass


async def create_tab(
    url: str = "about:blank",
    background: bool = True,
    allow_foreground_fallback: bool = True,
) -> dict[str, Any]:
    """新建标签页；background=True 时不会切到前台。"""
    try:
        target_id = await _create_target(url, background)
    except Exception:
        if not allow_foreground_fallback:
            raise
        return await asyncio.to_thread(_legacy_new_tab)

    for _ in range(25):
        page = await asyncio.to_thread(_find_page, target_id)
        if page:
            return page
        await asyncio.sleep(0.2)
    await close_tab(target_id)
    raise RuntimeError("后台标签页创建后未能取得调试地址")


async def close_tab(tab_id: str) -> None:
    if not tab_id:
        return
    await asyncio.to_thread(_close, tab_id)


def open_background_tab(url: str) -> dict[str, Any] | None:
    """同步封装：后台新开标签并导航到 url；浏览器不可用时返回 None。

    供短链解析这类一次性任务使用；失败时不做前台兜底，避免抢用户焦点。
    """
    try:
        return asyncio.run(create_tab(url, background=True, allow_foreground_fallback=False))
    except Exception:  # noqa: BLE001
        return None


def close_background_tab(tab_id: str) -> None:
    """同步封装：关闭后台标签；失败时忽略（浏览器可能已退出）。"""
    try:
        asyncio.run(close_tab(tab_id))
    except Exception:  # noqa: BLE001
        pass


@asynccontextmanager
async def worker_cdp(
    tmall_module: Any,
    background: bool = True,
    on_fallback: Callable[[Exception], None] | None = None,
) -> AsyncIterator[Any]:
    """提供一个用于采集的 CDP 会话。

    background=True 时使用独立的后台标签页，不碰用户当前正在看的页面；
    创建失败则退回原仓库的选择逻辑（复用已有页面），并回调说明原因。
    """
    tab: dict[str, Any] | None = None
    if background:
        try:
            tab = await create_tab("about:blank", background=True)
        except Exception as exc:  # noqa: BLE001
            if on_fallback is not None:
                on_fallback(exc)
            tab = None

    if tab:
        ws_url = tab["webSocketDebuggerUrl"]
    else:
        # The upstream script keeps its own CDP_HOST; keep its fallback on the
        # same selected endpoint rather than silently returning to port 9222.
        tmall_module.CDP_HOST = get_cdp_host()
        ws_url = tmall_module.get_page_ws_url()
    try:
        async with tmall_module.CDPClient(ws_url) as cdp:
            yield cdp
    finally:
        if tab:
            await close_tab(str(tab.get("id") or ""))
