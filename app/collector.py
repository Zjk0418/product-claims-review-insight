"""天猫/淘宝低星评价采集核心。

复用已验证的 CDP + MTOP 方案，做四件事：
1. 按时间倒序翻页，只保留星级 <= 3 的评价；
2. 按数量或时间范围停止；
3. 每页保存断点，遇到可恢复障碍时暂停（awaiting_user），不直接终止；
4. 恢复时复用断点里的已扫描页码与已命中评价，避免重复写入。

不绕过验证码；平台验证一律交给用户在调试浏览器里手动完成。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from cdp_tabs import (
    create_tab,
    find_tab,
    list_tabs,
    worker_cdp,  # noqa: F401 - 保留给旧的调用方式
)
from intervention import (
    build as build_intervention,
    classify_page,
    is_gate_href,
    item_id_in_href,
    is_product_host,
    product_content_ready,
)
from product import collect_product_info, finish_detail_images, scrape_detail_page
from resources import resource_root
from cdp_endpoint import get_cdp_host

ROOT = resource_root()
REPO_SCRIPT = (
    ROOT
    / "taobao-jd-review-cdp"
    / "taobao-tmall"
    / "taobao-tmall-review-exporter"
    / "scripts"
    / "tmall_rate_auto.py"
)

_spec = importlib.util.spec_from_file_location("tmall_rate_auto", REPO_SCRIPT)
if _spec is None or _spec.loader is None:
    raise RuntimeError(f"无法加载原仓库脚本：{REPO_SCRIPT}")
tmall = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tmall)

DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
RATE_LABEL = {"1": "好评", "0": "中评", "-1": "差评"}

CHECKPOINT_VERSION = 1
MIN_RECOMMENDED_REVIEWS = 5
# 页面响应超时是浏览器还活着、只是这一页没回应：同一页重试，不误报成断线
MAX_PAGE_TIMEOUT_RETRIES = 2
PAGE_TIMEOUT_RETRY_DELAY = 2.0

# 数据访问状态（第二级检查的结果口径）
# not_checked   还没有发过真实评价请求，只有页面状态
# ready         已用真实请求确认可以读取评价数据
# still_blocked 平台仍然拒绝评价接口（验证/限流/接口错误）
# login_required 浏览器登录态不可用
# browser_error 调试浏览器连接异常
# unknown       证据不足，无法判断
ACCESS_NOT_CHECKED = "not_checked"
ACCESS_READY = "ready"
ACCESS_STILL_BLOCKED = "still_blocked"
ACCESS_LOGIN_REQUIRED = "login_required"
ACCESS_BROWSER_ERROR = "browser_error"
ACCESS_PAGE_TIMEOUT = "page_timeout"
ACCESS_UNKNOWN = "unknown"

# 断点里页码允许的上限，只用于挡明显被改坏的文件
MAX_REASONABLE_PAGE = 500

PAGE_STATE_JS = r"""(() => {
  const visible = (node) => {
    const r = node.getBoundingClientRect(), style = getComputedStyle(node);
    return r.width > 0 && r.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
  };
  const nodes = (selector) => Array.from(document.querySelectorAll(selector)).filter(visible);
  const body = document.body && document.body.innerText || '';
  const compact = (text) => String(text || '').replace(/\s+/g, '');
  const title = String(document.title || '').replace(/\s*[-_|]\s*(?:天猫|淘宝网?|tmall|taobao).*$/i, '').trim();
  const genericTitle = /^(?:天猫|淘宝网?|商品详情|宝贝详情|商品页|首页|加载中)[\s\-_|]*$/i;
  const titleInBody = title.length >= 10 && !genericTitle.test(title) && compact(body).includes(compact(title));
  const titleInNode = nodes('h1, [class*="mainTitle"], [class*="MainTitle"], [class*="itemTitle"], [class*="ItemTitle"]')
    .some((node) => { const t = node.innerText.trim(); return t.length >= 6 && !genericTitle.test(t); });
  const hasPrice = nodes('[class*="price" i], [data-testid*="price" i]')
    .some((node) => /(?:[¥￥]\s*\d|\d[.,]\d|\d\s*元)/.test(node.innerText))
    || /[¥￥]\s*\d+(?:\.\d+)?/.test(body);
  const hasBuy = nodes('button, a, [role="button"], [class*="buy" i], [class*="cart" i]')
    .some((node) => /^(立即购买|加入购物车|立刻购买|立即抢购)$/.test(node.innerText.trim()));
  const hasImage = nodes('img').some((node) => {
    const r = node.getBoundingClientRect();
    return node.complete && node.naturalWidth >= 180 && r.width >= 160 && r.height >= 160;
  });
  const dialogs = nodes('[role="dialog"], [class*="loginDialog"], [class*="loginModal"], [class*="captcha" i]');
  const loginOverlay = dialogs.some((node) => /扫码登录|密码登录|请先登录/.test(node.innerText))
    || nodes('iframe[src*="login.taobao.com"], iframe[src*="login.tmall.com"]').some((node) => {
      const r = node.getBoundingClientRect(); return r.width >= 200 && r.height >= 150;
    });
  const verificationOverlay = dialogs.some((node) => /安全验证|滑动验证|拖动滑块|点选验证/.test(node.innerText));
  return {href: location.href, title: document.title, ready_state: document.readyState,
    text: body.slice(0, 2000), has_product_title: titleInBody || titleInNode,
    has_product_price: hasPrice, has_product_image: hasImage, has_purchase_action: hasBuy,
    has_login_overlay: loginOverlay, has_verification_overlay: verificationOverlay,
    has_comment_dom: !!document.querySelector('[class*="rate-"], [class*="Rate"], [class*="comment"], [id*="comment"], [class*="评价"]')};
})()"""


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    match = DATE_RE.search(str(value))
    if not match:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def star_of(rate: dict[str, Any]) -> int | None:
    raw = (rate.get("extraInfoMap") or {}).get("userGrade")
    try:
        star = int(str(raw))
    except (TypeError, ValueError):
        return None
    return star if 1 <= star <= 5 else None


def clean_text(value: Any) -> str:
    return CONTROL_RE.sub("", str(value or ""))


def image_urls(rate: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for item in rate.get("rateResourceList") or []:
        if isinstance(item, str):
            value = item
        elif isinstance(item, dict):
            value = next(
                (item.get(key) for key in ("url", "picUrl", "imageUrl", "src") if item.get(key)),
                "",
            )
        else:
            continue
        if not isinstance(value, str) or not value:
            continue
        if value.startswith("//"):
            value = "https:" + value
        if value.startswith("http") and value not in urls:
            urls.append(value)
    return urls


def _first(rate: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = rate.get(key)
        if value not in (None, "", []):
            return value
    return None


def _media_urls(rate: dict[str, Any]) -> list[str]:
    """收集评论附带的视频引用；平台不返回时保持空列表（缺失，不假装没有）。"""
    urls: list[str] = []
    for key in ("video", "videos", "videoUrl", "videoUrls", "media"):
        value = rate.get(key)
        if isinstance(value, str) and value.startswith("http"):
            urls.append(value)
        elif isinstance(value, dict):
            urls.extend(image_urls(value))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.startswith("http"):
                    urls.append(item)
                elif isinstance(item, dict):
                    urls.extend(image_urls(item))
    return list(dict.fromkeys(urls))


def simplify(rate: dict[str, Any]) -> dict[str, Any]:
    star = star_of(rate)
    rate_type = str(rate.get("rateType") or "")
    review_date = parse_date(rate.get("feedbackDate"))
    # 追评：不同版本页面字段名不一致，逐个尝试；都没有时为空并标记 appended=False。
    append_text = clean_text(_first(rate, "appendFeedback", "appendComment", "appendContent", "append"))
    append_date = parse_date(_first(rate, "appendDate", "appendTime", "appendFeedbackDate"))
    images = image_urls(rate)
    videos = _media_urls(rate)
    return {
        "review_id": str(rate.get("id") or ""),
        "star": star,
        "star_source": "extraInfoMap.userGrade" if star is not None else "rate_type_only",
        "rate_type": rate_type,
        "rate_label": RATE_LABEL.get(rate_type, "未知"),
        "date": review_date.strftime("%Y-%m-%d") if review_date else "",
        "date_raw": str(rate.get("feedbackDate") or ""),
        "content": clean_text(rate.get("feedback")),
        "sku": clean_text(rate.get("skuValueStr")),
        "sku_id": str(rate.get("skuId") or ""),
        "nick": clean_text(rate.get("reduceUserNick") or rate.get("userNick")),
        "reply": clean_text(rate.get("reply")),
        "images": images,
        "videos": videos,
        "has_media": bool(images or videos),
        "append_content": append_text,
        "append_date": append_date.strftime("%Y-%m-%d") if append_date else "",
        "append_gap_days": (
            (append_date - review_date).days
            if append_date is not None and review_date is not None and append_date >= review_date
            else None
        ),
        "appended": bool(append_text),
    }


def normalize_scope(mode: str, value: int, unit: str) -> dict[str, Any]:
    if mode == "count":
        return {"mode": "count", "count": int(value), "unit": "reviews"}
    if mode == "time":
        factor = {"day": 1, "month": 30, "year": 365}.get(unit)
        if factor is None:
            raise ValueError("时间单位只支持 day / month / year")
        days = int(value) * factor
        return {"mode": "time", "days": days, "value": int(value), "unit": unit}
    raise ValueError("mode 只支持 count / time")


def is_traffic_error(payload: dict[str, Any]) -> bool:
    text = json.dumps(payload, ensure_ascii=False)
    return any(
        key in text
        for key in ("FAIL_SYS_TRAFFIC_LIMIT", "TRAFFIC_LIMIT", "FAIL_SYS_FLOWLIMIT")
    )


# ---------- 断点 ----------

def new_state(
    item_id: str,
    product_url: str,
    scope: dict[str, Any],
    page_size: int,
    max_scan: int,
) -> dict[str, Any]:
    return {
        "version": CHECKPOINT_VERSION,
        "product_id": item_id,
        "product_url": product_url,
        "scope": scope,
        "page_size": int(page_size),
        "max_scan": int(max_scan),
        "next_page": 1,
        "scanned": 0,
        "pages": 0,
        "reviews": [],
        "seen": [],
        "matched_without_star": 0,
        "product_title": "",
        "api_total": None,
        "api_total_page": None,
        "oldest": "",
        "newest": "",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "resume_count": 0,
        "tab_id": "",
        "tab_owned": False,
        "errors": [],
        "validate_url": "",
        "validate_at": "",
    }


def restore_state(raw: dict[str, Any], item_id: str) -> dict[str, Any]:
    """把断点恢复成内存状态；商品不匹配或版本不认识时回退为全新任务。"""
    if not isinstance(raw, dict) or raw.get("version") != CHECKPOINT_VERSION:
        raise ValueError("断点版本不兼容")
    if str(raw.get("product_id") or "") != str(item_id):
        raise ValueError("断点的商品 ID 与当前任务不一致")
    state = new_state(
        item_id,
        str(raw.get("product_url") or ""),
        raw.get("scope") or {},
        int(raw.get("page_size") or 50),
        int(raw.get("max_scan") or 500),
    )
    for key in (
        "next_page",
        "scanned",
        "pages",
        "matched_without_star",
        "product_title",
        "api_total",
        "api_total_page",
        "oldest",
        "newest",
        "started_at",
        "resume_count",
        "tab_id",
        "tab_owned",
        "validate_url",
        "validate_at",
    ):
        if key in raw and raw[key] is not None:
            state[key] = raw[key]
    state["reviews"] = [r for r in (raw.get("reviews") or []) if isinstance(r, dict)]
    state["seen"] = [str(s) for s in (raw.get("seen") or []) if s]
    state["errors"] = [str(e) for e in (raw.get("errors") or [])]
    return state


def snapshot(state: dict[str, Any]) -> dict[str, Any]:
    return {
        **state,
        "reviews": list(state.get("reviews") or []),
        "seen": list(state.get("seen") or []),
        "errors": list(state.get("errors") or []),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


# ---------- 页面检查 ----------

async def read_page_state(cdp: Any) -> dict[str, Any]:
    value = await cdp.eval(PAGE_STATE_JS)
    return value if isinstance(value, dict) else {}


async def wait_page_ready(cdp: Any, timeout: float = 15.0, interval: float = 0.5) -> dict[str, Any]:
    """等待商品主体就绪或资源加载结束，不要求所有子资源都完成。

    后台标签页首屏经常 3 秒内还是 interactive，直接判定「页面未加载」会把
    正常的商品页误报成需要人工介入。
    """
    deadline = time.monotonic() + max(1.0, float(timeout))
    state = await read_page_state(cdp)
    def checkable(value: dict[str, Any]) -> bool:
        # 登录/验证/下架应交给分类器立即说明，不必继续等不存在的商品主体。
        kind = classify_page(value, item_id_in_href(str(value.get("href") or "")) or "").get("kind")
        if kind in {"platform_validate", "login_required", "product_unavailable"}:
            return True
        ready = str(value.get("ready_state") or "")
        if ready not in {"complete", "interactive"}:
            return False
        signals_known = any(key in value for key in (
            "has_product_title", "has_product_price", "has_product_image", "has_purchase_action"))
        return product_content_ready(value) if signals_known else ready == "complete"

    while not checkable(state) and time.monotonic() < deadline:
        await asyncio.sleep(interval)
        state = await read_page_state(cdp)
    return state


async def navigate_product_if_needed(cdp: Any, item_id: str, detail_url: str) -> bool:
    """复用已经打开的同商品页面，重检/恢复不反复刷新其已加载内容。"""
    state = await read_page_state(cdp)
    href = str(state.get("href") or "")
    if is_product_host(href) and item_id_in_href(href) == item_id and not is_gate_href(href):
        return False
    await cdp.navigate(detail_url, wait_seconds=3.0)
    return True


async def probe_product_page(
    cdp: Any, item_id: str, ready_timeout: float | None = None
) -> dict[str, Any]:
    state = await wait_page_ready(
        cdp, timeout=float(os.getenv("TMALL_PAGE_READY_TIMEOUT", "15"))
        if ready_timeout is None
        else ready_timeout
    )
    return classify_page(state, item_id)


def _access_payload(status: str, page_no: int, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "page_no": int(page_no or 1),
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "detail": "",
        "reason_code": "",
        "count": 0,
        "product_match": None,
        "validate_url": "",
    }
    payload.update(extra)
    return payload


def public_access(access: dict[str, Any] | None) -> dict[str, Any]:
    """去掉不能落盘的原始响应，只留下状态字段。"""
    if not isinstance(access, dict):
        return _access_payload(ACCESS_NOT_CHECKED, 1, detail="尚未发起真实评价请求")
    return {key: value for key, value in access.items() if key != "payload"}


async def probe_rate_access(
    cdp: Any,
    item_id: str,
    page_no: int,
    page_size: int,
    detail_url: str,
) -> dict[str, Any]:
    """第二级检查：用一次真实评价请求确认「数据能不能读」。

    约定：
    - 只发一次请求，不重试、不循环、不等待冷却；
    - 成功时的原始响应会作为第一页数据交给 _run_pages 复用，
      这样"检查"和"采集"不会各请求一次；
    - 页面检查通过不等于这里通过，两者分开汇报。
    """
    try:
        payload = await tmall.request_page_with_retry(
            cdp=cdp,
            item_id=item_id,
            page_no=int(page_no),
            page_size=int(page_size),
            detail_url=detail_url,
            retries=1,
            validate_retries=0,
            validate_cooldown=0.0,
            manual_captcha=False,
            manual_captcha_wait=0.0,
            order_type="feedbackdate",
        )
    except Exception as exc:  # noqa: BLE001 - 检查失败不能把任务打挂
        name = type(exc).__name__
        text = str(exc)
        if "_m_h5_tk" in text:
            return _access_payload(
                ACCESS_LOGIN_REQUIRED,
                page_no,
                detail="浏览器里没有 _m_h5_tk，登录态不可用",
            )
        if _is_connection_lost(exc):
            return _access_payload(
                ACCESS_BROWSER_ERROR, page_no, detail=f"{name}: {text}"[:160]
            )
        if _is_page_timeout(exc):
            # 页面没回应不等于浏览器断了：明确区分，避免让用户白重启一次
            return _access_payload(
                ACCESS_PAGE_TIMEOUT,
                page_no,
                detail=f"{name}: 页面在时限内没有回应",
            )
        return _access_payload(ACCESS_UNKNOWN, page_no, detail=f"{name}: {text}"[:160])

    if tmall.is_success(payload):
        data = payload.get("data") or {}
        rate_list = data.get("rateList") if isinstance(data, dict) else None
        rate_list = rate_list if isinstance(rate_list, list) else []
        product_match: bool | None = None
        foreign_id = ""
        for rate in rate_list:
            if not isinstance(rate, dict):
                continue
            raw_id = str(rate.get("auctionNumId") or "")
            if raw_id:
                product_match = raw_id == str(item_id)
                if not product_match:
                    foreign_id = raw_id
                break
        if product_match is False:
            return _access_payload(
                ACCESS_UNKNOWN,
                page_no,
                count=len(rate_list),
                product_match=False,
                reason_code="product_mismatch",
                detail=f"接口返回的评价属于商品 {foreign_id}，与任务商品 {item_id} 不一致",
            )
        return _access_payload(
            ACCESS_READY,
            page_no,
            payload=payload,
            count=len(rate_list),
            product_match=product_match,
            detail="接口已能正常返回评价数据"
            if rate_list
            else "接口返回成功，但该页没有评价",
        )

    if tmall.is_validate_error(payload):
        return _access_payload(
            ACCESS_STILL_BLOCKED,
            page_no,
            reason_code="validate",
            validate_url=tmall.get_validate_url(payload),
            detail="评价接口仍返回 FAIL_SYS_USER_VALIDATE",
        )
    if is_traffic_error(payload):
        return _access_payload(
            ACCESS_STILL_BLOCKED,
            page_no,
            reason_code="traffic",
            detail="评价接口仍在限流",
        )
    return _access_payload(
        ACCESS_UNKNOWN,
        page_no,
        reason_code="api_error",
        detail=(tmall.ret_text(payload) or "接口返回非预期结果")[:200],
    )


def _access_message(access: dict[str, Any]) -> str:
    """用户可见的一句话：数据到底能不能读。"""
    status = access.get("status")
    page_no = int(access.get("page_no") or 1)
    if status == ACCESS_READY:
        return f"数据访问已确认，正在从第 {page_no} 页继续采集…"
    if status == ACCESS_STILL_BLOCKED:
        if access.get("reason_code") == "validate":
            return "页面已恢复，但评价接口仍要求人工验证；本次未写入任何数据，断点保持不变。"
        if access.get("reason_code") == "traffic":
            return "评价接口仍在限流；本次未写入任何数据，断点保持不变。"
        return "评价接口仍返回异常；本次未写入任何数据，断点保持不变。"
    if status == ACCESS_LOGIN_REQUIRED:
        return "调试浏览器的登录状态无效，需要在采集浏览器里重新登录。"
    if status == ACCESS_BROWSER_ERROR:
        return "调试浏览器连接中断，断点已保留。"
    if status == ACCESS_PAGE_TIMEOUT:
        return "商品页在时限内没有回应（浏览器仍在运行），稍后可重试同一页。"
    return "无法确认数据访问状态（现有证据不足），将按正常流程再试一次。"


def _access_block(
    access: dict[str, Any],
) -> tuple[str, str, str] | None:
    """把数据访问状态映射成 (采集状态, 介入类型, 停止原因)；None 表示可以继续。"""
    status = access.get("status")
    reason = access.get("reason_code")
    page_no = int(access.get("page_no") or 1)
    if status == ACCESS_STILL_BLOCKED:
        if reason == "validate":
            return (
                "awaiting_user",
                "platform_validate",
                f"页面正常，但第 {page_no} 页的评价接口仍要求人工验证（RGV587）",
            )
        if reason == "traffic":
            return ("rate_limited", "rate_limited", "评价接口持续限流，已停止自动重试")
        return ("failed", "api_error", "评价接口返回异常")
    if status == ACCESS_LOGIN_REQUIRED:
        return ("awaiting_user", "login_required", "调试浏览器的登录状态无效")
    if status == ACCESS_BROWSER_ERROR:
        return ("awaiting_user", "browser_disconnected", "调试浏览器连接中断")
    if status == ACCESS_PAGE_TIMEOUT:
        return (
            "awaiting_user",
            "page_timeout",
            f"商品页在第 {page_no} 页没有在时限内回应",
        )
    if status == ACCESS_UNKNOWN and reason == "product_mismatch":
        return ("failed", "unknown", "接口返回的评价不属于当前商品，已停止采集")
    return None


def validate_checkpoint(
    checkpoint: dict[str, Any] | None,
    product_id: str,
) -> dict[str, Any]:
    """恢复前校验断点结构与数据一致性；只读，不改动断点文件。"""
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "", blocking: bool = True) -> bool:
        checks.append(
            {"name": name, "ok": bool(ok), "detail": detail, "blocking": bool(blocking)}
        )
        return bool(ok)

    def fail(reason: str) -> dict[str, Any]:
        return {"ok": False, "checks": checks, "reason": reason}

    if not isinstance(checkpoint, dict) or not checkpoint:
        add("断点文件", False, "断点不存在或无法解析")
        return fail("断点文件不存在或无法解析")

    add("断点文件可读", True, f"{len(checkpoint)} 个字段")
    if not add(
        "断点版本",
        checkpoint.get("version") == CHECKPOINT_VERSION,
        f"version={checkpoint.get('version')}",
    ):
        return fail("断点版本与当前程序不兼容")

    cp_pid = str(checkpoint.get("product_id") or "")
    if not add("商品 ID 一致", cp_pid == str(product_id), f"断点={cp_pid} 任务={product_id}"):
        return fail(f"断点属于商品 {cp_pid}，与任务商品 {product_id} 不一致")

    try:
        next_page = int(checkpoint.get("next_page"))
    except (TypeError, ValueError):
        next_page = 0
    total_page = checkpoint.get("api_total_page")
    page_detail = f"next_page={next_page}"
    page_ok = 1 <= next_page <= MAX_REASONABLE_PAGE
    if page_ok and isinstance(total_page, int) and total_page > 0:
        page_ok = next_page <= total_page + 1
        page_detail += f"，api_total_page={total_page}"
    if not add("页码合法", page_ok, page_detail):
        return fail(f"断点页码不合法（{page_detail}）")

    try:
        pages = int(checkpoint.get("pages") or 0)
        scanned = int(checkpoint.get("scanned") or 0)
    except (TypeError, ValueError):
        pages, scanned = -1, -1
    if not add("已完成页数与页码一致", 0 <= pages <= next_page - 1, f"pages={pages}", blocking=False):
        # 页数只是展示计数，不影响"不重复写入"，所以只提示、不阻断恢复
        pass
    if not add("扫描量为非负数", scanned >= 0, f"scanned={scanned}"):
        return fail("扫描量不合法")

    reviews = checkpoint.get("reviews")
    seen = checkpoint.get("seen")
    reviews = reviews if isinstance(reviews, list) else []
    seen = seen if isinstance(seen, list) else []
    seen_set = {str(item) for item in seen if item}
    review_ids = [str((item or {}).get("review_id") or "") for item in reviews if isinstance(item, dict)]
    add("已命中评价结构完整", len(review_ids) == len(reviews), f"reviews={len(reviews)}")
    if not add("评价 ID 不重复", len(set(review_ids)) == len(review_ids), f"unique={len(set(review_ids))}"):
        return fail("断点里的评价 ID 有重复，可能导致重复写入")
    missing = [rid for rid in review_ids if rid and rid not in seen_set]
    if not add("已命中评价都在去重集合里", not missing, f"缺失 {len(missing)} 条"):
        return fail("断点的评价与去重集合不一致，无法保证不重复写入")
    if not add("去重集合覆盖扫描量", len(seen_set) >= scanned, f"seen={len(seen_set)} scanned={scanned}"):
        return fail("断点去重集合小于已扫描数量，继续会重复计数")

    ok = all(item["ok"] for item in checks if item.get("blocking"))
    return {
        "ok": ok,
        "checks": checks,
        "reason": "" if ok else "断点校验未通过",
        "next_page": next_page,
        "scanned": scanned,
        "reviews": len(reviews),
    }


async def _open_tab(tab_id: str, background: bool) -> tuple[Any, str, bool]:
    """返回 (tab, tab_id, owned)。

    优先复用暂停时留下的标签页。owned 只表示「本次调用新建的标签页」，
    复用的标签页不归本程序所有：采集中断/完成后都不关，避免把用户正在
    操作的验证页或登录页一起关掉。
    """
    tab = await asyncio.to_thread(find_tab, tab_id) if tab_id else None
    if tab:
        return tab, str(tab.get("id") or ""), False
    if background:
        try:
            tab = await create_tab("about:blank", background=True)
            return tab, str(tab.get("id") or ""), True
        except Exception:  # noqa: BLE001 - 回退到已有页面
            pass
    return None, "", False


def _ws_url_of(tab: Any) -> str:
    if tab:
        url = tab.get("webSocketDebuggerUrl")
        if url:
            return str(url)
    tmall.CDP_HOST = get_cdp_host()
    return tmall.get_page_ws_url()


def _tab_id_of_ws(ws_url: str) -> str:
    for page in list_tabs():
        if page.get("webSocketDebuggerUrl") == ws_url:
            return str(page.get("id") or "")
    return ""


async def _close_tab(tab_id: str) -> None:
    if not tab_id:
        return
    from cdp_tabs import close_tab

    await close_tab(tab_id)


async def _sleep_step(delay: float, cancel_check: Callable[[], bool] | None) -> bool:
    """分段等待，期间响应取消；返回 True 表示被取消。"""
    remaining = max(0.0, float(delay))
    while remaining > 0:
        if cancel_check is not None and cancel_check():
            return True
        step = min(0.5, remaining)
        await asyncio.sleep(step)
        remaining -= step
    return bool(cancel_check and cancel_check())


# ---------- 单页循环 ----------

async def _request_with_backoff(
    cdp: Any,
    item_id: str,
    page_no: int,
    page_size: int,
    detail_url: str,
    emit: Callable[[str, str], None],
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for attempt in range(3):
        payload = await tmall.request_page_with_retry(
            cdp=cdp,
            item_id=item_id,
            page_no=page_no,
            page_size=page_size,
            detail_url=detail_url,
            retries=3,
            validate_retries=0,
            validate_cooldown=180.0,
            manual_captcha=False,
            manual_captcha_wait=0.0,
            order_type="feedbackdate",
        )
        if tmall.is_success(payload) or not is_traffic_error(payload):
            return payload
        if cancel_check is not None and cancel_check():
            return payload
        wait = 20 * (attempt + 1)
        emit("backoff", f"触发限流，等待 {wait} 秒后重试第 {page_no} 页…")
        for _ in range(wait):
            if cancel_check is not None and cancel_check():
                return payload
            await asyncio.sleep(1)
    return payload


def _is_connection_lost(exc: BaseException) -> bool:
    """只有「浏览器真的不在了」才算断线。

    CDP 的 call() 在等不到响应时抛的是 TimeoutError，页面可能只是卡住，
    浏览器进程和 WebSocket 都还活着。把它算成断线会让用户白重启一次浏览器。
    """
    if isinstance(exc, (ConnectionRefusedError, ConnectionResetError, BrokenPipeError)):
        return True
    name = type(exc).__name__.lower()
    if isinstance(exc, ConnectionError) and "timeout" not in name:
        return True
    return any(
        token in name
        for token in (
            "connectionclosed",
            "connectionrefused",
            "connectionreset",
            "websocketclosed",
            "incompleteread",
            "invalidhandshake",
        )
    )


def _is_page_timeout(exc: BaseException) -> bool:
    """CDP 单次调用超时：页面/渲染进程没在时限内回应，连接本身可能还是好的。"""
    if isinstance(exc, TimeoutError):
        return True
    name = type(exc).__name__.lower()
    return "timeout" in name or "timedout" in name


def _classify_request_error(exc: BaseException) -> str:
    """把翻页请求异常分成三类，避免把超时误报成断线。"""
    if _is_connection_lost(exc):
        return "browser_disconnected"
    if _is_page_timeout(exc):
        return "page_timeout"
    return "failed"


async def _run_pages(
    cdp: Any,
    st: dict[str, Any],
    cutoff: datetime | None,
    min_delay: float,
    max_delay: float,
    emit: Callable[[str, str], None],
    on_checkpoint: Callable[[dict[str, Any]], None] | None,
    cancel_check: Callable[[], bool] | None,
    prime: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """按断点继续翻页，返回 (status, stop_reason)。"""
    item_id = st["product_id"]
    scope = st["scope"]
    page_size = int(st["page_size"])
    max_scan = int(st["max_scan"])
    detail_url = f"https://detail.tmall.com/item.htm?id={item_id}"
    seen = set(st["seen"])
    reviews: list[dict[str, Any]] = st["reviews"]
    matched_without_star = int(st["matched_without_star"])
    scanned = int(st["scanned"])
    pages = int(st["pages"])
    oldest = parse_date(st["oldest"])
    newest = parse_date(st["newest"])
    errors: list[str] = st["errors"]
    timeouts = 0

    def save_checkpoint() -> None:
        if on_checkpoint is None:
            return
        st.update(
            {
                "seen": list(seen),
                "reviews": reviews,
                "matched_without_star": matched_without_star,
                "scanned": scanned,
                "pages": pages,
                "oldest": oldest.strftime("%Y-%m-%d") if oldest else "",
                "newest": newest.strftime("%Y-%m-%d") if newest else "",
                "errors": errors,
            }
        )
        on_checkpoint(snapshot(st))

    while True:
        if cancel_check is not None and cancel_check():
            st.update({"scanned": scanned, "pages": pages})
            save_checkpoint()
            return "cancelled", "用户取消任务"

        page_no = int(st["next_page"])
        payload: dict[str, Any] | None = None
        if (
            prime
            and int(prime.get("page_no") or 0) == page_no
            and isinstance(prime.get("payload"), dict)
        ):
            # 数据可用性检查已经请求过这一页，直接复用，避免同一页请求两次
            payload = prime["payload"]
            prime = None
            emit("fetching", f"数据访问已确认，复用第 {page_no} 页的检查结果…")
        if payload is None:
            emit("fetching", f"正在读取第 {page_no} 页…")
            try:
                payload = await _request_with_backoff(
                    cdp, item_id, page_no, page_size, detail_url, emit, cancel_check
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"第 {page_no} 页请求异常：{type(exc).__name__}: {exc}"[:300])
                st.update({"scanned": scanned, "pages": pages, "errors": errors})
                save_checkpoint()
                kind = _classify_request_error(exc)
                if kind == "browser_disconnected":
                    # 浏览器被关掉/进程退出：断点还在，属于可恢复障碍
                    return "browser_disconnected", f"调试浏览器连接中断：{type(exc).__name__}"
                if kind == "page_timeout":
                    # 页面没在时限内回应：同一页重试，不丢断点、不误报成断线
                    timeouts += 1
                    if timeouts <= MAX_PAGE_TIMEOUT_RETRIES:
                        errors[-1] = (
                            f"第 {page_no} 页响应超时（第 {timeouts} 次），稍后重试同一页"
                        )
                        st.update({"scanned": scanned, "pages": pages, "errors": errors})
                        save_checkpoint()
                        emit(
                            "waiting",
                            f"第 {page_no} 页响应超时，{PAGE_TIMEOUT_RETRY_DELAY:.0f} 秒后重试…",
                        )
                        await asyncio.sleep(PAGE_TIMEOUT_RETRY_DELAY)
                        continue
                    return (
                        "page_timeout",
                        f"第 {page_no} 页连续 {timeouts} 次响应超时，已停止自动重试",
                    )
                return "failed", f"请求异常：{type(exc).__name__}"

        if cancel_check is not None and cancel_check():
            save_checkpoint()
            return "cancelled", "用户取消任务"

        if not tmall.is_success(payload):
            st["next_page"] = page_no
            if is_traffic_error(payload):
                st.update({"scanned": scanned, "pages": pages, "errors": errors})
                save_checkpoint()
                return "rate_limited", "接口持续限流，已停止自动重试"
            if tmall.is_validate_error(payload):
                st.update({"scanned": scanned, "pages": pages, "errors": errors})
                st["validate_url"] = tmall.get_validate_url(payload)
                save_checkpoint()
                return "awaiting_user", tmall.ret_text(payload) or "平台要求人工验证"
            st.update({"scanned": scanned, "pages": pages, "errors": errors})
            save_checkpoint()
            return "api_error", tmall.ret_text(payload) or "接口返回失败"

        data = payload.get("data") or {}
        rate_list = data.get("rateList") or []
        if data.get("total") is not None:
            try:
                st["api_total"] = int(data["total"])
            except (TypeError, ValueError):
                pass
        if data.get("totalPage") is not None:
            try:
                st["api_total_page"] = int(data["totalPage"])
            except (TypeError, ValueError):
                pass
        if not isinstance(rate_list, list) or not rate_list:
            st.update({"scanned": scanned, "pages": pages})
            save_checkpoint()
            if pages == 0:
                return "empty", "第 1 页未返回评价：可能无评价或访问受限"
            return "success", "接口已无更多评价"

        reached_cutoff = False
        for rate in rate_list:
            review_id = str(rate.get("id") or "")
            if not review_id or review_id in seen:
                continue
            seen.add(review_id)
            review_date = parse_date(rate.get("feedbackDate"))

            if scope.get("mode") == "count":
                if scanned >= int(scope.get("count") or 0):
                    break
            else:
                if review_date is not None and cutoff is not None and review_date < cutoff:
                    reached_cutoff = True
                    break

            scanned += 1
            if review_date:
                if newest is None or review_date > newest:
                    newest = review_date
                if oldest is None or review_date < oldest:
                    oldest = review_date
            if not st.get("product_title"):
                st["product_title"] = clean_text(rate.get("auctionTitle"))

            star = star_of(rate)
            rate_type = str(rate.get("rateType") or "")
            if star is not None and star <= 3:
                reviews.append(simplify(rate))
            elif star is None and rate_type in {"0", "-1"}:
                matched_without_star += 1
                reviews.append(simplify(rate))

        pages += 1
        st.update(
            {
                "next_page": page_no + 1,
                "scanned": scanned,
                "pages": pages,
                "matched_without_star": matched_without_star,
                "oldest": oldest.strftime("%Y-%m-%d") if oldest else "",
                "newest": newest.strftime("%Y-%m-%d") if newest else "",
                "errors": errors,
            }
        )
        st["reviews"] = reviews
        st["seen"] = list(seen)
        emit(
            "page_done",
            f"第 {page_no} 页完成：累计扫描 {scanned} 条，命中 {len(reviews)} 条",
        )
        save_checkpoint()

        if scope.get("mode") == "count" and scanned >= int(scope.get("count") or 0):
            return "success", f"已扫描最近 {scope['count']} 条评价"
        if scope.get("mode") == "time" and reached_cutoff:
            return "success", f"已超出时间范围（早于 {scope.get('cutoff')}）"
        if data.get("hasNext") is False:
            return "success", "接口已无更多评价"
        if st.get("api_total_page") and page_no >= int(st["api_total_page"]):
            return "success", "已到评价末页"
        if scanned >= max_scan:
            return "partial", f"达到扫描上限 {max_scan} 条"

        delay = random.uniform(min_delay, max_delay)
        emit("waiting", f"等待 {delay:.0f} 秒后读取第 {page_no + 1} 页…")
        if await _sleep_step(delay, cancel_check):
            save_checkpoint()
            return "cancelled", "用户取消任务"


# ---------- 对外接口 ----------

async def _peek_href(ws_url: str) -> str:
    """只读当前标签页地址，不导航。"""
    if not ws_url:
        return ""
    try:
        async with tmall.CDPClient(ws_url) as cdp:
            await cdp.call("Runtime.enable")
            return str(await cdp.eval("location.href") or "")
    except Exception:  # noqa: BLE001
        return ""


async def inspect_product_page(
    product_url: str,
    tab_id: str = "",
    background: bool = True,
) -> dict[str, Any]:
    """只检查页面，不采集；用于「重新检查页面」按钮。"""
    try:
        item_id, detail_url = tmall.normalize_detail_url(product_url)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "kind": "page_unexpected",
            "detail": f"无法解析商品链接：{exc}",
            "checks": [{"name": "商品链接", "ok": False, "detail": str(exc)[:120]}],
            "tab_id": "",
            "page": {},
        }

    checks: list[dict[str, Any]] = []
    try:
        import requests

        session = requests.Session()
        session.trust_env = False
        host = get_cdp_host()
        session.get(f"{host}/json/version", timeout=3).raise_for_status()
        checks.append({"name": "调试浏览器连接", "ok": True, "detail": f"{host} 可用"})
    except Exception as exc:  # noqa: BLE001
        checks.append({"name": "调试浏览器连接", "ok": False, "detail": f"{type(exc).__name__}"})
        verdict = build_intervention("browser_disconnected", item_id)
        return {
            "ok": False,
            "kind": "browser_disconnected",
            "detail": verdict["reason"],
            "checks": checks,
            "tab_id": tab_id,
            "page": {},
        }

    tab, current_tab_id, owned = await _open_tab(tab_id, background)
    reused_tab = bool(tab_id and tab and str(tab.get("id") or "") == str(tab_id))
    checks.append(
        {
            "name": "采集标签页",
            "ok": bool(tab),
            "detail": "复用已有标签页" if tab and tab_id else ("新建后台标签页" if tab else "无法创建标签页"),
        }
    )

    # 暂停时保留的标签页可能正是用户正在拖滑块的验证页。
    # 直接导航它会把验证现场冲掉，所以另开一个临时页面做检查。
    preserve_tab_id = ""
    if reused_tab:
        href_before = await _peek_href(str(tab.get("webSocketDebuggerUrl") or ""))
        gate_kind = is_gate_href(href_before)
        if gate_kind:
            preserve_tab_id = current_tab_id
            checks.append(
                {
                    "name": "保留验证/登录页面",
                    "ok": True,
                    "detail": "当前标签页停在验证/登录页，检查改在临时页面进行，不会打断你的操作",
                }
            )
            try:
                fresh = await create_tab("about:blank", background=True)
            except Exception:  # noqa: BLE001
                fresh = None
            if fresh:
                tab = fresh
                current_tab_id = str(fresh.get("id") or "")
                owned = True
                reused_tab = False
            else:
                preserve_tab_id = ""
                checks.append(
                    {
                        "name": "另开检查页面",
                        "ok": False,
                        "detail": "无法新建标签页，本次检查会导航当前页面",
                    }
                )

    ws_url = _ws_url_of(tab)
    try:
        async with tmall.CDPClient(ws_url) as cdp:
            await cdp.call("Runtime.enable")
            await cdp.call("Page.enable")
            await navigate_product_if_needed(cdp, item_id, detail_url)
            state = await wait_page_ready(cdp)
            verdict = classify_page(state, item_id)
    except Exception as exc:  # noqa: BLE001
        if owned and current_tab_id:
            await _close_tab(current_tab_id)
        checks.append({"name": "打开商品页", "ok": False, "detail": f"{type(exc).__name__}: {exc}"})
        return {
            "ok": False,
            "kind": "browser_disconnected",
            "detail": "无法连接到调试浏览器页面",
            "checks": checks,
            "tab_id": preserve_tab_id,
            "page": {},
        }

    page = {
        "href": str(state.get("href") or ""),
        "title": str(state.get("title") or ""),
        "has_comment_dom": bool(state.get("has_comment_dom")),
    }
    checks.append({"name": "打开商品页", "ok": bool(page["href"]), "detail": page["href"][:120]})
    found_id = item_id_in_href(page["href"])
    id_ok = found_id == item_id if found_id else verdict["kind"] not in {
        "wrong_product",
        "page_unexpected",
    }
    checks.append(
        {
            "name": "商品 ID 一致",
            "ok": id_ok,
            "detail": f"页面 id={found_id}" if found_id else "页面地址里没有商品 ID",
        }
    )
    checks.append(
        {
            "name": "页面可继续采集",
            "ok": bool(verdict["ok"]),
            "detail": verdict.get("detail") or "页面状态正常",
        }
    )
    checks.append(
        {
            "name": "评价区域",
            "ok": bool(page["has_comment_dom"]),
            "detail": "页面存在评价区域" if page["has_comment_dom"] else "页面未渲染评价区域（接口仍可能可用）",
        }
    )

    if preserve_tab_id:
        # 关掉临时检查页，保留用户的验证页，并把任务标签页指回它
        if owned and current_tab_id:
            await _close_tab(current_tab_id)
        current_tab_id = preserve_tab_id
        owned = False
        close_temp = False
    else:
        close_temp = bool(verdict["ok"] and owned and current_tab_id and not reused_tab)

    if close_temp:
        # 只关这次检查临时新建的标签页；暂停时保留的页面（用户可能刚在上面完成验证）不能关
        await _close_tab(current_tab_id)
        current_tab_id = ""

    return {
        "ok": bool(verdict["ok"]),
        "kind": verdict["kind"],
        "detail": verdict.get("detail", ""),
        "checks": checks,
        "tab_id": current_tab_id,
        "page": page,
    }


def _result(
    *,
    product_url: str,
    item_id: str,
    product_info: dict[str, Any],
    st: dict[str, Any],
    status: str,
    stop_reason: str,
    started_at: datetime,
    page_size: int,
    matched_without_star: int,
    pages: int,
    intervention: dict[str, Any] | None,
    review_dom_present: bool,
    access: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reviews = st["reviews"]
    sufficient = len(reviews) >= MIN_RECOMMENDED_REVIEWS
    if status == "success" and not sufficient:
        status = "partial"
        if "样本不足" not in stop_reason:
            stop_reason = f"{stop_reason}；有效低星评价仅 {len(reviews)} 条，样本不足"

    finished_at = datetime.now()
    info: dict[str, Any] = {
        "status": status,
        "is_complete": status == "success",
        "stop_reason": stop_reason,
        "scanned_count": int(st["scanned"]),
        "matched_count": len(reviews),
        "matched_without_star": int(matched_without_star),
        "pages_fetched": int(pages),
        "page_size": int(page_size),
        "api_total": st.get("api_total"),
        "api_total_page": st.get("api_total_page"),
        "oldest_scanned_date": st.get("oldest") or "",
        "newest_scanned_date": st.get("newest") or "",
        "started_at": started_at.strftime("%Y-%m-%dT%H:%M:%S"),
        "finished_at": finished_at.strftime("%Y-%m-%dT%H:%M:%S"),
        "resume_count": int(st.get("resume_count") or 0),
        "next_page": int(st.get("next_page") or 1),
        "review_dom_present": review_dom_present,
        "tab_id": str(st.get("tab_id") or ""),
        "review_sufficiency": {
            "matched": len(reviews),
            "minimum_recommended": MIN_RECOMMENDED_REVIEWS,
            "sufficient": sufficient,
        },
        "errors": list(st.get("errors") or []),
        "validate_url": str(st.get("validate_url") or ""),
        "access": public_access(access),
        "product_info": product_info.get("collection_info", {}),
    }
    return {
        "platform": "tmall",
        "product_id": item_id,
        "product_url": product_url,
        "product": product_info.get("product") or {"id": item_id, "title": st.get("product_title", "")},
        "promotional_claims": product_info.get("promotional_claims", []),
        "filter": {
            "max_star": 3,
            "star_field": "extraInfoMap.userGrade",
            "include_category_only": True,
        },
        "scope": st["scope"],
        "reviews": reviews,
        "collection_info": info,
        "intervention": intervention,
    }


async def collect(
    product_url: str,
    mode: str,
    value: int,
    unit: str = "day",
    page_size: int = 50,
    max_scan: int = 500,
    min_delay: float | None = None,
    max_delay: float | None = None,
    background: bool | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    state: dict[str, Any] | None = None,
    on_checkpoint: Callable[[dict[str, Any]], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    item_id, detail_url = tmall.normalize_detail_url(product_url)
    scope = normalize_scope(mode, value, unit)
    cutoff = None
    if mode == "time":
        cutoff = datetime.now() - timedelta(days=scope["days"])
        scope["cutoff"] = cutoff.strftime("%Y-%m-%d")

    if background is None:
        background = os.getenv("TMALL_BACKGROUND", "1").strip().lower() not in {
            "0",
            "false",
            "no",
        }

    min_delay = float(os.getenv("TMALL_MIN_DELAY", "5")) if min_delay is None else min_delay
    max_delay = float(os.getenv("TMALL_MAX_DELAY", "8")) if max_delay is None else max_delay

    started_at = datetime.now()
    restored = False
    if state:
        # 断点版本不兼容或商品 ID 不一致时必须报错：静默从头采集会重复请求、
        # 也可能把别的商品数据混进来，比直接失败严重得多。
        st = restore_state(state, item_id)
        restored = True
        # 恢复次数写回采集状态，保证导出 JSON 与任务记录一致
        st["resume_count"] = int(st.get("resume_count") or 0) + 1
        try:
            started_at = datetime.strptime(
                st.get("started_at") or started_at.strftime("%Y-%m-%dT%H:%M:%S"),
                "%Y-%m-%dT%H:%M:%S",
            )
        except ValueError:
            started_at = datetime.now()
    else:
        st = new_state(item_id, product_url, scope, page_size, max_scan)
    st["scope"] = scope
    st["page_size"] = int(page_size)
    st["max_scan"] = int(max_scan)
    st["product_url"] = product_url

    status = "collecting"
    stop_reason = ""
    intervention: dict[str, Any] | None = None
    review_dom_present = False
    access: dict[str, Any] | None = None
    product_info: dict[str, Any] = {
        "product": {"id": item_id, "title": st.get("product_title", "")},
        "promotional_claims": [],
        "collection_info": {
            "ssr_ok": False,
            "source": "none",
            "main_image_count": 0,
            "detail_image_count": 0,
            "ocr_image_count": 0,
            "ocr_status": "skipped",
            "errors": [],
        },
    }

    def emit(stage: str, message: str) -> None:
        if progress is None:
            return
        payload: dict[str, Any] = {
            "stage": stage,
            "message": message,
            "scanned": int(st.get("scanned") or 0),
            "matched": len(st.get("reviews") or []),
            "pages": int(st.get("pages") or 0),
            "api_total": st.get("api_total"),
            "api_total_page": st.get("api_total_page"),
            "oldest_date": st.get("oldest") or "",
            "newest_date": st.get("newest") or "",
            "next_page": int(st.get("next_page") or 1),
            "resume_count": int(st.get("resume_count") or 0),
        }
        if access is not None:
            payload["access"] = public_access(access)
        progress(payload)

    # 详情图预取：与评价采集并行（用独立的后台标签页打开图文详情，
    # 不占用采集标签）。评价要跑几分钟，把下载 + OCR 藏在这段时间里，
    # 评价结束时商品信息阶段只剩秒级组装。
    detail_prepared: dict[str, Any] = {}
    detail_log: list[str] = []

    def _detail_worker() -> None:
        async def _scrape() -> tuple[list[str], str]:
            return await scrape_detail_page(tmall, item_id)

        try:
            urls, text = asyncio.run(_scrape())
            detail_log.append(f"详情页图片 {len(urls)} 张，后台下载与识别开始")
            result = finish_detail_images(
                item_id,
                urls,
                text,
                cancel_check=lambda: bool(cancel_check and cancel_check()),
                log=detail_log,
            )
            if result.get("ok"):
                detail_prepared["value"] = result
            else:
                detail_prepared["error"] = str(result.get("error") or "unknown")
        except Exception as exc:  # noqa: BLE001 - 预取失败不影响主流程
            detail_prepared["error"] = f"{type(exc).__name__}: {exc}"[:300]

    detail_thread = threading.Thread(target=_detail_worker, name="detail-prep", daemon=True)
    emit(
        "connecting",
        "继续上次进度，连接调试浏览器…" if restored else "连接调试浏览器…",
    )

    try:
        tab, tab_id, owned = await _open_tab(str(st.get("tab_id") or ""), background)
        ws_url = _ws_url_of(tab)
        # 浏览器确认可用后再启动详情图预取（独立后台标签页）
        detail_thread.start()
        emit("product", "详情图预取已在后台启动（与评价采集并行）…")
    except Exception as exc:  # noqa: BLE001 - 浏览器不可用时不抛给调用方
        st["errors"].append(f"调试浏览器不可用：{type(exc).__name__}: {exc}"[:300])
        return _result(
            product_url=product_url,
            item_id=item_id,
            product_info=product_info,
            st=st,
            status="awaiting_user",
            stop_reason="调试浏览器未连接",
            started_at=started_at,
            page_size=page_size,
            matched_without_star=int(st.get("matched_without_star") or 0),
            pages=int(st.get("pages") or 0),
            intervention=build_intervention(
                "browser_disconnected", item_id, detail=str(exc)[:200]
            ),
            review_dom_present=False,
            access=access,
        )
    st["tab_id"] = tab_id or _tab_id_of_ws(ws_url)
    st["tab_owned"] = bool(owned)
    if not tab:
        st["errors"].append("后台标签页不可用，已回退到现有页面")

    try:
        async with tmall.CDPClient(ws_url) as cdp:
            await cdp.call("Runtime.enable")
            await cdp.call("Network.enable")
            await cdp.call("Page.enable")
            emit("navigating", "打开商品页…")
            await navigate_product_if_needed(cdp, item_id, detail_url)

            verdict = await probe_product_page(cdp, item_id)
            review_dom_present = bool(
                (verdict.get("diagnostics") or {}).get("has_comment_dom")
            )
            if not verdict["ok"]:
                status = "awaiting_user"
                intervention = build_intervention(
                    verdict["kind"],
                    item_id,
                    detail=verdict.get("detail", ""),
                    diagnostics=verdict.get("diagnostics"),
                )
                stop_reason = intervention["reason"]
                st["next_page"] = int(st.get("next_page") or 1)
                # 记下卡住的页面地址：主讲人一眼能看出是验证码页、登录页还是别的拦截页
                if verdict.get("url"):
                    st["validate_url"] = str(verdict["url"])[:300]
                    st["validate_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                st["errors"].append(
                    f"页面检查未通过：{verdict.get('kind')} {verdict.get('detail') or ''}".strip()
                )
                if st["errors"] and on_checkpoint is not None:
                    on_checkpoint(snapshot(st))
            else:
                warnings = verdict.get("warnings") or []
                if warnings:
                    st["errors"].append("页面警告：" + "；".join(warnings)[:200])
                    emit("fetching", "；".join(warnings))

                prime: dict[str, Any] | None = None
                blocked = False
                if restored:
                    # 第二级检查：页面能打开 ≠ 评价接口能读。
                    # 这里只发一次真实请求，成功时把结果直接复用为当前页数据。
                    access = await probe_rate_access(
                        cdp,
                        item_id,
                        int(st["next_page"]),
                        int(st["page_size"]),
                        detail_url,
                    )
                    emit("access", _access_message(access))
                    if access.get("validate_url"):
                        st["validate_url"] = str(access["validate_url"])
                        st["validate_at"] = str(access.get("checked_at") or "")
                    hit = _access_block(access)
                    if hit is not None:
                        status, kind, stop_reason = hit
                        blocked = True
                        intervention = build_intervention(
                            kind,
                            item_id,
                            detail=str(access.get("detail") or ""),
                            diagnostics={
                                "access_status": access.get("status"),
                                "reason_code": access.get("reason_code"),
                                "page_no": access.get("page_no"),
                            },
                            extra={"next_page": int(st.get("next_page") or 1)},
                        )
                    elif access.get("status") == ACCESS_READY and access.get("payload"):
                        prime = {
                            "page_no": int(access.get("page_no") or 1),
                            "payload": access["payload"],
                        }

                if not blocked:
                    status, stop_reason = await _run_pages(
                        cdp,
                        st,
                        cutoff,
                        min_delay,
                        max_delay,
                        emit,
                        on_checkpoint,
                        cancel_check,
                        prime=prime,
                    )

                    if status == "browser_disconnected":
                        status = "awaiting_user"
                        intervention = build_intervention(
                            "browser_disconnected", item_id, detail=stop_reason
                        )

                    if status == "page_timeout":
                        # 页面超时不是断线：保留断点，让用户刷新页面后重试同一页
                        status = "awaiting_user"
                        intervention = build_intervention(
                            "page_timeout",
                            item_id,
                            detail=stop_reason,
                            extra={"next_page": int(st.get("next_page") or 1)},
                        )

                    if status == "empty":
                        if not review_dom_present:
                            status = "awaiting_user"
                            intervention = build_intervention(
                                "review_area_missing", item_id, detail=stop_reason
                            )
                        else:
                            status = "partial"

                    if (
                        status == "awaiting_user"
                        and stop_reason
                        and "RGV587" in stop_reason.upper()
                    ):
                        intervention = build_intervention(
                            "platform_validate",
                            item_id,
                            detail="评价接口返回 FAIL_SYS_USER_VALIDATE",
                            diagnostics={"raw": stop_reason[:300]},
                        )
                    elif status == "awaiting_user" and intervention is None:
                        intervention = build_intervention(
                            "platform_validate",
                            item_id,
                            detail="评价接口要求人工验证",
                        )

                    if status == "rate_limited":
                        # 限流是可等待的：保留断点，等用户确认后再继续，不当作永久失败
                        intervention = build_intervention(
                            "rate_limited", item_id, detail=stop_reason
                        )
                    elif status == "api_error":
                        status = "failed"
                        intervention = build_intervention(
                            "api_error", item_id, detail=stop_reason
                        )

                    if status in {"success", "partial"}:
                        emit("product", "评价采集完成，开始采集商品信息…")
                        try:
                            if detail_thread.is_alive():
                                emit("product", "等待后台详情图识别完成…")
                                # join 是阻塞调用，必须丢进线程，避免卡住事件循环
                                await asyncio.to_thread(detail_thread.join, 180)
                            prepared = detail_prepared.get("value")
                            if prepared:
                                emit("product",
                                     f"详情图已在后台识别：{prepared.get('detail_count', 0)} 张"
                                     f" / OCR {prepared.get('ocr_count', 0)} 张")
                            elif detail_prepared.get("error"):
                                emit("product", f"详情图预取未完成，回退主流程（{detail_prepared['error']}）")
                            product_info = await collect_product_info(
                                cdp,
                                tmall,
                                item_id,
                                prepared=prepared,
                                progress=lambda message: emit("product", message),
                            )
                            if not product_info["product"].get("title"):
                                product_info["product"]["title"] = st.get("product_title", "")
                        except Exception as exc:  # noqa: BLE001
                            product_info["collection_info"]["errors"].append(
                                f"商品信息：{type(exc).__name__}: {exc}"
                            )
                    elif status in {"awaiting_user", "rate_limited", "failed", "cancelled"}:
                        product_info["collection_info"]["errors"].append(
                            "评价采集未完成，跳过商品信息采集"
                        )
                else:
                    product_info["collection_info"]["errors"].append(
                        "评价采集未完成，跳过商品信息采集"
                    )
    except Exception as exc:  # noqa: BLE001
        st["errors"].append(f"采集异常：{type(exc).__name__}: {exc}"[:300])
        name = type(exc).__name__.lower()
        connection_lost = isinstance(exc, (ConnectionError, OSError, TimeoutError)) or any(
            token in name for token in ("websocket", "connection", "timeout", "closed")
        )
        if connection_lost and on_checkpoint is not None:
            # 浏览器关掉/进程退出属于可恢复障碍：断点还在，等用户重新打开浏览器
            status = "awaiting_user"
            stop_reason = "调试浏览器连接中断"
            intervention = build_intervention(
                "browser_disconnected", item_id, detail=str(exc)[:200]
            )
        else:
            status = "failed"
            stop_reason = f"采集异常：{type(exc).__name__}"
            intervention = build_intervention(
                "browser_disconnected" if connection_lost else "unknown",
                item_id,
                detail=str(exc)[:200],
            )
    finally:
        if owned and tab_id and status not in {"awaiting_user", "rate_limited"}:
            await _close_tab(tab_id)

    if status == "collecting":
        status = "success"

    result = _result(
        product_url=product_url,
        item_id=item_id,
        product_info=product_info,
        st=st,
        status=status,
        stop_reason=stop_reason or "采集结束",
        started_at=started_at,
        page_size=page_size,
        matched_without_star=int(st.get("matched_without_star") or 0),
        pages=int(st.get("pages") or 0),
        intervention=intervention,
        review_dom_present=review_dom_present,
        access=access,
    )
    if on_checkpoint is not None and status in {"awaiting_user", "rate_limited", "failed", "cancelled"}:
        on_checkpoint(snapshot(st))
    return result
