"""本地 Web 服务：输入商品 URL，按数量/时间采集 <=3 星评价，输出 JSON。"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import time
import uuid
import io
import html
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, quote

import requests
from resources import resource_root
from session_workspace import SessionWorkspace
from api_settings import SETTINGS, test_connection
from cdp_endpoint import get_cdp_host
import product as product_module

from cdp_tabs import find_tab, focus_tab, open_foreground_tab
from collector import collect, inspect_product_page, validate_checkpoint
from importer import validate_import, write_imported_export
from intervention import build as build_intervention
from job_store import JobStore
from product_cache import ProductCache, ProductCacheError
import history_cache
from product_links import ProductLinkError, resolve_product_input, resolve_share_in_browser

os.environ.setdefault("REVIEW_APP_MODE", "1")
ROOT = resource_root()
APP_DIR = ROOT / "app"
INDEX_FILE = APP_DIR / "static" / "index.html"
SESSION = SessionWorkspace(reap_abandoned=False)
PRODUCT_CACHE = ProductCache()
CACHE_ENABLED = False
CACHE_SOURCES: dict[str, dict[str, Any]] = {}
EXPORT_DIR = SESSION.root / "runtime" / "exports"
EXPORT_DIR.mkdir(parents=True, exist_ok=True)
ANALYSIS_DIR = SESSION.root / "runtime" / "analysis"
ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

HOST = "127.0.0.1"
PORT = int(os.getenv("PORT", "8787"))

ANALYSIS_JOBS: dict[str, "AnalysisJob"] = {}
LOCK = threading.Lock()
ACTIVE_ANALYSIS: str | None = None
STORE = JobStore(SESSION.root)
LEGACY_STORE = JobStore(ROOT) if (ROOT / "runtime/jobs").is_dir() else None
LEGACY_EXPORT_DIR = ROOT / "runtime/exports"
product_module.ROOT = SESSION.root

# 采集阶段到中文说明的映射（前端进度条用）
COLLECT_STAGE_LABELS = {
    "connecting": "连接调试浏览器",
    "navigating": "打开商品页",
    "access": "确认数据访问",
    "fetching": "读取评价分页",
    "waiting": "翻页等待",
    "backoff": "触发限流，等待重试",
    "page_done": "本页完成",
    "product": "采集商品信息与图片",
    "done": "结束",
}

# 平台验证链接（x5secdata）有时效，超过这个秒数就认为不能再用
VALIDATE_URL_TTL = int(os.getenv("TMALL_VALIDATE_URL_TTL", "600"))

CONNECTION_ERROR_TOKENS = ("connectionclosed", "websocket", "连接中断", "断线", "timeout", "timed out")


@dataclass
class AnalysisJob:
    id: str
    product_id: str
    status: str = "queued"  # queued / running / success / failed / blocked
    stage: str = "等待开始"
    step: str = ""
    summary: dict[str, Any] | None = None
    error: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    steps: list[dict[str, Any]] = field(default_factory=list)
    progress: float = 0.0


STAGE_LABELS = {
    "preprocess": "清洗与去重",
    "category": "识别商品品类",
    "extract": "LLM 抽取负面问题",
    "embed": "BGE 向量化",
    "cluster": "问题聚类",
    "statistics": "统计高频问题",
    "claims": "抽取宣传主张",
    "claim_groups": "宣传主张分组",
    "gaps": "宣传—体验落差判断",
}

# 每个阶段在总进度里占的区间（百分比），用于把细粒度计数换算成进度条宽度。
STAGE_SPAN = {
    "preprocess": (0, 6),
    "category": (6, 10),
    "extract": (10, 40),
    "embed": (40, 46),
    "cluster": (46, 56),
    "statistics": (56, 60),
    "claims": (60, 80),
    "claim_groups": (80, 85),
    "gaps": (85, 99),
}
STAGE_DONE_STATUSES = {"ok", "loaded", "skipped", "blocked", "manual_gold"}
STAGE_STATUS_RANK = {"pending": 0, "running": 1, "ok": 2, "loaded": 2, "skipped": 2,
                     "blocked": 2, "manual_gold": 2, "failed": 3}


def stage_detail(entry: dict[str, Any]) -> str:
    """把流水线回调里的附加信息压成一行可读文案。"""
    done, total = entry.get("done"), entry.get("total")
    if isinstance(done, int) and isinstance(total, int) and total > 0:
        return f"{done}/{total}"
    for key, unit in (("issue_count", "个问题"), ("claim_count", "条主张"),
                      ("gap_count", "条判断"), ("cluster_count", "个问题簇"),
                      ("claim_group_count", "组")):
        value = entry.get(key)
        if isinstance(value, int):
            return f"{value} {unit}"
    reason = str(entry.get("reason") or "")
    return {
        "missing_llm_config": "未配置模型，已跳过",
        "no_clusters": "无可用问题簇",
        "no_issues": "未抽取到问题",
    }.get(reason, "")


def stage_percent(step: str, status: str, entry: dict[str, Any]) -> float:
    start, end = STAGE_SPAN.get(step, (0, 100))
    if status in STAGE_DONE_STATUSES:
        return float(end)
    done, total = entry.get("done"), entry.get("total")
    if isinstance(done, int) and isinstance(total, int) and total > 0:
        return round(start + (end - start) * min(1.0, done / total), 1)
    return float(start)


def record_stage(job: "AnalysisJob", entry: dict[str, Any]) -> None:
    """把一次进度回调合并进 job.steps，并按阶段推进总进度（不允许回退）。"""
    step = str(entry.get("step") or "")
    if not step:
        return
    status = str(entry.get("status") or "running")
    label = STAGE_LABELS.get(step, step)
    detail = stage_detail(entry)
    job.step = step
    job.stage = label
    existing = next((row for row in job.steps if row["step"] == step), None)
    if existing is None:
        job.steps.append({"step": step, "label": label, "status": status, "detail": detail})
    else:
        accepted = STAGE_STATUS_RANK.get(status, 0) >= STAGE_STATUS_RANK.get(existing["status"], 0)
        if accepted:
            existing["status"] = status
            if detail:
                existing["detail"] = detail
        elif detail and not existing["detail"]:
            existing["detail"] = detail
    job.progress = max(job.progress, stage_percent(step, status, entry))


def _relative(path: str) -> str:
    if not path:
        return ""
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path


def _seconds_since(stamp: str) -> int | None:
    """本地时间戳（%Y-%m-%dT%H:%M:%S）距今多少秒；解析失败返回 None。"""
    try:
        return max(0, int(time.time() - time.mktime(time.strptime(str(stamp), "%Y-%m-%dT%H:%M:%S"))))
    except (ValueError, TypeError, OverflowError):
        return None


APP_VERSION = "3.17.1"


def initialize_history_cache(*, cache=None, roots=None) -> list[dict]:
    """Only normal app startup migrates user history, never a module import/test."""
    global PRODUCT_CACHE, CACHE_ENABLED
    if cache is not None:
        PRODUCT_CACHE = cache
    CACHE_ENABLED = True
    if roots is None:
        roots = [ROOT]
        for folder in SESSION.base.glob("session-*"):
            if folder == SESSION.root or folder.is_symlink():
                continue
            marker = history_cache.read_json(folder / ".review-session.json")
            if (re.fullmatch(r"[0-9a-f]{32}", str(marker.get("token") or ""))
                    and folder.resolve().parent == SESSION.base):
                roots.append(folder)
    return history_cache.migrate_roots(PRODUCT_CACHE, list(roots))


def _select_cached_snapshot(product_id: str) -> dict | None:
    if not CACHE_ENABLED:
        return None
    manifest = PRODUCT_CACHE.get(product_id)
    if not manifest:
        return None
    selected = CACHE_SOURCES.get(product_id)
    if selected and selected.get("snapshot_id") == manifest["snapshot_id"] and Path(selected["analysis_dir"]).parent.exists():
        return selected
    base = SESSION.root / "restored" / product_id / manifest["snapshot_id"]
    try:
        restored = PRODUCT_CACHE.restore(product_id, base / "exports", base / "analysis", base / "products" / product_id)
    except (OSError, ProductCacheError):
        return None
    if not restored:
        return None
    restored["analysis_available"] = bool(restored.get("has_analysis") and history_cache.usable_analysis(
        product_id, Path(restored["analysis_dir"]), [Path(p) for p in restored["export_files"]]))
    CACHE_SOURCES[product_id] = restored
    return restored


def _cached_record(manifest: dict, *, job_id: str | None = None) -> dict:
    pid = manifest["product_id"]
    files = [Path(p) for p in manifest.get("export_files", [])]
    source = history_cache.read_json(files[-1]) if files else {}
    collection = source.get("collection_info") or {}
    product = source.get("product") or {"id": pid, "title": manifest.get("product_title", "")}
    ready = manifest.get("analysis_available", manifest.get("has_analysis", False))
    return {"id": job_id or "cache" + pid, "product_id": pid, "product_title": product.get("title", ""),
        "product_url": source.get("product_url") or f"https://detail.tmall.com/item.htm?id={pid}",
        "status": "completed", "stage": "完成", "message": "", "mode": "history", "value": 0, "unit": "",
        "scope": source.get("scope") or {}, "source": "history", "cache_hit": True, "historical_view": True,
        "analysis_available": bool(ready), "created_at": manifest.get("analysis_created_at") or manifest.get("stored_at", ""),
        "updated_at": manifest.get("stored_at", ""), "export_path": str(files[-1]) if files else "",
        "summary": {"product_id": pid, "product_title": product.get("title", ""), "product": product,
                    "promotional_claims": source.get("promotional_claims") or [], "collection_info": collection,
                    "scope": source.get("scope") or {}},
        "progress": {"scanned": collection.get("scanned_count", manifest.get("scanned_count")),
                     "matched": collection.get("matched_count", manifest.get("matched_count")),
                     "pages": collection.get("pages_fetched"), "api_total": collection.get("api_total"),
                     "oldest_date": collection.get("oldest_scanned_date", ""), "newest_date": collection.get("newest_scanned_date", "")},
        "review_sufficiency": collection.get("review_sufficiency") or {}, "access": collection.get("access") or {}}


def _persist_history(product_id: str, *, analysis_dir=None, exports=None) -> dict | None:
    if not CACHE_ENABLED:
        return None
    try:
        if analysis_dir is not None and exports is not None:
            if not history_cache.usable_analysis(product_id, analysis_dir, exports):
                return None
            product_dir = Path(CACHE_SOURCES[product_id]["product_dir"]) if product_id in CACHE_SOURCES and CACHE_SOURCES[product_id].get("product_dir") else SESSION.root / "runtime/products" / product_id
            manifest = PRODUCT_CACHE.save(product_id, exports, analysis_dir, product_dir)
        else:
            manifest = history_cache.save_product(PRODUCT_CACHE, product_id, SESSION.root)
        if manifest:
            for record in STORE.list_recent(200):
                if record.get("product_id") == product_id and record.get("source_input"):
                    history_cache.remember_share(PRODUCT_CACHE, record["source_input"], product_id)
        return manifest
    except (OSError, ProductCacheError, ValueError):
        return None


def cdp_health() -> dict[str, Any]:
    session = requests.Session()
    session.trust_env = False
    try:
        data = session.get(f"{get_cdp_host()}/json/version", timeout=3).json()
        return {"cdp": True, "browser": data.get("Browser", ""), "version": APP_VERSION}
    except Exception as exc:  # noqa: BLE001
        return {"cdp": False, "browser": "", "version": APP_VERSION, "error": str(exc)[:160]}


def job_payload(record: dict[str, Any]) -> dict[str, Any]:
    """统一的采集任务返回结构，供创建/查询/恢复接口复用。"""
    payload = STORE.summarize(record)
    export_path = record.get("export_path") or ""
    payload.update(
        {
            "download_url": f"/api/jobs/{record['id']}/download" if export_path or record.get("cache_hit") else "",
            "saved_to": "",
            "legacy": bool(record.get("_legacy")),
            "temporary": not bool(record.get("_legacy") or record.get("cache_hit")),
            "cache_hit": bool(record.get("cache_hit")),
            "historical_view": bool(record.get("historical_view")),
            "analysis_available": bool(record.get("analysis_available")),
        }
    )
    return payload


def _get_job(job_id: str) -> dict[str, Any] | None:
    if not job_id or not job_id.isalnum():
        return None
    record = STORE.get(job_id)
    if record:
        return record
    if CACHE_ENABLED and job_id.startswith("cache") and job_id[5:].isdigit():
        snapshot = _select_cached_snapshot(job_id[5:])
        return _cached_record(snapshot) if snapshot else None
    old = LEGACY_STORE.get(job_id) if LEGACY_STORE else None
    return {**old, "_legacy": True} if old else None


def _export_files(product_id: str) -> list[Path]:
    selected = CACHE_SOURCES.get(product_id) if CACHE_ENABLED else None
    if selected:
        return [Path(p) for p in selected["export_files"]]
    current = sorted(EXPORT_DIR.glob(f"tmall_{product_id}_*.json"))
    return current or sorted(LEGACY_EXPORT_DIR.glob(f"tmall_{product_id}_*.json"))


def _job_export_path(record: dict[str, Any]) -> Path | None:
    raw = str(record.get("export_path") or "")
    if not raw:
        return None
    candidate = Path(raw)
    allowed = [EXPORT_DIR.resolve(), LEGACY_EXPORT_DIR.resolve(), (SESSION.root / "restored").resolve()]
    if candidate.exists() and any(candidate.resolve().is_relative_to(base) for base in allowed):
        return candidate
    fallback = LEGACY_EXPORT_DIR / candidate.name
    return fallback if record.get("_legacy") and fallback.is_file() else None


def _load_checkpoint(job_id: str, record: dict[str, Any]) -> dict[str, Any] | None:
    if not record.get("resume_count") and record.get("status") != "resuming":
        return None
    checkpoint = STORE.load_checkpoint(job_id)
    if not isinstance(checkpoint, dict):
        return checkpoint
    # A focus/recheck action may have moved this task to a replacement tab
    # without collecting another page, so its checkpoint can retain the old ID.
    # Only hand off metadata belonging to this exact task and product; leave
    # mismatched checkpoints untouched for the normal restore validation.
    same_task = str(record.get("id") or "") == str(job_id)
    product_id = str(record.get("product_id") or "")
    same_product = bool(product_id) and str(checkpoint.get("product_id") or "") == product_id
    latest_tab = str(record.get("tab_id") or "")
    if same_task and same_product and latest_tab and latest_tab != str(checkpoint.get("tab_id") or ""):
        checkpoint = dict(checkpoint)
        checkpoint["tab_id"] = latest_tab
        # Ownership of the old tab must never transfer to the replacement:
        # it may now be the foreground page where the user logged in/verified.
        checkpoint["tab_owned"] = False
    return checkpoint


def _job_message(
    job_status: str,
    info: dict[str, Any],
    access: dict[str, Any] | None,
) -> str:
    """把「采集状态 + 数据访问状态」翻译成一句用户能看懂的话。

    页面能打开 ≠ 评价接口能读，所以这里按 access 分别给不同结论，
    不再统一显示"恢复失败，请重试"。
    """
    access = access or {}
    status = access.get("status")
    next_page = int(info.get("next_page") or 1)
    if job_status == "cancelled":
        return "用户取消任务"
    if job_status == "awaiting_user":
        if status == "still_blocked":
            reason = access.get("reason_code")
            if reason == "validate":
                return (
                    f"页面已恢复，但第 {next_page} 页的评价接口仍要求人工验证；"
                    "本次恢复没有写入任何数据，断点保持不变。"
                )
            if reason == "traffic":
                return f"评价接口仍在限流；本次恢复没有写入任何数据，断点保持在 {next_page} 页。"
            return "评价接口仍返回异常；本次恢复没有写入任何数据。"
        if status == "login_required":
            return "当前调试浏览器的登录状态无效，请在采集浏览器中重新登录后再继续。"
        if status == "browser_error":
            return "调试浏览器连接中断；断点已保留，重新打开浏览器后可继续。"
        return str(info.get("stop_reason") or "采集已暂停")
    return str(info.get("stop_reason") or "")


def _is_connection_failure(*texts: str) -> bool:
    blob = " ".join(str(item or "") for item in texts).lower()
    return any(token in blob for token in CONNECTION_ERROR_TOKENS)


def run_job(job_id: str) -> None:
    """执行或恢复一个采集任务；所有状态变化都写盘。"""
    record = STORE.get(job_id)
    if not record:
        return
    checkpoint = _load_checkpoint(job_id, record)

    STORE.update(
        job_id,
        status="collecting",
        stage="连接浏览器",
        message="正在确认浏览器与数据访问状态…" if checkpoint else "",
        intervention=None,
        last_error="",
    )

    def on_progress(payload: dict[str, Any]) -> None:
        stage = str(payload.get("stage") or "")
        extra: dict[str, Any] = {}
        if isinstance(payload.get("access"), dict):
            extra["access"] = payload["access"]
        STORE.patch_progress(
            job_id,
            {
                "scanned": payload.get("scanned"),
                "matched": payload.get("matched"),
                "pages": payload.get("pages"),
                "api_total": payload.get("api_total"),
                "api_total_page": payload.get("api_total_page"),
                "oldest_date": payload.get("oldest_date", ""),
                "newest_date": payload.get("newest_date", ""),
                "next_page": payload.get("next_page"),
            },
            stage=COLLECT_STAGE_LABELS.get(stage, stage or "采集中"),
            message=str(payload.get("message") or ""),
            **extra,
        )

    def on_checkpoint(snapshot: dict[str, Any]) -> None:
        STORE.save_checkpoint(job_id, snapshot)
        STORE.update(job_id, tab_id=str(snapshot.get("tab_id") or ""))

    try:
        if os.getenv("REVIEW_DESKTOP_BROWSER") == "1":
            from browser_session import COLLECTION_BROWSER, persistent_profile
            STORE.update(
                job_id,
                stage="启动浏览器",
                message="正在启动独立的采集浏览器；首次启动可能需要几十秒，请勿重复点击。",
            )
            # 持久化配置目录：登录一次后 Cookie 保留，下次不必重新扫码。
            COLLECTION_BROWSER.ensure(
                SESSION.root, record["product_url"], profile_dir=persistent_profile()
            )
            # 采集全程在后台标签页完成，窗口挪出屏幕并最小化，避免反复抢焦点。
            from cdp_tabs import hide_window

            hide_window()
        result = asyncio.run(
            collect(
                product_url=record["product_url"],
                mode=record["mode"],
                value=record["value"],
                unit=record["unit"],
                page_size=50,
                max_scan=record["value"]
                if record["mode"] == "count"
                else int(os.getenv("TMALL_MAX_SCAN", "500")),
                progress=on_progress,
                state=checkpoint,
                on_checkpoint=on_checkpoint,
                cancel_check=lambda: STORE.cancel_requested(job_id),
            )
        )
    except Exception as exc:  # noqa: BLE001
        message = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}: {exc}"
        STORE.update(
            job_id,
            status="failed",
            stage="失败",
            message=message[:300],
            last_error=message[:500],
        )
        return

    info = result["collection_info"]
    status = str(info.get("status") or "failed")
    intervention = result.get("intervention")

    summary = {
        "product_id": result["product_id"],
        "product_title": result["product"].get("title", ""),
        "product": result.get("product", {}),
        "promotional_claims": result.get("promotional_claims", []),
        "product_info": info.get("product_info", {}),
        "scope": result["scope"],
        "collection_info": info,
    }

    export_path = ""
    if status in {"success", "partial"} and result.get("reviews"):
        stamp = time.strftime("%Y%m%d_%H%M%S")
        scope_tag = (
            f"count{record['value']}"
            if record["mode"] == "count"
            else f"time{result['scope']['days']}d"
        )
        filename = f"tmall_{result['product_id']}_lowstar_{scope_tag}_{stamp}.json"
        path = EXPORT_DIR / filename
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        export_path = str(path)

    if status == "success":
        job_status = "completed"
        stage = "采集完成"
    elif status == "partial":
        job_status = "partial"
        stage = "采集部分完成"
    elif status in {"awaiting_user", "rate_limited"}:
        job_status = "awaiting_user"
        stage = "等待人工介入"
    elif status == "cancelled":
        job_status = "cancelled"
        stage = "已取消"
    else:
        job_status = "failed"
        stage = "失败"

    if job_status == "awaiting_user":
        intervention = intervention or build_intervention(
            "unknown", result["product_id"], detail=info.get("stop_reason", "")
        )

    last_error = ""
    if job_status == "failed":
        last_error = "；".join(
            [str(info.get("stop_reason") or "")]
            + [str(item) for item in (info.get("errors") or [])[:3]]
        )[:500]

    access = info.get("access") if isinstance(info.get("access"), dict) else None
    resumable = bool(
        job_status == "failed"
        and STORE.load_checkpoint(job_id)
        and _is_connection_failure(last_error)
    )

    STORE.update(
        job_id,
        status=job_status,
        stage=stage,
        message=_job_message(job_status, info, access),
        progress={
            "scanned": info.get("scanned_count"),
            "matched": info.get("matched_count"),
            "pages": info.get("pages_fetched"),
            "api_total": info.get("api_total"),
            "api_total_page": info.get("api_total_page"),
            "oldest_date": info.get("oldest_scanned_date", ""),
            "newest_date": info.get("newest_scanned_date", ""),
            "next_page": info.get("next_page"),
        },
        intervention=intervention if job_status == "awaiting_user" else None,
        summary=summary,
        export_path=export_path,
        tab_id=str(result.get("collection_info", {}).get("tab_id") or "") or record.get("tab_id", ""),
        review_sufficiency=info.get("review_sufficiency", {}),
        access=access or {"status": "not_checked"},
        resumable=resumable,
        last_error=last_error,
        cancel_requested=bool(job_status == "cancelled"),
    )

    if job_status == "awaiting_user":
        # 登录/过验证这类暂停，等用户处理完就自动继续，不必再回程序点一次。
        schedule_auto_resume(job_id, str((intervention or {}).get("kind") or ""))
    elif job_status in {"completed", "partial"}:
        _persist_history(str(record.get("product_id") or ""))


def _thread_start(job_id: str) -> None:
    threading.Thread(target=run_job, args=(job_id,), daemon=True).start()


def _active_collection() -> dict[str, Any] | None:
    return STORE.find_active()


# ---------- 登录后自动继续 ----------

# 只在这些暂停原因下自动等待登录；每 5 秒看一次登录态，避免打扰用户操作。
AUTO_RESUME_KINDS = {"login_required", "platform_validate", "page_unexpected"}
AUTO_RESUME_INTERVAL = 5.0
AUTO_RESUME_TIMEOUT = 900.0
_auto_resume_tokens: dict[str, int] = {}


def cancel_auto_resume(job_id: str) -> None:
    """手动继续 / 取消任务时作废正在等待的自动继续。"""
    _auto_resume_tokens[job_id] = _auto_resume_tokens.get(job_id, 0) + 1


def schedule_auto_resume(job_id: str, kind: str) -> None:
    if kind not in AUTO_RESUME_KINDS:
        return
    token = _auto_resume_tokens.get(job_id, 0) + 1
    _auto_resume_tokens[job_id] = token
    threading.Thread(
        target=_auto_resume_loop, args=(job_id, token), name="auto-resume", daemon=True
    ).start()


def _auto_resume_loop(job_id: str, token: int) -> None:
    from cdp_tabs import login_state

    deadline = time.time() + AUTO_RESUME_TIMEOUT
    while time.time() < deadline:
        time.sleep(AUTO_RESUME_INTERVAL)
        if _auto_resume_tokens.get(job_id) != token:
            return
        record = STORE.get(job_id)
        if not record or record.get("status") != "awaiting_user":
            return
        state = login_state()
        if not state.get("available") or not state.get("logged_in") or state.get("on_login_page"):
            continue
        with LOCK:
            ok, _message = STORE.begin_resume(job_id)
            if not ok:
                return
            STORE.update(
                job_id,
                status="resuming",
                stage="自动继续采集",
                message="检测到登录已完成，正在自动继续采集（无需手动点击）。",
            )
        _thread_start(job_id)
        return


# ---------- 分析任务 ----------

def _analysis_paths(product_id: str) -> dict[str, Path]:
    base = ANALYSIS_DIR / product_id
    return {
        "base": base,
        "summary": base / "analysis_summary.json",
        "statistics": base / "statistics.json",
        "claims": base / "extracted_claims.json",
        "gaps": base / "claim_gaps.json",
        "quality": base / "data_quality.json",
        "reviews": base / "cleaned_reviews.json",
        "clusters": base / "issue_clusters.json",
        "extracted": base / "extracted_issues.json",
        "flat": base / "issues_flat.json",
        "log": base / "analysis_log.json",
        "bundle": base / "analysis_bundle.json",
    }


def _analysis_read_paths(product_id: str) -> dict[str, Path]:
    current = _analysis_paths(product_id)
    selected = CACHE_SOURCES.get(product_id) if CACHE_ENABLED else None
    if selected and selected.get("analysis_available"):
        base = Path(selected["analysis_dir"])
        return {key: base if key == "base" else base / path.name for key, path in current.items()}
    if current["summary"].exists() or current["reviews"].exists():
        return current
    legacy = ROOT / "runtime/analysis" / product_id
    if (legacy / "analysis_summary.json").is_file():
        return {key: legacy if key == "base" else legacy / path.name for key, path in current.items()}
    return current


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _claim_dedupe_key(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(text or ""))


def _append_manual_claims(export_path: Path, lines: list[str]) -> int:
    """把用户手动补充的宣传语写进采集文件的 promotional_claims（source=manual）。

    重复内容（归一化后相同）不重复添加；返回实际新增条数。
    """
    data = _read_json(export_path)
    if not isinstance(data, dict):
        data = {"product": {"id": export_path.stem}, "promotional_claims": []}
    product = data.get("product") if isinstance(data.get("product"), dict) else {}
    existing = data.get("promotional_claims") or []
    if not isinstance(existing, list):
        existing = []
    pool = {_claim_dedupe_key(item.get("text") if isinstance(item, dict) else item) for item in existing}
    added = 0
    for line in lines:
        text = str(line or "").strip()
        key = _claim_dedupe_key(text)
        if not text or key in pool:
            continue
        existing.append({"text": text, "source": "manual", "confidence": 1.0})
        pool.add(key)
        added += 1
    if added:
        data["promotional_claims"] = existing
        product["promotional_claims"] = existing
        data["product"] = product
        export_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return added


def _analysis_ready(product_id: str) -> bool:
    result = load_analysis_result(product_id)
    return bool(result and result["freshness"]["is_current"])


def _analysis_freshness(product_id: str, payloads: dict[str, Any]) -> dict[str, Any]:
    """现场核对产物，不依赖可能已经过期的离线检查报告。"""
    from analysis import claim_extractor, gap_analyzer, issue_extractor
    from analysis.clustering import CLUSTER_RULE_VERSION
    from analysis.pipeline import clusters_fingerprint, input_digest
    from analysis.preprocessing import load_reviews

    summary = payloads.get("summary") or {}
    reasons: list[str] = []
    required = ["quality", "reviews", "statistics", "claims"]
    if summary.get("issue_count", 0):
        required.extend(["clusters", "extracted", "flat"])
    if summary.get("gap_count", 0) or (summary.get("stages") or {}).get("gaps") == "ok":
        required.append("gaps")
    for key in required:
        if not isinstance(payloads.get(key), dict):
            reasons.append(f"缺少或无法读取产物：{_analysis_paths(product_id)[key].name}")
    if summary.get("analysis_status") != "completed" or summary.get("errors"):
        reasons.append("上次分析未完整完成")
    extraction_rows = (payloads.get("extracted") or {}).get("reviews") or []
    if summary.get("valid_review_count", 0) and extraction_rows and all(not row.get("issues") and (row.get("errors") or row.get("status") in {
            "failed", "validation_failed", "blocked_missing_llm", "error"}) for row in extraction_rows):
        reasons.append("评论问题抽取全部失败，尚无可用分析")

    claims = payloads.get("claims") or {}
    gaps = payloads.get("gaps") or {}
    clusters = payloads.get("clusters") or {}
    if summary.get("issue_count", 0):
        if (clusters.get("params") or {}).get("rule_version") != CLUSTER_RULE_VERSION:
            reasons.append(f"问题簇规则版本已过期或未记录（当前 {CLUSTER_RULE_VERSION}）")
        if (summary.get("algorithm_params") or {}).get("rule_version") != CLUSTER_RULE_VERSION:
            reasons.append(f"分析摘要的聚类规则版本已过期或未记录（当前 {CLUSTER_RULE_VERSION}）")
        try:
            extracted = [issue for row in (payloads.get("extracted") or {}).get("reviews", [])
                         for issue in row.get("issues", [])]
            flat = (payloads.get("flat") or {}).get("issues", [])
            source_ids = [row["issue_id"] for row in extracted]
            flat_ids = [row["issue_id"] for row in flat]
            members = [iid for row in clusters.get("clusters", []) for iid in row.get("member_issue_ids", [])]
            if not (
                len(source_ids) == len(set(source_ids)) == len(flat_ids) == len(set(flat_ids))
                == len(members) == len(set(members)) == summary["issue_count"]
                and set(source_ids) == set(flat_ids) == set(members)
            ):
                reasons.append("抽取问题、扁平问题和问题簇的 ID 未完整一一对应")
            source_by_id = {row["issue_id"]: row for row in extracted}
            fields = ("review_id", "object", "aspect", "direction", "evidence")
            if any(row["issue_id"] in source_by_id and any(
                row.get(field) != source_by_id[row["issue_id"]].get(field) for field in fields
            ) for row in flat):
                reasons.append("抽取问题与下游问题的内容或规范化标签不一致")
        except (KeyError, TypeError, AttributeError):
            reasons.append("问题引用产物格式损坏，无法核对 ID")
    versions = {
        "问题抽取": ((summary.get("model") or {}).get("prompt_version"), issue_extractor.PROMPT_VERSION),
        "宣传主张": (claims.get("prompt_version"), claim_extractor.PROMPT_VERSION),
    }
    if "gaps" in required:
        versions["落差判断"] = (gaps.get("prompt_version"), gap_analyzer.PROMPT_VERSION)
        fingerprint = clusters_fingerprint(clusters)
        if gaps.get("clusters_fingerprint") != fingerprint or (
            summary.get("fingerprints") or {}
        ).get("clusters") != fingerprint:
            reasons.append("落差与当前问题簇的指纹不一致或缺失")
    for label, (actual, expected) in versions.items():
        if str(actual or "").split(":")[0] != expected:
            reasons.append(f"{label}版本已过期或未记录（当前 {expected}）")
    for count_key, payload_key, items_key in (
        ("cluster_count", "clusters", "clusters"),
        ("claim_count", "claims", "claims"),
        ("gap_count", "gaps", "gaps"),
    ):
        if summary.get(count_key) != len((payloads.get(payload_key) or {}).get(items_key) or []):
            reasons.append(f"{count_key} 与产物条数不一致")
    recorded_digest = (summary.get("input") or {}).get("digest")
    if not recorded_digest or recorded_digest != (payloads.get("reviews") or {}).get("input_digest"):
        reasons.append("分析输入指纹缺失或与清洗数据不一致")
    exports = _export_files(product_id)
    if exports and recorded_digest:
        try:
            reviews, _ = load_reviews([str(path) for path in exports], product_id=product_id)
            if input_digest(reviews) != recorded_digest:
                reasons.append("该商品历史评论已有更新，需要重新分析")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            reasons.append(f"无法核对当前历史评论：{type(exc).__name__}")
    return {
        "is_current": not reasons,
        "status": "current" if not reasons else "stale_or_incomplete",
        "reasons": reasons,
        "input_scope": "product_history_merged",
    }


def load_analysis_result(product_id: str) -> dict[str, Any] | None:
    """把分析产物整理成前端需要的结构；只读，不改原始文件。"""
    paths = _analysis_read_paths(product_id)
    summary = _read_json(paths["summary"])
    if not isinstance(summary, dict):
        return None
    payloads = {key: _read_json(paths[key]) for key in (
        "summary", "statistics", "claims", "gaps", "quality", "reviews", "clusters", "extracted", "flat"
    )}
    payloads = {key: value if isinstance(value, dict) else None for key, value in payloads.items()}
    freshness = _analysis_freshness(product_id, payloads)
    statistics = payloads["statistics"] or {}
    claims_payload = payloads["claims"] or {}
    gaps_payload = payloads["gaps"] or {}
    quality = payloads["quality"] or {}
    extraction_rows = (payloads["extracted"] or {}).get("reviews") or []
    legacy_failed = bool(summary.get("valid_review_count", 0) and extraction_rows) and all(not row.get("issues") and (row.get("errors") or row.get("status") in {
        "failed", "validation_failed", "blocked_missing_llm", "error"}) for row in extraction_rows)

    clusters = {c.get("cluster_id"): c for c in statistics.get("clusters", [])}
    top_problems = []
    for item in statistics.get("top_problems", [])[:8]:
        cluster = clusters.get(item.get("cluster_id"), {})
        top_problems.append(
            {
                **item,
                "object": cluster.get("object", ""),
                "description": cluster.get("description", ""),
                "evidence": (cluster.get("representative_reviews") or [])[:3],
            }
        )

    claims = claims_payload.get("claims") or []
    gaps = gaps_payload.get("gaps") or []
    return {
        "product_id": product_id,
        "cache_hit": bool(CACHE_ENABLED and product_id in CACHE_SOURCES),
        "historical_view": bool(CACHE_ENABLED and product_id in CACHE_SOURCES),
        "analysis_available": bool(CACHE_ENABLED and CACHE_SOURCES.get(product_id, {}).get("analysis_available")),
        "failure_reason": summary.get("failure_reason") or ("模型连接失败，评论问题尚未抽取完成。请重试分析或打开已有记录。" if legacy_failed else ""),
        "retryable": bool(summary.get("retryable")),
        "generated_at": summary.get("created_at", ""),
        "status": summary.get("analysis_status", "") if freshness["is_current"] else "stale_or_incomplete",
        "original_status": "failed" if legacy_failed else summary.get("analysis_status", ""),
        "freshness": freshness,
        "category": summary.get("category") or {},
        "denominators": summary.get("denominators") or {},
        "quality": {
            "valid_review_count": quality.get("valid_review_count"),
            "unique_review_count": quality.get("unique_review_count"),
            "low_information_count": quality.get("low_information_count"),
            "star_distribution": quality.get("star_distribution"),
            "date_range": quality.get("date_range"),
            "warnings": quality.get("warnings", []),
        },
        "top_problems": top_problems,
        "issues": {
            "issue_count": summary.get("issue_count", 0),
            "reviews_with_issues": summary.get("reviews_with_issues", 0),
            "cluster_count": summary.get("cluster_count", 0),
        },
        "claims": [
            {
                "claim_id": c.get("claim_id"),
                "claim_text": c.get("normalized_claim") or c.get("original_text"),
                "aspect": c.get("aspect"),
                "claim_type": c.get("claim_type"),
                "source": c.get("source"),
                "confidence": c.get("confidence"),
                "verifiable": c.get("verifiable"),
            }
            for c in claims
        ],
        "claim_by_type": summary.get("claim_by_type", {}),
        "gaps": gaps,
        "gap_by_type": summary.get("gap_by_type", {}),
        "limitations": summary.get("limitations", []),
        "files": {
            "summary": paths["summary"].name,
            "statistics": paths["statistics"].name
            if paths["statistics"].exists()
            else "",
            "claims": paths["claims"].name
            if paths["claims"].exists()
            else "",
            "gaps": paths["gaps"].name
            if paths["gaps"].exists()
            else "",
        },
    }


def analysis_bundle_bytes(product_id: str) -> bytes:
    """显式下载时在内存组装 JSON，不把导出文件写入会话或用户目录。"""
    paths = _analysis_read_paths(product_id)
    bundle = {
        "product_id": product_id,
        "freshness": (load_analysis_result(product_id) or {}).get("freshness"),
        "summary": _read_json(paths["summary"]),
        "data_quality": _read_json(paths["quality"]),
        "statistics": _read_json(paths["statistics"]),
        "claims": _read_json(paths["claims"]),
        "gaps": _read_json(paths["gaps"]),
    }
    return json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8")


def _merge_report_review_metadata(reviews: Any, export: Any) -> Any:
    """Restore whitelisted metadata lost by older preprocessing, by review ID.

    Only the export selected for this report is a source. Cleaned text, analysis
    eligibility and other analysis fields remain authoritative; display names
    never stand in for stable reviewer identifiers.
    """
    rows = reviews.get("reviews") if isinstance(reviews, dict) else reviews
    source_rows = export.get("reviews") if isinstance(export, dict) else None
    if not isinstance(rows, list) or not isinstance(source_rows, list):
        return reviews
    metadata_fields = (
        "sku_id", "product_id", "append_content", "append_date", "parent_review_id",
        "original_review_id", "reviewer_id", "buyer_id", "user_id",
    )
    text_fields = {"append_content", "append_date"}
    by_review_id: dict[str, dict[str, Any]] = {}
    for raw in source_rows:
        if not isinstance(raw, dict):
            continue
        rid = str(raw.get("review_id") or "").strip()
        if not rid:
            continue
        metadata = by_review_id.setdefault(rid, {})
        for key in metadata_fields:
            value = raw.get(key)
            if isinstance(value, str):
                if not value.strip():
                    continue
            elif key in text_fields or not isinstance(value, int) or isinstance(value, bool):
                continue
            metadata.setdefault(key, value)
    merged_rows = []
    for row in rows:
        if not isinstance(row, dict):
            merged_rows.append(row)
            continue
        merged = dict(row)
        rid = str(row.get("review_id") or "").strip()
        for key, value in by_review_id.get(rid, {}).items():
            if merged.get(key) in (None, ""):
                merged[key] = value
        merged_rows.append(merged)
    return {**reviews, "reviews": merged_rows} if isinstance(reviews, dict) else merged_rows


class AnalysisUnavailableError(RuntimeError):
    pass


def report_model(product_id: str) -> dict[str, Any]:
    from reporting import build_report_model
    result = load_analysis_result(product_id)
    if result is None:
        raise FileNotFoundError("该商品还没有分析结果")
    paths = _analysis_read_paths(product_id)
    payload = {key: _read_json(paths[key]) or {} for key in ("summary", "statistics", "claims", "gaps", "reviews")}
    extraction = _read_json(paths["extracted"])
    extraction_rows = (extraction or {}).get("reviews") or []
    all_failed = bool(payload["summary"].get("valid_review_count", 0) and extraction_rows) and all(not row.get("issues") and (row.get("errors") or row.get("status") in {
        "failed", "validation_failed", "blocked_missing_llm", "error"}) for row in extraction_rows)
    if payload["summary"].get("analysis_status") in {"failed", "blocked_missing_llm"} or all_failed:
        raise AnalysisUnavailableError(payload["summary"].get("failure_reason") or "问题抽取未完成，请重试分析或打开已有的分析记录。")
    source_files = _export_files(product_id)
    hint = Path(str(payload["claims"].get("input_export") or "")).name
    chosen = next((path for path in source_files if path.name == hint), source_files[-1] if source_files else None)
    export = _read_json(chosen) if chosen else {}
    product = dict((export or {}).get("product") or {})
    product["promotional_claims"] = (export or {}).get("promotional_claims") or []
    product.setdefault("id", product_id)
    return build_report_model(payload["summary"], payload["statistics"], payload["claims"], payload["gaps"],
                              _merge_report_review_metadata(payload["reviews"], export), product,
                              freshness=result["freshness"], collection=(export or {}).get("collection_info") or {},
                              issues=_read_json(paths["flat"]), extraction=extraction)


def cleanup_session() -> None:
    from browser_session import COLLECTION_BROWSER
    COLLECTION_BROWSER.close()
    if CACHE_ENABLED:
        for folder in ANALYSIS_DIR.iterdir():
            if folder.is_dir() and folder.name.isdigit():
                _persist_history(folder.name)
    for record in STORE.list_recent(200):
        if record.get("status") in {"queued", "collecting", "awaiting_user", "resuming"}:
            STORE.request_cancel(record["id"])
    SESSION.cleanup()


def clear_session() -> None:
    global SESSION, STORE, EXPORT_DIR, ANALYSIS_DIR, ACTIVE_ANALYSIS
    with LOCK:
        if _active_collection() or any(job.status in {"queued", "running"} for job in ANALYSIS_JOBS.values()):
            raise ValueError("请先结束当前任务，再清理会话")
        from browser_session import COLLECTION_BROWSER
        COLLECTION_BROWSER.close()
        if CACHE_ENABLED:
            for folder in ANALYSIS_DIR.iterdir():
                if folder.is_dir() and folder.name.isdigit():
                    _persist_history(folder.name)
        session_base = SESSION.base
        SESSION.cleanup()
        SESSION = SessionWorkspace(session_base, reap_abandoned=False)
        CACHE_SOURCES.clear()
        EXPORT_DIR = SESSION.root / "runtime/exports"
        ANALYSIS_DIR = SESSION.root / "runtime/analysis"
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
        STORE = JobStore(SESSION.root)
        product_module.ROOT = SESSION.root
        ANALYSIS_JOBS.clear()
        ACTIVE_ANALYSIS = None


def run_analysis_job(job_id: str, product_id: str, category: str | None) -> None:
    global ACTIVE_ANALYSIS
    job = ANALYSIS_JOBS[job_id]
    job.status = "running"
    job.stage = "准备中"

    def on_progress(entry: dict[str, Any]) -> None:
        with LOCK:
            record_stage(job, entry)

    try:
        from analysis.pipeline import run_pipeline
        input_files = _export_files(product_id)
        summary = run_pipeline(
            product_id=product_id,
            category=category,
            progress=on_progress,
            inputs=[str(path) for path in input_files],
            out_root=ANALYSIS_DIR,
            exports_dir=input_files[0].parent if input_files else EXPORT_DIR,
        )
        if summary.get("analysis_status") == "completed":
            manifest = _persist_history(product_id, analysis_dir=_analysis_paths(product_id)["base"], exports=input_files)
            if manifest and CACHE_ENABLED:
                CACHE_SOURCES.pop(product_id, None)
                _select_cached_snapshot(product_id)
        with LOCK:
            job.summary = summary
            job.finished_at = time.time()
            if summary.get("analysis_status") == "blocked_missing_llm":
                job.status = "blocked"
                job.error = "未配置 LLM_API_KEY，已跳过模型调用；清洗与质量报告仍已生成"
            elif summary.get("analysis_status") == "completed":
                result = load_analysis_result(product_id)
                if result and (result["freshness"]["is_current"] or result.get("analysis_available")):
                    job.status = "success"
                    job.stage = "完成"
                    job.progress = 100.0
                else:
                    job.status = "failed"
                    job.error = "分析产物未通过完整性检查，请重新分析：" + "；".join(
                        (result or {}).get("freshness", {}).get("reasons", ["分析产物缺失"])
                    )
            else:
                job.status = "failed"
                job.error = summary.get("failure_reason") or "；".join(summary.get("errors") or [])[:400] or "分析未完成"
    except Exception as exc:  # noqa: BLE001
        with LOCK:
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            job.finished_at = time.time()
    finally:
        with LOCK:
            ACTIVE_ANALYSIS = None



class Handler(BaseHTTPRequestHandler):
    server_version = "LowStarReview/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        message = fmt % args
        if "/api/health" in message or "/favicon.ico" in message:
            return
        print(f"[HTTP] {message}")

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        self._send_bytes(path.read_bytes(), "application/json; charset=utf-8", path.name, download=True)

    def _send_bytes(self, body: bytes, mime: str, filename: str = "", *, download: bool = False, csp: str = "") -> None:
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if filename:
            disposition = "attachment" if download else "inline"
            self.send_header("Content-Disposition", f"{disposition}; filename=artifact{Path(filename).suffix}; filename*=UTF-8''{quote(filename)}")
        if mime.startswith("text/html"):
            self.send_header("Content-Security-Policy", csp or "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _local_request(self, *, mutation: bool = False) -> bool:
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "").lower() not in allowed:
            self._send_json({"error": "仅接受本机应用请求"}, 403)
            return False
        origin = self.headers.get("Origin")
        if mutation and origin:
            parsed = urlparse(origin)
            if parsed.scheme != "http" or parsed.netloc.lower() not in allowed:
                self._send_json({"error": "拒绝跨来源修改应用数据"}, 403)
                return False
        return True

    def _artifact_get(self, product_id: str, action: str, artifact_id: str = "") -> None:
        try:
            from reporting import artifact_catalog, artifact_bytes, render_preview_html
            model = report_model(product_id)
            catalog = artifact_catalog(model)
            base = f"/api/analysis/product/{product_id}"
            if action == "artifacts":
                items = [{**item, "label": item.get("name", item["id"]), "format": Path(item["filename"]).suffix[1:],
                          "mime_type": item.get("mime"), "preview_url": f"{base}/pdf-preview" if item['id'] == 'report_pdf' else f"{base}/preview/{item['id']}",
                          "download_url": f"{base}/export/{item['id']}"} for item in catalog]
                self._send_json({"artifacts": items, "report_url": f"{base}/preview/report_html", "temporary": True})
                return
            item = next((row for row in catalog if row["id"] == artifact_id), None)
            if item is None:
                self._send_json({"error": "产物不存在"}, 404)
                return
            if action == "export" or artifact_id == "report_pdf":
                self._send_bytes(artifact_bytes(model, artifact_id), item["mime"], item["filename"], download=action == "export")
            else:
                self._send_bytes(render_preview_html(model, artifact_id).encode("utf-8"), "text/html; charset=utf-8")
        except AnalysisUnavailableError as exc:
            self._send_json({"error": str(exc), "kind": "analysis_incomplete"}, 409)
        except FileNotFoundError:
            self._send_json({"error": "请先完成分析，再预览报告"}, 404)
        except (ImportError, RuntimeError) as exc:
            self._send_json({"error": "报告组件不可用，请检查应用安装是否完整", "kind": type(exc).__name__}, 503)
        except (ValueError, KeyError, TypeError):
            self._send_json({"error": "报告数据不完整，请重新生成分析"}, 409)

    def _artifact_export(self, product_id: str, payload: dict[str, Any]) -> None:
        try:
            from reporting import artifact_catalog, artifact_bytes
            model = report_model(product_id)
            selected = payload.get("artifact_ids")
            catalog = {row["id"]: row for row in artifact_catalog(model)}
            if not isinstance(selected, list) or not selected or len(selected) > len(catalog) or any(
                not isinstance(key, str) or key not in catalog for key in selected
            ):
                self._send_json({"error": "请选择需要导出的有效产物"}, 400)
                return
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for key in dict.fromkeys(selected):
                    archive.writestr(catalog[key]["filename"], artifact_bytes(model, key))
            self._send_bytes(out.getvalue(), "application/zip", f"review_report_{product_id}.zip", download=True)
        except FileNotFoundError:
            self._send_json({"error": "分析结果不存在"}, 404)
        except (ValueError, KeyError, TypeError, ImportError, RuntimeError):
            self._send_json({"error": "导出未完成，请检查报告数据和应用组件"}, 409)

    def _send_binary(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self) -> None:
        if not INDEX_FILE.exists():
            self._send_json({"error": "index.html 不存在"}, 500)
            return
        body = INDEX_FILE.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if not self._local_request():
            return
        path = urlparse(self.path).path
        if path == "/":
            self._send_html()
            return
        if path.startswith("/static/"):
            allowed = {"/static/pdf-preview.mjs": ("pdf-preview.mjs", "text/javascript; charset=utf-8"),
                       "/static/workspace.css": ("workspace.css", "text/css; charset=utf-8"),
                       "/static/workspace-shell.js": ("workspace-shell.js", "text/javascript; charset=utf-8"),
                       "/static/hero-illustration.svg": ("hero-illustration.svg", "image/svg+xml; charset=utf-8"),
                       "/static/pdfjs/pdf.min.mjs": ("pdfjs/pdf.min.mjs", "text/javascript; charset=utf-8"),
                       "/static/pdfjs/pdf.worker.min.mjs": ("pdfjs/pdf.worker.min.mjs", "text/javascript; charset=utf-8")}
            asset = allowed.get(path)
            if not asset or not (APP_DIR / "static" / asset[0]).is_file():
                self._send_json({"error": "静态资源不存在"}, 404)
                return
            self._send_bytes((APP_DIR / "static" / asset[0]).read_bytes(), asset[1])
            return
        if path == "/api/health":
            self._send_json(cdp_health())
            return
        if path == "/api/settings":
            self._send_json(SETTINGS.public())
            return
        if path == "/api/session":
            self._send_json({"temporary": True, "configured": SETTINGS.public()["configured"],
                             "note": "本次会话临时处理，关闭应用自动清理；仅显式导出才保存文件"})
            return
        if path.startswith("/api/products/"):
            parts = path.strip("/").split("/")
            if len(parts) == 5 and parts[1] == "products" and parts[3] == "images":
                product_id, filename = parts[2], parts[4]
                if (
                    not product_id.isdigit()
                    or not filename
                    or "/" in filename
                    or "\\" in filename
                    or filename.startswith(".")
                ):
                    self._send_json({"error": "非法图片路径"}, 400)
                    return
                image_path = SESSION.root / "runtime" / "products" / product_id / "images" / filename
                selected = CACHE_SOURCES.get(product_id) if CACHE_ENABLED else None
                if selected and selected.get("product_dir"):
                    image_path = Path(selected["product_dir"]) / "images" / filename
                if not image_path.exists():
                    image_path = ROOT / "runtime" / "products" / product_id / "images" / filename
                if not image_path.exists() or not image_path.is_file():
                    self._send_json({"error": "图片不存在"}, 404)
                    return
                suffix = image_path.suffix.lower()
                content_type = {
                    ".png": "image/png",
                    ".webp": "image/webp",
                    ".gif": "image/gif",
                }.get(suffix, "image/jpeg")
                self._send_binary(image_path, content_type)
                return
        if path.startswith("/api/analysis/"):
            parts = path.strip("/").split("/")
            if len(parts) in {5, 6} and parts[2] == "product" and parts[3].isdigit():
                if len(parts) == 5 and parts[4] == "pdf-preview":
                    if not load_analysis_result(parts[3]):
                        self._send_json({"error": "请先完成分析，再预览报告"}, 404)
                        return
                    page = (APP_DIR / "static/pdf-preview.html").read_text(encoding="utf-8")
                    from reporting import preview_styles
                    page = page.replace("__PREVIEW_STYLES__", preview_styles())
                    page = page.replace("__PDF_URL__", f"/api/analysis/product/{parts[3]}/preview/report_pdf")
                    self._send_bytes(page.encode("utf-8"), "text/html; charset=utf-8",
                                     csp="default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; img-src data: blob:; font-src data: blob:; worker-src 'self' blob:; connect-src 'self'; base-uri 'none'")
                    return
                if len(parts) == 5 and parts[4] == "artifacts":
                    self._artifact_get(parts[3], "artifacts")
                    return
                if len(parts) == 6 and parts[4] in {"preview", "export"}:
                    self._artifact_get(parts[3], parts[4], parts[5])
                    return
            if len(parts) == 4 and parts[1] == "analysis" and parts[2] == "product":
                product_id = parts[3]
                if not product_id.isdigit():
                    self._send_json({"error": "商品 ID 不合法"}, 400)
                    return
                result = load_analysis_result(product_id)
                if not result:
                    self._send_json({"error": "该商品还没有分析结果"}, 404)
                    return
                self._send_json(result)
                return
            if (
                len(parts) == 5
                and parts[1] == "analysis"
                and parts[2] == "product"
                and parts[4] == "download"
            ):
                product_id = parts[3]
                if not product_id.isdigit() or not load_analysis_result(product_id):
                    self._send_json({"error": "该商品还没有分析结果"}, 404)
                    return
                self._send_bytes(analysis_bundle_bytes(product_id), "application/json; charset=utf-8", f"analysis_{product_id}.json", download=True)
                return
            if len(parts) == 3 and parts[1] == "analysis":
                job = ANALYSIS_JOBS.get(parts[2])
                if not job:
                    self._send_json({"error": "分析任务不存在"}, 404)
                    return
                with LOCK:
                    payload = {
                        "id": job.id,
                        "product_id": job.product_id,
                        "status": job.status,
                        "stage": job.stage,
                        "step": job.step,
                        "error": job.error,
                        "summary": job.summary,
                        "elapsed": round((job.finished_at or time.time()) - job.created_at, 1),
                        "progress": job.progress,
                        "steps": list(job.steps),
                        "plan": [{"step": key, "label": label} for key, label in STAGE_LABELS.items()],
                        "result_url": f"/api/analysis/{job.id}/result"
                        if job.status == "success"
                        else "",
                    }
                self._send_json(payload)
                return
            if len(parts) == 4 and parts[3] == "result":
                job = ANALYSIS_JOBS.get(parts[2])
                if not job or job.status != "success":
                    self._send_json({"error": "分析尚未完成"}, 409)
                    return
                result = load_analysis_result(job.product_id)
                if not result:
                    self._send_json({"error": "分析产物缺失"}, 404)
                    return
                self._send_json(result)
                return
            if len(parts) == 4 and parts[3] == "download":
                job = ANALYSIS_JOBS.get(parts[2])
                if not job or job.status != "success":
                    self._send_json({"error": "分析尚未完成"}, 409)
                    return
                self._send_bytes(analysis_bundle_bytes(job.product_id), "application/json; charset=utf-8", f"analysis_{job.product_id}.json", download=True)
                return
            self._send_json({"error": "not found"}, 404)
            return
        if path in {"/api/jobs", "/api/jobs/recent"}:
            records = STORE.list_recent(8)
            if LEGACY_STORE:
                known = {row["id"] for row in records}
                records.extend({**row, "_legacy": True} for row in LEGACY_STORE.list_recent(8) if row["id"] not in known)
            if CACHE_ENABLED:
                known = {row["id"] for row in records}
                historical = [_cached_record(item) for item in PRODUCT_CACHE.list()[:20]
                              if "cache" + item["product_id"] not in known]
                records = historical + records
            self._send_json(
                {
                    "jobs": [job_payload(record) for record in records],
                    "active": (lambda active: job_payload(active) if active else None)(
                        _active_collection()
                    ),
                }
            )
            return
        if path.startswith("/api/jobs/"):
            parts = path.strip("/").split("/")
            if len(parts) == 3:
                record = _get_job(parts[2])
                if not record:
                    self._send_json({"error": "任务不存在"}, 404)
                    return
                self._send_json(job_payload(record))
                return
            if len(parts) == 4 and parts[3] == "download":
                record = _get_job(parts[2])
                export_path = _job_export_path(record) if record else None
                if export_path is None:
                    self._send_json({"error": "结果文件不存在"}, 404)
                    return
                self._send_file(export_path)
                return
            self._send_json({"error": "not found"}, 404)
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_json({"error": "请求长度无效"}, 400)
            return
        if length < 0:
            self._send_json({"error": "请求长度无效"}, 400)
            return
        if length > 25 * 1024 * 1024:
            self._send_json({"error": "请求体过大（上限 25 MB）"}, 413)
            return
        # Read bounded content before a rejection, so closing the HTTP/1.0 connection
        # cannot reset an unread POST body and hide the 403 response on Windows.
        body = self.rfile.read(length)
        if not self._local_request(mutation=True):
            return
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            self._send_json({"error": "请求体不是合法 JSON"}, 400)
            return
        if not isinstance(payload, dict):
            self._send_json({"error": "请求体必须是 JSON 对象"}, 400)
            return
        if path in {"/api/settings", "/api/settings/test"}:
            try:
                self._send_json(SETTINGS.update(payload) if path == "/api/settings" else test_connection(payload))
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
            return
        if path == "/api/session/clear":
            try:
                clear_session()
                self._send_json({"ok": True, "temporary": True})
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 409)
            return
        export_match = re.fullmatch(r"/api/analysis/product/(\d+)/export", path)
        if export_match:
            self._artifact_export(export_match.group(1), payload)
            return

        manual_match = re.fullmatch(r"/api/product/(\d+)/claims/manual", path)
        if manual_match:
            self._handle_manual_claims(manual_match.group(1), payload)
            return

        if path == "/api/analysis":
            self._handle_analysis_create(payload)
            return

        if path == "/api/import":
            self._handle_import(payload)
            return

        action = re.fullmatch(r"/api/jobs/([0-9a-zA-Z]+)/(resume|recheck|cancel|focus)", path)
        if action:
            self._handle_job_action(action.group(1), action.group(2))
            return

        if path == "/api/jobs":
            self._handle_job_create(payload)
            return

        self._send_json({"error": "not found"}, 404)

    # ---------- 采集任务 ----------

    def _handle_job_create(self, payload: dict[str, Any]) -> None:
        if any(job.status in {"queued", "running"} for job in ANALYSIS_JOBS.values()):
            self._send_json({"error": "请等待当前分析完成，再创建采集任务"}, 409)
            return
        url = str(payload.get("url") or "").strip()
        mode = str(payload.get("mode") or "count")
        unit = str(payload.get("unit") or "day")
        try:
            value = int(payload.get("value") or 0)
        except (TypeError, ValueError):
            value = 0

        if not url:
            self._send_json({"error": "请填写商品 URL 或商品 ID"}, 400)
            return
        if mode not in {"count", "time"}:
            self._send_json({"error": "mode 只支持 count / time"}, 400)
            return
        if mode == "count" and not 1 <= value <= 300:
            self._send_json({"error": "数量范围应为 1–300 条"}, 400)
            return
        if mode == "time":
            if unit not in {"day", "month", "year"}:
                self._send_json({"error": "时间单位只支持 day / month / year"}, 400)
                return
            days = value * {"day": 1, "month": 30, "year": 365}[unit]
            if not 1 <= days <= 3650:
                self._send_json({"error": "时间范围应为 1 天–10 年"}, 400)
                return

        active = _active_collection()
        if active:
            self._send_json(
                {
                    "error": "已有采集任务未结束，请先继续或取消它",
                    "active_job": {
                        "id": active.get("id"),
                        "status": active.get("status"),
                        "product_id": active.get("product_id"),
                    },
                },
                409,
            )
            return

        try:
            # 淘系分享页已改为客户端渲染：HTTP 解析不到时，用调试浏览器读最终跳转地址。
            known_product = history_cache.lookup_share(PRODUCT_CACHE, url) if CACHE_ENABLED and not payload.get("force_refresh") else ""
            if known_product:
                product_id, product_url = known_product, f"https://detail.tmall.com/item.htm?id={known_product}"
            else:
                product_id, product_url = resolve_product_input(url, browser_resolver=resolve_share_in_browser)
        except ProductLinkError as exc:
            self._send_json({"error": str(exc)}, 400)
            return

        scope = (
            {"mode": "count", "count": value, "unit": "reviews"}
            if mode == "count"
            else {
                "mode": "time",
                "days": value * {"day": 1, "month": 30, "year": 365}[unit],
                "value": value,
                "unit": unit,
            }
        )
        with LOCK:
            # Resolving a share link can take seconds; recheck before creating work.
            if _active_collection() or any(job.status in {"queued", "running"} for job in ANALYSIS_JOBS.values()):
                self._send_json({"error": "已有采集或分析任务正在进行，请先完成或取消后再重试"}, 409)
                return
            record = STORE.create(
                product_url=product_url,
                product_id=product_id,
                mode=mode,
                value=value,
                unit=unit,
                scope=scope,
            )
            STORE.update(record["id"], source_input=url)
            if CACHE_ENABLED and not payload.get("force_refresh"):
                snapshot = _select_cached_snapshot(product_id)
                if snapshot:
                    history_cache.remember_share(PRODUCT_CACHE, url, product_id)
                    cached = _cached_record(snapshot, job_id=record["id"])
                    STORE.update(record["id"], **{key: value for key, value in cached.items() if key != "id"})
                    self._send_json(job_payload(STORE.get(record["id"]) or cached), 200)
                    return
            CACHE_SOURCES.pop(product_id, None)
        _thread_start(record["id"])
        self._send_json(job_payload(record), 202)

    def _handle_job_action(self, job_id: str, action: str) -> None:
        record = STORE.get(job_id)
        if not record:
            self._send_json({"error": "任务不存在"}, 404)
            return

        if action == "resume":
            status = str(record.get("status") or "")
            if status not in {"awaiting_user", "failed"}:
                self._send_json({"error": f"任务当前状态为 {status}，不能继续"}, 409)
                return
            if status == "failed" and not record.get("resumable"):
                self._send_json(
                    {
                        "error": (
                            "这个任务是异常结束后留下的（不是连接中断），不支持从断点继续；"
                            "已采集的数据不会删除，请取消后重新采集"
                        )
                    },
                    409,
                )
                return
            health = cdp_health()
            if not health.get("cdp"):
                self._send_json(
                    {
                        "error": "调试浏览器未连接，请先重新启动采集环境",
                        "intervention": build_intervention(
                            "browser_disconnected", str(record.get("product_id") or "")
                        ),
                    },
                    409,
                )
                return
            checkpoint = STORE.load_checkpoint(job_id)
            if not checkpoint:
                self._send_json(
                    {"error": "断点文件已丢失，无法从原进度继续；请取消后重新采集"}, 409
                )
                return
            task_pid = str(record.get("product_id") or "")
            verdict = validate_checkpoint(checkpoint, task_pid)
            if not verdict["ok"]:
                self._send_json(
                    {
                        "error": (
                            f"任务恢复信息不完整：{verdict['reason']}。已停止继续，"
                            "原始采集数据和断点文件都保留，可取消后重新采集"
                        ),
                        "kind": "checkpoint_invalid",
                        "checks": verdict["checks"],
                    },
                    409,
                )
                return
            busy = STORE.find_active()
            if (
                busy
                and str(busy.get("id") or "") != job_id
                and str(busy.get("status") or "") in {"queued", "collecting", "resuming"}
            ):
                self._send_json(
                    {
                        "error": "另一个采集任务正在运行，请等它结束或取消后再继续",
                        "active_job": {
                            "id": busy.get("id"),
                            "status": busy.get("status"),
                            "product_id": busy.get("product_id"),
                        },
                    },
                    409,
                )
                return
            cancel_auto_resume(job_id)  # 手动继续后，不再让后台自动恢复重复触发
            ok, message = STORE.begin_resume(job_id)
            if not ok:
                self._send_json({"error": message}, 409)
                return
            _thread_start(job_id)
            self._send_json(
                {
                    "id": job_id,
                    "status": "resuming",
                    "resume_count": record.get("resume_count", 0) + 1,
                    "next_page": verdict.get("next_page"),
                    "checks": verdict["checks"],
                },
                202,
            )
            return

        if action == "recheck":
            if record.get("status") not in {"awaiting_user", "failed"}:
                self._send_json(
                    {"error": f"任务当前状态为 {record.get('status')}，不需要检查页面"}, 409
                )
                return
            health = cdp_health()
            if not health.get("cdp"):
                self._send_json(
                    {
                        "ok": False,
                        "kind": "browser_disconnected",
                        "detail": "调试浏览器未连接",
                        "checks": [
                            {"name": "调试浏览器连接", "ok": False, "detail": "本次采集浏览器无响应"}
                        ],
                        "intervention": build_intervention(
                            "browser_disconnected", str(record.get("product_id") or "")
                        ),
                    },
                    200,
                )
                return
            result = asyncio.run(
                inspect_product_page(
                    str(record.get("product_url") or ""),
                    tab_id=str(record.get("tab_id") or ""),
                )
            )
            if result.get("tab_id"):
                STORE.update(job_id, tab_id=str(result["tab_id"]))
            page_ok = bool(result.get("ok"))
            next_page = int((record.get("progress") or {}).get("next_page") or 1)
            if page_ok:
                # 页面检查通过 ≠ 评价接口已解锁。这里不发任何请求，
                # 只把状态明确标成「数据访问尚未验证」，真正的确认放在继续时的一次真实请求里。
                intervention = build_intervention(
                    "data_unverified",
                    str(record.get("product_id") or ""),
                    detail=f"第 {next_page} 页的数据访问还没有验证过",
                    extra={"next_page": next_page},
                )
            else:
                intervention = build_intervention(
                    str(result.get("kind") or "unknown"),
                    str(record.get("product_id") or ""),
                    detail=str(result.get("detail") or ""),
                    extra={"next_page": next_page},
                )
            STORE.update(job_id, intervention=intervention)
            self._send_json(
                {
                    **result,
                    "page_ok": page_ok,
                    "data_status": "unverified",
                    "data_note": (
                        "页面检查通过，但评价数据访问尚未验证；点「继续采集」会发一次真实评价请求确认。"
                        if page_ok
                        else "页面检查未通过，先按上面的步骤处理，暂时不用尝试继续。"
                    ),
                    "next_page": next_page,
                    "intervention": intervention,
                }
            )
            return

        if action == "cancel":
            cancel_auto_resume(job_id)
            ok, message = STORE.request_cancel(job_id)
            if not ok:
                self._send_json({"error": message}, 409)
                return
            self._send_json(job_payload(STORE.get(job_id) or record))
            return

        if action == "focus":
            if record.get("status") not in {"awaiting_user", "failed"}:
                self._send_json(
                    {"error": f"任务当前状态为 {record.get('status')}，无需打开浏览器"}, 409
                )
                return
            tab_id = str(record.get("tab_id") or "")
            kind = str((record.get("intervention") or {}).get("kind") or "")
            # 采集期间窗口被移出屏幕，需要人工处理时先放回来，否则用户看不到页面。
            from cdp_tabs import show_window

            show_window()
            checkpoint = STORE.load_checkpoint(job_id) or {}
            validate_url = str(checkpoint.get("validate_url") or "")
            validate_at = str(checkpoint.get("validate_at") or "")
            validate_age = _seconds_since(validate_at)
            stale = validate_age is not None and validate_age > VALIDATE_URL_TTL
            stale_hint = ""
            if kind == "platform_validate" and stale and validate_age is not None:
                stale_hint = (
                    f"上次的验证链接大约 {max(1, validate_age // 60)} 分钟前生成，可能已过期"
                    "（页面会提示「等待太久」）。请先点「继续采集」重新触发一次，"
                    "任务会暂停并给出新链接，再点本按钮打开。"
                )

            # 平台验证类暂停要把「能拖滑块的那一页」摆到用户面前，
            # 而不是一个没有验证入口的商品页。
            if kind == "platform_validate" and validate_url and not stale:
                page = open_foreground_tab(validate_url)
                if page and page.get("id"):
                    STORE.update(job_id, tab_id=str(page["id"]))
                    self._send_json(
                        {
                            "ok": True,
                            "mode": "validate",
                            "tab_id": str(page["id"]),
                            "validate_at": validate_at,
                            "message": "已在新标签页打开平台验证页（与采集共用同一个浏览器登录态）",
                        }
                    )
                    return

            if tab_id and find_tab(tab_id) and focus_tab(tab_id):
                payload = {"ok": True, "mode": "tab", "tab_id": tab_id}
                if stale_hint:
                    payload["validate_stale"] = True
                    payload["hint"] = stale_hint
                elif kind == "platform_validate" and not validate_url:
                    payload["hint"] = (
                        "这次暂停没有拿到验证链接；可以在商品页刷新一次，"
                        "或点「继续采集」重新触发验证。"
                    )
                self._send_json(payload)
                return

            target_url = product_url = str(record.get("product_url") or "")
            if kind == "platform_validate" and validate_url and not stale:
                target_url = validate_url
            page = open_foreground_tab(target_url)
            if page and page.get("id"):
                STORE.update(job_id, tab_id=str(page["id"]))
                payload = {
                    "ok": True,
                    "mode": "new_tab",
                    "tab_id": str(page["id"]),
                    "message": "已在调试浏览器中打开验证页（与采集共用同一个登录态）"
                    if target_url != product_url
                    else "已在调试浏览器中打开商品页",
                }
                if stale_hint:
                    payload["validate_stale"] = True
                    payload["hint"] = stale_hint
                self._send_json(payload)
                return
            self._send_json(
                {"error": "无法打开调试浏览器，请先双击「启动低星采集.bat」"}, 409
            )
            return

        self._send_json({"error": "not found"}, 404)

    def _handle_import(self, payload: dict[str, Any]) -> None:
        if _active_collection() or any(job.status in {"queued", "running"} for job in ANALYSIS_JOBS.values()):
            self._send_json({"error": "请先结束当前任务，再导入其他数据"}, 409)
            return
        content = payload.get("content")
        if not isinstance(content, str):
            self._send_json({"error": "缺少 content 字段（JSON 文本）"}, 400)
            return
        filename = str(payload.get("filename") or "")
        record, error = validate_import(content, filename)
        if record is None:
            self._send_json({"error": error}, 400)
            return
        path = write_imported_export(record, EXPORT_DIR)
        info = record["collection_info"]
        created = STORE.create(
            product_url=str(record.get("product_url") or f"https://detail.tmall.com/item.htm?id={record['product_id']}"),
            product_id=record["product_id"],
            mode="import",
            value=0,
            unit="",
            scope={},
            source="import",
            product_title=str((record.get("product") or {}).get("title") or ""),
        )
        STORE.update(
            created["id"],
            status="completed",
            stage="已导入",
            message="导入历史数据分析（非实时采集）",
            summary={
                "product_id": record["product_id"],
                "product_title": (record.get("product") or {}).get("title", ""),
                "product": record.get("product", {}),
                "promotional_claims": record.get("promotional_claims", []),
                "product_info": info.get("product_info", {}),
                "scope": record.get("scope", {}),
                "collection_info": info,
                "import_meta": record.get("import_meta", {}),
            },
            export_path=str(path),
            progress={
                "scanned": info.get("scanned_count", 0),
                "matched": info.get("matched_count", 0),
                "pages": info.get("pages_fetched", 0),
                "api_total": info.get("api_total"),
                "api_total_page": info.get("api_total_page"),
                "oldest_date": info.get("oldest_scanned_date", ""),
                "newest_date": info.get("newest_scanned_date", ""),
            },
            review_sufficiency=info.get("review_sufficiency", {}),
        )
        CACHE_SOURCES.pop(record["product_id"], None)
        _persist_history(record["product_id"])
        self._send_json(job_payload(STORE.get(created["id"]) or created), 201)

    def _handle_manual_claims(self, product_id: str, payload: dict[str, Any]) -> None:
        """把用户手动补充的宣传语写进该商品的采集文件，供下次分析使用。"""
        lines = [str(item or "").strip() for item in (payload.get("lines") or [])]
        blob = str(payload.get("text") or "")
        if blob:
            lines.extend(part.strip() for part in re.split(r"[\n;；]+", blob) if part.strip())
        lines = [line for line in lines if line][:50]
        if not lines:
            self._send_json({"error": "没有可添加的宣传语"}, 400)
            return
        paths = _analysis_read_paths(product_id)
        claims_path = paths.get("claims") if isinstance(paths, dict) else None
        claims_payload = _read_json(claims_path) if claims_path else None
        hint = Path(str((claims_payload or {}).get("input_export") or "")).name
        source_files = _export_files(product_id)
        chosen = next((path for path in source_files if path.name == hint),
                      source_files[-1] if source_files else None)
        if not chosen:
            self._send_json({"error": "找不到该商品的采集文件，请先完成采集"}, 404)
            return
        try:
            added = _append_manual_claims(chosen, lines)
        except OSError as exc:
            self._send_json({"error": f"写入失败：{type(exc).__name__}"}, 500)
            return
        self._send_json({"ok": True, "added": added, "export": chosen.name,
                         "hint": "重新分析后，补充的宣传语会参与落差判断"})

    def _handle_analysis_create(self, payload: dict[str, Any]) -> None:
        global ACTIVE_ANALYSIS
        product_id = str(payload.get("product_id") or "").strip()
        category = str(payload.get("category") or "").strip() or None
        force = bool(payload.get("force"))
        if not product_id.isdigit():
            self._send_json({"error": "缺少合法的商品 ID"}, 400)
            return
        if not _export_files(product_id):
            self._send_json({"error": "没有找到该商品的采集结果，请先采集"}, 400)
            return
        if not force:
            result = load_analysis_result(product_id)
            if result and (result["freshness"]["is_current"] or result.get("analysis_available")):
                self._send_json({"status": "exists", "product_id": product_id, "analysis_available": True,
                                 "cache_hit": bool(result.get("cache_hit")), "historical_view": bool(result.get("historical_view"))})
                return
        try:
            from analysis.llm_client import LLMConfig

            if LLMConfig.from_env(ROOT) is None:
                self._send_json(
                    {"error": "请在应用的 API 设置中配置接口、模型和密钥后再开始分析"},
                    400,
                )
                return
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": f"分析环境不可用：{type(exc).__name__}"}, 500)
            return
        if _active_collection():
            self._send_json({"error": "请先结束当前采集任务，再生成分析报告"}, 409)
            return
        if _analysis_ready(product_id) and not force:
            self._send_json(
                {
                    "status": "exists",
                    "product_id": product_id,
                    "message": "该商品已有分析结果；如需重新分析请传 force=true",
                }
            )
            return
        with LOCK:
            current = ANALYSIS_JOBS.get(ACTIVE_ANALYSIS or "")
            if current and current.status in {"queued", "running"}:
                self._send_json({"error": "已有分析任务在运行，请等待完成"}, 409)
                return
            job_id = uuid.uuid4().hex[:10]
            ANALYSIS_JOBS[job_id] = AnalysisJob(id=job_id, product_id=product_id)
            ACTIVE_ANALYSIS = job_id
        threading.Thread(
            target=run_analysis_job,
            args=(job_id, product_id, category),
            daemon=True,
        ).start()
        self._send_json({"id": job_id, "status": "queued"}, 202)


def _progress_from_checkpoint(checkpoint: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """用断点里的真实页码纠正任务记录，避免重启后界面显示旧进度。"""
    fresh = {
        "scanned": checkpoint.get("scanned"),
        "matched": len(checkpoint.get("reviews") or []),
        "pages": checkpoint.get("pages"),
        "api_total": checkpoint.get("api_total"),
        "api_total_page": checkpoint.get("api_total_page"),
        "oldest_date": checkpoint.get("oldest") or "",
        "newest_date": checkpoint.get("newest") or "",
        "next_page": checkpoint.get("next_page"),
    }
    return {**base, **{key: value for key, value in fresh.items() if value is not None}}


def recover_orphaned_jobs() -> list[str]:
    """服务启动时处理上个进程遗留的未完成任务。

    没有这一步，进程被杀后任务会永远停在 collecting：既不能继续，也会挡住新任务。
    另外把「连接中断」留下的历史 failed 任务翻成可继续，接口类错误不在这里翻案。
    """
    recovered: list[str] = []
    for record in STORE.list_recent(200):
        status = str(record.get("status") or "")
        job_id = str(record.get("id") or "")
        if not job_id:
            continue
        if status not in {"queued", "collecting", "resuming", "failed"}:
            continue
        if record.get("imported"):
            continue

        checkpoint = STORE.load_checkpoint(job_id)
        if status == "failed":
            resumable = bool(record.get("resumable")) or _is_connection_failure(
                str(record.get("last_error") or ""), str(record.get("message") or "")
            )
            if not checkpoint or not resumable:
                continue
            STORE.update(
                job_id,
                status="awaiting_user",
                stage="等待人工介入",
                message="上次采集因浏览器连接中断而停止，断点已保留，可继续",
                intervention=build_intervention(
                    "interrupted",
                    str(record.get("product_id") or ""),
                    detail="上次是连接中断结束的；断点与已采集评论都在，可从断点继续",
                ),
                progress=_progress_from_checkpoint(checkpoint, record.get("progress") or {}),
                resumable=True,
            )
            recovered.append(job_id)
            continue

        if checkpoint:
            STORE.update(
                job_id,
                status="awaiting_user",
                stage="等待人工介入",
                message="服务重启导致采集中断，可从断点继续",
                intervention=build_intervention(
                    "interrupted",
                    str(record.get("product_id") or ""),
                    detail="服务重启，采集中断；断点已保留",
                ),
                progress=_progress_from_checkpoint(checkpoint, record.get("progress") or {}),
            )
        else:
            STORE.update(
                job_id,
                status="failed",
                stage="已中断",
                message="服务重启前没有保存断点",
                last_error="服务在首个断点写入前退出，无法从原进度继续",
            )
        recovered.append(job_id)
    return recovered


def create_app_server(host: str = HOST, port: int = 0) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("本地应用只允许绑定回环地址")
    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    initialize_history_cache()
    recovered = recover_orphaned_jobs()
    if recovered:
        print(f"已恢复 {len(recovered)} 个中断任务：{', '.join(recovered)}")
    server = create_app_server(HOST, PORT)
    print(f"低星评价采集页面：http://{HOST}:{PORT}/")
    print("数据仅本次会话临时处理，关闭应用自动清理；导出由用户选择")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
        cleanup_session()


if __name__ == "__main__":
    main()
