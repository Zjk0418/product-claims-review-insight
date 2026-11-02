"""Private, durable product inputs and analysis snapshots.

Only collected exports, allow-listed analysis intermediates, and product images
are cached. Rendered reports, API settings and browser profiles never enter this
store. Snapshots preserve original bytes; cache timestamps never replace source
collection/evidence dates. No server import, network calls, or automatic eviction.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import threading
from typing import Any
import uuid


VERSION = "product-cache-v1"
ANALYSIS_FILES = frozenset({
    "analysis_summary.json", "statistics.json", "extracted_claims.json",
    "claim_gaps.json", "data_quality.json", "cleaned_reviews.json",
    "issue_clusters.json", "extracted_issues.json", "issues_flat.json",
    "analysis_log.json", "embedding_cache.json", "llm_cache.jsonl",
    "claim_cache.jsonl", "gap_cache.jsonl", "llm_calls.jsonl",
    "claim_calls.jsonl", "gap_calls.jsonl", "llm_raw_responses.jsonl",
    "claim_raw_responses.jsonl", "gap_raw_responses.jsonl",
})
_READY_FILES = {"analysis_summary.json", "statistics.json", "cleaned_reviews.json",
                "issue_clusters.json", "extracted_issues.json", "issues_flat.json"}
_SENSITIVE = {"api_key", "apikey", "llm_api_key", "openai_api_key", "authorization",
              "cookie", "cookies", "password", "secret", "access_token", "refresh_token",
              "id_token", "bearer_token", "token", "encrypted_api_key", "api_key_encrypted"}
_PRODUCT_ID = re.compile(r"[0-9]{1,32}\Z")
_SNAPSHOT_ID = re.compile(r"[0-9a-f]{32}\Z")
_SAFE_EXPORT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.json\Z")
_IMAGE = re.compile(r"images/(?:main|detail)_[0-9]{2,4}\.(?:jpg|jpeg|png|webp)\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
MAX_FILES = 160
_ROOT_MARKER = {"kind": "review-insight-product-cache", "version": VERSION}


class ProductCacheError(ValueError):
    """A cache/input boundary or integrity check failed, without exposing data."""


def default_cache_directory() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "ReviewInsight/product-cache"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "review-insight/product-cache"


def _pid(value: Any) -> str:
    value = str(value).strip()
    if not _PRODUCT_ID.fullmatch(value):
        raise ProductCacheError("商品缓存需要有效的商品 ID")
    return value


def _plain(path: Path) -> Path:
    """Reject symlinks and Windows junctions before resolving any boundary."""
    path = Path(os.path.abspath(path))
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ProductCacheError("缓存路径不能经过链接或目录联接")
    return path.resolve()


def _has_credentials(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SENSITIVE and item not in (None, "", [], {}):
                return True
            if _has_credentials(item):
                return True
    elif isinstance(value, list):
        return any(_has_credentials(item) for item in value)
    return False


def _json_data(data: bytes, *, jsonl: bool = False) -> Any:
    try:
        text = data.decode("utf-8-sig")
        value = [json.loads(line) for line in text.splitlines() if line.strip()] if jsonl else json.loads(text)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ProductCacheError("缓存输入不是有效的 JSON 数据") from None
    if _has_credentials(value):
        raise ProductCacheError("缓存输入包含凭据字段，已拒绝保存")
    return value


def _read(path: Path) -> bytes:
    path = _plain(path)
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ProductCacheError("缓存文件不可用或超过大小限制")
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise ProductCacheError("缓存文件超过大小限制")
    return data


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_atomic(path: Path, data: bytes) -> None:
    parent = _plain(path.parent)
    parent.mkdir(parents=True, exist_ok=True)
    path = _plain(path)
    with tempfile.NamedTemporaryFile(dir=parent, prefix=".cache-write-", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")


def _file_key(area: str, name: str) -> bool:
    parts = PurePosixPath(name)
    if parts.is_absolute() or ".." in parts.parts or "\\" in name:
        return False
    if area == "analysis":
        return name in ANALYSIS_FILES
    if area == "exports":
        return bool(_SAFE_EXPORT.fullmatch(name))
    if area == "product":
        return name == "images/ocr_cache.json" or bool(_IMAGE.fullmatch(name))
    return False


class ProductCache:
    def __init__(self, root: Path | None = None):
        self.root = _plain(root or default_cache_directory())
        self._lock = threading.RLock()

    def _owned_root(self, *, create: bool) -> bool:
        root = _plain(self.root)
        marker = root / ".product-cache.json"
        if not root.exists():
            if not create:
                return False
            root.mkdir(parents=True)
        if marker.exists():
            try:
                return _json_data(_read(marker)) == _ROOT_MARKER
            except ProductCacheError:
                return False
        if not create:
            return False
        if any(root.iterdir()):
            raise ProductCacheError("拒绝使用没有所有权标记的非空缓存目录")
        _write_atomic(marker, _encoded(_ROOT_MARKER))
        return True

    def _product_folder(self, product_id: str, *, create: bool) -> Path | None:
        if not self._owned_root(create=create):
            if create:
                raise ProductCacheError("缓存目录所有权检查未通过")
            return None
        folder = _plain(self.root / product_id)
        marker = folder / ".product.json"
        expected = {"version": VERSION, "product_id": product_id}
        if not folder.exists():
            if not create:
                return None
            folder.mkdir()
            _write_atomic(marker, _encoded(expected))
        try:
            if _json_data(_read(marker)) != expected:
                return None
        except (OSError, ProductCacheError):
            if create:
                raise ProductCacheError("商品缓存目录所有权检查未通过") from None
            return None
        return folder

    def _snapshot(self, product_id: str, *, verify: bool = True) -> tuple[dict, Path] | None:
        folder = self._product_folder(product_id, create=False)
        if folder is None:
            return None
        pointer = _json_data(_read(folder / "current.json"))
        snapshot_id = pointer.get("snapshot") if isinstance(pointer, dict) else None
        if (pointer.get("version") != VERSION or pointer.get("product_id") != product_id
                or not isinstance(snapshot_id, str) or not _SNAPSHOT_ID.fullmatch(snapshot_id)):
            return None
        snapshot = _plain(folder / "snapshots" / snapshot_id)
        manifest = _json_data(_read(snapshot / "manifest.json"))
        if (not isinstance(manifest, dict) or manifest.get("version") != VERSION
                or manifest.get("product_id") != product_id or manifest.get("snapshot_id") != snapshot_id):
            return None
        files = manifest.get("files")
        if not isinstance(files, list) or not files or len(files) > MAX_FILES:
            return None
        seen, total, identities = set(), 0, []
        for item in files:
            if not isinstance(item, dict):
                return None
            area, name = item.get("area"), item.get("name")
            if not isinstance(name, str) or not _file_key(area, name) or (area, name) in seen:
                return None
            seen.add((area, name))
            size = item.get("size")
            if (isinstance(size, bool) or not isinstance(size, int) or size < 0 or size > MAX_FILE_BYTES
                    or not isinstance(item.get("sha256"), str) or not _SHA256.fullmatch(item["sha256"])):
                return None
            source = _plain(snapshot / "files" / area / name)
            if verify:
                data = _read(source)
                if size != len(data) or item["sha256"] != _hash(data):
                    return None
                if name.endswith((".json", ".jsonl")):
                    value = _json_data(data, jsonl=name.endswith(".jsonl"))
                    if area == "exports" and (not isinstance(value, dict) or str(value.get("product_id")) != product_id or not isinstance(value.get("reviews"), list)):
                        return None
            elif not source.is_file() or source.stat().st_size != size:
                return None
            total += size
            identities.append([area, name, item["sha256"]])
            if total > MAX_SNAPSHOT_BYTES:
                return None
        if manifest.get("content_fingerprint") != _hash(_encoded(identities)):
            return None
        return manifest, snapshot

    def get(self, product_id: str) -> dict | None:
        product_id = _pid(product_id)
        with self._lock:
            try:
                result = self._snapshot(product_id)
            except (OSError, ProductCacheError, ValueError, TypeError, AttributeError):
                return None
            return copy.deepcopy(result[0]) if result else None

    def has(self, product_id: str) -> bool:
        return self.get(product_id) is not None

    def list(self) -> list[dict]:
        with self._lock:
            try:
                owned = self._owned_root(create=False)
            except (OSError, ProductCacheError):
                return []
            if not owned:
                return []
            result = []
            for child in self.root.iterdir():
                if _PRODUCT_ID.fullmatch(child.name):
                    try:
                        found = self._snapshot(child.name, verify=False)
                    except (OSError, ProductCacheError, ValueError, TypeError, AttributeError):
                        found = None
                    if found:
                        result.append(copy.deepcopy(found[0]))
            return sorted(result, key=lambda item: item.get("stored_at", ""), reverse=True)

    def save(self, product_id: str, export_files: list[Path], analysis_dir: Path | None,
             product_dir: Path | None = None) -> dict:
        product_id = _pid(product_id)
        with self._lock:
            payloads, files, total = {}, [], 0
            latest_export, summary, latest_mtime = {}, {}, -1

            def add(area: str, name: str, source: Path) -> Any:
                nonlocal total
                if not _file_key(area, name):
                    raise ProductCacheError("文件不在商品缓存白名单内")
                source = _plain(source)
                data = _read(source)
                parsed = None
                if name.endswith((".json", ".jsonl")):
                    parsed = _json_data(data, jsonl=name.endswith(".jsonl"))
                else:
                    valid_image = (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8")
                                   or data[:4] == b"RIFF" and data[8:12] == b"WEBP")
                    if not valid_image:
                        raise ProductCacheError("商品缓存图片格式无效")
                total += len(data)
                if total > MAX_SNAPSHOT_BYTES or len(files) >= MAX_FILES:
                    raise ProductCacheError("商品缓存快照超过大小限制")
                key = area + "/" + name
                if key in payloads:
                    raise ProductCacheError("商品缓存含重复文件名")
                payloads[key] = data
                files.append({"area": area, "name": name, "size": len(data), "sha256": _hash(data),
                              "source_mtime_ns": source.stat().st_mtime_ns})
                return parsed

            for source in sorted(map(Path, export_files), key=lambda p: p.name):
                data = add("exports", source.name, source)
                if not isinstance(data, dict) or str(data.get("product_id")) != product_id or not isinstance(data.get("reviews"), list):
                    raise ProductCacheError("采集文件与商品 ID 不一致")
                if source.stat().st_mtime_ns >= latest_mtime:
                    latest_export, latest_mtime = data, source.stat().st_mtime_ns
            if not latest_export:
                raise ProductCacheError("缺少可复用的真实采集文件")
            if analysis_dir is not None and Path(analysis_dir).exists():
                analysis_dir = _plain(Path(analysis_dir))
                if not analysis_dir.is_dir() or analysis_dir.name != product_id:
                    raise ProductCacheError("分析缓存目录须属于当前商品")
                for name in sorted(ANALYSIS_FILES):
                    source = analysis_dir / name
                    if source.exists():
                        value = add("analysis", name, source)
                        if name == "analysis_summary.json":
                            if not isinstance(value, dict) or value.get("product_id") not in (None, product_id):
                                raise ProductCacheError("分析结果与商品 ID 不一致")
                            summary = value
            if product_dir is not None and Path(product_dir).exists():
                product_dir = _plain(Path(product_dir))
                if not product_dir.is_dir() or product_dir.name != product_id:
                    raise ProductCacheError("商品图片目录须属于当前商品")
                images = product_dir / "images"
                if images.exists():
                    images = _plain(images)
                    for source in sorted(images.iterdir()):
                        name = "images/" + source.name
                        if _file_key("product", name):
                            add("product", name, source)
            files.sort(key=lambda item: (item["area"], item["name"]))
            identities = [[item["area"], item["name"], item["sha256"]] for item in files]
            fingerprint = _hash(_encoded(identities))
            existing = self.get(product_id)
            if existing and existing.get("content_fingerprint") == fingerprint:
                return existing
            product = latest_export.get("product") or {}
            collection = latest_export.get("collection_info") or {}
            collection = collection if isinstance(collection, dict) else {}
            summary_input = summary.get("input") or {}
            summary_input = summary_input if isinstance(summary_input, dict) else {}
            manifest = {"version": VERSION, "product_id": product_id, "snapshot_id": uuid.uuid4().hex,
                        "stored_at": datetime.now(timezone.utc).isoformat(), "content_fingerprint": fingerprint,
                        "product_title": str(product.get("title") or "") if isinstance(product, dict) else "",
                        "source_created_at": summary.get("created_at"),
                        "analysis_created_at": summary.get("created_at"),
                        "collection_finished_at": collection.get("finished_at"),
                        "analysis_status": summary.get("analysis_status"),
                        "input_digest": summary_input.get("digest"),
                        "valid_review_count": summary.get("valid_review_count"),
                        "issue_count": summary.get("issue_count"), "cluster_count": summary.get("cluster_count"),
                        "claim_count": summary.get("claim_count"),
                        "scanned_count": collection.get("scanned_count"),
                        "matched_count": collection.get("matched_count", len(latest_export["reviews"])),
                        "has_analysis": summary.get("analysis_status") == "completed" and _READY_FILES.issubset({f["name"] for f in files if f["area"] == "analysis"}),
                        "files": files}
            if existing and existing.get("has_analysis") and not manifest["has_analysis"]:
                return existing
            folder = self._product_folder(product_id, create=True)
            if folder is None:
                raise ProductCacheError("商品缓存目录不可写入")
            snapshots = _plain(folder / "snapshots")
            snapshots.mkdir(exist_ok=True)
            pending = snapshots / (".pending-" + manifest["snapshot_id"])
            pending.mkdir()
            for key, data in payloads.items():
                target = pending / "files" / key
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            (pending / "manifest.json").write_bytes(_encoded(manifest))
            snapshot = snapshots / manifest["snapshot_id"]
            os.replace(pending, snapshot)
            _write_atomic(folder / "current.json", _encoded({"version": VERSION, "product_id": product_id,
                                                           "snapshot": manifest["snapshot_id"]}))
            return copy.deepcopy(manifest)

    def restore(self, product_id: str, export_dir: Path, analysis_base: Path,
                product_dir: Path | None = None, *, overwrite: bool = False) -> dict | None:
        product_id = _pid(product_id)
        with self._lock:
            try:
                found = self._snapshot(product_id)
            except (OSError, ProductCacheError, ValueError, TypeError, AttributeError):
                return None
            if found is None:
                return None
            manifest, snapshot = found
            destinations = {"exports": _plain(Path(export_dir)), "analysis": _plain(Path(analysis_base) / product_id)}
            if product_dir is not None:
                destinations["product"] = _plain(Path(product_dir))
                if destinations["product"].name != product_id:
                    raise ProductCacheError("商品图片恢复目录须属于当前商品")
            prepared = []
            for item in manifest["files"]:
                area, name = item["area"], item["name"]
                if area not in destinations:
                    continue
                target = _plain(destinations[area] / name)
                if not target.is_relative_to(destinations[area]):
                    raise ProductCacheError("拒绝恢复到缓存目标目录之外")
                data = _read(snapshot / "files" / area / name)
                if _hash(data) != item["sha256"]:
                    return None
                if target.exists() and not overwrite and _read(target) != data:
                    raise ProductCacheError("当前商品已有不同数据，未使用历史缓存覆盖")
                prepared.append((target, data, item["source_mtime_ns"]))
            for target, data, mtime_ns in prepared:
                _write_atomic(target, data)
                if isinstance(mtime_ns, int) and mtime_ns >= 0:
                    os.utime(target, ns=(mtime_ns, mtime_ns))
            result = copy.deepcopy(manifest)
            result["export_files"] = [str(destinations["exports"] / item["name"]) for item in manifest["files"] if item["area"] == "exports"]
            result["analysis_dir"] = str(destinations["analysis"])
            result["product_dir"] = str(destinations["product"]) if "product" in destinations else None
            return result
