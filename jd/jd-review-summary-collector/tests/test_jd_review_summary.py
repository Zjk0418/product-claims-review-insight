import importlib.util
import inspect
import re
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "jd_review_summary.py"
SPEC = importlib.util.spec_from_file_location("jd_review_summary", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_public_signature_is_date_only():
    assert list(inspect.signature(MODULE.run).parameters) == ["dt"]


def test_runtime_is_isolated_inside_jd_project():
    project_root = SCRIPT.parents[2]
    assert MODULE.PROJECT_ROOT == project_root
    assert MODULE.RUNTIME_ROOT == project_root / "runtime"
    assert MODULE.CHROME_PROFILE == project_root / "runtime" / "browser-profile"
    assert MODULE._checkpoint_path("2026-08-16") == (
        project_root / "runtime" / "review-summary" / "2026-08-16" / "checkpoint.jsonl"
    )


def test_feishu_secrets_are_loaded_from_environment():
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'APP_SECRET = os.environ.get("FEISHU_APP_SECRET"' in source
    assert not re.search(r'^APP_SECRET\s*=\s*["\'][^"\']+["\']', source, re.MULTILINE)
    assert not re.search(r'^BASE_TOKEN\s*=\s*["\'][^"\']+["\']', source, re.MULTILINE)


def test_date_and_spu_validation():
    assert MODULE._validate_date("2026-08-16") == "2026-08-16"
    assert MODULE._normalize_spu("100047005216") == "100047005216"
    assert MODULE._normalize_spu_input("https://item.jd.com/100047005216.html") == "100047005216"
    assert MODULE._normalize_spu_inputs(["100047005216,100047005216", "100012043978"]) == [
        "100047005216",
        "100012043978",
    ]


def test_direct_spu_collection_does_not_construct_feishu_client(monkeypatch, tmp_path):
    class UnexpectedFeishuClient:
        def __init__(self):
            raise AssertionError("直采模式不应读取飞书配置")

    def unexpected_env_read(_path):
        raise AssertionError("直采模式不应读取 .env")

    async def fake_collect(spus, checkpoint, signed_only=False):
        summaries = {
            spu: MODULE.ReviewSummary(spu, "10万+", "超99%买家赞不绝口", "batch")
            for spu in spus
        }
        for summary in summaries.values():
            MODULE._append_checkpoint(checkpoint, summary)
        return summaries, []

    monkeypatch.setattr(MODULE, "FeishuClient", UnexpectedFeishuClient)
    monkeypatch.setattr(MODULE, "_load_env_file", unexpected_env_read)
    monkeypatch.setattr(MODULE, "_collect_summaries", fake_collect)
    monkeypatch.setattr(MODULE, "RUNTIME_ROOT", tmp_path)

    result = MODULE.collect_spus(
        ["https://item.jd.com/100047005216.html", "100012043978"],
        run_name="ai-direct-test",
    )

    assert result["status"] == "complete"
    assert result["mode"] == "direct_spu"
    assert result["successful_spus"] == 2
    assert Path(result["results_file"]).exists()
    assert Path(result["checkpoint_file"]).exists()


def test_checkpoint_preserves_full_good_rate_display(tmp_path):
    checkpoint = tmp_path / "checkpoint.jsonl"
    MODULE._append_checkpoint(
        checkpoint,
        MODULE.ReviewSummary("100047005216", "10万+", "超99%买家赞不绝口", "batch"),
    )
    loaded = MODULE._load_checkpoint(checkpoint)
    assert loaded["100047005216"].good_rate == "超99%买家赞不绝口"


def test_parse_current_api_nested_envelope():
    payload = {
        "code": "0",
        "data": {"showType": 2, "allCntStr": "10万+", "goodRate": "99%"},
    }
    result = MODULE._parse_current_summary("100047005216", payload)
    assert result.review_count == "10万+"
    assert result.good_rate == "超99%买家赞不绝口"
    assert result.source == "getLegoWareDetailComment"


def test_parse_batch_api_and_requested_filter():
    payload = {
        "CommentsCount": [
            {"SkuId": 100047005216, "CommentCountStr": "10万+", "GoodRateShow": 99},
            {"SkuId": 999999999999, "CommentCountStr": "1万+", "GoodRateShow": 98},
        ]
    }
    result = MODULE._parse_batch_summaries(payload, {"100047005216"})
    assert list(result) == ["100047005216"]
    assert result["100047005216"].review_count == "10万+"
    assert result["100047005216"].good_rate == "超99%买家赞不绝口"


def test_rate_normalization():
    assert MODULE._normalize_rate(99) == "99%"
    assert MODULE._normalize_rate(0.985) == "98.5%"
    assert MODULE._normalize_rate("超99%买家") == "99%"


def test_corrupted_replacement_char_count_is_rejected():
    assert MODULE._normalize_count("10\ufffd\ufffd+") == ""


def test_verification_detection():
    cases = [
        (403, "", "https://api.m.jd.com/"),
        (200, "请完成验证", "https://api.m.jd.com/"),
        (200, "", "https://cfe.m.jd.com/privatedomain/risk_handler/03101900/"),
    ]
    for status, text, final_url in cases:
        try:
            MODULE._raise_if_verification_required(status, text, final_url)
        except MODULE.JDVerificationRequired:
            continue
        raise AssertionError(f"未识别京东验证场景：{status}, {text}, {final_url}")


def test_normal_response_does_not_trigger_verification():
    MODULE._raise_if_verification_required(200, '{"code":"0"}', "https://api.m.jd.com/")


def test_feishu_date_filter_and_text_cells():
    assert MODULE._date_filter("2026-08-16") == 'CurrentValue.[日期]=TODATE("2026-08-16")'
    assert MODULE._extract_text_cell([{"type": "text", "text": "100047005216"}]) == "100047005216"


def test_jd_batch_response_is_decoded_as_gb18030_bytes():
    source = inspect.getsource(MODULE.CDPClient.batch_summaries)
    assert "response.arrayBuffer()" in source
    assert "TextDecoder('gb18030')" in source


def test_collect_summaries_supports_signed_only_resume():
    assert "signed_only" in inspect.signature(MODULE._collect_summaries).parameters
