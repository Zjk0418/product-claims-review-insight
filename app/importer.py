"""导入历史采集 JSON 的校验与规范化。

用途：实时采集始终无法恢复时，允许用户把手头已有的采集结果导入，
继续走同一套分析流水线。导入数据必须明确标记来源，不能伪装成实时采集成功。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

MAX_BYTES = 20 * 1024 * 1024
MIN_REVIEWS = 1


def _readable_json_error(exc: json.JSONDecodeError) -> str:
    return f"JSON 解析失败：第 {exc.lineno} 行第 {exc.colno} 列附近格式不正确（{exc.msg}）"


def _product_id(record: dict[str, Any]) -> str:
    raw = record.get("product_id")
    if raw in (None, ""):
        product = record.get("product")
        if isinstance(product, dict):
            raw = product.get("id")
    value = str(raw or "").strip()
    return value if value.isdigit() else ""


def validate_import(text: str, filename: str = "") -> tuple[dict[str, Any] | None, str]:
    """校验导入内容，返回 (规范化记录, 错误信息)。错误信息必须是可读中文。"""
    if not text or not text.strip():
        return None, "文件内容为空"
    if len(text.encode("utf-8")) > MAX_BYTES:
        return None, f"文件超过 {MAX_BYTES // (1024 * 1024)} MB，无法导入"

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, _readable_json_error(exc)
    if not isinstance(raw, dict):
        return None, "顶层必须是 JSON 对象，当前是 " + type(raw).__name__

    product_id = _product_id(raw)
    if not product_id:
        return None, "缺少合法的商品 ID（product_id 或 product.id 必须是数字）"

    reviews = raw.get("reviews")
    if not isinstance(reviews, list):
        return None, "缺少 reviews 数组"
    if len(reviews) < MIN_REVIEWS:
        return None, "reviews 为空：没有可分析的低星评价，请换一份包含评价的文件"

    cleaned_reviews: list[dict[str, Any]] = []
    invalid = 0
    seen: set[str] = set()
    for index, item in enumerate(reviews, 1):
        if not isinstance(item, dict):
            invalid += 1
            continue
        review_id = str(item.get("review_id") or item.get("id") or "").strip()
        content = item.get("content")
        if not review_id:
            invalid += 1
            continue
        if review_id in seen:
            continue
        seen.add(review_id)
        if content is None:
            content = ""
        if not isinstance(content, (str, int, float)):
            invalid += 1
            continue
        normalized = dict(item)
        normalized["review_id"] = review_id
        normalized["content"] = str(content)
        star = item.get("star")
        if star is not None:
            try:
                normalized["star"] = int(star)
            except (TypeError, ValueError):
                normalized["star"] = None
        cleaned_reviews.append(normalized)

    if not cleaned_reviews:
        return None, f"reviews 里没有可用评价（共 {len(reviews)} 条，均缺少 review_id 或格式错误）"

    product = raw.get("product")
    if not isinstance(product, dict):
        product = {}
    product.setdefault("id", product_id)
    if not product.get("title"):
        product["title"] = f"商品 {product_id}（导入）"

    collection_info = raw.get("collection_info")
    if not isinstance(collection_info, dict):
        collection_info = {}
    collection_info = {
        **collection_info,
        "status": "imported",
        "is_complete": False,
        "stop_reason": "导入历史采集数据，非实时采集",
        "scanned_count": collection_info.get("scanned_count", len(cleaned_reviews)),
        "matched_count": len(cleaned_reviews),
        "pages_fetched": collection_info.get("pages_fetched", 0),
        "imported": True,
    }

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    record = {
        **raw,
        "platform": raw.get("platform") or "tmall",
        "product_id": product_id,
        "product": product,
        "reviews": cleaned_reviews,
        "collection_info": collection_info,
        "import_meta": {
            "source": "import",
            "imported_at": now,
            "original_filename": filename or "",
            "original_review_count": len(reviews),
            "accepted_review_count": len(cleaned_reviews),
            "invalid_review_count": invalid,
            "note": "导入历史数据分析，不代表本次实时采集成功",
        },
    }
    return record, ""


def write_imported_export(record: dict[str, Any], export_dir: Path) -> Path:
    """把导入数据写成独立文件，不覆盖任何已有采集 JSON。"""
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = export_dir / f"tmall_{record['product_id']}_imported_{stamp}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
