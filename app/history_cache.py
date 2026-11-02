"""Archive complete real analyses without combining different collection rounds."""
from __future__ import annotations

import json
import re
import os
import tempfile
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def matching_exports(product_id: str, analysis_dir: Path, export_dir: Path) -> list[Path]:
    from analysis.pipeline import input_digest
    from analysis.preprocessing import load_reviews

    summary = read_json(analysis_dir / "analysis_summary.json")
    target = (summary.get("input") or {}).get("digest")
    candidates = sorted(export_dir.glob(f"tmall_{product_id}_*.json"))
    if not target or not candidates:
        return []

    def matches(paths):
        try:
            rows, _ = load_reviews([str(path) for path in paths], product_id=product_id)
            return input_digest(rows) == target
        except (OSError, ValueError, TypeError, KeyError):
            return False

    hints = {str(name).replace("\\", "/").rsplit("/", 1)[-1]
             for name in (summary.get("input") or {}).get("files", [])}
    named = [p for p in candidates if p.name in hints]
    if named and matches(named):
        return named
    if matches(candidates):
        return candidates
    return next(([p] for p in candidates if matches([p])), [])


def usable_analysis(product_id: str, analysis_dir: Path, exports: list[Path]) -> bool:
    """Historical versions are allowed; failed extraction is never a zero result."""
    from analysis.pipeline import input_digest, clusters_fingerprint

    summary = read_json(analysis_dir / "analysis_summary.json")
    if summary.get("analysis_status") != "completed" or summary.get("errors"):
        return False
    if str(summary.get("product_id")) != product_id or not exports:
        return False
    names = ("cleaned_reviews", "extracted_issues", "issues_flat", "issue_clusters", "statistics", "extracted_claims", "claim_gaps")
    payloads = {name: read_json(analysis_dir / (name + ".json")) for name in names}
    if not all(payloads.values()):
        return False
    cleaned, extraction = payloads["cleaned_reviews"], payloads["extracted_issues"]
    rows = cleaned.get("reviews")
    target = (summary.get("input") or {}).get("digest")
    if not isinstance(rows, list) or not target or cleaned.get("input_digest") != target:
        return False
    try:
        if input_digest(rows) != target:
            return False
        completed = {str(row.get("review_id")) for row in extraction.get("reviews", [])
                     if row.get("status") in {"ok", "empty", "cached"} and not row.get("errors")}
        eligible = {str(row.get("review_id")) for row in rows if row.get("valid_for_analysis")}
        if not eligible.issubset(completed):
            return False
        flat = payloads["issues_flat"].get("issues", [])
        source = [issue for row in extraction.get("reviews", []) for issue in row.get("issues", [])]
        members = [iid for cluster in payloads["issue_clusters"].get("clusters", [])
                   for iid in cluster.get("member_issue_ids", [])]
        source_ids = [row["issue_id"] for row in source]
        flat_ids = [row["issue_id"] for row in flat]
        if not (len(source_ids) == len(set(source_ids)) == len(flat_ids) == len(set(flat_ids))
                == len(members) == len(set(members)) == summary.get("issue_count", 0)
                and set(source_ids) == set(flat_ids) == set(members)):
            return False
        for name, count_key, array_key in (("issue_clusters", "cluster_count", "clusters"),
                                            ("extracted_claims", "claim_count", "claims"),
                                            ("claim_gaps", "gap_count", "gaps")):
            if payloads[name].get("errors") or len(payloads[name].get(array_key, [])) != summary.get(count_key, 0):
                return False
        recorded = (summary.get("fingerprints") or {}).get("clusters")
        if recorded and recorded != clusters_fingerprint(payloads["issue_clusters"]):
            return False
    except (KeyError, TypeError, AttributeError):
        return False
    # Verify the actual inputs, not an old absolute source filename.
    from analysis.preprocessing import load_reviews
    try:
        source_reviews, _ = load_reviews([str(p) for p in exports], product_id=product_id)
        return input_digest(source_reviews) == target
    except (OSError, ValueError, TypeError, KeyError):
        return False


def save_product(cache, product_id: str, runtime_root: Path, *, only_if_newer=True) -> dict | None:
    """Best complete snapshot wins; failed/new collections cannot erase it."""
    from product_cache import ProductCacheError

    runtime = runtime_root / "runtime"
    analysis_dir = runtime / "analysis" / product_id
    exports = matching_exports(product_id, analysis_dir, runtime / "exports")
    complete = usable_analysis(product_id, analysis_dir, exports) if exports else False
    existing = cache.get(product_id)
    if existing and existing.get("has_analysis"):
        if not complete:
            return existing
        incoming = str(read_json(analysis_dir / "analysis_summary.json").get("created_at") or "")
        if only_if_newer and incoming <= str(existing.get("analysis_created_at") or existing.get("source_created_at") or ""):
            return existing
    if not complete:
        exports = sorted((runtime / "exports").glob(f"tmall_{product_id}_*.json"))[-1:]
    if not exports:
        return None
    try:
        return cache.save(product_id, exports, analysis_dir if complete else None, runtime / "products" / product_id)
    except (OSError, ValueError, ProductCacheError):
        return None


def migrate_roots(cache, roots: list[Path]) -> list[dict]:
    results = []
    for root in roots:
        exports = root / "runtime/exports"
        ids = {m.group(1) for path in exports.glob("tmall_*_*.json")
               if (m := re.fullmatch(r"tmall_(\d+)_.*\.json", path.name))}
        for pid in sorted(ids):
            result = save_product(cache, pid, root)
            if result:
                results.append(result)
    return results


def _share_key(value: str) -> str:
    from urllib.parse import urlsplit
    match = re.search(r"https?://(?:e|m)\.tb\.cn/[^\s]+", value, re.I)
    if not match:
        return ""
    try:
        parsed = urlsplit(match.group().rstrip("，。；）)]}"))
        if parsed.netloc.lower() not in {"e.tb.cn", "m.tb.cn"} or parsed.username or parsed.password:
            return ""
        return "https://" + parsed.netloc.lower() + parsed.path
    except ValueError:
        return ""


def lookup_share(cache, value: str) -> str:
    key = _share_key(value)
    path = cache.root / "share-links.json"
    if not key or path.is_symlink():
        return ""
    pid = read_json(path).get(key)
    return pid if isinstance(pid, str) and pid.isdigit() and cache.get(pid) else ""


def remember_share(cache, value: str, product_id: str) -> None:
    key = _share_key(value)
    if not key or not cache.get(product_id):
        return
    path = cache.root / "share-links.json"
    if path.is_symlink():
        return
    links = read_json(path)
    links[key] = product_id
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache.root, suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(links, stream, ensure_ascii=False)
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
