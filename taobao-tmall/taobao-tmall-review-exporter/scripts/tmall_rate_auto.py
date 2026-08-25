import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import pandas as pd
import requests
import websockets


CDP_HOST = "http://127.0.0.1:9222"
API_URL = "https://h5api.m.tmall.com/h5/mtop.taobao.rate.detaillist.get/6.0/"
API_NAME = "mtop.taobao.rate.detaillist.get"
APP_KEY = "12574478"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "runtime" / "reviews"


@dataclass
class CrawlResult:
    excel_file: Path
    jsonl_file: Path
    count: int
    status: str
    stop_reason: str
    next_page: int
    status_file: Path

    def __iter__(self):
        """兼容旧代码的 ``excel, jsonl, count = result`` 写法。"""
        yield self.excel_file
        yield self.jsonl_file
        yield self.count


def extract_item_id(value: str) -> str:
    """从天猫/淘宝商品链接里提取 id，也允许直接传商品 ID。"""
    value = value.strip()
    if re.fullmatch(r"\d{6,}", value):
        return value

    parsed = urlparse(value)
    qs = parse_qs(parsed.query)
    item_id = (qs.get("id") or qs.get("itemId") or qs.get("item_id") or [""])[0]
    if item_id and item_id.isdigit():
        return item_id

    match = re.search(r"(?:id|itemId|item_id)=(\d+)", value)
    if match:
        return match.group(1)

    raise ValueError(f"无法从链接中提取商品 ID：{value}")


def normalize_detail_url(item_or_url: str) -> tuple[str, str]:
    item_id = extract_item_id(item_or_url)
    return item_id, f"https://detail.tmall.com/item.htm?id={item_id}"


def compact_json(obj: dict[str, Any]) -> str:
    # data 字符串要同时用于 sign 和请求参数，不能签名用一份、请求又换一份。
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def calc_sign(token: str, t: str, app_key: str, data: str) -> str:
    return hashlib.md5(f"{token}&{t}&{app_key}&{data}".encode("utf-8")).hexdigest()


def build_data(
    item_id: str,
    page_no: int,
    page_size: int,
    order_type: str = "",
) -> str:
    # 全量评价口径：不要传 skuVids / foldFlag。
    # 诊断确认：传 foldFlag=0 会把这个商品限制到约 202 条；
    # 不传 foldFlag/skuVids 时返回 total=3000+、totalPage=62，可继续翻页。
    data_obj = {
        "showTrueCount": False,
        "auctionNumId": str(item_id),
        "pageNo": int(page_no),
        "pageSize": int(page_size),
        "rateType": "",
        "searchImpr": "-8",
        "orderType": order_type,
        "expression": "",
        "rateSrc": "pc_rate_list",
    }
    return compact_json(data_obj)


def local_get_json(url: str) -> Any:
    session = requests.Session()
    session.trust_env = False
    response = session.get(url, timeout=5)
    response.raise_for_status()
    return response.json()


def get_page_ws_url() -> str:
    targets = local_get_json(f"{CDP_HOST}/json/list")
    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        raise RuntimeError(
            "没有找到 Chrome 页面。请确认你当前 Chrome 已开启 --remote-debugging-port=9222。"
        )

    pages.sort(
        key=lambda t: (
            0
            if "detail.tmall.com" in t.get("url", "")
            else 1
            if "taobao.com" in t.get("url", "") or "tmall.com" in t.get("url", "")
            else 2
        )
    )
    page = pages[0]
    print(f"[CDP] 使用浏览器页面：{page.get('title')} | {page.get('url')}")
    return page["webSocketDebuggerUrl"]


class CDPClient:
    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.ws = None
        self.msg_id = 0

    async def __aenter__(self) -> "CDPClient":
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        self.ws = await websockets.connect(
            self.ws_url,
            max_size=50_000_000,
            proxy=None,
            ping_interval=None,
            ping_timeout=None,
        )
        return self

    async def __aexit__(self, *_args: Any) -> None:
        if self.ws:
            await self.ws.close()

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: int = 40,
    ) -> dict[str, Any]:
        if self.ws is None:
            raise RuntimeError("CDP websocket 未连接")

        self.msg_id += 1
        current_id = self.msg_id
        await self.ws.send(json.dumps({"id": current_id, "method": method, "params": params or {}}))

        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=max(0.1, deadline - time.time())))
            if msg.get("id") != current_id:
                continue
            if "error" in msg:
                raise RuntimeError(f"{method} failed: {msg['error']}")
            return msg.get("result", {})

        raise TimeoutError(method)

    async def eval(self, expression: str, await_promise: bool = False, timeout: int = 45) -> Any:
        result = await self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
                "userGesture": True,
            },
            timeout=timeout,
        )
        return result.get("result", {}).get("value")

    async def navigate(self, url: str, wait_seconds: float = 3.0) -> None:
        await self.call("Page.navigate", {"url": url})
        await asyncio.sleep(wait_seconds)
        title = await self.eval("document.title")
        href = await self.eval("location.href")
        print(f"[PAGE] {title} | {href}")

    async def wait_for_captcha_completion(self, max_wait: float, poll_interval: float = 2.0) -> bool:
        """等待人工验证完成；成功后立即返回，不再机械等待固定时长。"""
        deadline = time.time() + max_wait
        while time.time() < deadline:
            try:
                state = await self.eval(
                    "({href:location.href,title:document.title,text:(document.body?.innerText||'').slice(0,800)})"
                )
            except Exception:
                state = None
            if isinstance(state, dict):
                href = str(state.get("href") or "")
                text = f"{state.get('title') or ''} {state.get('text') or ''}"
                challenge_url = "_____tmd_____" in href or "/punish" in href
                success_text = "验证通过" in text or "验证成功" in text
                if not challenge_url or success_text:
                    print("[CAPTCHA] 检测到人工验证已完成，立即继续")
                    return True
            await asyncio.sleep(poll_interval)
        print(f"[CAPTCHA] 等待 {max_wait:.0f} 秒后仍未检测到验证完成，将回商品页测试")
        return False

    async def get_m_h5_token(self) -> str:
        result = await self.call(
            "Network.getCookies",
            {
                "urls": [
                    "https://h5api.m.tmall.com/",
                    "https://detail.tmall.com/",
                    "https://login.taobao.com/",
                    "https://taobao.com/",
                    "https://tmall.com/",
                ]
            },
        )

        # 关键：浏览器里可能同时有 .tmall.com 和 .taobao.com 两套 _m_h5_tk。
        # h5api.m.tmall.com 应优先使用 .tmall.com 的 token，否则容易 FAIL_SYS_ILLEGAL_ACCESS。
        token_cookie = ""
        fallback_token_cookie = ""
        for cookie in result.get("cookies", []):
            if cookie.get("name") != "_m_h5_tk":
                continue

            value = cookie.get("value") or ""
            domain = cookie.get("domain") or ""

            if not fallback_token_cookie:
                fallback_token_cookie = value

            if "tmall.com" in domain:
                token_cookie = value
                break

        if not token_cookie:
            token_cookie = fallback_token_cookie

        token = token_cookie.split("_", 1)[0] if "_" in token_cookie else token_cookie
        if not token:
            raise RuntimeError("当前浏览器没有 _m_h5_tk。请先在这个 Chrome 里登录淘宝/打开商品页。")
        return token

    async def request_rate_page(
        self,
        item_id: str,
        page_no: int,
        page_size: int,
        order_type: str = "",
    ) -> dict[str, Any]:
        token = await self.get_m_h5_token()
        data = build_data(
            item_id=item_id,
            page_no=page_no,
            page_size=page_size,
            order_type=order_type,
        )
        t = str(int(time.time() * 1000))
        sign = calc_sign(token, t, APP_KEY, data)
        callback = f"mtopjsonppcdetail_auto_{t}"

        params = [
            ("jsv", "2.7.5"),
            ("appKey", APP_KEY),
            ("t", t),
            ("sign", sign),
            ("_bx-login", "new"),
            ("api", API_NAME),
            ("v", "6.0"),
            ("isSec", "0"),
            ("ecode", "1"),
            ("timeout", "20000"),
            ("dataType", "jsonp"),
            ("valueType", "string"),
            ("type", "jsonp"),
            ("jsonpIncPrefix", "pcdetail"),
            ("callback", callback),
            ("data", data),
            ("_", t),
        ]
        url = API_URL + "?" + "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params)

        expression = f"""
new Promise((resolve) => {{
  const cb = {json.dumps(callback)};
  const s = document.createElement('script');
  let done = false;

  const cleanup = () => {{
    try {{ delete window[cb]; }} catch (e) {{}}
    try {{ s.remove(); }} catch (e) {{}}
  }};

  window[cb] = (data) => {{
    if (done) return;
    done = true;
    cleanup();
    resolve(JSON.stringify(data));
  }};

  s.onerror = () => {{
    if (done) return;
    done = true;
    cleanup();
    resolve(JSON.stringify({{ error: 'script_error' }}));
  }};

  s.src = {json.dumps(url)};
  document.head.appendChild(s);

  setTimeout(() => {{
    if (done) return;
    done = true;
    cleanup();
    resolve(JSON.stringify({{ error: 'timeout' }}));
  }}, 30000);
}})
"""
        raw = await self.eval(expression, await_promise=True, timeout=45)
        return json.loads(raw)


def ret_text(payload: dict[str, Any]) -> str:
    ret = payload.get("ret") or []
    return " ".join(ret) if isinstance(ret, list) else str(ret)


def is_success(payload: dict[str, Any]) -> bool:
    return "SUCCESS" in ret_text(payload)


def is_retryable_token_error(payload: dict[str, Any]) -> bool:
    text = json.dumps(payload, ensure_ascii=False)
    return (
        "TOKEN" in text
        or "ILLEGAL_ACCESS" in text
        or "非法请求" in text
        or "非法令牌" in text
    )


def is_validate_error(payload: dict[str, Any]) -> bool:
    text = json.dumps(payload, ensure_ascii=False)
    return "FAIL_SYS_USER_VALIDATE" in text or "RGV587" in text or "punish" in text


def get_validate_url(payload: dict[str, Any]) -> str:
    data = payload.get("data") or {}
    url = data.get("url") or data.get("h5url") or ""
    return url if isinstance(url, str) else ""


async def request_page_with_retry(
    cdp: CDPClient,
    item_id: str,
    page_no: int,
    page_size: int,
    detail_url: str,
    retries: int = 3,
    validate_retries: int = 0,
    validate_cooldown: float = 180.0,
    manual_captcha: bool = False,
    manual_captcha_wait: float = 180.0,
    order_type: str = "",
) -> dict[str, Any]:
    last_payload: dict[str, Any] = {}
    validate_seen = 0

    for attempt in range(1, retries + 1):
        payload = await cdp.request_rate_page(
            item_id=item_id,
            page_no=page_no,
            page_size=page_size,
            order_type=order_type,
        )
        last_payload = payload

        if is_success(payload):
            return payload

        if is_validate_error(payload):
            validate_seen += 1
            validate_url = get_validate_url(payload)

            if manual_captcha and validate_url:
                print(f"[CAPTCHA] 第 {page_no} 页触发验证码，已打开验证页，请在浏览器中手动完成。")
                print(f"[CAPTCHA] {validate_url}")
                await cdp.call("Page.bringToFront")
                await cdp.navigate(validate_url, wait_seconds=2.0)
                print(f"[CAPTCHA] 最多等待 {manual_captcha_wait:.0f} 秒；完成后会自动继续。")
                await cdp.wait_for_captcha_completion(manual_captcha_wait)
                print("[CAPTCHA] 回到商品页并重试当前页")
                await cdp.navigate(detail_url, wait_seconds=3.0)
                continue

            if validate_seen <= validate_retries:
                print(
                    f"[VALIDATE] 第 {page_no} 页触发淘宝风控，冷却 {validate_cooldown:.0f} 秒后重试 "
                    f"{validate_seen}/{validate_retries}"
                )
                await asyncio.sleep(validate_cooldown)
                continue
            # 不在底层抛异常：让上层保存断点并把商品放回重试队列。
            return payload

        if is_retryable_token_error(payload) and attempt < retries:
            print(f"[RETRY] 第 {page_no} 页 token/sign 状态刷新，重试 {attempt}/{retries}")
            await asyncio.sleep(1.0)
            continue

        return payload

    return last_payload


def flatten_rate(rate: dict[str, Any]) -> dict[str, Any]:
    return {
        "商品ID": rate.get("auctionNumId", ""),
        "商品标题": rate.get("auctionTitle", ""),
        "评价ID": rate.get("id", ""),
        "评价日期": rate.get("feedbackDate") or rate.get("createTime", ""),
        "评价内容": rate.get("feedback", ""),
        "完整SKU": rate.get("skuValueStr", ""),
        "SKU_ID": rate.get("skuId", ""),
        "买家昵称": rate.get("reduceUserNick") or rate.get("userNick") or "",
        "是否复购": str(rate.get("repeatBusiness", "")).lower() == "true",
        "商家回复": rate.get("reply", ""),
        "原始评价JSON": json.dumps(rate, ensure_ascii=False),
    }


async def crawl_from_url(
    item_or_url: str,
    max_reviews: int,
    page_size: int,
    start_page: int,
    min_delay: float,
    max_delay: float,
    out_dir: str,
    navigate: bool,
    validate_retries: int,
    validate_cooldown: float,
    manual_captcha: bool,
    manual_captcha_wait: float,
    resume_excel: str | None = None,
    order_type: str = "",
) -> CrawlResult:
    item_id, detail_url = normalize_detail_url(item_or_url)
    end_page = max(start_page, (max_reviews + page_size - 1) // page_size)

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    raw_rates: list[dict[str, Any]] = []
    if resume_excel:
        resume_path = Path(resume_excel)
        if resume_path.exists():
            resume_frame = pd.read_excel(resume_path)
            rows = resume_frame.to_dict(orient="records")
            for value in resume_frame.get("原始评价JSON", pd.Series(dtype=str)).dropna():
                try:
                    raw = json.loads(str(value))
                    if isinstance(raw, dict):
                        raw_rates.append(raw)
                except json.JSONDecodeError:
                    continue
            inferred_page = len(rows) // page_size + 1
            start_page = max(start_page, inferred_page)
            print(f"[RESUME] 已加载 {len(rows)} 条，从第 {start_page} 页继续：{resume_path}")

    seen_keys: set[str] = set()
    for index, row in enumerate(rows):
        key = str(row.get("评价ID") or f"resume-{index}")
        seen_keys.add(key)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    partial_excel_file = out_path / f"tmall_reviews_{item_id}_partial_{stamp}.xlsx"
    partial_jsonl_file = out_path / f"tmall_reviews_{item_id}_partial_{stamp}.jsonl"
    status_file = out_path / f"tmall_reviews_{item_id}_status_{stamp}.json"
    status = "running"
    stop_reason = ""
    next_page = start_page

    def save_partial() -> None:
        if not rows:
            return
        pd.DataFrame(rows).to_excel(partial_excel_file, index=False)
        with partial_jsonl_file.open("w", encoding="utf-8") as f:
            for rate in raw_rates:
                f.write(json.dumps(rate, ensure_ascii=False) + "\n")
        print(f"[PARTIAL] 已保存 {len(rows)} 条：{partial_excel_file}")

    async with CDPClient(get_page_ws_url()) as cdp:
        await cdp.call("Runtime.enable")
        await cdp.call("Network.enable")
        await cdp.call("Page.enable")

        if navigate:
            print(f"[OPEN] {detail_url}")
            await cdp.navigate(detail_url)

        for page_no in range(start_page, end_page + 1):
            if len(rows) >= max_reviews:
                status = "complete_limit"
                stop_reason = f"已达到上限 {max_reviews} 条"
                break

            print(f"[FETCH] item={item_id} page={page_no}/{end_page}")
            try:
                payload = await request_page_with_retry(
                    cdp=cdp,
                    item_id=item_id,
                    page_no=page_no,
                    page_size=page_size,
                    detail_url=detail_url,
                    validate_retries=validate_retries,
                    validate_cooldown=validate_cooldown,
                    manual_captcha=manual_captcha,
                    manual_captcha_wait=manual_captcha_wait,
                    order_type=order_type,
                )
            except Exception:
                save_partial()
                raise

            if not is_success(payload):
                print(f"[STOP] 第 {page_no} 页失败：{payload.get('ret')}")
                print(json.dumps(payload, ensure_ascii=False)[:500])
                status = "blocked_validate" if is_validate_error(payload) else "api_error"
                stop_reason = ret_text(payload)
                next_page = page_no
                break

            data = payload.get("data") or {}
            rate_list = data.get("rateList") or []
            if not isinstance(rate_list, list) or not rate_list:
                print(f"[STOP] 第 {page_no} 页没有 rateList")
                status = "complete_all"
                stop_reason = "接口已无更多评价"
                next_page = page_no
                break

            unique_rates: list[dict[str, Any]] = []
            for rate in rate_list:
                key = str(rate.get("id") or json.dumps(rate, ensure_ascii=False, sort_keys=True))
                if key not in seen_keys:
                    seen_keys.add(key)
                    unique_rates.append(rate)

            remaining = max_reviews - len(rows)
            unique_rates = unique_rates[:remaining]
            raw_rates.extend(unique_rates)
            rows.extend(flatten_rate(rate) for rate in unique_rates)
            next_page = page_no + 1
            save_partial()

            print(
                f"[OK] page={page_no} got={len(unique_rates)} total_got={len(rows)} "
                f"total={data.get('total')} totalPage={data.get('totalPage')} hasNext={data.get('hasNext')}"
            )

            if data.get("hasNext") is False:
                print("[DONE] 已到最后一页")
                status = "complete_all"
                stop_reason = "接口标记 hasNext=false"
                break

            await asyncio.sleep(random.uniform(min_delay, max_delay))

    if status == "running":
        if len(rows) >= max_reviews:
            status = "complete_limit"
            stop_reason = f"已达到上限 {max_reviews} 条"
        else:
            status = "partial"
            stop_reason = "本轮页码已执行完"

    excel_file = out_path / f"tmall_reviews_{item_id}_{len(rows)}条_{stamp}.xlsx"
    jsonl_file = out_path / f"tmall_reviews_{item_id}_{len(rows)}条_{stamp}.jsonl"

    pd.DataFrame(rows).to_excel(excel_file, index=False)
    with jsonl_file.open("w", encoding="utf-8") as f:
        for rate in raw_rates:
            f.write(json.dumps(rate, ensure_ascii=False) + "\n")

    status_payload = {
        "item_id": item_id,
        "status": status,
        "stop_reason": stop_reason,
        "count": len(rows),
        "next_page": next_page,
        "max_reviews": max_reviews,
        "excel_file": str(excel_file),
        "jsonl_file": str(jsonl_file),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    status_file.write_text(json.dumps(status_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return CrawlResult(
        excel_file=excel_file,
        jsonl_file=jsonl_file,
        count=len(rows),
        status=status,
        stop_reason=stop_reason,
        next_page=next_page,
        status_file=status_file,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="自动抓取淘宝/天猫商品评价，默认最多 1500 条。")
    parser.add_argument("url", help="商品链接，或直接传商品 ID")
    parser.add_argument("--max-reviews", type=int, default=1500, help="最多抓取评价条数，默认 1500")
    parser.add_argument("--page-size", type=int, default=50, help="每页条数，默认 50")
    parser.add_argument("--start-page", type=int, default=1, help="从第几页开始抓，默认 1")
    parser.add_argument("--min-delay", type=float, default=2.5, help="分页最小等待秒数")
    parser.add_argument("--max-delay", type=float, default=5.5, help="分页最大等待秒数")
    parser.add_argument("--validate-retries", type=int, default=0, help="遇到淘宝风控后的冷却重试次数")
    parser.add_argument("--validate-cooldown", type=float, default=180.0, help="每次风控冷却秒数")
    parser.add_argument(
        "--manual-captcha",
        action="store_true",
        help="遇到验证码时自动打开验证页，等待你手动完成后继续",
    )
    parser.add_argument(
        "--manual-captcha-wait",
        type=float,
        default=180.0,
        help="打开验证码页后等待你手动完成的秒数",
    )
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_OUTPUT_ROOT),
        help=f"输出目录，默认 {DEFAULT_OUTPUT_ROOT}",
    )
    parser.add_argument("--resume-excel", default=None, help="从已有单品 Excel 断点续抓")
    parser.add_argument(
        "--time-sort",
        action="store_true",
        help="按评价时间倒序抓取；对应天猫页面的‘时间排序’",
    )
    parser.add_argument(
        "--no-navigate",
        action="store_true",
        help="不自动打开商品页；用于当前浏览器已经在目标商品页时",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    excel_file, jsonl_file, count = asyncio.run(
        crawl_from_url(
            item_or_url=args.url,
            max_reviews=args.max_reviews,
            page_size=args.page_size,
            start_page=args.start_page,
            min_delay=args.min_delay,
            max_delay=args.max_delay,
            out_dir=args.out_dir,
            navigate=not args.no_navigate,
            validate_retries=args.validate_retries,
            validate_cooldown=args.validate_cooldown,
            manual_captcha=args.manual_captcha,
            manual_captcha_wait=args.manual_captcha_wait,
            resume_excel=args.resume_excel,
            order_type="feedbackdate" if args.time_sort else "",
        )
    )
    print(f"[DONE] 成功导出 {count} 条评价")
    print(f"[DONE] Excel: {excel_file}")
    print(f"[DONE] JSONL: {jsonl_file}")


if __name__ == "__main__":
    main()
