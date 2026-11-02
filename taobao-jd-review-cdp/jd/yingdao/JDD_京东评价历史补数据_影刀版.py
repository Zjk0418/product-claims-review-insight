# 使用提醒:
# 1. xbot包提供软件自动化、数据表格、Excel、日志、AI等功能
# 2. package包提供访问当前应用数据的功能，如获取元素、访问全局变量、获取资源文件等功能
# 3. 当此模块作为流程独立运行时执行main函数
# 4. 可视化流程中可以通过"调用模块"的指令使用此模块

import xbot
from xbot import print, sleep
from .import package
from .package import variables as glv

"""影刀 Python 3.7 单文件历史补数模块；调用 backfill(start_date, end_date)。"""

import ast
import asyncio
import base64
import json
import os
import random
import re
import shutil
import subprocess
import time
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import requests
import websockets


def _load_env_file(path):
    """读取本地 KEY=VALUE 配置；环境变量优先于 .env。"""
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


def _load_project_env():
    module_dir = Path(__file__).resolve().parent
    for candidate in (module_dir.parent / ".env", module_dir / ".env"):
        if candidate.exists():
            _load_env_file(candidate)
            return


def _require_settings(settings):
    missing = [name for name, value in settings.items() if not value]
    if missing:
        raise RuntimeError(
            "缺少飞书配置：" + ", ".join(missing) + "。请配置环境变量或在模块旁放置本地 .env。"
        )


def _find_chrome_path():
    candidates = []
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(variable)
        if root:
            candidates.append(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe")
    command_path = shutil.which("chrome.exe") or shutil.which("chrome")
    if command_path:
        candidates.append(Path(command_path))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


CDP_HOST = "http://127.0.0.1:9222"
LOCAL_DATA_ROOT = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
CHROME_PATH = _find_chrome_path()
CHROME_PROFILE = LOCAL_DATA_ROOT / "JDD_京东评价采集" / "chrome-profile"
_load_project_env()
FEISHU_API = "https://open.feishu.cn/open-apis"
APP_ID = os.environ.get("FEISHU_APP_ID", "").strip()
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "").strip()
BASE_TOKEN = os.environ.get("FEISHU_BASE_TOKEN", "").strip()
TABLE_ID = os.environ.get("FEISHU_TABLE_ID", "").strip()
VIEW_ID = os.environ.get("FEISHU_VIEW_ID", "").strip()
NOTIFY_APP_ID = os.environ.get("FEISHU_NOTIFY_APP_ID", "").strip()
NOTIFY_APP_SECRET = os.environ.get("FEISHU_NOTIFY_APP_SECRET", "").strip()
NOTIFY_PHONE = os.environ.get("FEISHU_NOTIFY_PHONE", "").strip()
SPU_FIELD = "spu"
DATE_FIELD = "日期"
COUNT_FIELD = "评价数"
RATE_FIELD = "好评率"
QIMING_BAD_RATE_FIELD = "启明星不良率-近30天"

BATCH_SIZE = 20
TRANSIENT_RETRIES = 3
JD_SUMMARY_RETRIES = 1
JD_BATCH_TIMEOUT = 10
JD_SIGNED_TIMEOUT = 12
JD_FETCH_TIMEOUT_MS = 8000
JD_RETRY_DELAY = 1.5
FEISHU_PAGE_SIZE = 500
FEISHU_WRITE_BATCH_SIZE = 500
JD_SIGNED_ITEM_DELAY_MIN = 1.2
JD_SIGNED_ITEM_DELAY_MAX = 2.0
JD_BATCH_DELAY_MIN = 4.0
JD_BATCH_DELAY_MAX = 7.0
FEISHU_WRITE_DELAY_MIN = 1.5
FEISHU_WRITE_DELAY_MAX = 3.0
BACKFILL_DATE_DELAY_MIN = 5.0
BACKFILL_DATE_DELAY_MAX = 9.0
LOGIN_WAIT_SECONDS = 180
LOGIN_POLL_SECONDS = 3
BACKFILL_MAX_PASSES = 2
VERIFICATION_MARKERS = (
    "验证",
    "captcha",
    "risk",
    "no access",
)
VERIFICATION_HTTP_STATUSES = {403, 418}


class JDVerificationRequired(RuntimeError):
    """The active JD browser session needs manual risk verification."""

    def __init__(
        self,
        message: str,
        notification_attempted: bool = False,
        notification_sent: bool = False,
        notification_error: str = "",
    ) -> None:
        super().__init__(message)
        self.notification_attempted = notification_attempted
        self.notification_sent = notification_sent
        self.notification_error = notification_error


def _raise_if_verification_required(
    status: Any, text: str = "", final_url: str = "", error: str = ""
) -> None:
    try:
        status_code = int(status)
    except (TypeError, ValueError):
        status_code = 0
    evidence = " ".join((text, final_url, error)).lower()
    marker_found = any(marker.lower() in evidence for marker in VERIFICATION_MARKERS)
    risk_url = any(part in final_url.lower() for part in ("risk_handler", "cfe.m.jd.com", "verify"))
    if status_code in VERIFICATION_HTTP_STATUSES or marker_found or risk_url:
        raise JDVerificationRequired(
            "京东要求人工验证。请在程序打开的 Chrome 中完成验证后，按同一日期重跑。"
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
    """Minimal Feishu OpenAPI client using environment-provided credentials."""

    def __init__(self) -> None:
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
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
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

    def validate_fields(self) -> Dict[str, Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        page_token = ""
        while True:
            params: Dict[str, Any] = {"page_size": 100}
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


class FeishuNotifier:
    """Send one lightweight login alert with the dedicated Feishu bot."""

    def __init__(self) -> None:
        _require_settings(
            {
                "FEISHU_NOTIFY_APP_ID": NOTIFY_APP_ID,
                "FEISHU_NOTIFY_APP_SECRET": NOTIFY_APP_SECRET,
                "FEISHU_NOTIFY_PHONE": NOTIFY_PHONE,
            }
        )
        self.session = requests.Session()
        self.session.trust_env = False
        self._tenant_access_token = ""

    def _token(self) -> str:
        if self._tenant_access_token:
            return self._tenant_access_token
        response = self.session.post(
            f"{FEISHU_API}/auth/v3/tenant_access_token/internal/",
            json={"app_id": NOTIFY_APP_ID, "app_secret": NOTIFY_APP_SECRET},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0 or not payload.get("tenant_access_token"):
            raise RuntimeError(
                f"飞书通知应用获取 token 失败：{payload.get('msg') or payload.get('code')}"
            )
        self._tenant_access_token = str(payload["tenant_access_token"])
        return self._tenant_access_token

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        response = self.session.request(
            method,
            f"{FEISHU_API}{path}",
            params=params,
            json=body,
            headers={"Authorization": f"Bearer {self._token()}"},
            timeout=20,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"飞书通知接口返回非 JSON：HTTP {response.status_code}") from exc
        if not response.ok or payload.get("code", 0) != 0:
            raise RuntimeError(
                f"飞书通知接口失败：HTTP {response.status_code}, "
                f"code={payload.get('code')}, msg={payload.get('msg')}"
            )
        return payload

    def lookup_open_id(self, phone: str) -> str:
        payload = self._request(
            "POST",
            "/contact/v3/users/batch_get_id",
            params={"user_id_type": "open_id"},
            body={"mobiles": [phone], "include_resigned": False},
        )
        users = ((payload.get("data") or {}).get("user_list") or [])
        if len(users) != 1:
            raise RuntimeError("飞书通知应用未找到该手机号对应的唯一在职用户")
        user = users[0]
        open_id = str(user.get("user_id") or user.get("open_id") or "")
        status = user.get("status") or {}
        unavailable = (
            status.get("is_frozen")
            or status.get("is_resigned")
            or status.get("is_exited")
            or status.get("is_unjoin")
            or status.get("is_activated") is False
        )
        if not open_id or unavailable:
            raise RuntimeError("手机号对应的飞书用户当前不可接收通知")
        return open_id

    def upload_image(self, image_bytes: bytes) -> str:
        response = self.session.post(
            f"{FEISHU_API}/im/v1/images",
            data={"image_type": "message"},
            files={"image": ("jd-login.jpg", image_bytes, "image/jpeg")},
            headers={"Authorization": f"Bearer {self._token()}"},
            timeout=30,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"飞书图片上传返回非 JSON：HTTP {response.status_code}") from exc
        if not response.ok or payload.get("code", 0) != 0:
            raise RuntimeError(
                f"飞书图片上传失败：HTTP {response.status_code}, "
                f"code={payload.get('code')}, msg={payload.get('msg')}"
            )
        image_key = str(((payload.get("data") or {}).get("image_key") or ""))
        if not image_key:
            raise RuntimeError("飞书图片上传成功但未返回 image_key")
        return image_key

    def send_login_alert(
        self,
        target_date: str,
        reason: str,
        screenshot_bytes: bytes,
    ) -> str:
        open_id = self.lookup_open_id(NOTIFY_PHONE)
        image_key = self.upload_image(screenshot_bytes)
        computer_name = os.environ.get("COMPUTERNAME") or "影刀调度电脑"
        text = (
            "检测到京东登录状态失效或需要人工验证。\n"
            f"日期：{target_date}\n"
            f"电脑：{computer_name}\n"
            "程序已打开京东扫码登录页，正在等待扫码。\n"
            "请在3分钟内完成登录；成功后程序会自动继续采集，无需重新运行。\n"
            f"原因：{reason[:300]}"
        )
        content = {
            "zh_cn": {
                "title": "京东评价日更：请扫码登录",
                "content": [
                    [{"tag": "text", "text": text}],
                    [{"tag": "img", "image_key": image_key}],
                ],
            }
        }
        payload = self._request(
            "POST",
            "/im/v1/messages",
            params={"receive_id_type": "open_id"},
            body={
                "receive_id": open_id,
                "msg_type": "post",
                "content": json.dumps(content, ensure_ascii=False),
            },
        )
        return str(((payload.get("data") or {}).get("message_id") or ""))


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


def _normalize_count(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if not text or "\ufffd" in text:
        return ""
    return text.replace(",", "")


def _is_zero_count(value: Any) -> bool:
    """Return True only when JD explicitly reports a zero review count."""
    return bool(re.fullmatch(r"0(?:\.0+)?", _normalize_count(value)))


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


def _walk_dicts(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_dicts(item)


def _parse_current_summary(spu: str, payload: Any) -> Optional[ReviewSummary]:
    for obj in _walk_dicts(payload):
        if "allCntStr" not in obj:
            continue
        count = _normalize_count(obj.get("allCntStr"))
        if _is_zero_count(count):
            return ReviewSummary(spu, "0", "已下架", "getLegoWareDetailComment_zero")
        if "goodRate" not in obj:
            continue
        rate = _normalize_good_rate_display(obj.get("goodRate"))
        if count and rate:
            return ReviewSummary(spu, count, rate, "getLegoWareDetailComment")
    return None


def _parse_batch_summaries(payload: Any, requested: Set[str]) -> Dict[str, ReviewSummary]:
    if not isinstance(payload, dict):
        return {}
    rows = payload.get("CommentsCount")
    if not isinstance(rows, list):
        return {}
    output: Dict[str, ReviewSummary] = {}
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
        if _is_zero_count(count):
            output[spu] = ReviewSummary(spu, "0", "已下架", "productCommentSummaries_zero")
            continue
        rate = _normalize_good_rate_display(row.get("GoodRateShow"))
        if count and rate:
            output[spu] = ReviewSummary(spu, count, rate, "productCommentSummaries")
    return output


def _chunks(values: List[Any], size: int) -> Iterable[List[Any]]:
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


def _read_targets(dt: str, feishu: Optional[FeishuClient] = None) -> List[TargetRecord]:
    client = feishu or FeishuClient()
    client.validate_fields()
    output: List[TargetRecord] = []
    page_token = ""
    while True:
        params: Dict[str, Any] = {
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
        raise RuntimeError("找不到 Google Chrome，请先在机器人电脑安装 Chrome")
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


async def _send_browser_close(ws_url: str) -> None:
    websocket = await websockets.connect(
        ws_url,
        ping_interval=None,
        ping_timeout=None,
    )
    try:
        await websocket.send(json.dumps({"id": 1, "method": "Browser.close"}))
        try:
            await asyncio.wait_for(websocket.recv(), timeout=2)
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


def _close_debug_chrome() -> bool:
    """仅关闭监听 9222 端口的专用 Chrome；关闭失败不影响采集结果。"""
    try:
        version = _local_get_json(f"{CDP_HOST}/json/version")
        ws_url = str(version.get("webSocketDebuggerUrl") or "")
        if not ws_url:
            return False
        asyncio.run(_send_browser_close(ws_url))
    except Exception:
        return False

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            _local_get_json(f"{CDP_HOST}/json/version")
        except Exception:
            return True
        time.sleep(0.2)
    return False


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


async def _inspect_browser_login(ws_url: str) -> Tuple[bool, str]:
    """Check the persistent JD session without copying or storing cookie values."""
    async with CDPClient(ws_url) as cdp:
        await cdp.call("Network.enable")
        cookie_result = await cdp.call("Network.getAllCookies")
        valid_names: Set[str] = set()
        now = time.time()
        for cookie in cookie_result.get("cookies") or []:
            domain = str(cookie.get("domain") or "").lower().lstrip(".")
            name = str(cookie.get("name") or "")
            value = str(cookie.get("value") or "")
            try:
                expires = float(cookie.get("expires") or 0)
            except (TypeError, ValueError):
                expires = 0
            if (
                (domain == "jd.com" or domain.endswith(".jd.com"))
                and value
                and value.lower() != "deleted"
                and (expires <= 0 or expires > now)
            ):
                valid_names.add(name)

        state_raw = await cdp.eval(
            """
JSON.stringify((() => {
  const loginBox = document.querySelector('#ttbar-login');
  const loginText = (loginBox && loginBox.innerText || '').trim();
  return {
    href: location.href,
    title: document.title || '',
    loginText: loginText,
    bodyText: (document.body && document.body.innerText || '').slice(0, 500)
  };
})())
"""
        )
        state = json.loads(state_raw or "{}")
        href = str(state.get("href") or "").lower()
        title = str(state.get("title") or "")
        login_text = str(state.get("loginText") or "")
        body_text = str(state.get("bodyText") or "")
        visible_text = " ".join((title, login_text, body_text))
        risk_page = any(
            part in href
            for part in ("risk_handler", "cfe.m.jd.com", "passport.jd.com", "verify")
        ) or any(
            marker in visible_text
            for marker in ("快速验证", "安全验证", "验证码", "扫码登录", "账户登录")
        )
        explicitly_logged_out = "请登录" in login_text or "你好，请登录" in login_text
        mobile_session = "pt_key" in valid_names and bool(
            {"pt_pin", "pin"}.intersection(valid_names)
        )
        pc_qr_session = "pin" in valid_names and bool(
            {"thor", "token"}.intersection(valid_names)
        )
        has_session = mobile_session or pc_qr_session
        if risk_page:
            return False, "当前浏览器位于京东登录或风险验证页"
        if explicitly_logged_out:
            return False, "京东商品页显示当前账号未登录"
        if not has_session:
            return False, "京东登录 Cookie 不存在或已经过期"
        return True, "京东登录状态可用"


async def _navigate_browser_to_login(ws_url: str, first_spu: str) -> None:
    return_url = f"https%3A%2F%2Fitem.jd.com%2F{first_spu}.html"
    login_url = f"https://passport.jd.com/new/login.aspx?ReturnUrl={return_url}"
    async with CDPClient(ws_url) as cdp:
        await cdp.call("Page.enable")
        await cdp.call("Page.navigate", {"url": login_url})
        try:
            await cdp.call("Page.bringToFront")
        except Exception:
            pass
        await asyncio.sleep(2)


async def _capture_browser_screenshot(ws_url: str) -> bytes:
    async with CDPClient(ws_url) as cdp:
        await cdp.call("Page.enable")
        result = await cdp.call(
            "Page.captureScreenshot",
            {
                "format": "jpeg",
                "quality": 85,
                "fromSurface": True,
                "captureBeyondViewport": False,
            },
            timeout=20,
        )
    encoded = str(result.get("data") or "")
    if not encoded:
        raise RuntimeError("京东登录页截图为空")
    image_bytes = base64.b64decode(encoded)
    if not image_bytes:
        raise RuntimeError("京东登录页截图解码失败")
    return image_bytes


async def _wait_for_login_completion(
    ws_url: str,
    timeout_seconds: int = LOGIN_WAIT_SECONDS,
) -> Tuple[bool, str]:
    deadline = time.monotonic() + timeout_seconds
    last_reason = "等待扫码登录"
    while time.monotonic() < deadline:
        try:
            logged_in, last_reason = await _inspect_browser_login(ws_url)
            if logged_in:
                return True, last_reason
        except Exception as exc:
            last_reason = f"登录状态检查失败：{exc}"
        remaining = deadline - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(min(LOGIN_POLL_SECONDS, remaining))
    return False, last_reason


async def _navigate_browser_to_product(ws_url: str, first_spu: str) -> None:
    async with CDPClient(ws_url) as cdp:
        await cdp.call("Page.enable")
        await cdp.call(
            "Page.navigate",
            {"url": f"https://item.jd.com/{first_spu}.html"},
        )
        try:
            await cdp.call("Page.bringToFront")
        except Exception:
            pass
        await asyncio.sleep(2)


def _wait_for_manual_login(
    first_spu: str,
    target_date: str,
    reason: str,
) -> None:
    navigation_error = ""
    ws_url = ""
    screenshot_bytes = b""
    try:
        ws_url = _select_or_create_jd_target(first_spu)
        asyncio.run(_navigate_browser_to_login(ws_url, first_spu))
        screenshot_bytes = asyncio.run(_capture_browser_screenshot(ws_url))
    except Exception as exc:
        navigation_error = str(exc)

    notification_sent = False
    notification_error = ""
    try:
        if not screenshot_bytes:
            raise RuntimeError("未能获取京东扫码登录页截图")
        FeishuNotifier().send_login_alert(target_date, reason, screenshot_bytes)
        notification_sent = True
    except Exception as exc:
        notification_error = str(exc)

    if ws_url and not navigation_error:
        logged_in, last_reason = asyncio.run(
            _wait_for_login_completion(ws_url, LOGIN_WAIT_SECONDS)
        )
        if logged_in:
            asyncio.run(_navigate_browser_to_product(ws_url, first_spu))
            return
    else:
        last_reason = reason

    details = "等待扫码登录超时：3分钟内未检测到京东登录成功。"
    if navigation_error:
        details += f" 打开登录页失败：{navigation_error}"
    if notification_error:
        details += f" 飞书提醒失败：{notification_error}"
    if last_reason:
        details += f" 最后状态：{last_reason}"
    raise JDVerificationRequired(
        details,
        notification_attempted=True,
        notification_sent=notification_sent,
        notification_error=notification_error,
    )


def _ensure_login_ready(first_spu: str, target_date: str) -> bool:
    ws_url = _select_or_create_jd_target(first_spu)
    try:
        logged_in, reason = asyncio.run(_inspect_browser_login(ws_url))
    except Exception as exc:
        logged_in = False
        reason = f"京东登录预检失败：{exc}"
    if not logged_in:
        _wait_for_manual_login(first_spu, target_date, reason)
        return True
    return False


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
            ping_interval=None,
            ping_timeout=None,
        )
        return self

    async def __aexit__(self, *_args: Any) -> None:
        if self.ws:
            await self.ws.close()

    async def call(
        self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 60
    ) -> Dict[str, Any]:
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
            if any(marker.lower() in text for marker in VERIFICATION_MARKERS):
                raise JDVerificationRequired(
                    "京东页面要求验证或当前访问受限，请在该 Chrome 中人工处理后按同一日期重跑"
                )
            await asyncio.sleep(1)
        href = await self.eval("location.href")
        if "item.jd.com/" not in str(href):
            await self.call("Page.navigate", {"url": f"https://item.jd.com/{first_spu}.html"})
        raise RuntimeError("京东页面签名组件未就绪，请刷新商品页后重试")

    async def batch_summaries(self, spus: List[str]) -> Dict[str, ReviewSummary]:
        expression = f"""
(async () => {{
  const ids = {json.dumps(spus)};
  const url = 'https://club.jd.com/comment/productCommentSummaries.action?referenceIds=' + encodeURIComponent(ids.join(','));
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), {JD_FETCH_TIMEOUT_MS});
  try {{
    const response = await fetch(url, {{credentials:'include', signal:controller.signal, headers:{{'accept':'application/json,text/plain,*/*'}}}});
    const bytes = await response.arrayBuffer();
    const contentType = response.headers.get('content-type') || '';
    const text = /charset\\s*=\\s*(?:gbk|gb2312|gb18030)/i.test(contentType)
      ? new TextDecoder('gb18030').decode(bytes)
      : new TextDecoder('utf-8').decode(bytes);
    return JSON.stringify({{status:response.status, text:text, finalUrl:response.url}});
  }} catch (error) {{
    return JSON.stringify({{status:0, error:String(error)}});
  }} finally {{
    clearTimeout(timer);
  }}
}})()
"""
        raw = await self.eval(expression, timeout=JD_BATCH_TIMEOUT)
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

    async def signed_summary(self, spu: str) -> Optional[ReviewSummary]:
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
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), {JD_FETCH_TIMEOUT_MS});
    const response = await fetch('https://api.m.jd.com/?' + params.toString(), {{
      credentials:'include', signal:controller.signal, headers:{{'accept':'application/json,text/plain,*/*'}}
    }});
    const text = await response.text();
    clearTimeout(timer);
    return JSON.stringify({{status:response.status,text:text,finalUrl:response.url}});
  }} catch (error) {{
    return JSON.stringify({{status:0,error:String(error)}});
  }}
}})()
"""
        raw = await self.eval(expression, timeout=JD_SIGNED_TIMEOUT)
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


async def _collect_summaries(spus: List[str]) -> Tuple[Dict[str, ReviewSummary], List[str]]:
    summaries: Dict[str, ReviewSummary] = {}
    ws_url = _select_or_create_jd_target(spus[0])
    failed: List[str] = []
    async with CDPClient(ws_url) as cdp:
        await cdp.wait_jd_ready(spus[0])
        for batch in _chunks(spus, BATCH_SIZE):
            found: Dict[str, ReviewSummary] = {}
            for attempt in range(JD_SUMMARY_RETRIES):
                try:
                    found = await cdp.batch_summaries(batch)
                except JDVerificationRequired:
                    raise
                except Exception:
                    found = {}
                if found:
                    break
                await asyncio.sleep((attempt + 1) * JD_RETRY_DELAY)
            for summary in found.values():
                summaries[summary.spu] = summary
            unresolved = [spu for spu in batch if spu not in found]
            for spu in unresolved:
                summary = None
                for attempt in range(JD_SUMMARY_RETRIES):
                    try:
                        summary = await cdp.signed_summary(spu)
                    except JDVerificationRequired:
                        raise
                    except Exception:
                        summary = None
                    if summary:
                        break
                    await asyncio.sleep((attempt + 1) * JD_RETRY_DELAY)
                if summary:
                    summaries[spu] = summary
                else:
                    failed.append(spu)
                await asyncio.sleep(
                    random.uniform(
                        JD_SIGNED_ITEM_DELAY_MIN,
                        JD_SIGNED_ITEM_DELAY_MAX,
                    )
                )
            await asyncio.sleep(
                random.uniform(JD_BATCH_DELAY_MIN, JD_BATCH_DELAY_MAX)
            )
    return summaries, failed


def _write_results(
    targets: List[TargetRecord],
    summaries: Dict[str, ReviewSummary],
    feishu: Optional[FeishuClient] = None,
) -> int:
    client = feishu or FeishuClient()
    records: List[Dict[str, Any]] = []
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
        time.sleep(
            random.uniform(FEISHU_WRITE_DELAY_MIN, FEISHU_WRITE_DELAY_MAX)
        )
    return updated


def _run(dt, handle_login=False):
    """Collect and write JD review summaries for Feishu rows on one date."""
    target_date = _validate_date(dt)
    feishu = FeishuClient()
    targets = _read_targets(target_date, feishu)
    unique_spus = sorted({item.spu for item in targets})
    login_waited = False
    try:
        if not targets:
            result = {
                "status": "no_target_records",
                "date": target_date,
                "target_records": 0,
                "unique_spus": 0,
                "successful_spus": 0,
                "failed_spus": [],
                "updated_records": 0,
            }
        else:
            if handle_login:
                login_waited = _ensure_login_ready(unique_spus[0], target_date)
            collection_login_retries = 0
            while True:
                try:
                    summaries, failed = asyncio.run(_collect_summaries(unique_spus))
                    break
                except JDVerificationRequired as login_exc:
                    if not handle_login or collection_login_retries >= 1:
                        raise
                    _wait_for_manual_login(
                        unique_spus[0],
                        target_date,
                        str(login_exc),
                    )
                    login_waited = True
                    collection_login_retries += 1
            successful = {spu: summaries[spu] for spu in unique_spus if spu in summaries}
            unresolved = sorted(set(failed) | (set(unique_spus) - set(successful)))
            updated = _write_results(targets, successful, feishu)
            result = {
                "status": "complete" if not unresolved else "partial",
                "date": target_date,
                "target_records": len(targets),
                "unique_spus": len(unique_spus),
                "successful_spus": len(successful),
                "failed_spus": unresolved,
                "updated_records": updated,
                "login_waited": login_waited,
            }
    except Exception as exc:
        result = {
            "status": "blocked",
            "needs_manual_verification": isinstance(exc, JDVerificationRequired),
            "notification_sent": bool(
                isinstance(exc, JDVerificationRequired) and exc.notification_sent
            ),
            "date": target_date,
            "target_records": len(targets),
            "unique_spus": len(unique_spus),
            "successful_spus": 0,
            "failed_spus": unique_spus,
            "updated_records": 0,
            "login_waited": login_waited,
            "error": str(exc),
        }
        if isinstance(exc, JDVerificationRequired) and exc.notification_error:
            result["notification_error"] = exc.notification_error
    return result


def collect(date_value):
    """影刀调用入口：传入日期，直接采集并覆盖写入飞书。"""
    if isinstance(date_value, (date, datetime)):
        target_date = date_value.strftime("%Y-%m-%d")
    else:
        target_date = _validate_date(str(date_value).strip().replace("/", "-")[:10])
    result = None
    try:
        result = _run(target_date)
        return result
    finally:
        needs_manual_verification = bool(
            isinstance(result, dict) and result.get("needs_manual_verification")
        )
        if not needs_manual_verification:
            _close_debug_chrome()


def collect_backfill_date(date_value):
    """历史补数入口：不做登录通知或等待，也不关闭专用浏览器。"""
    if isinstance(date_value, (date, datetime)):
        target_date = date_value.strftime("%Y-%m-%d")
    else:
        target_date = _validate_date(str(date_value).strip().replace("/", "-")[:10])
    return _run(target_date, handle_login=False)


def get_today_spus(date_value):
    """返回飞书表中指定日期的全部 SPU，保留原始行顺序。"""
    if isinstance(date_value, (date, datetime)):
        target_date = date_value.strftime("%Y-%m-%d")
    else:
        target_date = _validate_date(str(date_value).strip().replace("/", "-")[:10])
    targets = _read_targets(target_date)
    return [target.spu for target in targets]


def save_qiming_bad_rate(spu, date_value, bad_rate):
    """按 SPU 和日期批量定位飞书行，写入启明星近 30 天不良率。"""
    def decode_list_string(value):
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not (text.startswith("[") and text.endswith("]")):
            return value
        for parser in (json.loads, ast.literal_eval):
            try:
                parsed = parser(text)
            except (ValueError, SyntaxError, json.JSONDecodeError):
                continue
            if isinstance(parsed, (list, tuple)):
                return list(parsed)
        return value

    spu = decode_list_string(spu)
    date_value = decode_list_string(date_value)
    bad_rate = decode_list_string(bad_rate)
    single_mode = not isinstance(spu, (list, tuple))
    spus = [spu] if single_mode else list(spu)
    if not spus:
        return {
            "status": "no_items",
            "requested_items": 0,
            "updated_records": 0,
            "not_found": [],
        }

    if isinstance(date_value, (list, tuple)):
        dates = list(date_value)
        if len(dates) != len(spus):
            raise ValueError("日期列表长度必须与 SPU 列表一致")
    else:
        dates = [date_value] * len(spus)

    if isinstance(bad_rate, (list, tuple)):
        values = list(bad_rate)
        if len(values) != len(spus):
            raise ValueError("不良率列表长度必须与 SPU 列表一致")
    else:
        values = [bad_rate] * len(spus)

    requested: Dict[str, Dict[str, Any]] = {}
    normalized_items: List[Tuple[str, str, Any]] = []
    for raw_spu, raw_date, raw_value in zip(spus, dates, values):
        target_spu = _normalize_spu(raw_spu)
        if isinstance(raw_date, (date, datetime)):
            target_date = raw_date.strftime("%Y-%m-%d")
        else:
            target_date = _validate_date(str(raw_date).strip().replace("/", "-")[:10])
        if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
            raise ValueError(f"SPU {target_spu} 的 bad_rate 不能为空")
        value = raw_value.strip() if isinstance(raw_value, str) else raw_value
        requested.setdefault(target_date, {})[target_spu] = value
        normalized_items.append((target_spu, target_date, value))

    feishu = FeishuClient()
    records: List[Dict[str, Any]] = []
    found_keys: Set[Tuple[str, str]] = set()
    for target_date, values_by_spu in requested.items():
        targets = _read_targets(target_date, feishu)
        for target in targets:
            if target.spu not in values_by_spu:
                continue
            records.append(
                {
                    "record_id": target.record_id,
                    "fields": {QIMING_BAD_RATE_FIELD: values_by_spu[target.spu]},
                }
            )
            found_keys.add((target_date, target.spu))

    not_found = [
        {"spu": target_spu, "date": target_date}
        for target_spu, target_date, _value in normalized_items
        if (target_date, target_spu) not in found_keys
    ]
    if not records:
        if single_mode:
            target_spu, target_date, _value = normalized_items[0]
            return {
                "status": "not_found",
                "spu": target_spu,
                "date": target_date,
                "updated_records": 0,
            }
        return {
            "status": "not_found",
            "requested_items": len(normalized_items),
            "updated_records": 0,
            "not_found": not_found,
        }

    updated = 0
    for batch in _chunks(records, FEISHU_WRITE_BATCH_SIZE):
        payload = feishu.request(
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

    if single_mode:
        target_spu, target_date, value = normalized_items[0]
        return {
            "status": "updated",
            "spu": target_spu,
            "date": target_date,
            "value": value,
            "updated_records": updated,
        }
    return {
        "status": "updated" if not not_found else "partial",
        "requested_items": len(normalized_items),
        "matched_items": len(normalized_items) - len(not_found),
        "updated_records": updated,
        "not_found": not_found,
    }


def _normalize_backfill_date(value, field_name):
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    text = str(value or "").strip().replace("/", "-")[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(
            "%s 必须是 YYYY-MM-DD，例如 2026-02-01" % field_name
        ) from exc


def _backfill_dates(start_date, end_date):
    start_text = _normalize_backfill_date(start_date, "start_date")
    end_text = _normalize_backfill_date(end_date, "end_date")
    current = date.fromisoformat(start_text)
    end_value = date.fromisoformat(end_text)
    if current > end_value:
        raise ValueError("start_date 不能晚于 end_date")
    output = []
    while current <= end_value:
        output.append(current.isoformat())
        current += timedelta(days=1)
    return output


def backfill(start_date, end_date):
    """影刀调用入口：补完指定闭区间，不通知登录且不关闭浏览器。"""
    all_dates = _backfill_dates(start_date, end_date)
    start_text = all_dates[0]
    end_text = all_dates[-1]
    pending_dates = list(all_dates)
    completed_dates = []
    no_target_dates = []
    last_results = {}

    for pass_number in range(1, BACKFILL_MAX_PASSES + 1):
        if not pending_dates:
            break
        current_pending = list(pending_dates)
        pending_dates = []
        print(
            "京东历史补数：第%s轮，待处理%s天"
            % (pass_number, len(current_pending))
        )

        for index, target_date in enumerate(current_pending, 1):
            print(
                "京东历史补数：第%s轮 %s/%s，日期 %s"
                % (pass_number, index, len(current_pending), target_date)
            )
            try:
                result = collect_backfill_date(target_date)
            except Exception as exc:
                result = {
                    "status": "blocked",
                    "date": target_date,
                    "needs_manual_verification": False,
                    "error": str(exc),
                }
            last_results[target_date] = result
            status = str(result.get("status") or "")

            if status == "complete":
                completed_dates.append(target_date)
            elif status == "no_target_records":
                no_target_dates.append(target_date)
            elif result.get("needs_manual_verification"):
                remaining = [target_date]
                remaining.extend(current_pending[index:])
                remaining.extend(pending_dates)
                return {
                    "status": "blocked",
                    "needs_manual_verification": True,
                    "start_date": start_text,
                    "end_date": end_text,
                    "total_dates": len(all_dates),
                    "completed_dates": len(set(completed_dates)),
                    "no_target_dates": len(set(no_target_dates)),
                    "remaining_dates": sorted(set(remaining)),
                    "blocked_date": target_date,
                    "error": result.get("error") or "京东要求登录或人工验证",
                }
            else:
                pending_dates.append(target_date)
            sleep(
                random.uniform(
                    BACKFILL_DATE_DELAY_MIN,
                    BACKFILL_DATE_DELAY_MAX,
                )
            )

    failed_dates = sorted(set(pending_dates))
    return {
        "status": "complete" if not failed_dates else "partial",
        "needs_manual_verification": False,
        "start_date": start_text,
        "end_date": end_text,
        "total_dates": len(all_dates),
        "completed_dates": len(set(completed_dates)),
        "no_target_dates": len(set(no_target_dates)),
        "failed_dates": failed_dates,
        "failed_details": {
            target_date: last_results.get(target_date, {}).get("error", "")
            for target_date in failed_dates
        },
    }
