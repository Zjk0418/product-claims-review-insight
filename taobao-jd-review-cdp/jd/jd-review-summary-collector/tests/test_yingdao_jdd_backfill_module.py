import importlib.util
import inspect
import asyncio
import ast
import re
import sys
import types
from pathlib import Path


MODULE_PATH = (
    Path(__file__).parents[2] / "yingdao" / "JDD_京东评价历史补数据_影刀版.py"
)


def _load_module():
    xbot = types.ModuleType("xbot")
    xbot.print = lambda *_args: None
    xbot.sleep = lambda *_args: None
    sys.modules["xbot"] = xbot

    package_root = types.ModuleType("jdd_backfill_test_package")
    package_root.__path__ = []
    package = types.ModuleType("jdd_backfill_test_package.package")
    package.variables = {}
    package_root.package = package
    sys.modules["jdd_backfill_test_package"] = package_root
    sys.modules["jdd_backfill_test_package.package"] = package

    spec = importlib.util.spec_from_file_location(
        "jdd_backfill_test_package.jdd_backfill_module", MODULE_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_backfill_public_interface_accepts_start_and_end_for_197_days():
    calls = []

    def collect_backfill_date(target_date):
        calls.append(target_date)
        return {"status": "no_target_records", "date": target_date}

    module = _load_module()
    module.collect_backfill_date = collect_backfill_date
    assert list(inspect.signature(module.backfill).parameters) == [
        "start_date",
        "end_date",
    ]
    result = module.backfill("2026-02-01", "2026-08-16")
    assert result["status"] == "complete"
    assert result["total_dates"] == 197
    assert result["no_target_dates"] == 197
    assert calls[0] == "2026-02-01"
    assert calls[-1] == "2026-08-16"
    assert len(calls) == 197


def test_backfill_retries_only_failed_dates_on_second_pass():
    attempts = {}

    def collect_backfill_date(target_date):
        attempts[target_date] = attempts.get(target_date, 0) + 1
        if target_date == "2026-05-01" and attempts[target_date] == 1:
            return {
                "status": "blocked",
                "needs_manual_verification": False,
                "error": "temporary failure",
            }
        return {"status": "no_target_records", "date": target_date}

    module = _load_module()
    module.collect_backfill_date = collect_backfill_date
    result = module.backfill("2026-02-01", "2026-08-16")
    assert result["status"] == "complete"
    assert attempts["2026-05-01"] == 2
    assert sum(attempts.values()) == 198


def test_backfill_stops_on_manual_verification_without_notification_or_close():
    calls = []

    def collect_backfill_date(target_date):
        calls.append(target_date)
        return {
            "status": "blocked",
            "needs_manual_verification": True,
            "error": "需要登录",
        }

    module = _load_module()
    module.collect_backfill_date = collect_backfill_date
    module.FeishuNotifier = lambda: (_ for _ in ()).throw(
        AssertionError("补数据入口不应调用登录通知")
    )
    module._close_debug_chrome = lambda: (_ for _ in ()).throw(
        AssertionError("补数据入口不应关闭浏览器")
    )
    result = module.backfill("2026-02-01", "2026-08-16")
    assert result["status"] == "blocked"
    assert result["blocked_date"] == "2026-02-01"
    assert len(result["remaining_dates"]) == 197
    assert calls == ["2026-02-01"]


def test_backfill_module_is_standalone_and_does_not_import_jdd_claw():
    module = _load_module()
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "from . import JDD_claw" not in source
    assert callable(module.collect_backfill_date)
    assert callable(module._collect_summaries)
    assert callable(module._write_results)
    assert (module.JD_SIGNED_ITEM_DELAY_MIN, module.JD_SIGNED_ITEM_DELAY_MAX) == (
        1.2,
        2.0,
    )
    assert (module.JD_BATCH_DELAY_MIN, module.JD_BATCH_DELAY_MAX) == (4.0, 7.0)
    assert (module.BACKFILL_DATE_DELAY_MIN, module.BACKFILL_DATE_DELAY_MAX) == (
        5.0,
        9.0,
    )


def test_backfill_module_is_python37_and_accepts_yingdao_prefix():
    source = MODULE_PATH.read_text(encoding="utf-8")
    ast.parse(source, filename=str(MODULE_PATH), feature_version=(3, 7))
    compile("from .package import variables as glv\n" + source, "<module>", "exec")


def test_backfill_module_has_no_hardcoded_feishu_secrets():
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert 'APP_SECRET = os.environ.get("FEISHU_APP_SECRET"' in source
    assert 'NOTIFY_APP_SECRET = os.environ.get("FEISHU_NOTIFY_APP_SECRET"' in source
    assert not re.search(r'^APP_SECRET\s*=\s*["\'][^"\']+["\']', source, re.MULTILINE)
    assert not re.search(r'^NOTIFY_APP_SECRET\s*=\s*["\'][^"\']+["\']', source, re.MULTILINE)


def test_backfill_date_parameters_accept_slashes_and_reject_reverse_range():
    module = _load_module()
    assert module._backfill_dates("2026/02/01", "2026/02/03") == [
        "2026-02-01",
        "2026-02-02",
        "2026-02-03",
    ]
    try:
        module._backfill_dates("2026-02-03", "2026-02-01")
    except ValueError as exc:
        assert "不能晚于" in str(exc)
    else:
        raise AssertionError("反向日期区间必须拒绝")


def test_backfill_partial_date_does_not_write_zero_for_failed_spu():
    module = _load_module()
    targets = [
        module.TargetRecord("rec1", "1001", "2026-02-01"),
        module.TargetRecord("rec2", "1002", "2026-02-01"),
    ]
    module.FeishuClient = lambda: object()
    module._read_targets = lambda _dt, _client: targets

    async def collect(_spus):
        return {
            "1001": module.ReviewSummary(
                "1001", "10万+", "超99%买家赞不绝口", "batch"
            )
        }, ["1002"]

    written = {}

    def write(_targets, summaries, _client):
        written.update(summaries)
        return len(summaries)

    module._collect_summaries = collect
    module._write_results = write
    result = module.collect_backfill_date("2026-02-01")
    assert result["status"] == "partial"
    assert result["failed_spus"] == ["1002"]
    assert set(written) == {"1001"}
