"""Compact report decisions; engineering attention scores stay separate from probability.

No I/O, seller assumptions, LLM calls, or mutation of legacy risk-v2 data.
"""
from __future__ import annotations

from datetime import timedelta
from difflib import SequenceMatcher
import math

import issue_evidence
import risk_model

VERSION = "report-decision-v1"
PARAMS = {"quality": {"concrete": 1.0, "partial": .62, "vague": .3, "unknown": .5},
          "half_saturation_mass": .5, "band_medium": 35, "band_high": 65}
METHOD = {
    "version": VERSION, "calibrated": False,
    "advertised_index": "100 × W / (0.5 + W); W为原文可核验、去重的明确矛盾证据具体性权重之和",
    "unadvertised_index": "未归入明确宣传矛盾的最主要额外具体问题，其同规格条件复现概率 × 100",
    "overall_index": "max(宣传矛盾指数, 额外问题关注指数); 缺失保留未知，强项不被另一项平均稀释",
    "overall_meaning": "综合关注指数是报告排序策略，不是总体矛盾概率或购买后问题发生概率",
    "advertised_probability": "与原文可核验的明确矛盾引用匹配的最主要具体问题复现概率，不是任意宣传矛盾事件的概率",
    "params": PARAMS,
    "limitations": ["证据指数、具体性权重、半饱和常数与分档均为未校准的工程展示策略。",
        "宣传匹配未对每条可比评论逐条标注反驳/非反驳，不能把未列为矛盾证据的评论当成反例估计总体宣传矛盾概率。",
        "综合指数比较两类关注信号的展示强度，不将它们视为同一种概率或独立事件。",
        "具体问题概率只预测同规格、同窗口条件下下一条可分析低星反馈；采样选择与不可见评论偏差未获校正。"]}


def _s(value):
    return str(value or "").strip()


def _compact(value):
    return issue_evidence._snapshot_text(value)


def band(value):
    if value is None:
        return "none"
    return "high" if value >= PARAMS["band_high"] else "medium" if value >= PARAMS["band_medium"] else "low"


def _empty_assessment():
    return {"name": "", "review_ids": [], "recent_review_ids": [], "repeat_count": 0,
            "max_same_sku_count": 0, "attention_level": "none", "attention_label": "暂无可估计的具体问题",
            "probability": None, "purchase_probability": None,
            "probability_estimate": {"status": "insufficient", "estimate": None, "interval": None,
                "successes": 0, "total": 0, "reason": "没有可核验且满足条件的具体问题。", "tone": "none"}}


def _verified_units(units, reviews):
    result = []
    for unit in units:
        rid = _s(unit.get("review_id"))
        review = reviews.get(rid, {})
        text = _s(unit.get("text"))
        content = _s(review.get("content") or review.get("text"))
        # A model's contradiction label alone is insufficient: the quotation
        # must be checkable in the supplied snapshot. Do not run a second
        # keyword classifier here: "没有图片中宽松肥大" is a meaningful
        # contradiction even though the legacy defect lexicon misses it.
        if (not rid or not content or review.get("valid_for_analysis") is False or not text
                or _compact(text) not in _compact(content)
                or unit.get("quote_status") not in {"verbatim", "original_review"}
                or risk_model.relation_of(unit.get("relation"), text) != "contradiction"):
            continue
        result.append({**unit, "review_id": rid, "quality": risk_model.quality_of(content, False, unit.get("quality")),
                       "full_text": content, "source": issue_evidence._source(review)})
    # Connected components preserve bridge duplicates and OR all linked claims.
    # Short identical complaints are not automatically the same buyer/feedback.
    parents = list(range(len(result)))
    def find(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i
    for i, left in enumerate(result):
        for j, right in enumerate(result[:i]):
            same = left["review_id"] == right["review_id"] or left["source"] == right["source"]
            a, b = _compact(left["full_text"]), _compact(right["full_text"])
            copied = (len(a) >= 24 and len(b) >= 24 and min(len(a), len(b)) / max(len(a), len(b)) >= .9
                      and SequenceMatcher(None, a, b, autojunk=False).ratio() >= .95)
            if same or copied:
                parents[find(i)] = find(j)
    groups = {}
    for i, row in enumerate(result):
        groups.setdefault(find(i), []).append(row)
    return result, list(groups.values())


def _advertised_row(aspect, units, reviews, *, as_of=None, days=None):
    candidates = units
    excluded_dates = 0
    if days is not None:
        candidates = []
        for row in units:
            day = issue_evidence._day(reviews.get(_s(row.get("review_id")), {}).get("date"))
            if as_of and day and as_of - timedelta(days=days-1) <= day <= as_of:
                candidates.append(row)
            else:
                excluded_dates += 1
    verified, groups = _verified_units(candidates, reviews)
    mass = sum(max(PARAMS["quality"].get(row["quality"], .5) for row in group) for group in groups)
    index = round(100 * mass / (PARAMS["half_saturation_mass"] + mass), 1) if mass else None
    return {"aspect": aspect, "index": index, "band": band(index), "kind": "contradiction_evidence_strength",
            "contradiction_count": len(groups), "review_ids": sorted({r["review_id"] for r in verified}),
            "safety_alert": any(risk_model.severity_of(r["text"], r.get("severity")) == "critical" for r in verified),
            "weighted_evidence_mass": round(mass, 4), "excluded_date_count": excluded_dates,
            "assessment": _empty_assessment(), "_verified_units": verified}


def _matches_problem(problem, row):
    if problem.get("aspect") != row["aspect"] or problem.get("unadvertised"):
        return False
    by_id = {}
    for unit in row["_verified_units"]:
        # No explicit, targeted quotation: retain the contradiction index but
        # do not associate every defect in an entire multi-issue review with it.
        if unit.get("provided_quote") and unit.get("quote_status") == "verbatim":
            by_id.setdefault(unit["review_id"], []).append(unit)
    for evidence in problem.get("evidence") or []:
        if not evidence.get("specific"):
            continue
        for unit in by_id.get(_s(evidence.get("review_id")), []):
            signature, _ = issue_evidence._signature(unit.get("text"))
            other, _ = issue_evidence._signature(evidence.get("quote"))
            if signature and signature == other:
                return True
    return False


def _probability_index(assessment):
    p = assessment.get("probability_estimate") or {}
    value = p.get("estimate")
    if p.get("status") != "estimated" or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        return None
    return round(100 * value, 1)


def _strongest(rows):
    known = [r for r in rows if r["index"] is not None]
    return max(known, key=lambda r: (r["index"], r["aspect"])) if known else None


def _public(row):
    return {k: v for k, v in row.items() if not k.startswith("_")}


def build(*, aspects, recurrence, reviews, advertised_units, counts):
    """Assemble independent ad/extra sections without modifying their inputs."""
    review_index = {_s(r.get("review_id")): r for r in reviews}
    all_ad_rows = [_advertised_row(a["aspect"], advertised_units.get(a["aspect"], []), review_index)
                   for a in aspects if a.get("claim_count")]
    all_ad_rows.sort(key=lambda r: (-(r["index"] if r["index"] is not None else -1), r["aspect"]))
    as_of = issue_evidence._day(recurrence.get("as_of"))
    windows = {}
    for window, source in (recurrence.get("windows") or {}).items():
        days = int(window)
        problems = source.get("problems") or []
        ad_rows = [_advertised_row(a["aspect"], advertised_units.get(a["aspect"], []), review_index, as_of=as_of, days=days)
                   for a in aspects if a.get("claim_count")]
        # Preserve the recurrence engine's evidence/source ordering. A tiny SKU
        # with a high percentage never wins simply because of that percentage.
        matched = []
        for row in ad_rows:
            matches = [p for p in problems if _matches_problem(p, row)]
            row["assessment"] = matches[0] if matches else _empty_assessment()
            matched.extend(matches)
        matched_ids = {p.get("problem_id") for p in matched}
        ad_assessment = next((p for p in problems if p.get("problem_id") in matched_ids), _empty_assessment())
        top_ad = _strongest(ad_rows)
        ad_index = top_ad["index"] if top_ad else None
        # An aspect having *some* claim does not mean every specific defect in
        # that aspect has been advertised or tested against that claim. Keep
        # all other concrete problems visible as additional experience issues.
        # The source unadvertised bool stays unchanged for provenance.
        extra = [{**p, "scope_reason": "no_claim_in_aspect" if p.get("unadvertised") else "not_established_ad_contradiction"}
                 for p in problems if p.get("problem_id") not in matched_ids]
        extra_assessment = extra[0] if extra else _empty_assessment()
        extra_rows = []
        for aspect in dict.fromkeys(p["aspect"] for p in extra):
            items = [p for p in extra if p["aspect"] == aspect]
            assessment = items[0]
            index = _probability_index(assessment)
            extra_rows.append({"aspect": aspect, "index": index, "band": band(index),
                "kind": "conditional_problem_recurrence", "assessment": assessment, "problem_count": len(items),
                "review_ids": sorted({rid for p in items for rid in p.get("review_ids") or []})})
        extra_index = _probability_index(extra_assessment)
        components = {"advertised": ad_index, "unadvertised": extra_index}
        known = {k: v for k, v in components.items() if v is not None}
        dominant = max(known, key=known.get) if known else None
        overall_index = known.get(dominant) if dominant else None
        windows[window] = {
            "overall": {"index": overall_index, "band": band(overall_index), "dominant": dominant,
                "components": components, "missing_components": [k for k, v in components.items() if v is None],
                "status": "complete" if len(known) == 2 else "partial" if known else "unavailable",
                "label": "综合关注指数", "is_probability": False},
            "advertised": {"index": ad_index, "band": band(ad_index), "aspects": [_public(r) for r in ad_rows],
                "top_aspect": top_ad["aspect"] if top_ad else "", "probability_assessment": ad_assessment,
                "probability_label": "宣传相关问题复现概率",
                "probability_scope": "与明确矛盾引用匹配的最主要具体问题；不是全部宣传的总体矛盾概率"},
            "unadvertised": {"index": extra_index, "band": band(extra_index), "aspects": extra_rows, "problems": extra,
                "top_aspect": extra_assessment.get("aspect", ""), "probability_assessment": extra_assessment,
                "probability_label": "额外问题复现概率", "probability_scope": "无同方面宣传，或尚未与明确宣传矛盾关联的具体体验问题；规格分别估计"}}
    top = _strongest(all_ad_rows)
    return {"version": VERSION, "counts": dict(counts), "default_window_days": recurrence.get("default_window_days", 30),
            "advertised_aspects": [_public(r) for r in all_ad_rows],
            "advertised": {"index": top["index"] if top else None, "band": band(top["index"] if top else None),
                "top_aspect": top["aspect"] if top else ""}, "windows": windows, "method": METHOD}
