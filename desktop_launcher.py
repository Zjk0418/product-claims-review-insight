"""Review Insight desktop entry point; no project secrets or generated files loaded."""
from __future__ import annotations

import argparse
import atexit
import io
import json
import os
import sys
import tempfile
import threading
import urllib.request
import webbrowser
from pathlib import Path

ASSETS = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
if str(ASSETS / "app") not in sys.path:
    sys.path.insert(0, str(ASSETS / "app"))


class DesktopSession:
    def __init__(self, *, restore_history: bool = True):
        # Explicitly configured desktop credentials only, never the development .env.
        os.environ["REVIEW_APP_MODE"] = "1"
        os.environ["REVIEW_DESKTOP_BROWSER"] = "1"
        sys.dont_write_bytecode = True
        import server

        initialize = getattr(server, "initialize_history_cache", None)
        if restore_history and callable(initialize):
            initialize()

        self.module = server
        self.httpd = server.create_app_server(host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="review-local-server", daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.httpd.server_address[1]) + "/"
        self._closed = False
        self._lock = threading.Lock()
        atexit.register(self.close)

    def close(self, *_args):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        self.module.cleanup_session()


def _product_history_cache_check() -> bool:
    """Exercise the frozen cache backend only inside a disposable private root."""
    from product_cache import ProductCache

    try:
        with tempfile.TemporaryDirectory(prefix="review-cache-self-test-") as temporary:
            base = Path(temporary)
            product_id = "9912345678901"
            original_date = "2026-10-29T12:00:00+08:00"
            review = {"review_id": "cache-test-1", "product_id": product_id, "star": 1,
                      "date": "2026-10-28", "sku_id": "cache-test-sku", "sku": "标准款",
                      "content": "包装破损", "text": "包装破损", "valid_for_analysis": True}
            issue = {"issue_id": "cache-issue-1", "review_id": review["review_id"], "object": "包装",
                     "aspect": "包装与物流", "manifestation": "包装破损", "evidence": review["content"], "basis": "explicit"}
            cluster = {"cluster_id": "cache-cluster-1", "aspect": issue["aspect"], "problem_name": "包装破损",
                       "review_ids": [review["review_id"]], "member_issue_ids": [issue["issue_id"]],
                       "review_evidence": {review["review_id"]: review["content"]}}
            summary = {"product_id": product_id, "analysis_status": "completed", "created_at": original_date,
                       "valid_review_count": 1, "issue_count": 1, "cluster_count": 1,
                       "claim_count": 0, "input": {"digest": "cache-self-test-input"}}
            export = base / "source" / f"tmall_{product_id}_self_test.json"
            analysis = base / "source" / "analysis" / product_id
            analysis.mkdir(parents=True)
            export.write_text(json.dumps({"product_id": product_id, "product": {"title": "缓存自检商品"},
                "reviews": [review], "collection_info": {"finished_at": original_date,
                    "scanned_count": 1, "matched_count": 1}}, ensure_ascii=False), encoding="utf-8")
            payloads = {
                "analysis_summary.json": summary,
                "cleaned_reviews.json": {"product_id": product_id, "reviews": [review]},
                "statistics.json": {"clusters": [cluster]},
                "issue_clusters.json": {"clusters": [cluster]},
                "issues_flat.json": {"issues": [issue]},
                "extracted_issues.json": {"reviews": [{**review, "status": "ok", "errors": [], "issues": [issue]}]},
            }
            for name, payload in payloads.items():
                (analysis / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            # Both instances receive an explicit temporary path. Never open the
            # user's default store during a packaging or --self-test run.
            cache_root = base / "private-cache"
            saved = ProductCache(cache_root).save(product_id, [export], analysis)
            restored = ProductCache(cache_root).restore(product_id, base / "restored" / "exports",
                                                         base / "restored" / "analysis")
            if not restored or not saved.get("has_analysis") or not restored.get("has_analysis"):
                return False
            destination = Path(restored["analysis_dir"])
            return (restored["snapshot_id"] == saved["snapshot_id"]
                    and restored["input_digest"] == summary["input"]["digest"]
                    and restored["analysis_created_at"] == original_date
                    and restored["collection_finished_at"] == original_date
                    and len(restored["export_files"]) == 1
                    and Path(restored["export_files"][0]).read_bytes() == export.read_bytes()
                    and all((destination / name).read_bytes() == (analysis / name).read_bytes() for name in payloads))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def self_test(session: DesktopSession) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(session.url, timeout=10) as response:
        page = response.read().decode("utf-8")
    # Import application features to expose missing frozen dependencies without API calls.
    from api_settings import SETTINGS
    from cdp_endpoint import get_cdp_host
    import numpy
    import onnxruntime
    import tokenizers
    import sklearn
    import rapidocr_onnxruntime
    import reportlab
    import openpyxl
    from analysis.embeddings import BGEEmbedder
    from analysis.claim_dedup import group_near_duplicate_claims
    from reporting import artifact_bytes, build_report_model
    from intervention import classify_page

    vector = BGEEmbedder().encode(["包装破损"])
    groups, group_map = group_near_duplicate_claims([
        {"claim_id": "a", "normalized_claim": "防水耐用"},
        {"claim_id": "b", "normalized_claim": "防水耐用"},
    ])
    grouping_check = (len(groups) == 1 and group_map.get("a") == group_map.get("b")
                      and groups[0].get("grouping", {}).get("status") == "ok")
    model = build_report_model(
        summary={"product_id": "self-test", "analysis_status": "completed", "valid_review_count": 1,
                 "created_at": "2026-10-29T12:00:00+08:00"},
        product={"id": "self-test", "title": "安装包自检样例"},
        reviews=[{"review_id": "test1", "star": 1, "content": "包装破损", "valid_for_analysis": True,
                  "date": "2026-10-28", "sku_id": "self-test-sku"}],
        statistics={"clusters": [{"cluster_id": "test", "aspect": "包装与物流", "problem_name": "包装破损",
                                  "review_ids": ["test1"], "review_evidence": {"test1": "包装破损"}}]},
        extraction={"reviews": [{"review_id": "test1", "status": "ok", "errors": [], "content": "包装破损"}]},
    )
    generated = {kind: artifact_bytes(model, kind) for kind in (
        "report_html", "report_pdf", "reviews_xlsx", "reviews_json", "product_json"
    )}
    artifact_check = (generated["report_pdf"].startswith(b"%PDF")
                      and generated["reviews_xlsx"].startswith(b"PK")
                      and b"<html" in generated["report_html"].lower())
    shopping_check = (model["overview"]["shopping"]["negative_review_count"] == 1
                      and model["aspects"][0]["claim_count"] == 0
                      and model["aspects"][0]["shopping"]["attention_level"] == "single")
    sample_check = (model["overview"]["unadvertised_risk"]["review_count"] == 1
                    and model["overview"]["sample_risk"]["scanned"]["rate"] == 1
                    and b"sampleDenominator" in generated["report_html"])
    recurrence_check = (model["issue_evidence"]["version"] == "issue-recurrence-v1"
                        and set(model["issue_evidence"]["windows"]) == {"7", "30", "90"}
                        and b"evidenceWindow" in generated["report_html"])
    probability = model["issue_evidence"]["windows"]["30"]["overall"].get("probability_estimate") or {}
    probability_check = (probability.get("status") == "estimated" and probability.get("estimate") == .75
                         and len(probability.get("interval") or []) == 2
                         and model["issue_evidence"]["windows"]["30"]["overall"].get("purchase_probability") is None)
    decision = model["report_decision"]["windows"]["30"]
    decision_check = (decision["advertised"]["index"] is None
                      and decision["unadvertised"]["index"] == 75.0
                      and decision["overall"]["index"] == 75.0
                      and decision["overall"]["is_probability"] is False
                      and all(token in generated["report_html"] for token in
                              (b'overallDecision', b'advertisedSection', b'extraSection')))
    page_check = classify_page({"href": "https://detail.tmall.com/item.htm?id=123",
        "ready_state": "interactive", "has_product_title": True, "has_product_price": True}, "123")["ok"]
    history_cache_check = _product_history_cache_check()

    settings = SETTINGS.public()
    try:
        get_cdp_host()
        requires_owned_browser = False
    except RuntimeError:
        requires_owned_browser = True
    return {
        "status": "ok" if "<html" in page.lower() else "failed",
        "frozen": bool(getattr(sys, "frozen", False)),
        "asset_model_present": (ASSETS / "models/bge-small-zh-v1.5/model.onnx").is_file(),
        "collector_asset_present": (ASSETS / "taobao-jd-review-cdp/taobao-tmall/taobao-tmall-review-exporter/scripts/tmall_rate_auto.py").is_file(),
        "pdf_preview_assets_present": all((ASSETS / "app/static" / name).is_file() for name in (
            "pdf-preview.html", "pdf-preview.mjs", "artifact-preview.css", "pdfjs/pdf.min.mjs", "pdfjs/pdf.worker.min.mjs")),
        "desktop_api_mode": os.environ.get("REVIEW_APP_MODE") == "1",
        "desktop_cdp_requires_owned_browser": requires_owned_browser,
        "api_configured": settings["configured"],
        "api_key_returned": "api_key" in settings,
        "local_http_ready": True,
        "bge_vector_shape": list(vector.shape),
        "bge_claim_grouping_ready": grouping_check,
        "report_decision_ready": decision_check,
        "product_history_cache_ready": history_cache_check,
        "shopping_without_claim_ready": shopping_check,
        "sample_rate_summary_ready": sample_check,
        "issue_recurrence_ready": recurrence_check,
        "conditional_probability_ready": probability_check,
        "interactive_product_ready": page_check,
        "artifacts_generated_in_memory": artifact_check,
        "artifact_sizes": {key: len(value) for key, value in generated.items()},
    }


def browser_controller(session: DesktopSession) -> None:
    """Fallback remains easy to stop, so temporary files have a defined lifetime."""
    import tkinter as tk
    from tkinter import ttk

    window = tk.Tk()
    window.title("商品宣传与体验落差分析")
    window.geometry("430x200")
    window.resizable(False, False)
    body = ttk.Frame(window, padding=22)
    body.pack(fill="both", expand=True)
    ttk.Label(body, text="应用已在浏览器中打开", font=("Microsoft YaHei", 13, "bold")).pack(anchor="w")
    ttk.Label(body, text="请保持此窗口开启。关闭时将清理本次临时产物。", wraplength=380).pack(anchor="w", pady=(12, 18))
    buttons = ttk.Frame(body)
    buttons.pack(fill="x")
    ttk.Button(buttons, text="打开应用", command=lambda: webbrowser.open(session.url)).pack(side="left")
    ttk.Button(buttons, text="退出并清理", command=window.destroy).pack(side="right")
    webbrowser.open(session.url)
    window.mainloop()


def run_window(session: DesktopSession, *, browser_only: bool = False) -> str:
    if not browser_only:
        try:
            import webview

            webview.settings["ALLOW_DOWNLOADS"] = True
            window = webview.create_window("商品宣传与体验落差分析", session.url, width=1360, height=900,
                                           min_size=(940, 640), background_color="#f4f5f7")
            storage = session.module.SESSION.root / "browser-cache"
            storage.mkdir(parents=True, exist_ok=True)
            webview.start(debug=False, private_mode=True, storage_path=str(storage))
            return "embedded"
        except Exception:
            # Existing Windows machines without WebView2 can still use the local app.
            pass
    browser_controller(session)
    return "browser"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="商品宣传落差分析桌面应用")
    parser.add_argument("--browser", action="store_true", help="在系统浏览器中使用")
    parser.add_argument("--self-test", action="store_true", help="不打开窗口的安装包自检")
    parser.add_argument("--self-test-output", type=Path, help="自检结果保存位置（仅显式使用时写入）")
    args = parser.parse_args(argv)
    # Windowed builds have no std streams; imported libraries can still print safely.
    if sys.stdout is None:
        sys.stdout = io.StringIO()
    if sys.stderr is None:
        sys.stderr = io.StringIO()
    session = None
    try:
        session = DesktopSession(restore_history=not args.self_test)
        if args.self_test:
            result = self_test(session)
            success = (result["status"] == "ok" and result["asset_model_present"] and result["collector_asset_present"]
                       and result["pdf_preview_assets_present"]
                       and result["desktop_cdp_requires_owned_browser"]
                       and result["bge_claim_grouping_ready"] and result["shopping_without_claim_ready"]
                       and result["sample_rate_summary_ready"] and result["interactive_product_ready"]
                       and result["issue_recurrence_ready"]
                       and result["conditional_probability_ready"]
                       and result["report_decision_ready"]
                       and result["product_history_cache_ready"]
                       and result["bge_vector_shape"] == [1, 512] and result["artifacts_generated_in_memory"])
            result["status"] = "ok" if success else "failed"
            if args.self_test_output:
                args.self_test_output.parent.mkdir(parents=True, exist_ok=True)
                args.self_test_output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                print(json.dumps(result, ensure_ascii=False))
            return 0 if success else 1
        run_window(session, browser_only=args.browser)
        return 0
    except Exception as exc:
        message = "启动失败（" + type(exc).__name__ + "）。请检查程序文件是否完整。"
        if args.self_test_output:
            args.self_test_output.write_text(json.dumps({"status": "failed", "error": message}, ensure_ascii=False), encoding="utf-8")
        elif not args.self_test:
            try:
                import tkinter as tk
                from tkinter import messagebox

                root = tk.Tk()
                root.withdraw()
                messagebox.showerror("商品宣传落差分析", message)
                root.destroy()
            except Exception:
                pass
        return 1
    finally:
        if session is not None:
            session.close()


if __name__ == "__main__":
    raise SystemExit(main())
