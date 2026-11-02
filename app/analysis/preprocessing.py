"""评论数据读取、清洗、去重与质量检查。

只做“可追溯的清洗”：原始 content 原样保留，text 仅去掉控制字符、零宽字符并规范空白。
任何删除/降权都通过 flags 标记，不改变评论事实。
"""
from __future__ import annotations

import glob
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2028\u2029\ufeff]")
SPACE_RE = re.compile(r"[ \t\u00a0\u3000]+")
NEWLINE_RE = re.compile(r"\n{3,}")

LOW_INFO_PATTERNS = [
    re.compile(pattern)
    for pattern in (
        r"^该用户未填写评价内容$",
        r"^此用户未填写评价内容$",
        r"^默认好评$",
        r"^系统自动评价$",
        r"^系统默认评价$",
        r"^自动收货$",
        r"^此用户没有填写评价$",
        r"^用户未填写评价$",
        r"^评价方未及时做出评价.*$",
        r"^好评[!！。.\s]*$",
        r"^很好[!！。.\s]*$",
        r"^不错[!！。.\s]*$",
        r"^满意[!！。.\s]*$",
        r"^还可以?[!！。.\s]*$",
        r"^一般[!！。.\s]*$",
        r"^还行[!！。.\s]*$",
        r"^无[!！。.\s]*$",
        r"^好的?[!！。.\s]*$",
        r"^赞[!！。.\s]*$",
    )
]

NEGATIVE_HINTS = (
    "差",
    "烂",
    "垃圾",
    "开胶",
    "脱胶",
    "断",
    "破",
    "磨脚",
    "挤脚",
    "偏大",
    "偏小",
    "太硬",
    "太软",
    "臭",
    "异味",
    "退",
    "掉",
    "色差",
    "变形",
    "不透气",
    "不防滑",
    "贵",
    "薄",
    "硬",
    # 通用负面线索（非鞋类商品同样适用）
    "过敏",
    "泛红",
    "刺痛",
    "没用",
    "无效",
    "漏",
    "坏",
    "少",
    "慢",
    "假",
    "过期",
    "变质",
)

SIZE_RE = re.compile(r"(\d{2,3})\s*(?:码|\[|\(|（)")
DATE_PATTERNS = ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日", "%Y.%m.%d")


def clean_text(value: Any) -> str:
    text = str(value or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = CONTROL_RE.sub("", text)
    text = ZERO_WIDTH_RE.sub("", text)
    text = SPACE_RE.sub(" ", text)
    text = NEWLINE_RE.sub("\n\n", text)
    return text.strip()


def parse_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for fmt in DATE_PATTERNS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    match = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", text)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).strftime(
                "%Y-%m-%d"
            )
        except ValueError:
            return ""
    return ""


def parse_star(value: Any) -> int | None:
    try:
        star = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return star if 1 <= star <= 5 else None


def parse_sku(sku: str) -> tuple[str, str]:
    """从规格文本中拆出颜色与尺码，拆不出则返回空串。"""
    text = clean_text(sku)
    if not text:
        return "", ""
    parts = re.split(r"[；;]", text, maxsplit=1)
    color = parts[0].strip(" .。·") if parts else ""
    size = ""
    if len(parts) > 1:
        match = SIZE_RE.search(parts[1])
        if match:
            size = match.group(1)
    if not size:
        match = SIZE_RE.search(text)
        if match:
            size = match.group(1)
    return color, size


def is_low_information(text: str) -> bool:
    value = clean_text(text)
    if not value:
        return True
    bare = re.sub(r"[\s\W_]+", "", value, flags=re.UNICODE)
    if not bare:
        return True
    for pattern in LOW_INFO_PATTERNS:
        if pattern.match(value):
            return True
    # 极短且没有任何负面线索：属于无信息量（如“好”“可以”）
    if len(bare) <= 2 and not any(hint in value for hint in NEGATIVE_HINTS):
        return True
    return False


def _load_file(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def discover_inputs(patterns: Iterable[str] | None, root: Path) -> list[Path]:
    if not patterns:
        return sorted((root / "runtime" / "exports").glob("*.json"))
    files: list[Path] = []
    for pattern in patterns:
        candidate = Path(pattern)
        if candidate.is_file():
            files.append(candidate)
            continue
        matches = [Path(p) for p in glob.glob(str(candidate))]
        if not matches and not candidate.is_absolute():
            matches = [Path(p) for p in glob.glob(str(root / pattern))]
        files.extend(matches)
    return sorted({p.resolve() for p in files if p.suffix.lower() == ".json"})


def load_reviews(
    patterns: Iterable[str] | None = None,
    product_id: str | None = None,
    root: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """读取并合并导出文件，返回 (reviews, quality)。"""
    root = root or Path(__file__).resolve().parents[2]
    files = discover_inputs(patterns, root)
    if not files:
        raise FileNotFoundError("没有找到可读取的采集 JSON")

    records: list[dict[str, Any]] = []
    source_runs: list[dict[str, Any]] = []
    product_meta: dict[str, Any] = {}
    product_ids: Counter[str] = Counter()

    for path in files:
        try:
            data = _load_file(path)
        except (json.JSONDecodeError, OSError) as exc:
            source_runs.append({"file": str(path), "error": f"{type(exc).__name__}: {exc}"})
            continue
        pid = str(data.get("product_id") or "")
        product_ids[pid] += 1
        if product_id and pid != product_id:
            continue
        info = data.get("collection_info") or {}
        source_runs.append(
            {
                "file": str(path),
                "product_id": pid,
                "status": info.get("status", ""),
                "stop_reason": info.get("stop_reason", ""),
                "scanned_count": info.get("scanned_count"),
                "matched_count": info.get("matched_count"),
                "api_total": info.get("api_total"),
                "review_records": len(data.get("reviews") or []),
                "started_at": info.get("started_at", ""),
                "finished_at": info.get("finished_at", ""),
            }
        )
        if not product_meta and data.get("product"):
            product_meta = data.get("product") or {}
        for review in data.get("reviews") or []:
            item = dict(review)
            item["_source_file"] = path.name
            records.append(item)

    if product_id is None and product_ids:
        product_id = product_ids.most_common(1)[0][0]
        product_meta = product_meta or {}

    merged: dict[str, dict[str, Any]] = {}
    duplicate_id_count = 0
    for record in records:
        rid = str(record.get("review_id") or "").strip()
        if not rid:
            continue
        if rid in merged:
            duplicate_id_count += 1
            # 同一 ID 保留正文更长的一条
            if len(str(record.get("content") or "")) > len(str(merged[rid].get("content") or "")):
                merged[rid] = record
            continue
        merged[rid] = record

    reviews: list[dict[str, Any]] = []
    content_counter = Counter(clean_text(r.get("content")) for r in merged.values())

    for rid, record in merged.items():
        content = str(record.get("content") or "")
        text = clean_text(content)
        star = parse_star(record.get("star"))
        rate_type = str(record.get("rate_type") or "")
        date = parse_date(record.get("date") or record.get("date_raw"))
        sku = clean_text(record.get("sku"))
        color, size = parse_sku(sku)
        low_info = is_low_information(text)

        flags: list[str] = []
        if not text:
            flags.append("empty_content")
        if low_info:
            flags.append("low_information")
        if len(text) > 1000:
            flags.append("abnormal_length_long")
        if len(text) < 4:
            flags.append("abnormal_length_short")
        if star is None:
            flags.append("missing_or_invalid_star")
        if rate_type and star is not None:
            expected = "1" if star >= 4 else "0" if star == 3 else "-1"
            if rate_type != expected:
                flags.append("star_rate_mismatch")
        if not sku:
            flags.append("missing_sku")
        if not date:
            flags.append("missing_or_invalid_date")
        if content_counter[text] > 1:
            flags.append("duplicate_content")

        valid = bool(text) and not low_info
        reviews.append(
            {
                "review_id": rid,
                "product_id": str(record.get("product_id") or product_id or ""),
                "star": star,
                "star_source": record.get("star_source", ""),
                "rate_type": rate_type,
                "rate_label": record.get("rate_label", ""),
                "date": date,
                "content": content,
                "text": text,
                "sku": sku,
                "sku_id": str(record.get("sku_id") or ""),
                "sku_color": color,
                "sku_size": size,
                "nick": record.get("nick", ""),
                "reply": record.get("reply", ""),
                "images": record.get("images") or [],
                "source_file": record.get("_source_file", ""),
                "flags": flags,
                "valid_for_analysis": valid,
            }
        )

    reviews.sort(key=lambda item: (item["date"], item["review_id"]), reverse=True)
    quality = build_quality(reviews, records, source_runs, product_id or "", product_meta, duplicate_id_count)
    return reviews, quality


def build_quality(
    reviews: list[dict[str, Any]],
    records: list[dict[str, Any]],
    source_runs: list[dict[str, Any]],
    product_id: str,
    product: dict[str, Any],
    duplicate_id_count: int = 0,
) -> dict[str, Any]:
    stars = Counter(item["star"] for item in reviews if item["star"] is not None)
    lengths = sorted(len(item["text"]) for item in reviews)
    dates = [item["date"] for item in reviews if item["date"]]
    sku_sizes = Counter(item["sku_size"] for item in reviews if item["sku_size"])
    sku_colors = Counter(item["sku_color"] for item in reviews if item["sku_color"])

    def percentile(values: list[int], ratio: float) -> int:
        if not values:
            return 0
        index = min(len(values) - 1, max(0, int(round((len(values) - 1) * ratio))))
        return values[index]

    api_totals = [
        int(run["api_total"]) for run in source_runs if isinstance(run.get("api_total"), int)
    ]
    scanned = [
        int(run["scanned_count"]) for run in source_runs if isinstance(run.get("scanned_count"), int)
    ]
    statuses = Counter(str(run.get("status") or "") for run in source_runs)

    low_info_reviews = [item["review_id"] for item in reviews if "low_information" in item["flags"]]
    empty_reviews = [item["review_id"] for item in reviews if "empty_content" in item["flags"]]
    duplicate_content = [
        item["review_id"] for item in reviews if "duplicate_content" in item["flags"]
    ]

    warnings: list[str] = []
    if len(reviews) < 50:
        warnings.append(f"有效样本较少（{len(reviews)} 条唯一低星评论），统计结果仅作抽样参考")
    if any(status not in {"success", ""} for status in statuses):
        warnings.append("部分采集任务未正常结束，样本可能不完整")
    if low_info_reviews:
        warnings.append(f"{len(low_info_reviews)} 条评论被标记为低信息量，不进入问题抽取")
    if any("duplicate_content" in item["flags"] for item in reviews):
        warnings.append("存在内容重复的评论（保留原始记录，仅标记）")

    return {
        "product_id": product_id,
        "product_title": product.get("title", ""),
        "input_files": [run.get("file") for run in source_runs if run.get("file")],
        "source_runs": source_runs,
        "raw_review_records": len(records),
        "unique_review_count": len(reviews),
        "valid_review_count": sum(1 for item in reviews if item["valid_for_analysis"]),
        "low_information_count": len(low_info_reviews),
        "low_information_review_ids": low_info_reviews,
        "empty_content_count": len(empty_reviews),
        "empty_content_review_ids": empty_reviews,
        "duplicate_review_id_count": duplicate_id_count,
        "duplicate_content_review_ids": duplicate_content,
        "missing_star_count": sum(1 for item in reviews if item["star"] is None),
        "missing_sku_count": sum(1 for item in reviews if not item["sku"]),
        "missing_date_count": sum(1 for item in reviews if not item["date"]),
        "star_distribution": {str(key): value for key, value in sorted(stars.items())},
        "content_length": {
            "min": lengths[0] if lengths else 0,
            "p25": percentile(lengths, 0.25),
            "median": percentile(lengths, 0.5),
            "p75": percentile(lengths, 0.75),
            "max": lengths[-1] if lengths else 0,
        },
        "date_range": {"min": min(dates) if dates else "", "max": max(dates) if dates else ""},
        "sku_size_distribution": dict(sku_sizes.most_common()),
        "sku_color_distribution": dict(sku_colors.most_common()),
        "collection_status": dict(statuses),
        "denominators": {
            "platform_reported_total": max(api_totals) if api_totals else None,
            "max_scanned_in_one_run": max(scanned) if scanned else None,
            "low_star_records_before_dedupe": len(records),
            "unique_low_star_reviews": len(reviews),
            "valid_for_analysis": sum(1 for item in reviews if item["valid_for_analysis"]),
        },
        "warnings": warnings,
    }
