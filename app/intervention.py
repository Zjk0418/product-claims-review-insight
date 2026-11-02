"""人工介入：把采集障碍翻译成用户能执行的步骤，并在恢复前检查页面状态。

设计原则：
- 只对“用户确实能处理”的情况给出恢复路径（登录、平台验证、页面未加载、开错商品）；
- 程序异常、接口报错、限流等不伪装成需要登录，单独给重试建议；
- 所有判断都基于真实页面/接口状态，不根据单一错误字符串推断。
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any
from urllib.parse import parse_qs, urlsplit

# 只匹配明确的验证/challenge 标识，避免把正文里的“验证码”误判成拦截
CAPTCHA_HREF_MARKERS = ("_____tmd_____", "/punish", "punish", "captcha", "x5sec")
CAPTCHA_TITLE_MARKERS = ("验证码拦截", "安全验证", "滑动验证", "访问验证")
LOGIN_HREF_MARKERS = ("login.taobao.com", "login.tmall.com", "login.aliyun.com")
LOGIN_TEXT_MARKERS = ("亲，请登录", "扫码登录", "请先登录")
# 平台风控横幅：页面能打开、商品信息也在，但正文提示“访问异常”。
# 这类提示不直接阻断采集（数据来自接口），但要作为警告上报，接口报错时才按验证处理。
ACCESS_WARN_MARKERS = ("访问异常", "访问存在异常", "访问受阻", "被挤爆啦")
GONE_TEXT_MARKERS = (
    "宝贝不存在",
    "您查看的宝贝不存在",
    "商品已下架",
    "已下架",
    "页面不存在",
    "很抱歉，您查看的页面",
)

ITEM_ID_RE = re.compile(r"(?:id|itemId|item_id)=(\d+)")

GUIDES: dict[str, dict[str, Any]] = {
    "platform_validate": {
        "title": "平台要求人工验证",
        "reason": "淘宝/天猫对这个商品的评价接口返回了人机验证（RGV587），程序不会绕过验证，需要你在调试浏览器里手动完成一次。",
        "steps": [
            "点「打开采集浏览器」。它打开的是采集用的那个调试浏览器（与采集共用同一个登录态），不是你日常用的浏览器。",
            "如果看到滑块/点选验证，由你本人在页面里一次完成，中途不要刷新或换标签。",
            "如果页面提示「等待太久 / 验证已过期」，说明验证链接已失效：回到本页点「继续采集」，程序会重新请求一次并生成新链接，再点「打开采集浏览器」。",
            "如果打开后是商品 {product_id} 的详情页、没有验证入口，说明这次请求已不再要求验证，直接点「继续采集」即可。",
            "点「继续采集」后，程序会先用一次真实的评价请求确认接口是否解锁；仍被拦会保留断点并回到本页，不会写入任何数据。",
            "连续多次都失败时，先停手 1–2 小时再试（平台不公布限制时长），也可以导入历史采集 JSON 先做分析。",
        ],
        "can_resume": True,
    },
    "data_unverified": {
        "title": "页面已恢复，数据访问待确认",
        "reason": "浏览器和商品详情页都正常，但页面正常不代表评价接口已解锁。点「继续采集」会先发一次真实的评价请求来确认数据能否读取。",
        "steps": [
            "点「继续采集」，程序会对第 {next_page} 页发起一次真实请求，这就是数据可用性检查。",
            "如果返回正常，会直接把这一次的结果写入并继续往后翻页，不会重复请求同一页。",
            "如果平台仍要求验证，任务会回到等待状态，断点和已采集的评论都不会丢。",
        ],
        "can_resume": True,
    },
    "login_required": {
        "title": "调试浏览器未登录",
        "reason": "商品页被重定向到了淘宝登录页，当前调试浏览器没有可用的登录态。",
        "steps": [
            "点「打开采集浏览器」，在调试浏览器里用淘宝 App 扫码或账号登录。",
            "登录后确认能打开商品 {product_id} 的详情页。",
            "回到本页面点「我已完成，继续采集」。",
        ],
        "can_resume": True,
    },
    "page_not_loaded": {
        "title": "商品页没有正常加载",
        "reason": "页面还没有加载完成，或加载过程中被中断，无法确认当前页面状态。",
        "steps": [
            "点「打开采集浏览器」，等待页面完全加载。",
            "如果页面空白或报错，按一次 F5 刷新。",
            "确认能看到商品标题和评价区域后，回到本页面点「重新检查页面」。",
        ],
        "can_resume": True,
    },
    "page_unexpected": {
        "title": "当前页面不是商品详情页",
        "reason": "调试浏览器当前标签页不在商品详情页，可能被跳转到了首页、活动页或登录页。",
        "steps": [
            "点「打开采集浏览器」，确认地址栏里包含 id={product_id}。",
            "如果不是这个商品，请手动打开正确的商品链接。",
            "回到本页面点「重新检查页面」。",
        ],
        "can_resume": True,
    },
    "wrong_product": {
        "title": "当前页面是另一个商品",
        "reason": "页面上的商品 ID 与任务不一致，继续采集会把两个商品的评价混在一起。程序已停止写入，需要你确认页面。",
        "steps": [
            "点「打开采集浏览器」，检查地址栏的 id 是否为 {product_id}。",
            "不是的话，请在调试浏览器中打开本任务的商品链接。",
            "回到本页面点「重新检查页面」，确认商品 ID 一致后再继续。",
        ],
        "can_resume": True,
    },
    "product_unavailable": {
        "title": "商品页面不可用",
        "reason": "商品可能已下架、删除或链接失效，无法继续采集这个商品。",
        "steps": [
            "点「打开采集浏览器」确认页面提示。",
            "如果商品确实已失效，请点「取消任务」，换一个商品重新采集。",
            "也可以导入此前保存的采集 JSON 继续做分析。",
        ],
        "can_resume": False,
    },
    "review_area_missing": {
        "title": "看不到评价数据",
        "reason": "页面能打开，但评价接口没有返回数据，页面上也没有评价区域。可能是平台降级或评价被折叠。",
        "steps": [
            "点「打开采集浏览器」，确认页面上评价区域是否存在。",
            "若页面提示需要登录或验证，先完成登录/验证。",
            "回到本页面点「重新检查页面」；仍无数据时建议扩大扫描范围或换商品。",
        ],
        "can_resume": True,
    },
    "browser_disconnected": {
        "title": "调试浏览器已断开",
        "reason": "本次采集使用的独立浏览器没有响应，可能是窗口被关闭或进程退出。",
        "steps": [
            "确认本应用启动的独立采集浏览器是否仍在运行。不要用日常 Edge 窗口替代。",
            "浏览器恢复后，回到本页面点「重新检查页面」。",
            "已采集的进度和评论都保存在本地，不会丢失，恢复后会从断点继续。",
        ],
        "can_resume": True,
    },
    "page_timeout": {
        "title": "页面响应超时",
        "reason": "调试浏览器还开着，但商品页在第 {next_page} 页没有在时限内回应。这不是平台验证，也不代表浏览器坏了，重试同一页通常就能过去。",
        "steps": [
            "先点「打开采集浏览器」，看一下商品 {product_id} 的页面是否还在正常加载。",
            "如果页面卡住，按一次 F5 刷新，等页面完全加载完。",
            "回到本页面点「重新检查页面」，再点「我已完成，继续采集」，程序会重试第 {next_page} 页。",
            "如果连续多次都超时，先关掉调试浏览器里其他占用资源的标签页，再重新启动采集环境。",
        ],
        "can_resume": True,
    },
    "api_error": {
        "title": "评价接口返回异常",
        "reason": "接口返回了非验证类的错误，通常是登录态失效、签名刷新失败或商品状态变化。",
        "steps": [
            "点「重新检查页面」，确认商品页能正常打开。",
            "仍然失败时，先在调试浏览器里刷新一次商品页，再重试。",
            "连续失败请取消任务，稍后重新采集；已采集的进度会保留。",
        ],
        "can_resume": True,
    },
    "rate_limited": {
        "title": "请求过于频繁",
        "reason": "平台对评价接口做了限流。程序已经等待并重试过，继续硬重试只会延长限制时间。",
        "steps": [
            "先等待 10–30 分钟，不要反复点重试。",
            "期间可以在调试浏览器里正常浏览一下商品页。",
            "之后点「我已完成，继续采集」，程序会从当前页继续。",
        ],
        "can_resume": True,
    },
    "interrupted": {
        "title": "任务被中断",
        "reason": "本地服务或采集进程重启，任务停在半途。已采集的评论和页码都保存在断点里，不需要从头再来。",
        "steps": [
            "先确认本应用启动的独立采集浏览器仍在运行。",
            "点「重新检查页面」，确认商品 {product_id} 的详情页能正常打开。",
            "检查通过后点「我已完成，继续采集」，程序会从上次保存的页码继续。",
        ],
        "can_resume": True,
    },
    "unknown": {
        "title": "采集中断",
        "reason": "采集遇到了未归类的问题，需要先确认浏览器和页面状态。",
        "steps": [
            "点「重新检查页面」查看具体检查项。",
            "确认调试浏览器能正常打开该商品后，再点「我已完成，继续采集」。",
            "无法恢复时可以取消任务并导入历史 JSON 继续分析。",
        ],
        "can_resume": True,
    },
}


def item_id_in_href(href: str) -> str | None:
    try:
        params = parse_qs(urlsplit(str(href or "")).query, keep_blank_values=True)
    except ValueError:
        return None
    values = [value for key in ("id", "itemId", "item_id") for value in params.get(key, [])]
    return (values[0] if values and len(set(values)) == 1
            and re.fullmatch(r"[0-9]+", values[0]) else None)


def is_gate_href(href: str) -> str:
    """地址是否停留在验证页/登录页这类「人工操作页」。

    这类标签页属于用户正在操作的现场，检查商品页时不能把它导航走，
    否则滑块做到一半会被打断。返回 '' 表示不是人工操作页。
    """
    lowered = str(href or "").lower()
    if not lowered:
        return ""
    if any(marker in lowered for marker in CAPTCHA_HREF_MARKERS):
        return "platform_validate"
    if any(marker in lowered for marker in LOGIN_HREF_MARKERS):
        return "login_required"
    return ""


def guide_text(
    kind: str, expected_product_id: str = "", extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    base = GUIDES.get(kind) or GUIDES["unknown"]
    values: dict[str, Any] = {"product_id": str(expected_product_id or "")}
    if extra:
        values.update({key: str(value) for key, value in extra.items() if value is not None})
    return {
        "title": base["title"].format_map(defaultdict(str, values)),
        "reason": base["reason"].format_map(defaultdict(str, values)),
        "steps": [step.format_map(defaultdict(str, values)) for step in base["steps"]],
        "can_resume": bool(base["can_resume"]),
    }


def build(
    kind: str,
    expected_product_id: str = "",
    *,
    detail: str = "",
    diagnostics: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造前端可直接展示的介入说明。detail 只放可读的补充信息。"""
    payload = {
        "kind": kind,
        "expected_product_id": str(expected_product_id or ""),
        **guide_text(kind, expected_product_id, extra),
    }
    if detail:
        payload["detail"] = detail
    if diagnostics:
        payload["diagnostics"] = diagnostics
    return payload


def classify_page(state: dict[str, Any], expected_product_id: str) -> dict[str, Any]:
    """根据页面快照判断当前是否可继续采集。

    state 包含 href / title / text / ready_state 及可见商品核心内容信号。
    判断顺序：验证 → 登录 → 商品 ID → 失效 → 加载状态。
    """
    href = str(state.get("href") or "")
    title = str(state.get("title") or "")
    text = str(state.get("text") or "")
    ready_state = str(state.get("ready_state") or "complete")
    access_warning = any(marker in f"{title} {text}" for marker in ACCESS_WARN_MARKERS)
    diagnostics = {
        "href": href,
        "title": title,
        "ready_state": ready_state,
        "has_comment_dom": bool(state.get("has_comment_dom")),
        "product_content_ready": product_content_ready(state),
        "has_product_title": bool(state.get("has_product_title")),
        "has_product_price": bool(state.get("has_product_price")),
        "has_product_image": bool(state.get("has_product_image")),
        "has_purchase_action": bool(state.get("has_purchase_action")),
        "access_warning": access_warning,
        "text_sample": text[:200],
    }

    if not href:
        return {
            "ok": False,
            "kind": "browser_disconnected",
            "detail": "无法读取当前页面地址",
            "diagnostics": diagnostics,
        }

    lowered = href.lower()
    if bool(state.get("has_verification_overlay")) or any(marker in lowered for marker in CAPTCHA_HREF_MARKERS) or any(
        marker in title for marker in CAPTCHA_TITLE_MARKERS
    ):
        return {
            "ok": False,
            "kind": "platform_validate",
            "detail": "页面停留在验证/拦截页",
            "url": str(href)[:300],
            "diagnostics": diagnostics,
        }

    if bool(state.get("has_login_overlay")) or any(marker in lowered for marker in LOGIN_HREF_MARKERS) or (
        any(marker in text for marker in LOGIN_TEXT_MARKERS) and "id=" not in href
    ):
        return {
            "ok": False,
            "kind": "login_required",
            "detail": "页面被重定向到登录页",
            "url": str(href)[:300],
            "diagnostics": diagnostics,
        }

    if not is_product_host(href):
        return {"ok": False, "kind": "page_unexpected", "detail": "当前地址不在淘宝/天猫商品域名内", "diagnostics": diagnostics}

    found_id = item_id_in_href(href)
    if found_id and found_id != str(expected_product_id):
        return {
            "ok": False,
            "kind": "wrong_product",
            "detail": f"当前页面商品 ID 为 {found_id}，任务商品 ID 为 {expected_product_id}",
            "diagnostics": {**diagnostics, "found_product_id": found_id},
        }
    if not found_id:
        return {
            "ok": False,
            "kind": "page_unexpected",
            "detail": "当前页面地址里没有商品 ID",
            "diagnostics": diagnostics,
        }

    if any(marker in f"{title} {text}" for marker in GONE_TEXT_MARKERS):
        return {
            "ok": False,
            "kind": "product_unavailable",
            "detail": "页面提示商品不存在或已下架",
            "diagnostics": diagnostics,
        }

    # complete 表示所有子资源结束，并不等于商品主体才可用。
    # interactive 已完成DOM解析；主体已显示时可继续独立的评价接口探测。
    # 新快照对complete也检查空壳，旧断点没有这些字段则保留兼容行为。
    has_content_signals = any(key in state for key in (
        "has_product_title", "has_product_price", "has_product_image", "has_purchase_action"))
    content_ready = product_content_ready(state)
    if (ready_state not in {"complete", "interactive"}
            or ready_state == "interactive" and not content_ready
            or ready_state == "complete" and has_content_signals and not content_ready):
        return {
            "ok": False,
            "kind": "page_not_loaded",
            "detail": f"document.readyState = {ready_state}；尚未确认商品标题及价格、主图或购买控件",
            "diagnostics": diagnostics,
        }

    warnings = (
        ["页面出现「访问异常」提示（平台风控横幅），评价接口可能受限"] if access_warning else []
    )
    if ready_state == "interactive":
        warnings.append("商品主体已就绪，部分图片或脚本仍在加载；评价接口是否可读需另行确认")
    return {
        "ok": True,
        "kind": "ok",
        "detail": "",
        "warnings": warnings,
        "diagnostics": diagnostics,
    }


def is_product_host(href: str) -> bool:
    try:
        parsed = urlsplit(href)
        host = (parsed.hostname or "").lower()
        return parsed.scheme in {"http", "https"} and any(
            host == domain or host.endswith("." + domain) for domain in ("taobao.com", "tmall.com"))
    except ValueError:
        return False


def product_content_ready(state: dict[str, Any]) -> bool:
    """评价占位区域或浏览器标签标题本身不足以证明商品主体已出现。"""
    return bool(state.get("has_product_title") and (
        state.get("has_product_price") or state.get("has_product_image") and state.get("has_purchase_action")))
