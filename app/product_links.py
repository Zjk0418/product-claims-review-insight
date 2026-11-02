"""Resolve Taobao share text and short links.

优先纯 HTTP（302 / meta refresh / JS 跳转赋值）；淘系分享页改为客户端渲染后
（pages-fast.m.taobao.com 中转页，静态 HTML 无商品 ID），由调用方注入
`resolve_share_in_browser` 用调试浏览器读取最终地址里的 itemIds。
"""
from __future__ import annotations

import html
import re
import time
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import requests

MAX_BYTES = 1024 * 1024
MAX_HOPS = 5
TOTAL_TIMEOUT = 20
URL_RE = re.compile(r"https?://[^\s<>\"'“”‘’【】（）()，。；！、]+", re.I)
ASSIGN_RE = re.compile(
    r"(?:\b(?:url|targetUrl|redirectUrl|goUrl)\b|[\"'](?:url|targetUrl|redirectUrl|goUrl)[\"']"
    r"|(?:window\.)?location(?:\.href)?)\s*[:=]\s*([\"'])(.*?)\1", re.I | re.S)
LOCATION_RE = re.compile(r"(?:window\.)?location\.(?:replace|assign)\s*\(\s*([\"'])(.*?)\1", re.I | re.S)
UNRESOLVED = "分享链接未返回商品地址，可能已失效或需要登录/平台验证。请在浏览器打开后复制含 id= 的商品链接，或直接填写商品 ID。"


class ProductLinkError(ValueError):
    pass


def _decode(value: str) -> str:
    value = html.unescape(value).replace(r"\/", "/")
    value = re.sub(r"\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})",
                   lambda m: chr(int(m.group(1) or m.group(2), 16)), value)
    # Some share pages percent-encode their entire destination, sometimes twice.
    for _ in range(3):
        if re.match(r"https?%", value, re.I):
            value = unquote(value)
        else:
            break
    return value.strip()


def _official_url(value: str) -> str:
    value = _decode(value)
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        raise ProductLinkError("商品链接格式不正确") from None
    if (parsed.scheme not in {"http", "https"} or parsed.username is not None or parsed.password is not None
            or port not in {None, 80 if parsed.scheme == "http" else 443}
            or not any(host == domain or host.endswith("." + domain) for domain in ("taobao.com", "tmall.com", "tb.cn"))):
        raise ProductLinkError("仅支持淘宝/天猫商品链接及官方 tb.cn 分享短链")
    return parsed._replace(fragment="").geturl()


def _id_from_url(value: str, depth: int = 0) -> str:
    value = _official_url(value)
    query = parse_qs(urlsplit(value).query)
    for key in ("id", "itemId", "item_id", "itemIds", "item_ids"):
        for candidate in query.get(key, []):
            if re.fullmatch(r"[0-9]{1,20}", candidate):
                return candidate
    if depth < 3:
        for key in ("url", "redirect", "redirectUrl", "redirectURL", "redirect_url", "targetUrl", "target", "tu"):
            for nested in query.get(key, []):
                nested = _decode(nested)
                try:
                    found = _id_from_url(nested, depth + 1)
                except ProductLinkError:
                    continue
                if found:
                    return found
    return ""


class _PageTargets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.targets: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and str(attrs.get("http-equiv", "")).lower() == "refresh":
            target = re.search(r";\s*url\s*=\s*(.+)", attrs.get("content") or "", re.I)
            if target:
                self.targets.append(target.group(1).strip(" \"'"))
        if tag == "link" and "canonical" in str(attrs.get("rel", "")).lower().split():
            self.targets.append(attrs.get("href") or "")


def _page_targets(page: str, base: str) -> list[str]:
    parser = _PageTargets()
    parser.feed(page)
    values = [m.group(2) for m in ASSIGN_RE.finditer(page)]
    values += [m.group(2) for m in LOCATION_RE.finditer(page)] + parser.targets
    targets = []
    for value in values:
        value = _decode(value)
        if not value:
            continue
        target = _official_url(urljoin(base, value))
        if target not in targets:
            targets.append(target)
    return targets


def resolve_share_in_browser(url: str, timeout: float = 12.0) -> str:
    """在调试浏览器里打开分享链接，读取跳转后的商品 ID。

    e.tb.cn 的分享页已经不返回 302，而是一个纯客户端渲染的中转页
    （pages-fast.m.taobao.com/.../xsms-ssr-yyds/external），静态 HTML 里没有商品 ID，
    只能让浏览器执行脚本后再读最终地址（通常带 itemIds=...）。

    返回空串表示浏览器不可用、超时或没有拿到 ID；调用方应保留原始错误。
    """
    try:
        from cdp_endpoint import get_cdp_host
        session = requests.Session()
        session.trust_env = False
        try:
            session.get(f"{get_cdp_host()}/json/version", timeout=3).raise_for_status()
        finally:
            session.close()
    except Exception:  # noqa: BLE001 - 浏览器没起来就不做兜底
        return ""

    from cdp_tabs import close_background_tab, find_tab, open_background_tab

    tab = open_background_tab(url)
    if not tab:
        return ""
    tab_id = str(tab.get("id") or "")
    if not tab_id:
        return ""
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            info = find_tab(tab_id) or {}
            href = str(info.get("url") or "")
            if href:
                # 落到登录/风控页就不必等满超时，直接交回原始错误让用户处理
                if any(mark in href for mark in ("login.taobao.com", "login.tmall.com", "scanError", "/punish")):
                    return ""
                try:
                    product_id = _id_from_url(href)
                except ProductLinkError:
                    product_id = ""
                if product_id:
                    return product_id
            time.sleep(0.4)
        return ""
    finally:
        close_background_tab(tab_id)


def _resolve_via_http(start: str) -> tuple[str, str]:
    """沿官方短链的 302/JS 跳转解析商品 ID；失败抛 ProductLinkError。"""
    current = start
    deadline = time.monotonic() + TOTAL_TIMEOUT
    visited: set[str] = set()
    session = requests.Session()
    session.trust_env = False
    try:
        for _ in range(MAX_HOPS):
            current = _official_url(current)
            if current in visited:
                raise ProductLinkError("分享短链发生循环跳转，请复制浏览器中的最终商品链接或商品 ID")
            visited.add(current)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProductLinkError("分享短链解析超时，请重试，或填写跳转后的商品链接/ID")
            response = session.get(current, allow_redirects=False, stream=True,
                                   timeout=(min(5, remaining), min(10, remaining)),
                                   headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"})
            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location") or response.headers.get("location")
                    if not location:
                        raise ProductLinkError(UNRESOLVED)
                    targets = [_official_url(urljoin(current, location))]
                elif response.status_code == 200:
                    chunks, size = [], 0
                    for block in response.iter_content(32768):
                        size += len(block)
                        if size > MAX_BYTES:
                            raise ProductLinkError("分享页内容过大，请复制跳转后的商品链接或商品 ID")
                        if time.monotonic() > deadline:
                            raise ProductLinkError("分享短链解析超时，请重试，或填写跳转后的商品链接/ID")
                        chunks.append(block)
                    page = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
                    targets = _page_targets(page, current)
                else:
                    raise ProductLinkError(f"分享短链暂时无法访问（HTTP {response.status_code}），请在浏览器中确认链接是否有效")
            finally:
                response.close()
            for target in targets:
                product_id = _id_from_url(target)
                if product_id:
                    return product_id, f"https://detail.tmall.com/item.htm?id={product_id}"
            if not targets:
                raise ProductLinkError(UNRESOLVED)
            current = targets[0]
        raise ProductLinkError("分享链接跳转次数过多，请复制最终商品链接或商品 ID")
    except requests.Timeout:
        raise ProductLinkError("分享短链解析超时，请重试，或填写跳转后的商品链接/ID") from None
    except requests.RequestException:
        raise ProductLinkError("暂时无法连接淘宝分享链接，请检查网络，或填写跳转后的商品链接/ID") from None
    finally:
        session.close()


def resolve_product_input(text: str, browser_resolver=None) -> tuple[str, str]:
    """Return (product_id, canonical_url), resolving only official share redirects.

    browser_resolver: 可选的浏览器兜底函数 (url) -> product_id。
    淘系分享页改为客户端渲染后，纯 HTTP 解析拿不到 ID，需要由调用方注入浏览器解析。
    """
    text = str(text or "").strip()
    if not text or len(text) > 8192:
        raise ProductLinkError("请填写商品 ID、商品链接或一段淘宝分享文本")
    if re.fullmatch(r"[0-9]{1,20}", text):
        return text, f"https://detail.tmall.com/item.htm?id={text}"
    candidates = URL_RE.findall(_decode(text))
    if not candidates:
        raise ProductLinkError("无法识别商品链接，请粘贴完整的淘宝分享链接或商品 ID")
    candidates = list(dict.fromkeys(url.rstrip("，。；！!、;.)]}>") for url in candidates))
    if len(candidates) != 1:
        raise ProductLinkError("请一次只填写一个商品链接")
    current = _official_url(candidates[0])
    product_id = _id_from_url(current)
    if product_id:
        return product_id, f"https://detail.tmall.com/item.htm?id={product_id}"

    try:
        return _resolve_via_http(current)
    except ProductLinkError:
        if browser_resolver is not None:
            try:
                product_id = str(browser_resolver(current) or "")
            except Exception:  # noqa: BLE001 - 兜底失败不影响原始错误
                product_id = ""
            if re.fullmatch(r"[0-9]{1,20}", product_id):
                return product_id, f"https://detail.tmall.com/item.htm?id={product_id}"
        raise
