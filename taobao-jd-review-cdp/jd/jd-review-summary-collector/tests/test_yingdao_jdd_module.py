import importlib.util
import asyncio
import inspect
import re
import sys
import types
import builtins
from pathlib import Path


MODULE_PATH = Path(__file__).parents[2] / "yingdao" / "JDD_京东评价采集_影刀版.py"


def _load_module():
    xbot = types.ModuleType("xbot")
    xbot.print = builtins.print
    xbot.sleep = lambda *_args: None
    sys.modules["xbot"] = xbot
    package_root = types.ModuleType("jdd_test_package")
    package_root.__path__ = []
    package = types.ModuleType("jdd_test_package.package")
    package.variables = {}
    package_root.package = package
    sys.modules["jdd_test_package"] = package_root
    sys.modules["jdd_test_package.package"] = package
    spec = importlib.util.spec_from_file_location(
        "jdd_test_package.jdd_yingdao_module", MODULE_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_yingdao_public_interface_is_collect_date_only():
    module = _load_module()
    assert list(inspect.signature(module.collect).parameters) == ["date_value"]


def test_backfill_date_entry_disables_login_handling_and_browser_close():
    module = _load_module()
    calls = []
    module._run = lambda dt, handle_login=True: calls.append(
        (dt, handle_login)
    ) or {"status": "complete", "date": dt}
    module._close_debug_chrome = lambda: calls.append(("closed",)) or True
    result = module.collect_backfill_date("2026/02/01")
    assert result["status"] == "complete"
    assert calls == [("2026-02-01", False)]


def test_yingdao_collect_normalizes_slash_date():
    module = _load_module()
    module._run = lambda dt: {
        "status": "complete",
        "date": dt,
        "target_records": 1,
    }
    module._close_debug_chrome = lambda: True
    result = module.collect("2026/08/16")
    assert result["date"] == "2026-08-16"
    assert result["status"] == "complete"


def test_yingdao_generated_module_prefix_can_be_prepended():
    source = MODULE_PATH.read_text(encoding="utf-8")
    wrapped = "from .package import variables as glv\n" + source
    compile(wrapped, "<yingdao-module>", "exec")


def test_yingdao_python37_compatibility_contract():
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert not re.search(r"\b(?:dict|list|set|tuple)\[", source)
    assert " | None" not in source
    assert "proxy=None" not in source
    assert "import argparse" not in source
    assert "if __name__ ==" not in source
    assert "def main(" not in source


def test_system_busy_is_retryable_not_manual_verification():
    module = _load_module()
    module._raise_if_verification_required(200, "系统繁忙")


def test_scheduler_module_has_no_local_output_or_checkpoint_files():
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "output_dir" not in source
    assert "checkpoint" not in source.lower()
    assert "status.json" not in source


def test_scheduler_module_has_no_machine_specific_paths():
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert r"C:\Users" not in source
    assert r"D:\AI_Project" not in source
    assert 'Path(r"C:\\Program Files' not in source


def test_yingdao_module_has_no_hardcoded_feishu_secrets():
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert 'APP_SECRET = os.environ.get("FEISHU_APP_SECRET"' in source
    assert 'NOTIFY_APP_SECRET = os.environ.get("FEISHU_NOTIFY_APP_SECRET"' in source
    assert not re.search(r'^APP_SECRET\s*=\s*["\'][^"\']+["\']', source, re.MULTILINE)
    assert not re.search(r'^NOTIFY_APP_SECRET\s*=\s*["\'][^"\']+["\']', source, re.MULTILINE)


def test_current_api_keeps_full_good_rate_display_text():
    module = _load_module()
    payload = {
        "data": {
            "allCntStr": "10万+",
            "goodRate": "99%",
        }
    }
    summary = module._parse_current_summary("100047005216", payload)
    assert summary.review_count == "10万+"
    assert summary.good_rate == "超99%买家赞不绝口"


def test_batch_api_builds_full_good_rate_display_text():
    module = _load_module()
    payload = {
        "CommentsCount": [
            {
                "SkuId": 100047005216,
                "CommentCountStr": "10万+",
                "GoodRateShow": 99,
            }
        ]
    }
    result = module._parse_batch_summaries(payload, {"100047005216"})
    assert result["100047005216"].good_rate == "超99%买家赞不绝口"


def test_batch_failure_falls_back_to_signed_and_normal_failure_is_partial():
    module = _load_module()
    calls = {"batch": 0, "signed": []}

    class FakeCDP:
        def __init__(self, _ws_url):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def wait_jd_ready(self, _spu):
            return None

        async def batch_summaries(self, _spus):
            calls["batch"] += 1
            raise RuntimeError("系统繁忙")

        async def signed_summary(self, spu):
            calls["signed"].append(spu)
            if spu == "1001":
                return module.ReviewSummary(
                    spu, "10万+", "超99%买家赞不绝口", "signed"
                )
            raise TimeoutError("temporary timeout")

    module.CDPClient = FakeCDP
    module._select_or_create_jd_target = lambda _spu: "ws://example"
    module.TRANSIENT_RETRIES = 1
    module.BATCH_SIZE = 20
    module.random.uniform = lambda *_args: 0
    summaries, failed = asyncio.run(module._collect_summaries(["1001", "1002"]))
    assert calls["batch"] == 1
    assert calls["signed"] == ["1001", "1002"]
    assert summaries["1001"].good_rate == "超99%买家赞不绝口"
    assert failed == ["1002"]


def test_collection_login_problem_waits_then_retries_same_task():
    module = _load_module()
    events = []
    module.FeishuClient = lambda: object()
    module._read_targets = lambda _dt, _client: [
        module.TargetRecord("rec1", "1001", "2026-08-18")
    ]
    module._ensure_login_ready = lambda _spu, _dt: False
    module._wait_for_manual_login = (
        lambda spu, dt, _reason: events.append(("waited", spu, dt))
    )
    calls = {"collect": 0}

    async def login_blocked(_spus):
        calls["collect"] += 1
        if calls["collect"] == 1:
            raise module.JDVerificationRequired("需要登录")
        return {
            "1001": module.ReviewSummary(
                "1001", "10万+", "超99%买家赞不绝口", "batch"
            )
        }, []

    module._collect_summaries = login_blocked
    module._write_results = lambda _targets, _summaries, _feishu: 1
    module._close_debug_chrome = lambda: events.append(("closed",)) or True
    result = module.collect("2026-08-18")
    assert result["status"] == "complete"
    assert result["login_waited"] is True
    assert calls["collect"] == 2
    assert events == [
        ("waited", "1001", "2026-08-18"),
        ("closed",),
    ]


def test_login_preflight_timeout_blocks_collection_and_keeps_browser_open():
    module = _load_module()
    events = []
    module.FeishuClient = lambda: object()
    module._read_targets = lambda _dt, _client: [
        module.TargetRecord("rec1", "1001", "2026-08-22")
    ]

    def login_required(_spu, _dt):
        events.append("preflight")
        raise module.JDVerificationRequired(
            "等待扫码登录超时：3分钟内未检测到京东登录成功",
            notification_attempted=True,
            notification_sent=True,
        )

    async def must_not_collect(_spus):
        events.append("collected")
        return {}, []

    module._ensure_login_ready = login_required
    module._collect_summaries = must_not_collect
    module._close_debug_chrome = lambda: events.append("closed") or True
    result = module.collect("2026-08-22")
    assert result["status"] == "blocked"
    assert result["needs_manual_verification"] is True
    assert result["notification_sent"] is True
    assert "3分钟" in result["error"]
    assert events == ["preflight"]


def test_manual_login_sends_screenshot_waits_and_resumes():
    module = _load_module()
    events = []
    module._select_or_create_jd_target = lambda spu: events.append(
        ("target", spu)
    ) or "ws://example"

    async def navigate_login(ws, spu):
        events.append(("login_page", ws, spu))

    async def capture(ws):
        events.append(("screenshot", ws))
        return b"jpeg-bytes"

    async def wait_login(ws, timeout):
        events.append(("wait", ws, timeout))
        return True, "京东登录状态可用"

    async def navigate_product(ws, spu):
        events.append(("product_page", ws, spu))

    class FakeNotifier:
        def send_login_alert(self, dt, reason, screenshot):
            events.append(("notify", dt, reason, screenshot))
            return "message-id"

    module._navigate_browser_to_login = navigate_login
    module._capture_browser_screenshot = capture
    module._wait_for_login_completion = wait_login
    module._navigate_browser_to_product = navigate_product
    module.FeishuNotifier = FakeNotifier
    module._wait_for_manual_login("1001", "2026-08-22", "登录失效")
    assert events == [
        ("target", "1001"),
        ("login_page", "ws://example", "1001"),
        ("screenshot", "ws://example"),
        ("notify", "2026-08-22", "登录失效", b"jpeg-bytes"),
        ("wait", "ws://example", 180),
        ("product_page", "ws://example", "1001"),
    ]


def test_manual_login_timeout_is_three_minutes_and_reports_notification():
    module = _load_module()
    module._select_or_create_jd_target = lambda _spu: "ws://example"

    async def no_op(*_args):
        return None

    async def capture(_ws):
        return b"jpeg-bytes"

    async def timeout(_ws, seconds):
        assert seconds == 180
        return False, "仍在扫码登录页"

    class FakeNotifier:
        def send_login_alert(self, _dt, _reason, _screenshot):
            return "message-id"

    module._navigate_browser_to_login = no_op
    module._capture_browser_screenshot = capture
    module._wait_for_login_completion = timeout
    module.FeishuNotifier = FakeNotifier
    try:
        module._wait_for_manual_login("1001", "2026-08-22", "登录失效")
    except module.JDVerificationRequired as exc:
        assert "3分钟" in str(exc)
        assert exc.notification_attempted is True
        assert exc.notification_sent is True
    else:
        raise AssertionError("扫码等待超时后必须返回人工验证状态")


def test_login_wait_polling_returns_as_soon_as_scan_succeeds():
    module = _load_module()
    states = [
        (False, "仍在扫码登录页"),
        (True, "京东登录状态可用"),
    ]

    async def inspect(_ws):
        return states.pop(0)

    module._inspect_browser_login = inspect
    module.LOGIN_POLL_SECONDS = 0
    logged_in, reason = asyncio.run(
        module._wait_for_login_completion("ws://example", 1)
    )
    assert logged_in is True
    assert reason == "京东登录状态可用"
    assert states == []


def test_pc_qr_login_cookie_pair_is_recognized():
    module = _load_module()

    class FakeCDP:
        def __init__(self, _ws_url):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def call(self, method, _params=None, timeout=60):
            if method == "Network.enable":
                return {}
            if method == "Network.getAllCookies":
                return {
                    "cookies": [
                        {
                            "domain": ".jd.com",
                            "name": "pin",
                            "value": "masked-user",
                            "expires": 0,
                        },
                        {
                            "domain": ".jd.com",
                            "name": "thor",
                            "value": "masked-session",
                            "expires": 0,
                        },
                    ]
                }
            raise AssertionError(method)

        async def eval(self, _expression, timeout=60):
            return (
                '{"href":"https://item.jd.com/1001.html",'
                '"title":"京东商品","loginText":"","bodyText":"商品详情"}'
            )

    module.CDPClient = FakeCDP
    logged_in, reason = asyncio.run(module._inspect_browser_login("ws://example"))
    assert logged_in is True
    assert reason == "京东登录状态可用"


def test_successful_login_preflight_continues_collection():
    module = _load_module()
    events = []
    module.FeishuClient = lambda: object()
    targets = [module.TargetRecord("rec1", "1001", "2026-08-22")]
    module._read_targets = lambda _dt, _client: targets
    def login_ready(spu, dt):
        events.append(("ready", spu, dt))
        return False

    module._ensure_login_ready = login_ready

    async def collect_one(_spus):
        events.append(("collect", list(_spus)))
        return {
            "1001": module.ReviewSummary(
                "1001", "10万+", "超99%买家赞不绝口", "batch"
            )
        }, []

    module._collect_summaries = collect_one
    module._write_results = lambda _targets, _summaries, _feishu: 1
    module._close_debug_chrome = lambda: events.append(("closed",)) or True
    result = module.collect("2026-08-22")
    assert result["status"] == "complete"
    assert result["updated_records"] == 1
    assert result["login_waited"] is False
    assert events == [
        ("ready", "1001", "2026-08-22"),
        ("collect", ["1001"]),
        ("closed",),
    ]


def test_collect_closes_debug_browser_after_normal_completion():
    module = _load_module()
    calls = []
    module._run = lambda dt: {"status": "complete", "date": dt}
    module._close_debug_chrome = lambda: calls.append("closed") or True
    result = module.collect("2026-08-22")
    assert result["status"] == "complete"
    assert calls == ["closed"]


def test_collect_keeps_browser_open_for_manual_login():
    module = _load_module()
    calls = []
    module._run = lambda dt: {
        "status": "blocked",
        "date": dt,
        "needs_manual_verification": True,
    }
    module._close_debug_chrome = lambda: calls.append("closed") or True
    result = module.collect("2026-08-22")
    assert result["needs_manual_verification"] is True
    assert calls == []
