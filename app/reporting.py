"""可打包的应用运行时报告：纯数据聚合、离线 HTML 与按需内存导出。

模块不读取配置、密钥或环境变量，不访问网络，不创建输出目录。
比例的分母是实际扫描去重评论数；明确矛盾与可能矛盾分开统计，
未完成宣传匹配时返回 None（未评估），不把评论比例当作购买者风险概率。
"""
from __future__ import annotations

import html
import io
import json
import math
import re
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import risk_model
import shopping_risk
import sample_risk
import issue_evidence
import report_decision
from report_ui_legacy import HTML_TEMPLATE as _HTML_TEMPLATE
from resources import resource_path

GAP_LABELS = {
    "potential_conflict": "明确矛盾", "partial_gap": "部分体验差异",
    "no_direct_conflict_found": "未发现直接冲突", "insufficient_evidence": "证据不足",
}
# 统计口径严格分开：N 扫描、L 低星、E 有效差评、D 方面负面、M 明确矛盾、U 可能矛盾。
EVIDENCE_LABELS = {
    "confirmed": "明确矛盾", "possible": "具体关联", "none": "未检出明确矛盾",
    "insufficient": "证据不足", "not_evaluated": "未评估",
}
EVIDENCE_ORDER = {"confirmed": 4, "possible": 3, "none": 2, "insufficient": 1, "not_evaluated": 0}
WILSON_Z = 1.96

SCOPE_NOTE = ("宣传矛盾证据率 = 明确反驳宣传承诺的去重评论数 / 实际扫描去重评论数；"
              "负面提及率 = 方面负面评论数 / 实际扫描去重评论数。它们都不是全体买家的发生概率，"
              "也不是购买者遇到问题的概率。")
EVIDENCE_NOTE = ("证据按条分类：明确矛盾（contradiction）计入风险；具体关联（related_negative）权重很低且不能单独推高；"
                 "仅提及属性（topic_only）不计分；支持证据需具体且条件匹配才抵扣。"
                 "「未检出明确矛盾」只表示当前低星样本没有直接反驳，不等于宣传已被证实为真；"
                 "「未评估」表示尚未完成宣传匹配，不能写成 0。"
                 "负面提及数取该方面问题簇覆盖的去重评论并并入矛盾证据；尚未逐条做全量方面标注，D 可能偏低。")
SEVERITY_NOTE = ("严重程度未在评论侧标注；本报告不把严重程度折算成概率或权重，"
                 "需要时由人工按 1-5 级单独标注。")

_FONT_LOCK = threading.Lock()
_SECRET_KEYS = {"api_key", "apikey", "llm_api_key", "openai_api_key", "authorization", "cookie", "cookies", "token", "password", "secret"}
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _text(value: Any) -> str:
    return _ILLEGAL_XML.sub("", str(value if value is not None else "")).strip()


def _rows(value: Any, key: str) -> list[dict]:
    rows = value.get(key, []) if isinstance(value, dict) else value or []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, (list, tuple)) else []


def _safe_public(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _safe_public(v) for k, v in value.items()
                if str(k).lower().replace("-", "_") not in _SECRET_KEYS}
    if isinstance(value, (list, tuple)):
        return [_safe_public(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return _text(value) if isinstance(value, str) else value
    return _text(value)


def _basename(value: Any) -> str:
    return _text(value).replace("\\", "/").rsplit("/", 1)[-1]


def _wilson_interval(successes: int, total: int, z: float = WILSON_Z) -> list[float] | None:
    """二项比例的 Wilson 95% 区间；不校正评论自选择、漏采与模型错判。"""
    return sample_risk.wilson_interval(successes, total, z)


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator > 0 else None


def _probability_text(assessment: dict) -> str:
    value = assessment.get("probability_estimate") or {}
    estimate, interval = value.get("estimate"), value.get("interval")
    if value.get("status") != "estimated" or estimate is None:
        return "同类问题复现概率：尚未估计。" + _text(value.get("reason") or "缺少可核验的可比反馈分母。")
    bounds = (f"；95%模型可信区间 {interval[0]*100:.1f}%–{interval[1]*100:.1f}%"
              if isinstance(interval, list) and len(interval) == 2 else "")
    return (f"同类问题复现概率：{estimate*100:.1f}%{bounds}；"
            f"依据 {value.get('successes', 0)}/{value.get('total', 0)} 条可比低星反馈。"
            + ("规格：" + _text(value.get("sku_label")) + "。" if value.get("sku_label") else "")
            + ("样本少，估计不稳定。" if value.get("small_sample") else "")
            + "这是已完成抽取的同条件低星反馈中的模型估计，不是购买后故障率。")


def _compact(value: Any) -> str:
    return re.sub(r"[\s，。！？、,.!?~…\-—\"'“”‘’]+", "", _text(value))


def _quote_status(quote_text: str, content: str, evidence_status: str = "") -> str:
    """引用与原文的核对状态：逐字可核对才按满权重计入风险。"""
    quote_text, content = _text(quote_text), _text(content)
    if quote_text and content:
        return "verbatim" if _compact(quote_text) in _compact(content) else "paraphrase"
    if not quote_text:
        return "original_review" if content else "unknown"
    return "unknown"


_BOUNDED_HINTS = ("上限", "时间范围", "样本不足", "已扫描最近", "达到扫描")


def _coverage_state(collection: dict) -> str:
    """覆盖状态：full 完整；bounded 有明确上限；interrupted 中断；unknown 未知。

    ``is_complete`` 表示任务正常结束；达到扫描上限/时间范围/样本不足也属于
    “覆盖范围已知但有界”，不应和“采集被风控打断”混为一谈。
    """
    if collection.get("is_complete"):
        return "full"
    status = _text(collection.get("status"))
    reason = _text(collection.get("stop_reason"))
    if status in {"partial", "success"} and any(hint in reason for hint in _BOUNDED_HINTS):
        return "bounded"
    if not status:
        return "unknown"
    return "interrupted"


def build_report_model(
    summary: dict | None = None, statistics: dict | None = None,
    claims: dict | list | None = None, gaps: dict | list | None = None,
    reviews: dict | list | None = None, product: dict | None = None,
    freshness: dict | None = None, collection: dict | None = None,
    issues: dict | list | None = None,
    extraction: dict | list | None = None,
) -> dict:
    """从已分析数据构建报告模型；只做统计，不调用 LLM、不修改输入。

    所有比例都显式保留分子与分母。尚未完成宣传匹配的方面返回 None，而不是 0。
    """
    summary, statistics, product = summary or {}, statistics or {}, product or {}
    collection = collection or {}
    raw_promotional = product.get("promotional_claims") or []
    if isinstance(product.get("product"), dict):
        product = product["product"]
    raw_promotional = raw_promotional or product.get("promotional_claims") or []
    fields = ("id", "title", "subtitle", "price_text", "price", "attributes", "props", "skus", "sku_count", "brand", "category")
    clean_product = _safe_public({k: product[k] for k in fields if k in product})
    clean_product["promotional_claims"] = [
        {"text": _text(row.get("text")), "source": _text(row.get("source")),
         "confidence": row.get("confidence") if isinstance(row.get("confidence"), (int, float)) else None,
         "source_image": _basename(row.get("source_image") or row.get("image_path") or row.get("image")).split("?", 1)[0].split("#", 1)[0]}
        for row in raw_promotional if isinstance(row, dict) and _text(row.get("text"))
    ]
    images = []
    for row in product.get("images") or []:
        if not isinstance(row, dict):
            continue
        url = _text(row.get("url"))
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme in {"http", "https"} and parsed.netloc and not parsed.username and not parsed.password:
            images.append({"url": url, "kind": _text(row.get("kind"))})
    clean_product["images"] = images
    product_id = _text(summary.get("product_id") or product.get("id"))
    clean_product.setdefault("id", product_id)
    clean_product.setdefault("title", _text(summary.get("product_title")) or "商品宣传与低星体验分析")
    review_rows, claim_rows, gap_rows = _rows(reviews, "reviews"), _rows(claims, "claims"), _rows(gaps, "gaps")
    # 已有分析也按当前宣传范围展示；不需要付费重跑即可移除平台说明等无关维度。
    from analysis.claim_schema import non_actionable_reason_dict
    excluded_claims = []
    kept_claims = []
    for claim in claim_rows:
        reason = non_actionable_reason_dict(claim)
        if reason:
            excluded_claims.append({"claim_id": _text(claim.get("claim_id")),
                "text": _text(claim.get("normalized_claim") or claim.get("original_text")),
                "original_text": _text(claim.get("original_text")), "source": _text(claim.get("source")), "reason": reason})
        else:
            kept_claims.append(claim)
    excluded_ids = {c["claim_id"] for c in excluded_claims if c["claim_id"]}
    claim_rows = kept_claims
    gap_rows = [g for g in gap_rows if _text(g.get("claim_id")) not in excluded_ids]
    warnings: list[str] = list(dict.fromkeys(_text(v) for v in (summary.get("data_quality") or {}).get("warnings", []) if v))
    clean_reviews: list[dict] = []
    seen: set[str] = set()
    for raw in review_rows:
        rid = _text(raw.get("review_id"))
        if not rid or rid in seen:
            continue
        seen.add(rid)
        star = raw.get("star")
        valid = bool(raw.get("valid_for_analysis")) if "valid_for_analysis" in raw else bool(
            _text(raw.get("text") or raw.get("content")) and (star is None or isinstance(star, (int, float)) and star <= 3)
        )
        images = raw.get("images") if isinstance(raw.get("images"), list) else []
        clean_reviews.append({"review_id": rid, "star": star if isinstance(star, (int, float)) else None,
            "date": _text(raw.get("date") or raw.get("date_raw")), "sku": _text(raw.get("sku") or raw.get("sku_id")),
            "sku_id": _text(raw.get("sku_id")), "product_id": _text(raw.get("product_id")),
            "append_content": _text(raw.get("append_content")), "append_date": _text(raw.get("append_date")),
            "content": _text(raw.get("text") or raw.get("content")), "valid_for_analysis": valid,
            "images": [u for u in images if isinstance(u, str)][:6],
            "has_media": bool(images) or bool(raw.get("has_media")),
            "appended": bool(raw.get("appended"))})
    review_index = {r["review_id"]: r for r in clean_reviews}
    valid_ids = {r["review_id"] for r in clean_reviews if r["valid_for_analysis"]}
    denominator = len(valid_ids) if review_rows else int(summary.get("valid_review_count") or 0)
    if review_rows and summary.get("valid_review_count") is not None and denominator != summary["valid_review_count"]:
        warnings.append("当前评论与分析摘要的有效样本数不一致，报告以可核验评论为准。")

    # 采样口径：N 必须是实际扫描去重的评论数，不能用平台总量冒充。
    analyzed = len(clean_reviews) if review_rows else int(summary.get("unique_review_count") or 0)
    stars = [r["star"] for r in clean_reviews if isinstance(r["star"], (int, float))]
    low_star = sum(1 for star in stars if star <= 3)
    if isinstance(collection.get("matched_count"), int):
        low_star = max(low_star, int(collection["matched_count"]))
    scanned = collection.get("scanned_count")
    has_scan = isinstance(scanned, int) and scanned > 0
    scanned_count = int(scanned) if has_scan else analyzed
    low_star = max(low_star, denominator)
    scanned_count = max(scanned_count, low_star)
    low_star_rate = _rate(low_star, scanned_count)
    effective_low_star_rate = _rate(denominator, low_star)
    if has_scan:
        coverage_note = (f"扫描 {scanned_count} 条评论，命中 ≤3 星 {low_star} 条，"
                         f"其中 {denominator} 条有可分析正文；低星不一定表示正文负面，仅对低星评论做问题抽取和宣传匹配。")
    else:
        coverage_note = (f"未提供采集扫描总数，比例以本次分析输入 {scanned_count} 条为分母；"
                         f"其中低星 {low_star} 条、有效 {denominator} 条。")
        warnings.append("缺少采集扫描总数，低星占比按本次分析输入计算，不能当作平台整体比例。")

    clean_claims = []
    for c in claim_rows:
        clean_claims.append({"claim_id": _text(c.get("claim_id")), "claim_group": _text(c.get("claim_group") or c.get("claim_id")),
            "aspect": _text(c.get("aspect")) or "其他", "claim_type": _text(c.get("claim_type")),
            "text": _text(c.get("normalized_claim") or c.get("claim_text") or c.get("original_text")),
            "original_text": _text(c.get("original_text")), "source": _text(c.get("source")),
            "source_image": _basename(c.get("source_image")), "verifiable": bool(c.get("verifiable", True))})
    claim_index = {c["claim_id"]: c for c in clean_claims}
    clean_gaps: list[dict] = []
    for raw in gap_rows:
        cid = _text(raw.get("claim_id"))
        claim = claim_index.get(cid, {})
        ids = list(dict.fromkeys(_text(rid) for rid in raw.get("evidence_review_ids", []) if rid is not None))
        ids = [rid for rid in ids if rid in valid_ids]
        gap_type = raw.get("gap_type") if raw.get("gap_type") in GAP_LABELS else "insufficient_evidence"
        if gap_type in {"potential_conflict", "partial_gap"} and not ids:
            gap_type = "insufficient_evidence"
            warnings.append("有冲突判断缺少可核验的有效评论，报告已按证据不足展示。")
        quotes = []
        quoted_ids = set()
        for quote in raw.get("review_evidence") or []:
            rid = _text(quote.get("review_id"))
            if rid not in ids or rid in quoted_ids:
                continue
            quoted_ids.add(rid)
            review = review_index.get(rid, {})
            quotes.append({"review_id": rid, "evidence": _text(quote.get("evidence")),
                "content": review.get("content", ""), "date": review.get("date", ""),
                "star": review.get("star", quote.get("star")), "sku": review.get("sku", ""),
                "evidence_status": "provided_quote"})
        for rid in ids:
            if rid not in quoted_ids and rid in review_index:
                r = review_index[rid]
                quotes.append({**r, "evidence": r["content"], "evidence_status": "original_review_only"})
                warnings.append("部分引用未返回具体原文片段，已标记为补充原评论，需核查其直接相关性。")
        level = raw.get("confidence_level") if raw.get("confidence_level") in {"high", "medium", "low"} else "insufficient"
        detail_rows = []
        for row in raw.get("evidence_detail") or []:
            if not isinstance(row, dict):
                continue
            rid = _text(row.get("review_id"))
            if rid not in ids:
                continue
            detail_rows.append({
                "review_id": rid,
                "relation": _text(row.get("relation")),
                "evidence_kind": _text(row.get("evidence_kind")),
                "negative_fact": row.get("negative_fact"),
                "quality": _text(row.get("quality")),
                "severity": _text(row.get("severity")),
                "quote": _text(row.get("quote"))[:200],
            })
        clean_gaps.append({"claim_id": cid, "claim_group": claim.get("claim_group") or cid,
            "aspect": claim.get("aspect") or _text(raw.get("claim_aspect")) or "其他",
            "claim_text": claim.get("text") or _text(raw.get("claim_text")),
            "claim_original_text": claim.get("original_text") or _text(raw.get("claim_original_text")),
            "gap_type": gap_type, "gap_label": GAP_LABELS[gap_type], "confidence_level": level,
            "confidence_basis": _text(raw.get("confidence_basis")), "finding": _text(raw.get("finding")),
            "related_clusters": [_text(v) for v in raw.get("related_clusters", [])],
            "support_review_ids": ids, "support_review_count": len(ids), "quotes": quotes,
            "evidence_detail": detail_rows})
    clusters = []
    for raw in statistics.get("clusters", []) or []:
        ids = list(dict.fromkeys(_text(v) for v in raw.get("review_ids", [])))
        ids = [rid for rid in ids if rid in valid_ids]
        clusters.append({"cluster_id": _text(raw.get("cluster_id")), "aspect": _text(raw.get("aspect")) or "其他",
            "name": _text(raw.get("problem_name")), "description": _text(raw.get("description")), "review_ids": ids})
    aspect_names = set(c["aspect"] for c in clean_claims) | set(g["aspect"] for g in clean_gaps) | set(c["aspect"] for c in clusters)
    aspects = []
    advertised_units: dict[str, list[dict]] = {}
    all_confirmed: set[str] = set()
    all_possible: set[str] = set()
    all_negative: set[str] = set()
    for aspect in sorted(aspect_names):
        gs = [g for g in clean_gaps if g["aspect"] == aspect]
        cs = [c for c in clean_claims if c["aspect"] == aspect]
        confirmed = [g for g in gs if g["gap_type"] == "potential_conflict"]
        possible = [g for g in gs if g["gap_type"] == "partial_gap"]
        no_conflict = [g for g in gs if g["gap_type"] == "no_direct_conflict_found"]
        m_ids = {rid for g in confirmed for rid in g["support_review_ids"]}
        u_ids = {rid for g in possible for rid in g["support_review_ids"]}
        d_ids = {rid for c in clusters if c["aspect"] == aspect for rid in c["review_ids"]} | m_ids | u_ids
        if m_ids:
            status = "confirmed"
        elif u_ids:
            status = "possible"
        elif no_conflict:
            status = "none"
        elif gs:
            status = "insufficient"
        else:
            status = "not_evaluated"
        evaluated = status in {"confirmed", "possible", "none"}
        all_confirmed.update(m_ids)
        all_possible.update(u_ids)
        all_negative.update(d_ids)
        gap_rate = _rate(len(m_ids), scanned_count) if evaluated else None
        possible_rate = _rate(len(u_ids), scanned_count) if evaluated else None
        # 收集该维度的证据单元：优先用模型逐条给出的 relation，
        # 缺失时才按 gap_type 兜底；部分关联要看引用里是否真有负面事实。
        units: list[dict] = []
        for gap in confirmed + possible:
            fallback = "contradiction" if gap["gap_type"] == "potential_conflict" else "partial"
            detail = {
                _text(row.get("review_id")): row
                for row in (gap.get("evidence_detail") or [])
                if isinstance(row, dict)
            }
            quote_by_id = {_text(q.get("review_id")): q for q in gap.get("quotes") or []}
            for rid in gap["support_review_ids"]:
                review = review_index.get(rid, {})
                quote = quote_by_id.get(rid, {})
                extra = detail.get(rid, {})
                content = _text(review.get("content"))
                body = _text(quote.get("evidence") or quote.get("content") or content)
                # 具体性与严重程度按整条原评论判断：引用片段常常只有“有点薄”，
                # 只看片段会把大量证据误判为“笼统/未标注”。
                judge_text = content or body
                quote_text = _text(extra.get("quote"))
                if not quote_text and quote.get("evidence_status") == "provided_quote":
                    quote_text = _text(quote.get("evidence"))
                units.append({
                    "review_id": rid,
                    "relation": _text(extra.get("relation") or extra.get("evidence_kind")) or fallback,
                    "text": body,
                    "star": review.get("star"),
                    "sku": review.get("sku"),
                    "date": review.get("date"),
                    "has_media": bool(review.get("has_media")),
                    "quote_status": _quote_status(quote_text, content, _text(quote.get("evidence_status"))),
                    "quality": risk_model.quality_of(judge_text, False, extra.get("quality")),
                    "severity": risk_model.severity_of(judge_text, extra.get("severity")),
                    "claim_id": gap["claim_id"],
                    "provided_quote": bool(quote_text),
                })
        advertised_units[aspect] = units
        scan_complete = bool(collection.get("is_complete"))
        coverage_state = _coverage_state(collection)
        reverse_covered = bool(summary.get("reverse_evidence_covered"))
        assessment = risk_model.assess(
            units,
            scanned_count=scanned_count,
            effective_review_count=denominator,
            scan_complete=scan_complete,
            coverage_state=coverage_state,
            reverse_evidence_covered=reverse_covered,
        )
        legacy = risk_model.assess(
            units,
            scanned_count=scanned_count,
            effective_review_count=denominator,
            scan_complete=scan_complete,
            model_version="v1",
        )
        null_reason = "no_scoring_evidence"
        if not gs:
            null_reason = "no_claim"
        elif status == "not_evaluated":
            null_reason = "not_evaluated"
        # status=insufficient 表示已经做过匹配、只是没有足够证据，
        # 不能写成“未评估”，也不能写成 0。
        aspect_problems = [c for c in clusters if c["aspect"] == aspect]
        # 评论侧购物问题不要求存在宣传承诺；仅提及属性的关系证据不算负面。
        shopping_ids = ({rid for c in aspect_problems for rid in c["review_ids"]}
                        | set(assessment["contradiction_review_ids"])
                        | set(assessment["partial_review_ids"]))
        shopping = shopping_risk.assess_aspect(
            problems=aspect_problems, negative_ids=shopping_ids, reviews=review_index,
            scanned_count=scanned_count, low_star_count=low_star, effective_count=denominator,
        )
        aspects.append({
            "aspect": aspect, "evidence_status": status, "evidence_label": EVIDENCE_LABELS[status],
            "shopping": shopping,
            "sample_risk": sample_risk.signal(shopping["review_ids"], scanned_count, denominator,
                                              "collection_info" if has_scan else "analysis_input"),
            "claim_matching_status": "completed" if evaluated else ("insufficient" if status == "insufficient" else "not_evaluated"),
            "contradiction_count": len(m_ids), "contradiction_review_ids": sorted(m_ids),
            "possible_count": len(u_ids), "possible_review_ids": sorted(u_ids),
            "negative_count": len(d_ids), "negative_review_ids": sorted(d_ids),
            "gap_evidence_rate": gap_rate, "gap_evidence_ci": _wilson_interval(len(m_ids), scanned_count) if gap_rate is not None else None,
            "possible_evidence_rate": possible_rate,
            "negative_mention_rate": _rate(len(d_ids), scanned_count),
            "negative_concentration": _rate(len(d_ids), denominator),
            "risk_index": assessment["risk_index"],
            "risk_band": assessment["risk_band"],
            "risk_band_label": assessment["risk_band_label"],
            "risk_model_version": assessment["risk_model_version"],
            "risk_index_calibrated": False,
            "evidence_sufficiency": assessment["evidence_sufficiency"],
            "evidence_sufficiency_label": assessment["evidence_sufficiency_label"],
            "sufficiency_reasons": assessment["sufficiency_reasons"],
            "sufficiency_signals": assessment["sufficiency_signals"],
            "evidence_structure": assessment["relation_structure"],
            "risk_evidence": assessment["risk_evidence"],
            "safety_alert": assessment["safety_alert"],
            "null_reason": None if assessment["risk_index"] is not None else null_reason,
            "risk_calculation": assessment["calculation"],
            "legacy_risk": {
                "risk_index": legacy["risk_index"],
                "risk_band": legacy["risk_band"],
                "evidence_sufficiency": legacy["evidence_sufficiency"],
                "params_version": legacy["calculation"]["params_version"],
            },
            "legacy_direct_rate": gap_rate,
            "support_evidence_count": assessment["support_count"],
            "severity": assessment["severity"],
            "severity_label": assessment["severity_label"],
            "severity_distribution": assessment["severity_distribution"],
            "severity_status": "labeled" if assessment["severity"] != "unknown" else "not_labeled",
            "support_review_count": len(m_ids), "support_review_ids": sorted(m_ids),
            "complaint_review_count": len(d_ids),
            "claim_count": len(cs), "claim_group_count": len({c["claim_group"] for c in cs}),
            "conflict_group_count": len({g["claim_group"] for g in confirmed + possible}),
            "gap_count": len(gs), "gap_counts": dict(Counter(g["gap_type"] for g in gs)),
            "gap_ids": [g["claim_id"] for g in gs],
            "problems": aspect_problems,
        })
    aspects.sort(key=lambda x: (-(x["risk_index"] if x["risk_index"] is not None else -1),
                                -x["contradiction_count"], -x["possible_count"],
                                -x["negative_count"], x["aspect"]))
    # 商品自身问题：评论里反复出现、但商品页没有对应宣传承诺的方面。
    # 它们不计入“宣传落差指数”（没有承诺可反驳），但仍然是购物体验问题，
    # 不能因为“宣传没提”就在报告里消失。
    severity_order = {"unknown": 0, "minor": 1, "moderate": 2, "major": 3, "critical": 4}
    product_issues: list[dict] = []
    issue_review_ids: set[str] = set()
    issue_aspects: set[str] = set()
    repeated_issue_ids: set[str] = set()
    issue_safety = False
    for aspect in aspects:
        if aspect["claim_count"]:
            continue
        for cluster in aspect["problems"]:
            ids = [rid for rid in cluster["review_ids"] if rid in review_index]
            if not ids:
                continue
            texts = [_text(review_index[rid].get("content")) for rid in ids]
            # 严重程度只看问题簇本身的名称/描述，不能拿整条评论——
            # 一条评论可能同时说“客服态度差”和“用完过敏”，否则会把客服问题判成重大。
            severity = risk_model.severity_of(
                f"{cluster.get('name') or ''} {cluster.get('description') or ''}"
            )
            evidence = max((text for text in texts if text), key=len, default="")[:180]
            product_issues.append({
                "cluster_id": cluster["cluster_id"],
                "aspect": aspect["aspect"],
                "problem_name": cluster["name"],
                "review_count": len(ids),
                "review_ids": ids,
                "severity": severity,
                "severity_label": risk_model.SEVERITY_LABELS.get(severity, "未分级"),
                "evidence": evidence,
                "sample_risk": sample_risk.signal(ids, scanned_count, denominator,
                                                  "collection_info" if has_scan else "analysis_input"),
            })
            issue_review_ids.update(ids)
            issue_aspects.add(aspect["aspect"])
            if len(ids) >= 2:
                repeated_issue_ids.update(ids)
            issue_safety = issue_safety or severity == "critical"
    product_issues.sort(key=lambda row: (-row["review_count"],
                                         -severity_order.get(row["severity"], 0),
                                         row["problem_name"]))
    issue_problem_total = len(product_issues)
    # 所有问题保留；是否折叠交给界面，不能让超出前12项的证据失去入口。
    overall_negative_ids = {rid for a in aspects for rid in a["shopping"]["review_ids"]}
    overall_sample = sample_risk.signal(overall_negative_ids, scanned_count, denominator,
                                       "collection_info" if has_scan else "analysis_input")
    unadvertised_sample = sample_risk.signal(issue_review_ids, scanned_count, denominator,
                                            "collection_info" if has_scan else "analysis_input")
    warning = (f"识别到较多未对应商品宣传的负面评价：{len(issue_review_ids)}条，涉及{len(issue_aspects)}个方面，请注意甄别。"
               if len(issue_review_ids) >= 3 else
               f"识别到{len(issue_review_ids)}条未对应商品宣传的负面评价，请结合原文核查。"
               if issue_review_ids else "")
    if issue_safety:
        warning = "未对应宣传的问题中存在安全相关线索，请优先核查。" + warning
    unadvertised_sample.update(aspect_count=len(issue_aspects), problem_count=issue_problem_total,
        repeat_review_count=len(repeated_issue_ids),
        warning_level="high" if issue_safety else "medium" if issue_review_ids else "none",
        warning_text=warning,
        scope="未发现同方面的有效宣传主张；不代表商家故意隐瞒，也不表示已证明宣传矛盾")
    evaluated = [a for a in aspects if a["claim_matching_status"] == "completed"]
    distribution = []
    for status in ("confirmed", "possible", "none"):
        rows = [a for a in evaluated if a["evidence_status"] == status]
        ids: set[str] = set()
        for row in rows:
            ids.update(row["contradiction_review_ids"] if status == "confirmed" else row["possible_review_ids"])
        distribution.append({"status": status, "label": EVIDENCE_LABELS[status], "aspect_count": len(rows),
                             "review_count": len(ids),
                             "share": round(len(rows) / len(evaluated), 4) if evaluated else 0.0})
    scored = [a for a in aspects if a["risk_index"] is not None]
    risk_distribution = []
    for band, label in (("high", "高风险指数"), ("medium", "中风险指数"), ("low", "低风险指数")):
        rows = [a for a in scored if a["risk_band"] == band]
        risk_distribution.append({
            "band": band, "label": label, "aspect_count": len(rows),
            "share": round(len(rows) / len(scored), 4) if scored else 0.0,
        })
    overall_rate = _rate(len(all_confirmed), scanned_count) if evaluated else None
    overall_ci = _wilson_interval(len(all_confirmed), scanned_count) if overall_rate is not None else None
    limited = [_text(v) for v in summary.get("limitations", []) if v]
    # Stable source IDs are used transiently for deduplication, never exported.
    raw_index = {_text(r.get("review_id")): r for r in review_rows}
    evidence_reviews = [{**raw_index.get(r["review_id"], {}), **r} for r in clean_reviews]
    recurrence = issue_evidence.build(evidence_reviews, statistics.get("clusters") or [],
        _rows(issues, "issues"), product_id=product_id,
        claim_aspects={c["aspect"] for c in clean_claims}, collection=collection, summary=summary,
        extraction=_rows(extraction, "reviews") if extraction is not None else None)
    decision = report_decision.build(aspects=aspects, recurrence=recurrence, reviews=evidence_reviews,
        advertised_units=advertised_units, counts={"scanned_review_count": scanned_count,
            "low_star_review_count": low_star, "effective_negative_review_count": denominator,
            "claim_count": len(clean_claims), "claim_aspect_count": len({c["aspect"] for c in clean_claims}),
            "claim_group_count": len({c["claim_group"] for c in clean_claims})})
    limited = list(dict.fromkeys([issue_evidence.NOTE, sample_risk.NOTE, SCOPE_NOTE, EVIDENCE_NOTE, *limited]))
    return {
        "schema_version": "report-v4", "product_id": product_id, "product": clean_product,
        "title": clean_product["title"],
        "generated_at": _text(summary.get("created_at")) or datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "analysis_status": _text(summary.get("analysis_status")) or "not_started",
        "freshness": _safe_public(freshness or summary.get("freshness") or {}),
        "category": _safe_public(summary.get("category") or {}),
        "sampling": {
            "scanned_review_count": scanned_count, "low_star_review_count": low_star,
            "effective_negative_review_count": denominator, "analyzed_review_count": analyzed,
            "low_star_rate": low_star_rate, "effective_low_star_rate": effective_low_star_rate,
            "analysis_scope": "negative_aspects_in_reviews_with_star_lte_3",
            "source": "collection_info" if has_scan else "analysis_input",
            "scan_complete": bool(collection.get("is_complete")),
            "coverage_state": _coverage_state(collection),
            "reverse_evidence_covered": bool(summary.get("reverse_evidence_covered")),
            "stop_reason": _text(collection.get("stop_reason")),
            "platform_total": collection.get("api_total") if isinstance(collection.get("api_total"), int) else None,
            "coverage_note": coverage_note,
        },
        "overview": {
            "review_count": analyzed, "scanned_review_count": scanned_count,
            "low_star_review_count": low_star, "valid_review_count": denominator,
            "issue_count": int(summary.get("issue_count") or 0),
            "cluster_count": int(summary.get("cluster_count") or len(clusters)),
            "claim_count": len(clean_claims), "claim_group_count": len({c["claim_group"] for c in clean_claims}),
            "gap_count": len(clean_gaps),
            "evidence_aspect_count": len([a for a in aspects if a["evidence_status"] in {"confirmed", "possible"}]),
            "confirmed_aspect_count": len([a for a in aspects if a["evidence_status"] == "confirmed"]),
            "evaluated_aspect_count": len(evaluated),
            "gap_evidence_count": len(all_confirmed), "gap_evidence_rate": overall_rate,
            "gap_evidence_ci": overall_ci,
            "negative_mention_count": len(all_negative),
            "negative_mention_rate": _rate(len(all_negative), scanned_count) if aspects else None,
            "claim_matching_status": "completed" if evaluated and len(evaluated) == len([a for a in aspects if a["claim_count"]]) else ("partial" if evaluated else "not_evaluated"),
            "evidence_distribution": distribution,
            "risk_distribution": risk_distribution,
            "scored_aspect_count": len(scored),
            "risk_summary": risk_model.summarize(aspects),
            "shopping": shopping_risk.summarize(aspects, denominator),
            "sample_risk": overall_sample,
            "unadvertised_risk": unadvertised_sample,
            "safety_alert_count": len([a for a in aspects if a.get("safety_alert")]),
            "issue_only_aspect_count": len(issue_aspects),
            "issue_only_problem_count": issue_problem_total,
            "issue_only_review_count": len(issue_review_ids),
            "risk_model_version": risk_model.PARAMS["version"],
            "risk_index_calibrated": False,
            "support_review_count": len(all_confirmed | all_possible),
            "gap_counts": dict(Counter(g["gap_type"] for g in clean_gaps)),
        },
        "aspects": aspects, "product_issues": product_issues, "issue_evidence": recurrence,
        "report_decision": decision,
        "claims": clean_claims, "excluded_claims": excluded_claims,
        "gaps": clean_gaps, "reviews": clean_reviews, "clusters": clusters,
        "warnings": list(dict.fromkeys(warnings)), "limitations": limited,
        "method": {
            "probability_calibrated": False, "composite_score": None,
            "report_decision": report_decision.METHOD,
            "probability_display": {"color_scheme": "continuous_green_amber_red", "driven_by": "probability_estimate.estimate",
                                    "small_sample_affects_color": False, "missing_color": "gray"},
            "issue_evidence": {"version": issue_evidence.VERSION, "policy": issue_evidence.POLICY,
                               "meaning": issue_evidence.NOTE, "sources": issue_evidence.SOURCES},
            "sample_risk": {"version": sample_risk.VERSION, "thresholds": sample_risk.THRESHOLDS,
                            "role": "descriptive_context_only_legacy_bands_not_used_for_primary_warning",
                            "meaning": sample_risk.NOTE},
            "shopping": {"version": shopping_risk.VERSION, "policy": shopping_risk.POLICY,
                         "role": "legacy_aspect_summary_use_issue_evidence_for_primary_warning",
                         "meaning": shopping_risk.NOTE,
                         "attention_levels": "历史兼容方面级规则；当前主提醒采用issue_evidence的具体问题、窗口、规格与去重来源评估，不以方面比例分档。"},
            "risk_model": {
                "version": risk_model.PARAMS["version"],
                "aggregation": "hill",
                "params": {k: v for k, v in risk_model.PARAMS.items() if k != "sufficiency"},
                "calibrated": False,
                "meaning": "证据对宣传承诺的质疑强度，不是购买者遇到问题的概率",
                "sufficiency_levels": risk_model.SUFFICIENCY_LABELS,
                "severity": "单独展示，不并入指数；安全相关单独提醒",
            },
            "gap_evidence_rate": "明确矛盾去重评论数 / 实际扫描去重评论数",
            "negative_mention_rate": "方面负面去重评论数 / 实际扫描去重评论数",
            "negative_concentration": "方面负面去重评论数 / 有效差评数",
            "wilson": {"z": WILSON_Z, "confidence": 0.95},
            "evidence_status": EVIDENCE_LABELS,
            "severity": "not_labeled",
            "denominator": coverage_note,
        },
    }


def artifact_catalog(model: dict) -> list[dict]:
    prefix = re.sub(r"[^\w\u4e00-\u9fff-]", "_", _text(model.get("product_id")) or "商品")
    definitions = [
        ("report_html", "可视化分析报告", "html", "text/html; charset=utf-8", "html"),
        ("report_pdf", "简要分析报告", "pdf", "application/pdf", "pdf"),
        ("reviews_xlsx", "差评汇总", "xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "table"),
        ("reviews_json", "差评数据", "json", "application/json; charset=utf-8", "json"),
        ("product_json", "商品宣传信息", "json", "application/json; charset=utf-8", "json"),
    ]
    return [{"id": key, "name": name, "filename": f"{prefix}_{name}.{ext}", "mime": mime,
             "format": ext, "preview_kind": kind} for key, name, ext, mime, kind in definitions]


def artifact_bytes(model: dict, artifact_id: str) -> bytes:
    if artifact_id == "report_html":
        return render_html(model).encode("utf-8")
    if artifact_id == "report_pdf":
        return generate_pdf_bytes(model)
    if artifact_id == "reviews_xlsx":
        return generate_xlsx_bytes(model)
    if artifact_id == "reviews_json":
        payload = {"schema_version": "reviews-v1", "product_id": model["product_id"], "overview": model["overview"],
                   "sampling": model["sampling"], "aspects": model["aspects"], "issue_evidence": model.get("issue_evidence"),
                   "report_decision": model.get("report_decision"),
                   "reviews": model["reviews"], "problems": model["clusters"], "method": model["method"]}
    elif artifact_id == "product_json":
        payload = {"schema_version": "product-promotion-v1", "product": model["product"], "claims": model["claims"],
                   "excluded_claims": model.get("excluded_claims", [])}
    else:
        raise KeyError(f"未知产物：{artifact_id}")
    return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")


def preview_styles() -> str:
    """Embed the trusted local theme so isolated previews need no stylesheet fetch."""
    return resource_path("app", "static", "artifact-preview.css").read_text(encoding="utf-8")


def render_preview_html(model: dict, artifact_id: str) -> str:
    if artifact_id == "report_html":
        return render_html(model, standalone=False)
    if artifact_id == "product_json":
        columns, rows = ["宣传主张", "方面", "原文", "来源"], [[c["text"], c["aspect"], c["original_text"], c["source"]] for c in model["claims"]]
        title = "商品宣传信息"
    elif artifact_id in {"reviews_json", "reviews_xlsx"}:
        columns, rows = ["日期", "星级", "SKU", "评价内容", "进入分析"], [[r["date"], r["star"], r["sku"], r["content"], "是" if r["valid_for_analysis"] else "否"] for r in model["reviews"]]
        title = "差评汇总"
    else:
        raise KeyError(artifact_id)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(_text(v))}</td>" for v in row) + "</tr>" for row in rows)
    header = "".join(f"<th scope='col'>{html.escape(v)}</th>" for v in columns)
    product_section = ""
    if artifact_id == "product_json":
        labels = {"id":"商品 ID", "title":"标题", "subtitle":"副标题", "price_text":"价格", "price":"价格", "attributes":"商品属性", "props":"商品属性", "skus":"SKU", "sku_count":"SKU 数量", "brand":"品牌", "category":"品类"}
        info_rows = []
        for key, value in model["product"].items():
            if key not in labels:
                continue
            value = json.dumps(value, ensure_ascii=False, indent=2) if isinstance(value, (list, dict)) else _text(value)
            info_rows.append(f"<tr><th scope='row'>{labels[key]}</th><td>{html.escape(value)}</td></tr>")
        raw = model["product"].get("promotional_claims", [])
        raw_items = "".join(f"<li>{html.escape(row['text'])}<small>{html.escape(row['source'])}" +
                            (f" · {html.escape(row['source_image'])}" if row["source_image"] else "") + "</small></li>" for row in raw)
        product_section = ("<section class='preview-card'><div class='preview-card-heading'><h2>商品概况</h2></div>"
                           "<div class='table'><table class='product-properties'><tbody>" + "".join(info_rows) + "</tbody></table></div></section>"
                           + f"<details><summary>原始宣传文本（{len(raw)} 条）</summary><ol>{raw_items or '<li>未提供原始宣传文本。</li>'}</ol></details>"
                           "<h2 class='section-heading'>提取后的宣传主张</h2>")
    unit = "条主张" if artifact_id == "product_json" else "条评论"
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><style>{preview_styles()}</style></head><body><main class="{artifact_id}"><header class="preview-header"><p class="preview-eyebrow">商品分析工作区 / 文件预览</p><h1>{title}<span class="preview-count">{len(rows)} {unit}</span></h1><p class="subtitle">{html.escape(model['title'])}</p></header>{product_section}<div class="table"><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></div></main></body></html>'''


def render_html(model: dict, standalone: bool = True) -> str:
    """独立、无 CDN 的一页式可视化报告；数据只通过 textContent 插入 DOM。"""
    data = json.dumps(model, ensure_ascii=False, allow_nan=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return _HTML_TEMPLATE.replace("__REPORT_DATA__", data).replace("__REPORT_TITLE__", html.escape(model.get("title", "商品分析报告"))).replace("__VIEW_MODE__", "离线交互报告" if standalone else "应用内预览")


def _pdf_font() -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    with _FONT_LOCK:
        if "ReportCJK" in pdfmetrics.getRegisteredFontNames():
            return "ReportCJK"
        for candidate in (resource_path("app", "assets", "fonts", "NotoSansSC-Regular.ttf"),
                          Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/simsun.ttc")):
            if candidate.exists():
                try:
                    pdfmetrics.registerFont(TTFont("ReportCJK", str(candidate), subfontIndex=0))
                    return "ReportCJK"
                except Exception:
                    continue
        if "STSong-Light" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        return "STSong-Light"


def generate_pdf_bytes(model: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether
    font = _pdf_font()
    styles = {
        "title": ParagraphStyle("title", fontName=font, fontSize=22, leading=30, textColor=colors.HexColor("#0B1220"), spaceAfter=10),
        "sub": ParagraphStyle("sub", fontName=font, fontSize=9, leading=14, textColor=colors.HexColor("#63718A"), spaceAfter=7),
        "h2": ParagraphStyle("h2", fontName=font, fontSize=13, leading=20, textColor=colors.HexColor("#0B1220"), spaceBefore=10, spaceAfter=7),
        "body": ParagraphStyle("body", fontName=font, fontSize=9, leading=15, textColor=colors.HexColor("#152238"), spaceAfter=6, wordWrap="CJK", alignment=TA_LEFT),
        "small": ParagraphStyle("small", fontName=font, fontSize=8, leading=12, textColor=colors.HexColor("#63718A"), wordWrap="CJK"),
        "table_head": ParagraphStyle("table_head", fontName=font, fontSize=8, leading=12, textColor=colors.white, wordWrap="CJK"),
        "alert": ParagraphStyle("alert", fontName=font, fontSize=10, leading=16,
            textColor=colors.HexColor("#8a4b00"), backColor=colors.HexColor("#fff3df"),
            borderColor=colors.HexColor("#ebbe78"), borderWidth=.5, borderPadding=8,
            spaceBefore=9, spaceAfter=10, wordWrap="CJK"),
    }
    def p(text, kind="body"):
        return Paragraph(html.escape(_text(text)).replace("\n", "<br/>"), styles[kind])
    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=18*mm, rightMargin=18*mm, topMargin=17*mm, bottomMargin=17*mm,
                            title="商品宣传与低星体验分析", author="本地评论分析工具")
    o = model["overview"]
    story = [p("商品宣传与低星体验分析", "title"), p(model["title"]), p(f"商品 {model['product_id']}    生成于 {model['generated_at']}", "sub")]
    if model.get("freshness", {}).get("is_current") is False:
        story.append(p("历史版本：" + "；".join(model["freshness"].get("reasons", [])), "small"))
    def pct_text(value, digits=2):
        return f"{value * 100:.{digits}f}%" if value is not None else "未评估"
    shopping = o.get("shopping") or {}
    sampling = model["sampling"]
    sample = o.get("sample_risk") or {}
    extra_sample = o.get("unadvertised_risk") or {}
    recurrence = model.get("issue_evidence") or {}
    window = (recurrence.get("windows") or {}).get(str(recurrence.get("default_window_days", 30))) or {}
    primary, extra_primary = window.get("overall") or {}, window.get("unadvertised") or {}
    decision = model.get("report_decision") or {}
    decision_window = (decision.get("windows") or {}).get(str(recurrence.get("default_window_days", 30))) or {}
    ad_rows = {a["aspect"]: a for a in decision.get("advertised_aspects") or []}
    ad = decision_window.get("advertised") or {}
    extra_decision = decision_window.get("unadvertised") or {}
    metrics = [[p("扫描评论", "small"), p("低星评论 / 占扫描", "small"), p("可分析低星正文", "small"), p("宣传信息维度", "small")],
               [p(str(o["scanned_review_count"]), "h2"),
                p(f"{o['low_star_review_count']} / {pct_text(sampling['low_star_rate'])}", "h2"),
                p(str(o["valid_review_count"]), "h2"), p(str((decision.get("counts") or {}).get("claim_aspect_count", len({c['aspect'] for c in model['claims']}))), "h2")]]
    t = Table(metrics, colWidths=[43.5*mm]*4)
    t.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,-1), colors.HexColor("#EDF4FF")), ("VALIGN",(0,0),(-1,-1),"TOP"), ("LEFTPADDING",(0,0),(-1,-1),11), ("TOPPADDING",(0,0),(-1,-1),7), ("BOTTOMPADDING",(0,0),(-1,-1),7)]))
    shopping_aspects = sorted(
        [a for a in model["aspects"] if (a.get("shopping") or {}).get("negative_count")],
        key=lambda a: (a["shopping"]["attention_level"] != "safety", -a["shopping"]["negative_count"], a["aspect"]),
    )
    story += [t, Spacer(1, 3*mm), p("总体判断", "h2")]
    def index_text(value):
        return f"{value:.1f}/100" if isinstance(value, (int, float)) else "待评估"
    if decision_window:
        story.append(p("综合关注指数 " + index_text((decision_window.get("overall") or {}).get("index"))
            + "；宣传矛盾指数 " + index_text(ad.get("index")) + "；额外问题关注指数 " + index_text(extra_decision.get("index")), "body"))
        story.append(p("指数取较强的关注信号，不平均稀释；不是概率。", "small"))
        ad_primary = ad.get("probability_assessment") or {}
        story.append(p("宣传相关问题：" + (ad_primary.get("name") or "暂无可匹配具体问题") + "。" + _probability_text(ad_primary), "small"))
        extra_primary = extra_decision.get("probability_assessment") or extra_primary
        if extra_primary.get("review_count"):
            story.append(p("额外体验问题：" + extra_primary.get("name", "") + "。" + _probability_text(extra_primary), "small"))
        if extra_primary.get("review_count"):
            story.append(p("额外体验问题：" + extra_primary.get("name", "") + "；请查看以下证据。", "body"))
    else:
        story.append(p(_probability_text(primary), "small"))
    story += [p(sampling["coverage_note"], "small"), p("购物问题与证据", "h2")]
    by_aspect = {}
    for problem in window.get("problems", []):
        by_aspect.setdefault(problem["aspect"], problem)
    rows = [[p(v, "table_head") for v in ("方面", "购物提醒", "负面评论数", "占扫描样本", "占可分析低星", "宣传矛盾指数")]]
    for a in shopping_aspects[:14]:
        s = a["shopping"]
        rows.append([p(a["aspect"]), p(by_aspect.get(a["aspect"], {}).get("attention_label", "重复条件待核查")), p(str(s["negative_count"])),
                     p(pct_text(s["scanned_rate"])), p(pct_text(s["effective_share"])), p(index_text(ad_rows.get(a["aspect"], {}).get("index")))])
    if len(rows) == 1:
        rows.append([p("暂无已归类负面"), p("待核查"), p("0"), p("—"), p("—"), p("—")])
    table = Table(rows, colWidths=[34*mm, 34*mm, 24*mm, 26*mm, 26*mm, 24*mm], repeatRows=1)
    table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#13243A")), ("TEXTCOLOR",(0,0),(-1,0),colors.white), ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#f7f9fc")]), ("VALIGN",(0,0),(-1,-1),"TOP"), ("LINEBELOW",(0,0),(-1,0),0.6,colors.HexColor("#DCE5F2")), ("TOPPADDING",(0,0),(-1,-1),6), ("BOTTOMPADDING",(0,0),(-1,-1),6)]))
    story += [table, Spacer(1, 3*mm), p("表中占比为样本描述；方面内不同问题不合并触发重复提醒。", "small"),
              p("重点关注规则：同一具体问题在所选窗口内、已知同一规格至少四条具体去重反馈。四条是经验预警政策，未经购买结局校准；安全相关线索单列。", "small"),
              p("宣传矛盾与购物问题分开：没有对应宣传，或未检出直接矛盾，均不等于购物风险低。", "small")]
    if len(shopping_aspects) > 14:
        story.append(p(f"本页列出前14个方面，全部{len(shopping_aspects)}个方面及依据见交互报告。", "small"))
    if shopping.get("unclassified_effective_count"):
        story.append(p(f"另有 {shopping['unclassified_effective_count']} 条低星正文未归入负面方面，可能为正面正文、无具体问题或抽取遗漏，请核查原文。", "small"))
    if shopping_aspects:
        story += [PageBreak(), p("重点购物问题与评论依据", "title"),
                  p("以下摘取主要问题，完整证据可在 HTML 报告中展开查看。评论为用户陈述，尚未经实物核验。", "sub")]
        for i, a in enumerate(shopping_aspects[:3], 1):
            s = a["shopping"]
            names = "；".join(dict.fromkeys(c["name"] for c in s["problems"] if c["name"]))
            block = [p(f"{i}. {a['aspect']} · {by_aspect.get(a['aspect'], {}).get('attention_label', '重复条件待核查')}", "h2"),
                     p(f"{s['negative_count']} 条负面评论；占扫描样本 {pct_text(s['scanned_rate'])}；占可分析低星 {pct_text(s['effective_share'])}", "sub"), p(names)]
            block.append(p(_probability_text(by_aspect.get(a['aspect'], {})), "small"))
            for evidence in s["evidence"][:2]:
                block.append(p(f"评论 {evidence['review_id']}：{evidence['content'][:180]}", "small"))
            if not a["claim_count"]:
                block.append(p("未发现该方面的可比宣传承诺；以上问题仍需关注。", "small"))
            else:
                block.append(p(f"宣传匹配：{a['evidence_label']}；明确矛盾 {a['contradiction_count']} 条；宣传矛盾指数 "
                    + index_text(ad_rows.get(a["aspect"], {}).get("index")) + "。", "small"))
            block.append(Spacer(1, 3*mm))
            story.append(KeepTogether(block))
    story += [Spacer(1, 4*mm), p("样本与分析范围", "h2")]
    for note in model["limitations"][:5]:
        story.append(p(note, "small"))
    def footer(canvas, document):
        canvas.saveState();canvas.setFont(font, 8);canvas.setFillColor(colors.HexColor("#728195"))
        canvas.drawString(18*mm, 10*mm, "商品宣传与低星体验分析")
        canvas.drawRightString(A4[0]-18*mm, 10*mm, str(document.page));canvas.restoreState()
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return out.getvalue()


def generate_xlsx_bytes(model: dict) -> bytes:
    """应用运行时生成 XLSX，评论与 ID 明确作为文本，不执行输入公式。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.table import Table, TableStyleInfo
    from openpyxl.utils import get_column_letter
    wb = Workbook();wb.remove(wb.active)
    def sheet(name, headers, rows, widths):
        ws = wb.create_sheet(name)
        ws.append([name]);ws.append([model["title"]]);ws.append(headers)
        for values in rows:
            ws.append([_text(v)[:32767] if isinstance(v, str) else v for v in values])
        for row in ws:
            for cell in row:
                if isinstance(cell.value, str):
                    cell.data_type = "s"
                    cell.number_format = "@"
                cell.font = Font(name="微软雅黑", size=10, color="152238")
                cell.alignment = Alignment(vertical="top", wrap_text=True)
        for cell in ws[3]:
            cell.fill = PatternFill("solid", fgColor="13243A");cell.font = Font(name="微软雅黑", size=10, bold=True, color="FFFFFF")
        ws["A1"].font = Font(name="微软雅黑", size=18, bold=True, color="0B1220")
        ws["A2"].font = Font(name="微软雅黑", size=10, color="65768B")
        ws.merge_cells(start_row=1,start_column=1,end_row=1,end_column=len(headers));ws.merge_cells(start_row=2,start_column=1,end_row=2,end_column=len(headers))
        ws.row_dimensions[1].height=30;ws.row_dimensions[2].height=28;ws.row_dimensions[3].height=28
        for i, width in enumerate(widths, 1):ws.column_dimensions[get_column_letter(i)].width=width
        for row_number, values in enumerate(rows, 4):
            lines = max((sum(2 if ord(ch) > 127 else 1 for ch in _text(value)) / widths[col]
                         for col, value in enumerate(values)), default=1)
            ws.row_dimensions[row_number].height = min(240, max(27, 15 * math.ceil(lines)))
        ws.freeze_panes="B4";ws.sheet_view.showGridLines=False
        if rows:
            table=Table(displayName="Data"+str(len(wb.worksheets)),ref=f"A3:{get_column_letter(len(headers))}{len(rows)+3}")
            table.tableStyleInfo=TableStyleInfo(name="TableStyleMedium2",showRowStripes=True);ws.add_table(table)
        ws.print_title_rows="1:3";ws.sheet_properties.pageSetUpPr.fitToPage=True
        ws.page_setup.orientation="landscape";ws.page_setup.paperSize=ws.PAPERSIZE_A4;ws.page_setup.fitToWidth=1;ws.page_setup.fitToHeight=0
        return ws
    aspects = model["aspects"]
    engine = model.get("issue_evidence") or {}
    window = (engine.get("windows") or {}).get(str(engine.get("default_window_days", 30))) or {}
    decision = model.get("report_decision") or {}
    decision_window = (decision.get("windows") or {}).get(str(engine.get("default_window_days", 30))) or {}
    ad_rows = {a["aspect"]: a for a in decision.get("advertised_aspects") or []}
    by_aspect = {}
    for problem in window.get("problems", []):
        by_aspect.setdefault(problem["aspect"], problem)
    ws=sheet("风险概览", ["方面","宣传矛盾指数","宣传矛盾档位","证据充分性","明确矛盾数","具体关联数","仅提及数","支持证据数","严重程度","负面提及数","占扫描样本","宣传矛盾率","购物提醒","占低星评论","占可分析低星"],
        [[a["aspect"],ad_rows.get(a["aspect"], {}).get("index"),
          ({"high": "高", "medium": "中", "low": "低"}.get(ad_rows.get(a["aspect"], {}).get("band"), "") + "风险指数") if ad_rows.get(a["aspect"], {}).get("index") is not None else "暂未形成指数",
          a["evidence_sufficiency_label"],
          (a.get("evidence_structure") or {}).get("contradiction", a["contradiction_count"]),
          (a.get("evidence_structure") or {}).get("related_negative", a["possible_count"]),
          (a.get("evidence_structure") or {}).get("topic_only", 0),
          (a.get("evidence_structure") or {}).get("support", a.get("support_evidence_count",0)),
          a.get("severity_label") or "未分级",a["shopping"]["negative_count"],a["shopping"]["scanned_rate"],a.get("legacy_direct_rate"),
          by_aspect.get(a["aspect"], {}).get("attention_label", "重复条件待核查"),a["shopping"]["low_star_share"],a["shopping"]["effective_share"]] for a in aspects],
        [24,16,16,14,14,14,14,14,18,14,16,16,25,16,16])
    for row in range(4,len(aspects)+4):
        for column in (11, 12, 14, 15):
            ws.cell(row,column).number_format="0.00%"
        ws.cell(row,2).number_format="0.0"
    end=len(aspects)+6;ws.cell(end,1,issue_evidence.NOTE);ws.merge_cells(start_row=end,start_column=1,end_row=end,end_column=15);ws.cell(end,1).alignment=Alignment(wrap_text=True);ws.row_dimensions[end].height=60
    ws.cell(end+1,1,model["sampling"]["coverage_note"])
    ws.merge_cells(start_row=end+1,start_column=1,end_row=end+1,end_column=15)
    extra = model["overview"].get("unadvertised_risk") or {}
    if extra.get("warning_text"):
        ws.cell(end+2,1,extra["warning_text"])
        ws.merge_cells(start_row=end+2,start_column=1,end_row=end+2,end_column=15)
        ws.cell(end+2,1).font = Font(name="微软雅黑", size=10, bold=True, color="9C4200")
        ws.cell(end+2,1).fill = PatternFill("solid", fgColor="FFF3DF")
    rates = []
    for label, signal in (("整体已发现负面", model["overview"].get("sample_risk") or {}), ("未对应宣传问题", extra)):
        value = signal.get("scanned") or {}
        rate = value.get("rate")
        rates.append(f"{label}：{value.get('numerator', 0)}/{value.get('denominator', 0)}="
                     + (f"{rate*100:.2f}%" if rate is not None else "分母未提供"))
    ws.cell(end+3,1,"；".join(rates) + "。以上为本次样本占比，不是实际购买发生概率。")
    ws.merge_cells(start_row=end+3,start_column=1,end_row=end+3,end_column=15)
    if decision_window:
        def index_label(value):
            return f"{value:.1f}/100" if isinstance(value, (int, float)) else "待评估"
        summary_line = ("综合关注指数 " + index_label((decision_window.get("overall") or {}).get("index"))
            + "；宣传矛盾指数 " + index_label((decision_window.get("advertised") or {}).get("index"))
            + "；额外问题关注指数 " + index_label((decision_window.get("unadvertised") or {}).get("index"))
            + "。采用 report-decision-v1；综合取较强信号，不平均稀释；指数不是概率，旧 risk-v2 保留在 JSON。")
        ws.cell(end+4, 1, summary_line)
        ws.merge_cells(start_row=end+4,start_column=1,end_row=end+4,end_column=15)
        ws.cell(end+4, 1).alignment = Alignment(wrap_text=True)
        ws.row_dimensions[end+4].height = 32
    current = (decision_window.get("unadvertised") or {}).get("probability_assessment") or window.get("overall") or {}
    ws.cell(end+5, 1, "额外体验问题：" + (current.get("name") or "暂无可估计具体问题") + "；"
        + current.get("window_label", "截止日未知") + "；" + _probability_text(current))
    ws.merge_cells(start_row=end+5,start_column=1,end_row=end+5,end_column=15)
    ws.cell(end+5, 1).alignment = Alignment(wrap_text=True)
    ws.row_dimensions[end+5].height = 45
    ws.cell(end+6, 1, "具体问题：问题、方面、近期反馈、同规格最多、来源、提醒、原文ID、条件复现概率、95%区间下/上界、k/n、估计规格")
    ws.merge_cells(start_row=end+6,start_column=1,end_row=end+6,end_column=15)
    for number, problem in enumerate(window.get("problems", []), end+7):
        probability = problem.get("probability_estimate") or {}
        interval = probability.get("interval") or [None, None]
        for col, value in enumerate((problem["name"], problem["aspect"], problem["repeat_count"], problem["max_same_sku_count"],
                                     problem["identity_label"], problem["attention_label"], ", ".join(problem["review_ids"]),
                                     probability.get("estimate"), interval[0], interval[1],
                                     probability.get("successes"), probability.get("total"), probability.get("sku_label", "")), 1):
            ws.cell(number, col, value).data_type = "s" if isinstance(value, str) else "n"
            ws.cell(number, col).alignment = Alignment(wrap_text=True, vertical="top")
        for col in (8, 9, 10):
            ws.cell(number, col).number_format = "0.0%"
        ws.row_dimensions[number].height = 60
    by_review = {r["review_id"]: [] for r in model["reviews"]}
    for c in model["clusters"]:
        for rid in c["review_ids"]:by_review.setdefault(rid,[]).append(c["name"])
    sheet("差评明细", ["评论ID","日期","星级","SKU","评价原文","进入分析","关联问题"],
        [[r["review_id"],r["date"],r["star"],r["sku"],r["content"],"是" if r["valid_for_analysis"] else "否","；".join(by_review.get(r["review_id"],[]))] for r in model["reviews"]], [22,16,10,32,85,14,42])
    sheet("宣传与落差", ["主张ID","方面","宣传主张","宣传原文","判断","支持去重评论数","证据强度","分析依据","评论ID"],
        [[g["claim_id"],g["aspect"],g["claim_text"],g["claim_original_text"],
          {"potential_conflict":"明确矛盾","partial_gap":"可能矛盾","no_direct_conflict_found":"未检出明确矛盾","insufficient_evidence":"证据不足"}.get(g["gap_type"],g["gap_label"]),g["support_review_count"],
          {"high":"强","medium":"中","low":"弱","insufficient":"不足"}.get(g["confidence_level"],"不足"),g["finding"],", ".join(g["support_review_ids"])] for g in model["gaps"]], [23,20,48,48,23,19,16,80,40])
    sheet("商品宣传信息", ["主张ID","方面","宣传主张","原文","来源","来源图片","可验证"],
        [[c["claim_id"],c["aspect"],c["text"],c["original_text"],c["source"],c["source_image"],"是" if c["verifiable"] else "否"] for c in model["claims"]], [23,20,50,65,20,30,12])
    out=io.BytesIO();wb.save(out);return out.getvalue()
