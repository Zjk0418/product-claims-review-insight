from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import requests
import websockets


def _load_env_file(path: Path) -> None:
    """Load a small KEY=VALUE file without adding a third-party dependency."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def _require_settings(settings: dict[str, str]) -> None:
    missing = [name for name, value in settings.items() if not value]
    if missing:
        raise RuntimeError(
            "缺少京东项目飞书配置："
            + ", ".join(missing)
            + "。请复制 jd/.env.example 为 jd/.env 并填写实际值。"
        )


def _find_chrome_path() -> Path | None:
    candidates: list[Path] = []
    explicit = os.environ.get("JD_CHROME_PATH")
    if explicit:
        candidates.append(Path(explicit).expanduser())
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(variable)
        if root:
            candidates.append(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe")
    command_path = shutil.which("chrome.exe") or shutil.which("chrome")
    if command_path:
        candidates.append(Path(command_path))
    return next((path for path in candidates if path.exists()), None)


CDP_HOST = "http://127.0.0.1:9222"
CHROME_PATH = _find_chrome_path()
PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = PROJECT_ROOT / "runtime"
CHROME_PROFILE = RUNTIME_ROOT / "browser-profile"
FEISHU_API = "https://open.feishu.cn/open-apis"
APP_ID = os.environ.get("FEISHU_APP_ID", "").strip()
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "").strip()
BASE_TOKEN = os.environ.get("FEISHU_BASE_TOKEN", "").strip()
TABLE_ID = os.environ.get("FEISHU_TABLE_ID", "").strip()
VIEW_ID = os.environ.get("FEISHU_VIEW_ID", "").strip()
SPU_FIELD = "SPU"
DATE_FIELD = "日期"
COUNT_FIELD = "评价数"
RATE_FIELD = "好评率"


def _load_feishu_config() -> None:
    global APP_ID, APP_SECRET, BASE_TOKEN, TABLE_ID, VIEW_ID
    _load_env_file(PROJECT_ROOT / ".env")
    APP_ID = os.environ.get("FEISHU_APP_ID", "").strip()
    APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "").strip()
    BASE_TOKEN = os.environ.get("FEISHU_BASE_TOKEN", "").strip()
    TABLE_ID = os.environ.get("FEISHU_TABLE_ID", "").strip()
    VIEW_ID = os.environ.get("FEISHU_VIEW_ID", "").strip()

BATCH_SIZE = 20
TRANSIENT_RETRIES = 3
FEISHU_PAGE_SIZE = 500
FEISHU_WRITE_BATCH_SIZE = 500
BLOCKED_MARKERS = (
    "系统繁忙",
    "请稍后再试",
    "验证",
    "captcha",
    "risk",
    "no access",
)
BLOCKED_HTTP_STATUSES = {403, 418, 429}


class JDVerificationRequired(RuntimeError):
    """The active JD browser session needs manual risk verification."""


def _raise_if_verification_required(
    status: Any, text: str = "", final_url: str = "", error: str = ""
) -> None:
    try:
        status_code = int(status)
    except (TypeError, ValueError):
        status_code = 0
    evidence = " ".join((text, final_url, error)).lower()
    marker_found = any(marker.lower() in evidence for marker in BLOCKED_MARKERS)
    risk_url = any(part in final_url.lower() for part in ("risk_handler", "cfe.m.jd.com", "verify"))
    if status_code in BLOCKED_HTTP_STATUSES or marker_found or risk_url:
        raise JDVerificationRequired(
            "京东要求人工验证。请在调试 Chrome 中完成验证后，按同一日期重跑；"
            "脚本会复用 checkpoint，只补剩余 SPU。"
        )


@dataclass(frozen=True)
class TargetRecord:
    record_id: str
    spu: str
    dt: str


@dataclass(frozen=True)
class ReviewSummary:
    spu: str
    review_count: str
    good_rate: str
    source: str


class FeishuClient:
    """Minimal Feishu OpenAPI client using project-local environment settings."""

    def __init__(self) -> None:
        _load_feishu_config()
        _require_settings(
            {
                "FEISHU_APP_ID": APP_ID,
                "FEISHU_APP_SECRET": APP_SECRET,
                "FEISHU_BASE_TOKEN": BASE_TOKEN,
                "FEISHU_TABLE_ID": TABLE_ID,
                "FEISHU_VIEW_ID": VIEW_ID,
            }
        )
        self.session = requests.Session()
        self.session.trust_env = False
        self._tenant_access_token = ""
        self._token_expires_at = 0.0

    def _token(self, force_refresh: bool = False) -> str:
        if not force_refresh and self._tenant_access_token and time.time() < self._token_expires_at:
            return self._tenant_access_token
        response = self.session.post(
            f"{FEISHU_API}/auth/v3/tenant_access_token/internal/",
            json={"app_id": APP_ID, "app_secret": APP_SECRET},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0 or not payload.get("tenant_access_token"):
            raise RuntimeError(f"获取飞书 tenant_access_token 失败：{payload.get('msg') or payload.get('code')}")
        self._tenant_access_token = str(payload["tenant_access_token"])
        self._token_expires_at = time.time() + max(60, int(payload.get("expire") or 7200) - 300)
        return self._tenant_access_token

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{FEISHU_API}{path}"
        last_error = ""
        for attempt in range(TRANSIENT_RETRIES):
            token = self._token(force_refresh=attempt > 0 and "token" in last_error.lower())
            response = self.session.request(
                method,
                url,
                params=params,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            try:
                payload = response.json()
            except ValueError:
                payload = {"code": response.status_code, "msg": response.text[:300]}
            if response.ok and payload.get("code", 0) == 0:
                return payload
            last_error = f"HTTP {response.status_code}, code={payload.get('code')}, msg={payload.get('msg')}"
            if response.status_code not in {429, 500, 502, 503, 504} and payload.get("code") not in {99991663, 99991664}:
                break
            time.sleep(2 ** attempt)
        raise RuntimeError(f"飞书 OpenAPI 请求失败：{last_error}")

    def validate_fields(self) -> dict[str, dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            payload = self.request(
                "GET",
                f"/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}/fields",
                params=params,
            )
            data = payload.get("data") or {}
            items.extend(data.get("items") or [])
            if not data.get("has_more"):
                break
            page_token = str(data.get("page_token") or "")
            if not page_token:
                raise RuntimeError("飞书字段分页缺少 page_token")
        by_name = {str(item.get("field_name")): item for item in items}
        missing = [name for name in (SPU_FIELD, DATE_FIELD, COUNT_FIELD, RATE_FIELD) if name not in by_name]
        if missing:
            raise RuntimeError(f"飞书表缺少字段：{', '.join(missing)}")
        if int(by_name[DATE_FIELD].get("type") or 0) != 5:
            raise RuntimeError(f"{DATE_FIELD} 必须是日期字段")
        for name in (COUNT_FIELD, RATE_FIELD):
            if int(by_name[name].get("type") or 0) != 1:
                raise RuntimeError(f"{name} 必须是可写文本字段")
        return by_name


def _validate_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError("dt 必须是 YYYY-MM-DD，例如 2026-08-16") from exc
    return parsed.isoformat()


def _normalize_spu(value: Any) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{6,20}", text):
        raise ValueError(f"无效 SPU：{value!r}")
    return text


def _normalize_spu_input(value: Any) -> str:
    """Accept a bare JD SPU or a standard item.jd.com product URL."""
    text = str(value or "").strip()
    match = re.search(r"(?:item\.jd\.com/|[?&](?:sku|spu|id)=)(\d{6,20})", text)
    return _normalize_spu(match.group(1) if match else text)


def _normalize_spu_inputs(values: Iterable[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        for part in re.split(r"[,;\s]+", str(value or "").strip()):
            if not part:
                continue
            spu = _normalize_spu_input(part)
            if spu not in seen:
                seen.add(spu)
                output.append(spu)
    if not output:
        raise ValueError("至少需要一个京东 SPU 或商品链接")
    return output


def _normalize_count(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if not text or "\ufffd" in text:
        return ""
    return text.replace(",", "")


def _normalize_rate(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        number = float(value)
        if 0 <= number <= 1:
            number *= 100
        text = f"{number:.2f}".rstrip("0").rstrip(".")
        return f"{text}%"
    text = str(value).strip()
    if not text:
        return ""
    match = re.search(r"(\d+(?:\.\d+)?)\s*%?", text)
    return f"{match.group(1)}%" if match else ""


def _normalize_good_rate_display(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if "%" in text and ("买家" in text or "赞不绝口" in text):
        return text
    rate = _normalize_rate(value)
    return f"超{rate}买家赞不绝口" if rate else ""


def _walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_dicts(item)


def _parse_current_summary(spu: str, payload: Any) -> ReviewSummary | None:
    for obj in _walk_dicts(payload):
        if "allCntStr" not in obj or "goodRate" not in obj:
            continue
        count = _normalize_count(obj.get("allCntStr"))
        rate = _normalize_good_rate_display(obj.get("goodRate"))
        if count and rate:
            return ReviewSummary(spu, count, rate, "getLegoWareDetailComment")
    return None


def _parse_batch_summaries(payload: Any, requested: set[str]) -> dict[str, ReviewSummary]:
    if not isinstance(payload, dict):
        return {}
    rows = payload.get("CommentsCount")
    if not isinstance(rows, list):
        return {}
    output: dict[str, ReviewSummary] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_spu = row.get("SkuId") or row.get("ProductId") or row.get("skuId")
        try:
            spu = _normalize_spu(raw_spu)
        except ValueError:
            continue
        if spu not in requested:
            continue
        count = _normalize_count(row.get("CommentCountStr"))
        rate = _normalize_good_rate_display(row.get("GoodRateShow"))
        if count and rate:
            output[spu] = ReviewSummary(spu, count, rate, "productCommentSummaries")
    return output


def _chunks(values: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _date_filter(dt: str) -> str:
    return f'CurrentValue.[{DATE_FIELD}]=TODATE("{dt}")'


def _extract_text_cell(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    if isinstance(value, list):
        return "".join(str(item.get("text") or "") for item in value if isinstance(item, dict)).strip()
    return ""


def _read_targets(dt: str, feishu: FeishuClient | None = None) -> list[TargetRecord]:
    client = feishu or FeishuClient()
    client.validate_fields()
    output: list[TargetRecord] = []
    page_token = ""
    while True:
        params: dict[str, Any] = {
            "page_size": FEISHU_PAGE_SIZE,
            "view_id": VIEW_ID,
            "filter": _date_filter(dt),
            "field_names": json.dumps([SPU_FIELD, DATE_FIELD], ensure_ascii=False),
        }
        if page_token:
            params["page_token"] = page_token
        payload = client.request(
            "GET",
            f"/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}/records",
            params=params,
        )
        data = payload.get("data") or {}
        rows = data.get("items") or []
        for row in rows:
            fields = row.get("fields") or {}
            try:
                spu = _normalize_spu(_extract_text_cell(fields.get(SPU_FIELD)))
            except ValueError:
                continue
            output.append(TargetRecord(str(row.get("record_id") or ""), spu, dt))
        if not data.get("has_more"):
            break
        page_token = str(data.get("page_token") or "")
        if not page_token:
            raise RuntimeError("飞书记录分页缺少 page_token")
    return output


def _local_get_json(url: str) -> Any:
    session = requests.Session()
    session.trust_env = False
    response = session.get(url, timeout=5)
    response.raise_for_status()
    return response.json()


def _ensure_debug_chrome(first_spu: str) -> None:
    try:
        _local_get_json(f"{CDP_HOST}/json/version")
        return
    except Exception:
        pass
    if CHROME_PATH is None:
        raise RuntimeError("找不到 Chrome；请安装 Chrome 或设置 JD_CHROME_PATH")
    CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [
            str(CHROME_PATH),
            "--remote-debugging-port=9222",
            "--remote-allow-origins=*",
            f"--user-data-dir={CHROME_PROFILE}",
            "--no-first-run",
            "--no-default-browser-check",
            f"https://item.jd.com/{first_spu}.html",
        ],
        close_fds=True,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            _local_get_json(f"{CDP_HOST}/json/version")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("专用 Chrome 已启动，但 20 秒内未开放调试端口 9222")


def _select_or_create_jd_target(first_spu: str) -> str:
    _ensure_debug_chrome(first_spu)
    try:
        targets = _local_get_json(f"{CDP_HOST}/json/list")
    except Exception as exc:
        raise RuntimeError(
            "无法连接 Chrome 调试端口 9222。请用 --remote-debugging-port=9222 启动 Chrome。"
        ) from exc
    pages = [item for item in targets if item.get("type") == "page"]
    jd_pages = [item for item in pages if "item.jd.com/" in str(item.get("url") or "")]
    if jd_pages:
        return str(jd_pages[0]["webSocketDebuggerUrl"])
    url = f"https://item.jd.com/{first_spu}.html"
    session = requests.Session()
    session.trust_env = False
    response = session.put(f"{CDP_HOST}/json/new?{url}", timeout=10)
    response.raise_for_status()
    target = response.json()
    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError("已创建京东页面，但未获得 CDP WebSocket 地址")
    return str(ws_url)


class CDPClient:
    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.ws: Any = None
        self.message_id = 0
        self.last_diagnostic = ""

    async def __aenter__(self) -> "CDPClient":
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
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 60
    ) -> dict[str, Any]:
        self.message_id += 1
        current_id = self.message_id
        await self.ws.send(
            json.dumps({"id": current_id, "method": method, "params": params or {}})
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            message = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=remaining))
            if message.get("id") != current_id:
                continue
            if "error" in message:
                raise RuntimeError(f"CDP {method} 失败：{message['error']}")
            return message.get("result", {})
        raise TimeoutError(method)

    async def eval(self, expression: str, timeout: float = 60) -> Any:
        result = await self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": True,
            },
            timeout=timeout,
        )
        remote = result.get("result", {})
        if remote.get("subtype") == "error":
            raise RuntimeError(remote.get("description") or "浏览器执行 JavaScript 失败")
        return remote.get("value")

    async def wait_jd_ready(self, first_spu: str, timeout: float = 45) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = await self.eval(
                "JSON.stringify({href:location.href,ready:!!(window.PSign&&window.PSign.sign&&window.SHA256&&window.getJsToken),text:(document.body?.innerText||'').slice(0,500)})"
            )
            parsed = json.loads(state or "{}")
            if parsed.get("ready"):
                return
            text = str(parsed.get("text") or "").lower()
            if any(marker.lower() in text for marker in BLOCKED_MARKERS):
                raise JDVerificationRequired(
                    "京东页面要求验证或当前访问受限，请在该 Chrome 中人工处理后按同一日期重跑"
                )
            await asyncio.sleep(1)
        href = await self.eval("location.href")
        if "item.jd.com/" not in str(href):
            await self.call("Page.navigate", {"url": f"https://item.jd.com/{first_spu}.html"})
        raise RuntimeError("京东页面签名组件未就绪，请刷新商品页后重试")

    async def batch_summaries(self, spus: list[str]) -> dict[str, ReviewSummary]:
        expression = f"""
(async () => {{
  const ids = {json.dumps(spus)};
  const url = 'https://club.jd.com/comment/productCommentSummaries.action?referenceIds=' + encodeURIComponent(ids.join(','));
  try {{
    const response = await fetch(url, {{credentials:'include', headers:{{'accept':'application/json,text/plain,*/*'}}}});
    const bytes = await response.arrayBuffer();
    const contentType = response.headers.get('content-type') || '';
    const text = /charset\\s*=\\s*(?:gbk|gb2312|gb18030)/i.test(contentType)
      ? new TextDecoder('gb18030').decode(bytes)
      : new TextDecoder('utf-8').decode(bytes);
    return JSON.stringify({{status:response.status, text:text, finalUrl:response.url}});
  }} catch (error) {{
    return JSON.stringify({{status:0, error:String(error)}});
  }}
}})()
"""
        raw = await self.eval(expression, timeout=45)
        envelope = json.loads(raw or "{}")
        text = str(envelope.get("text") or "")
        self.last_diagnostic = f"batch status={envelope.get('status')} body={text[:300]} error={envelope.get('error', '')}"
        _raise_if_verification_required(
            envelope.get("status"), text, str(envelope.get("finalUrl") or ""), str(envelope.get("error") or "")
        )
        if envelope.get("status") != 200:
            return {}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return _parse_batch_summaries(payload, set(spus))

    async def signed_summary(self, spu: str) -> ReviewSummary | None:
        expression = f"""
(async () => {{
  const sku = {json.dumps(spu)};
  const body = {{shopType:'0', sku:String(sku), commentNum:5, source:'pc'}};
  const bodyText = JSON.stringify(body);
  const t = Date.now();
  if (!(window.PSign && window.PSign.sign && window.SHA256 && window.getJsToken)) {{
    return JSON.stringify({{status:0,error:'signer_not_ready'}});
  }}
  const signInput = {{
    appid:'item-v3', clientVersion:'1.0.0', client:'pc', t:t,
    body:window.SHA256(bodyText), functionId:'getLegoWareDetailComment'
  }};
  const jsToken = await new Promise((resolve) => {{
    let settled = false;
    const timer = setTimeout(() => {{ if (!settled) {{ settled=true; resolve(''); }} }}, 5000);
    try {{
      window.getJsToken((value) => {{
        if (!settled) {{ settled=true; clearTimeout(timer); resolve((value && value.jsToken) || ''); }}
      }}, 200);
    }} catch (_) {{ settled=true; clearTimeout(timer); resolve(''); }}
  }});
  try {{
    const signed = await window.PSign.sign(signInput);
    const cookieMatch = document.cookie.match(/(?:^|;\\s*)__jda=([^;]+)/);
    const uuid = cookieMatch ? ((cookieMatch[1].split('.')[1]) || '-1') : '-1';
    const params = new URLSearchParams();
    params.set('functionId','getLegoWareDetailComment');
    params.set('body',bodyText);
    params.set('h5st',encodeURI((signed && signed.h5st) || ''));
    params.set('uuid',uuid);
    params.set('loginType','3');
    params.set('appid','item-v3');
    params.set('clientVersion','1.0.0');
    params.set('client','pc');
    params.set('t',String(t));
    params.set('x-api-eid-token',jsToken || '');
    params.set('build','100000');
    const response = await fetch('https://api.m.jd.com/?' + params.toString(), {{
      credentials:'include', headers:{{'accept':'application/json,text/plain,*/*'}}
    }});
    const text = await response.text();
    return JSON.stringify({{status:response.status,text:text,finalUrl:response.url}});
  }} catch (error) {{
    return JSON.stringify({{status:0,error:String(error)}});
  }}
}})()
"""
        raw = await self.eval(expression, timeout=60)
        envelope = json.loads(raw or "{}")
        text = str(envelope.get("text") or "")
        self.last_diagnostic = f"signed status={envelope.get('status')} body={text[:300]} error={envelope.get('error', '')}"
        _raise_if_verification_required(
            envelope.get("status"), text, str(envelope.get("finalUrl") or ""), str(envelope.get("error") or "")
        )
        if envelope.get("status") != 200:
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        return _parse_current_summary(spu, payload)


def _checkpoint_path(dt: str) -> Path:
    return RUNTIME_ROOT / "review-summary" / dt / "checkpoint.jsonl"


def _load_checkpoint(path: Path) -> dict[str, ReviewSummary]:
    output: dict[str, ReviewSummary] = {}
    if not path.exists():
        return output
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            summary = ReviewSummary(
                _normalize_spu(row.get("spu")),
                _normalize_count(row.get("review_count")),
                _normalize_good_rate_display(row.get("good_rate")),
                str(row.get("source") or "checkpoint"),
            )
            if summary.review_count and summary.good_rate:
                output[summary.spu] = summary
        except (ValueError, json.JSONDecodeError, TypeError):
            continue
    return output


def _append_checkpoint(path: Path, summary: ReviewSummary) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "spu": summary.spu,
        "review_count": summary.review_count,
        "good_rate": summary.good_rate,
        "source": summary.source,
        "captured_at": datetime.now().astimezone().isoformat(),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _rewrite_checkpoint(path: Path, summaries: dict[str, ReviewSummary]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    captured_at = datetime.now().astimezone().isoformat()
    with temporary.open("w", encoding="utf-8") as handle:
        for spu in sorted(summaries):
            summary = summaries[spu]
            row = {
                "spu": summary.spu,
                "review_count": summary.review_count,
                "good_rate": summary.good_rate,
                "source": summary.source,
                "captured_at": captured_at,
            }
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    temporary.replace(path)


async def _collect_summaries(
    spus: list[str], checkpoint: Path, signed_only: bool = False
) -> tuple[dict[str, ReviewSummary], list[str]]:
    summaries = _load_checkpoint(checkpoint)
    pending = [spu for spu in spus if spu not in summaries]
    if not pending:
        return summaries, []
    ws_url = _select_or_create_jd_target(pending[0])
    failed: list[str] = []
    async with CDPClient(ws_url) as cdp:
        await cdp.wait_jd_ready(pending[0])
        for batch in _chunks(pending, BATCH_SIZE):
            found: dict[str, ReviewSummary] = {}
            if not signed_only:
                for attempt in range(TRANSIENT_RETRIES):
                    try:
                        found = await cdp.batch_summaries(batch)
                    except Exception:
                        found = {}
                    if found:
                        break
                    await asyncio.sleep((attempt + 1) * 2)
            for summary in found.values():
                summaries[summary.spu] = summary
                _append_checkpoint(checkpoint, summary)
            unresolved = [spu for spu in batch if spu not in found]
            for spu in unresolved:
                summary = None
                for attempt in range(TRANSIENT_RETRIES):
                    try:
                        summary = await cdp.signed_summary(spu)
                    except JDVerificationRequired:
                        raise
                    except Exception:
                        summary = None
                    if summary:
                        break
                    await asyncio.sleep((attempt + 1) * 2)
                if summary:
                    summaries[spu] = summary
                    _append_checkpoint(checkpoint, summary)
                else:
                    failed.append(spu)
                await asyncio.sleep(random.uniform(0.4, 0.9))
            await asyncio.sleep(random.uniform(1.0, 2.0))
    _rewrite_checkpoint(checkpoint, summaries)
    return summaries, failed


def _write_results(
    targets: list[TargetRecord],
    summaries: dict[str, ReviewSummary],
    feishu: FeishuClient | None = None,
) -> int:
    client = feishu or FeishuClient()
    records: list[dict[str, Any]] = []
    for target in targets:
        summary = summaries.get(target.spu)
        if summary:
            records.append(
                {
                    "record_id": target.record_id,
                    "fields": {
                        COUNT_FIELD: summary.review_count,
                        RATE_FIELD: summary.good_rate,
                    },
                }
            )
    updated = 0
    for batch in _chunks(records, FEISHU_WRITE_BATCH_SIZE):
        payload = client.request(
            "POST",
            f"/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}/records/batch_update",
            body={"records": batch},
        )
        returned = (payload.get("data") or {}).get("records") or []
        if returned and len(returned) != len(batch):
            raise RuntimeError(
                f"飞书批量更新数量不一致：请求 {len(batch)}，返回 {len(returned)}"
            )
        updated += len(batch)
        time.sleep(random.uniform(0.5, 1.0))
    return updated


def _direct_output_dir(run_name: str | None = None) -> Path:
    name = str(run_name or time.strftime("%Y%m%d_%H%M%S")).strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
    if not name or name in {".", ".."}:
        raise ValueError("run_name 不能是空目录名")
    return (RUNTIME_ROOT / "direct" / name).resolve()


def _summary_rows(
    spus: list[str], summaries: dict[str, ReviewSummary]
) -> list[dict[str, str]]:
    return [
        {
            "spu": spu,
            "review_count": summaries[spu].review_count,
            "good_rate": summaries[spu].good_rate,
            "source": summaries[spu].source,
        }
        for spu in spus
        if spu in summaries
    ]


def collect_spus(spus: Iterable[Any], run_name: str | None = None) -> dict[str, Any]:
    """Collect JD review summaries locally without reading or writing Feishu."""
    normalized = _normalize_spu_inputs([spus] if isinstance(spus, str) else spus)
    output_dir = _direct_output_dir(run_name)
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = output_dir / "checkpoint.jsonl"
    status_path = output_dir / "status.json"
    results_path = output_dir / "results.json"
    previous_status: dict[str, Any] = {}
    if status_path.exists():
        try:
            previous_status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous_status = {}
    signed_only_resume = bool(previous_status.get("needs_manual_verification"))
    try:
        summaries, failed = asyncio.run(
            _collect_summaries(normalized, checkpoint, signed_only=signed_only_resume)
        )
        successful = {spu: summaries[spu] for spu in normalized if spu in summaries}
        unresolved = sorted(set(failed) | (set(normalized) - set(successful)))
        result = {
            "status": "complete" if not unresolved else "partial",
            "mode": "direct_spu",
            "target_spus": len(normalized),
            "successful_spus": len(successful),
            "failed_spus": unresolved,
            "signed_only_resume": signed_only_resume,
            "results": _summary_rows(normalized, successful),
            "results_file": str(results_path),
            "checkpoint_file": str(checkpoint),
            "output_dir": str(output_dir),
        }
    except Exception as exc:
        summaries = _load_checkpoint(checkpoint)
        successful = {spu: summaries[spu] for spu in normalized if spu in summaries}
        result = {
            "status": "blocked",
            "mode": "direct_spu",
            "needs_manual_verification": isinstance(exc, JDVerificationRequired),
            "target_spus": len(normalized),
            "successful_spus": len(successful),
            "failed_spus": [spu for spu in normalized if spu not in successful],
            "error": str(exc),
            "results": _summary_rows(normalized, successful),
            "results_file": str(results_path),
            "checkpoint_file": str(checkpoint),
            "output_dir": str(output_dir),
        }
    results_path.write_text(
        json.dumps(result["results"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    status_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def run(dt):
    """Collect and write JD review summaries for Feishu rows on one date."""
    target_date = _validate_date(dt)
    feishu = FeishuClient()
    targets = _read_targets(target_date, feishu)
    unique_spus = sorted({item.spu for item in targets})
    output_dir = _checkpoint_path(target_date).parent.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    previous_status_path = output_dir / "status.json"
    previous_status: dict[str, Any] = {}
    if previous_status_path.exists():
        try:
            previous_status = json.loads(previous_status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous_status = {}
    signed_only_resume = bool(previous_status.get("needs_manual_verification"))
    try:
        if not targets:
            result = {
                "status": "complete",
                "date": target_date,
                "target_records": 0,
                "unique_spus": 0,
                "successful_spus": 0,
                "failed_spus": [],
                "updated_records": 0,
                "output_dir": str(output_dir),
            }
        else:
            summaries, failed = asyncio.run(
                _collect_summaries(
                    unique_spus,
                    _checkpoint_path(target_date),
                    signed_only=signed_only_resume,
                )
            )
            successful = {spu: summaries[spu] for spu in unique_spus if spu in summaries}
            updated = _write_results(targets, successful, feishu)
            unresolved = sorted(set(failed) | (set(unique_spus) - set(successful)))
            result = {
                "status": "complete" if not unresolved else "partial",
                "date": target_date,
                "target_records": len(targets),
                "unique_spus": len(unique_spus),
                "successful_spus": len(successful),
                "failed_spus": unresolved,
                "updated_records": updated,
                "signed_only_resume": signed_only_resume,
                "output_dir": str(output_dir),
            }
    except Exception as exc:
        result = {
            "status": "blocked",
            "needs_manual_verification": isinstance(exc, JDVerificationRequired),
            "date": target_date,
            "target_records": len(targets),
            "unique_spus": len(unique_spus),
            "successful_spus": len(_load_checkpoint(_checkpoint_path(target_date))),
            "failed_spus": unique_spus,
            "updated_records": 0,
            "error": str(exc),
            "output_dir": str(output_dir),
        }
        (output_dir / "status.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        raise
    (output_dir / "status.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def preflight(dt):
    """Validate credentials/schema and read the selected date without writing."""
    target_date = _validate_date(dt)
    client = FeishuClient()
    fields = client.validate_fields()
    targets = _read_targets(target_date, client)
    return {
        "status": "ready",
        "date": target_date,
        "target_records": len(targets),
        "unique_spus": len({item.spu for item in targets}),
        "sample_spus": [item.spu for item in targets[:5]],
        "field_types": {
            name: {"type": fields[name].get("type"), "ui_type": fields[name].get("ui_type")}
            for name in (SPU_FIELD, DATE_FIELD, COUNT_FIELD, RATE_FIELD)
        },
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="通过真实 Chrome CDP 采集京东评价数和好评率")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--date", help="飞书模式：读取该日期的 SPU 并回写，格式 YYYY-MM-DD")
    mode.add_argument("--spu", nargs="+", help="直采模式：一个或多个 SPU/京东商品链接，不需要飞书")
    mode.add_argument("--spu-file", help="直采模式：包含 SPU/商品链接的 UTF-8 文本文件")
    parser.add_argument("--run-name", help="直采输出目录名；同名重跑会复用 checkpoint")
    parser.add_argument("--preflight", action="store_true", help="只检查飞书凭证、字段和目标记录，不采集、不写回")
    args = parser.parse_args()
    if args.preflight and not args.date:
        parser.error("--preflight 只能和 --date 一起使用")
    if args.date:
        result = preflight(args.date) if args.preflight else run(args.date)
    else:
        values = args.spu or Path(args.spu_file).read_text(encoding="utf-8-sig").splitlines()
        result = collect_spus(values, run_name=args.run_name)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if args.preflight or result["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
