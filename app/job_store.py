"""采集任务的持久化存储与状态流转。

为什么不用内存字典：
- 页面刷新、服务重启后任务不能凭空消失；
- 人工介入可能持续几分钟到几小时，任务记录必须落盘；
- 断点（已扫描页码、已命中评论）要和任务绑定，避免商品之间串数据。

每个任务一个 JSON 文件：runtime/jobs/<id>.json
断点单独存：runtime/jobs/<id>.checkpoint.json（可能较大，轮询时不读）
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

ACTIVE_STATUSES = {"queued", "collecting", "awaiting_user", "resuming"}
FINAL_STATUSES = {"completed", "partial", "failed", "cancelled"}
ALL_STATUSES = ACTIVE_STATUSES | FINAL_STATUSES


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.dir = self.root / "runtime" / "jobs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ---------- 基础读写 ----------

    def _path(self, job_id: str) -> Path:
        if not job_id or not job_id.isalnum():
            raise ValueError("任务 ID 不合法")
        return self.dir / f"{job_id}.json"

    def _checkpoint_path(self, job_id: str) -> Path:
        return self.dir / f"{job_id}.checkpoint.json"

    def _write_json(self, path: Path, payload: Any) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _read_json(self, path: Path) -> Any:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    # ---------- 任务 ----------

    def create(
        self,
        *,
        product_url: str,
        product_id: str,
        mode: str,
        value: int,
        unit: str,
        scope: dict[str, Any],
        source: str = "live",
        product_title: str = "",
    ) -> dict[str, Any]:
        job_id = uuid.uuid4().hex[:10]
        now = _now_iso()
        record = {
            "id": job_id,
            "product_url": product_url,
            "product_id": str(product_id),
            "product_title": product_title,
            "mode": mode,
            "value": value,
            "unit": unit,
            "scope": scope,
            "source": source,  # live / import
            "status": "queued" if source == "live" else "completed",
            "stage": "等待开始" if source == "live" else "已导入",
            "message": "",
            "progress": {
                "scanned": 0,
                "matched": 0,
                "pages": 0,
                "api_total": None,
                "api_total_page": None,
                "oldest_date": "",
                "newest_date": "",
            },
            "intervention": None,
            "summary": None,
            "last_error": "",
            "created_at": now,
            "updated_at": now,
            "awaiting_since": "",
            "resume_count": 0,
            "cancel_requested": False,
            "tab_id": "",
            "export_path": "",
            "imported": source == "import",
            "review_sufficiency": {},
            "access": {"status": "not_checked"},
        }
        with self._lock:
            self._write_json(self._path(job_id), record)
        return record

    def get(self, job_id: str) -> dict[str, Any] | None:
        try:
            path = self._path(job_id)
        except ValueError:
            return None
        with self._lock:
            return self._read_json(path)

    def update(self, job_id: str, **fields: Any) -> dict[str, Any] | None:
        with self._lock:
            record = self.get(job_id)
            if record is None:
                return None
            record.update(fields)
            record["updated_at"] = _now_iso()
            if fields.get("status") == "awaiting_user" and not record.get("awaiting_since"):
                record["awaiting_since"] = record["updated_at"]
            if fields.get("status") in {"collecting", "resuming", "completed", "partial", "failed", "cancelled"}:
                record["awaiting_since"] = ""
            self._write_json(self._path(job_id), record)
            return record

    def patch_progress(self, job_id: str, progress: dict[str, Any], **fields: Any) -> None:
        with self._lock:
            record = self.get(job_id)
            if record is None:
                return
            record["progress"] = {**record.get("progress", {}), **progress}
            record.update(fields)
            record["updated_at"] = _now_iso()
            self._write_json(self._path(job_id), record)

    def list_recent(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            files = sorted(self.dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            records = []
            for path in files:
                if path.name.endswith(".checkpoint.json"):
                    continue
                record = self._read_json(path)
                if isinstance(record, dict) and record.get("id"):
                    records.append(record)
                if len(records) >= max(1, limit):
                    break
            return records

    def find_active(self) -> dict[str, Any] | None:
        """在所有任务里找未结束的一个（不只看最近 N 条）。"""
        with self._lock:
            files = sorted(self.dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for path in files:
                if path.name.endswith(".checkpoint.json"):
                    continue
                record = self._read_json(path)
                if isinstance(record, dict) and record.get("status") in ACTIVE_STATUSES:
                    return record
            return None

    def summarize(self, record: dict[str, Any]) -> dict[str, Any]:
        """给前端/接口的精简结构，不含断点大字段。"""
        job_id = str(record.get("id") or "")
        try:
            has_checkpoint = bool(job_id) and self._checkpoint_path(job_id).exists()
        except OSError:
            has_checkpoint = False
        return {
            "id": record.get("id"),
            "product_id": record.get("product_id"),
            "product_url": record.get("product_url"),
            "product_title": record.get("product_title", ""),
            "scope": record.get("scope", {}),
            "source": record.get("source", "live"),
            "imported": bool(record.get("imported")),
            "status": record.get("status"),
            "stage": record.get("stage", ""),
            "message": record.get("message", ""),
            "progress": record.get("progress", {}),
            "intervention": record.get("intervention"),
            "summary": record.get("summary"),
            "last_error": record.get("last_error", ""),
            "resume_count": record.get("resume_count", 0),
            "can_resume": record.get("status") == "awaiting_user"
            or (record.get("status") == "failed" and bool(record.get("resumable"))),
            "created_at": record.get("created_at", ""),
            "updated_at": record.get("updated_at", ""),
            "awaiting_since": record.get("awaiting_since", ""),
            "tab_id": record.get("tab_id", ""),
            "export_path": record.get("export_path", ""),
            "review_sufficiency": record.get("review_sufficiency", {}),
            "access": record.get("access") or {"status": "not_checked"},
            "has_checkpoint": has_checkpoint,
            "resumable": bool(record.get("resumable")),
            "resumable_reason": record.get("resumable_reason", ""),
        }

    # ---------- 断点 ----------

    def save_checkpoint(self, job_id: str, checkpoint: dict[str, Any]) -> None:
        with self._lock:
            self._write_json(self._checkpoint_path(job_id), checkpoint)

    def load_checkpoint(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            data = self._read_json(self._checkpoint_path(job_id))
            return data if isinstance(data, dict) else None

    def clear_checkpoint(self, job_id: str) -> None:
        with self._lock:
            path = self._checkpoint_path(job_id)
            if path.exists():
                path.unlink()

    # ---------- 恢复与取消（原子操作） ----------

    def begin_resume(self, job_id: str) -> tuple[bool, str]:
        """把任务从 awaiting_user 原子地切到 resuming，避免重复点击跑两次。"""
        with self._lock:
            record = self.get(job_id)
            if record is None:
                return False, "任务不存在"
            status = record.get("status")
            if status == "resuming":
                return False, "任务正在恢复中，请勿重复操作"
            if status == "collecting":
                return False, "任务正在采集中"
            if status != "awaiting_user":
                return False, f"任务当前状态为 {status}，不需要恢复"
            record["status"] = "resuming"
            record["stage"] = "检查浏览器并恢复"
            record["message"] = ""
            record["resume_count"] = int(record.get("resume_count") or 0) + 1
            record["awaiting_since"] = ""
            record["updated_at"] = _now_iso()
            self._write_json(self._path(job_id), record)
            return True, ""

    def request_cancel(self, job_id: str) -> tuple[bool, str]:
        with self._lock:
            record = self.get(job_id)
            if record is None:
                return False, "任务不存在"
            status = record.get("status")
            if status in FINAL_STATUSES:
                return False, f"任务已结束（{status}）"
            record["cancel_requested"] = True
            if status in {"queued", "awaiting_user", "resuming"}:
                record["status"] = "cancelled"
                record["stage"] = "已取消"
                record["message"] = "用户取消任务"
                record["intervention"] = None
                record["awaiting_since"] = ""
            record["updated_at"] = _now_iso()
            self._write_json(self._path(job_id), record)
            return True, ""

    def cancel_requested(self, job_id: str) -> bool:
        record = self.get(job_id)
        return bool(record and record.get("cancel_requested"))


def first_product_id(url_or_id: str) -> str:
    """从 URL 或纯 ID 里取商品 ID；失败返回空串。"""
    import re

    value = str(url_or_id or "").strip()
    if value.isdigit():
        return value
    match = re.search(r"(?:id|itemId|item_id)=(\d+)", value)
    return match.group(1) if match else ""
