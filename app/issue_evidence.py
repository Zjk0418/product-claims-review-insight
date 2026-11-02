"""Snapshot-based, denominator-independent repeated-problem warnings.

Four reports is an explicit attention policy, not a fitted probability or a
scientifically validated decision threshold. Conditional probability is computed
with explicit Bernoulli assumptions; this module does no I/O or model API calls.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher

from analysis.normalization import DEFECT_SIGNATURES, DEFECT_LABELS
import risk_model
from probability_estimation import VERSION as PROBABILITY_VERSION, beta_estimate

VERSION = "issue-recurrence-v1"
POLICY = {"repeat_threshold": 4, "minimum_specific_units": 2, "default_window_days": 30,
          "window_options": [7, 30, 90], "near_copy_min_chars": 24, "near_copy_similarity": .95}
NOTE = ("同类问题复现概率预测下一条同条件、同规格、完成抽取的可分析低星反馈再次识别到本问题的可能性，"
        "不是购买后故障概率。模型内95%后验可信区间不校正评论选择、来源相关或抽取错误。"
        "近期重复提醒与概率分开；四条仍为经验辅助预警规则，未经购买结果数据验证。")
SOURCES = [
    {"title": "The Polarity of Online Reviews: Prevalence, Drivers and Implications",
     "url": "https://business.columbia.edu/faculty/research/polarity-online-reviews-prevalence-drivers-and-implications"},
    {"title": "Towards Characterizing and Detecting Incentivized Reviews in Online Platforms",
     "url": "https://ojs.aaai.org/index.php/ICWSM/article/view/35877"},
    {"title": "NIST: Proportion Confidence Limits (Jeffreys method)",
     "url": "https://www.itl.nist.gov/div898/software/dataplot/refman1/auxillar/propconf.htm"},
]
_CONCRETE = (
    ("pilling", "起球", ("起球", "起毛球", "起小球")),
    ("loose_thread", "线头", ("线头",)),
    ("seam_split", "开线", ("开线", "开缝", "缝线裂")),
    ("color_fading", "掉色", ("掉色", "褪色", "脱色")),
    ("color_mismatch", "色差", ("色差",)),
    ("elasticity_low", "弹性不足", ("弹性不太够", "弹性不足", "没弹性", "没有弹性", "弹性差")),
    ("too_hard", "偏硬", ("太硬", "僵硬", "偏硬", "很硬")),
    ("too_soft", "偏软", ("太软", "偏软")),
    ("too_thin", "偏薄", ("太薄", "偏薄", "很薄")),
    ("too_thick", "偏厚", ("太厚", "偏厚")),
    ("unglued", "开胶", ("开胶", "脱胶")),
    ("broken", "破损", ("破损", "破了", "断裂", "破洞")),
    ("deformed", "变形", ("变形",)),
    ("electric_leak", "漏电", ("漏电",)),
    ("fire", "起火", ("起火", "爆炸",)),
    ("injury", "受伤", ("灼伤", "中毒", "受伤",)),
    ("refund_refused", "拒绝退款", ("不给退", "拒绝退款", "拒绝退货", "不退钱")),
    ("replacement_refused", "拒绝更换", ("不给更换", "不给换", "拒绝换货", "不给我换")),
    ("refund_delayed", "退款拖延", ("运费四天", "退款拖", "迟迟不退款", "退款没到账")),
    ("delivery_slow", "物流缓慢", ("物流慢", "物流太慢", "快递太慢", "迟迟不发货")),
    ("leaking", "漏液", ("漏液", "漏水", "漏出来")),
    ("odor", "异味", ("异味", "刺鼻", "浓的味", "味很重", "气味很大")),
    ("slippery", "打滑", ("不防滑", "打滑")),
)
_GENERIC = {"material_quality", "false_claim", "ad_mismatch", "dishonest", "brand_unknown",
            "price_unworthy", "attitude", "after_sales", "stitching", "follow_up", "partition", "upsell"}
_RANK = {"none": 0, "historical": 1, "incomplete": 2, "single": 3, "repeated": 4,
         "cross_sku": 5, "repeat": 6, "safety": 7}


def _s(value):
    return str(value or "").strip()


def _compact(value):
    return re.sub(r"[\W_]+", "", _s(value)).lower()


def _day(value):
    if isinstance(value, (date, datetime)):
        return value.date() if isinstance(value, datetime) else value
    match = re.match(r"^(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})", _s(value))
    try:
        return date(*map(int, match.groups())) if match else None
    except ValueError:
        return None


def _signature(quote):
    # Explicit concrete consequences take precedence over broad cluster labels.
    text = _s(quote)
    hits = []
    for key, label, words in _CONCRETE:
        for word in words:
            for match in re.finditer(re.escape(word), text):
                pos = match.start()
                if not _negated(text, pos):
                    hits.append((pos, -len(word), key, label))
    for key, words in DEFECT_SIGNATURES:
        if key in _GENERIC:
            continue
        for word in words:
            for match in re.finditer(re.escape(word), text):
                pos = match.start()
                if not _negated(text, pos):
                    hits.append((pos, -len(word), key, DEFECT_LABELS.get(key, key)))
    if hits:
        hit = min(hits)
        return hit[2], hit[3]
    return "", ""


def _negated(text, pos):
    return bool(re.search(r"(?:没有|不会|并不|不再|未见|并未|不是|并非|不|没|无|未)(?:出现|发生|明显的)?$", text[max(0, pos-8):pos]))


def _records(clusters, issues):
    rows = []
    membership = {}
    for c in clusters:
        for iid in c.get("member_issue_ids") or []:
            membership.setdefault(_s(iid), []).append(_s(c.get("cluster_id")))
    if issues:
        for issue in issues:
            if isinstance(issue, dict):
                rows.append({**issue, "cluster_ids": membership.get(_s(issue.get("issue_id")), [])})
        return rows
    # Older snapshots: split by the per-review quote, never assume every cluster
    # member expresses its label. Missing quotes remain visible but unqualified.
    for c in clusters:
        evidence = c.get("review_evidence") or {}
        if isinstance(evidence, list):
            evidence = {_s(e.get("review_id")): e.get("evidence") for e in evidence if isinstance(e, dict)}
        for rid in c.get("review_ids") or []:
            rows.append({"review_id": _s(rid), "object": c.get("object"), "aspect": c.get("aspect"),
                         "manifestation": c.get("problem_name") or c.get("name"),
                         "evidence": evidence.get(_s(rid), ""), "basis": "explicit",
                         "cluster_ids": [_s(c.get("cluster_id"))]})
    return rows


def _source(review):
    for key in ("reviewer_id", "buyer_id", "user_id"):
        if _s(review.get(key)):
            # Hash stable identifiers; report outputs never expose raw account IDs.
            return "source:" + hashlib.sha256(_s(review[key]).encode()).hexdigest()
    parent = _s(review.get("parent_review_id") or review.get("original_review_id"))
    return "review:" + (parent or _s(review.get("review_id")))


def _sku(review):
    sku_id, text = _s(review.get("sku_id")), _s(review.get("sku"))
    key = "id:" + sku_id if sku_id else "text:" + re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold() if text else ""
    return key, text or sku_id


def _is_low_star(review):
    star = review.get("star")
    if isinstance(star, (int, float)) and not isinstance(star, bool):
        return 1 <= star <= 3
    return _s(review.get("rate_type")) in {"-1", "0"} or _s(review.get("rate_label")) in {"差评", "中评", "中差评"}


def _extraction_index(extraction):
    records = extraction.get("reviews") if isinstance(extraction, dict) else extraction
    index = {}
    for record in records or []:
        if isinstance(record, dict) and _s(record.get("review_id")):
            index.setdefault(_s(record["review_id"]), []).append(record)
    return index


def _snapshot_text(value):
    """Snapshot equality keeps semantic punctuation such as 1.5 versus 15."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", _s(value)))


def _extraction_status(review, records):
    if not records:
        return "missing_extraction"
    for record in records:
        if record.get("status") not in {"ok", "empty", "cached"} or record.get("errors"):
            return "incomplete_extraction"
        for key in ("content", "text"):
            if key in record and _snapshot_text(record[key]) != _snapshot_text(review.get("text") or review.get("content")):
                return "snapshot_mismatch"
        for key in ("sku_id", "product_id"):
            if _s(record.get(key)) and _s(review.get(key)) and _s(record[key]) != _s(review[key]):
                return "snapshot_mismatch"
        if _s(record.get("date")) and _s(review.get("date")) and _day(record["date"]) != _day(review["date"]):
            return "snapshot_mismatch"
        if isinstance(record.get("star"), (int, float)) and isinstance(review.get("star"), (int, float)) and record["star"] != review["star"]:
            return "snapshot_mismatch"
    return "complete"


def _issue_snapshot_matches(records, problem_rows):
    """Reject contradictory explicit issue lists rather than treating them as zero.

    Older status-only records remain usable with an explicit provenance caveat.
    Concrete quoted propositions must agree in both available representations.
    """
    expected = [row["quote"] for row in problem_rows if row["specific"]]
    def same_fact(a, b):
        left, right = _snapshot_text(a), _snapshot_text(b)
        signature = _signature(a)[0]
        return bool(left and right and signature and signature == _signature(b)[0]
                    and (left in right or right in left))
    for record in records:
        if "issues" not in record:
            continue
        issues = record["issues"]
        if not isinstance(issues, list) or record.get("status") == "empty" and issues:
            return False
        actual = [_s(i.get("evidence")) for i in issues
                  if isinstance(i, dict) and i.get("basis") != "inferred" and _signature(i.get("evidence"))[0]]
        if any(not any(same_fact(quote, source) for source in actual) for quote in expected):
            return False
        if any(not any(same_fact(source, quote) for quote in expected) for source in actual):
            return False
    return True


def _cohort_units(events):
    """One common deduplication for all problem outcomes, retaining every member.

    Connected components make the result independent of row/issue order. An
    outcome is positive if any member has that attributed problem event; a
    copied negative cannot disappear because another member sorted first.
    """
    parent = list(range(len(events)))
    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    def join(i, j):
        a, b = root(i), root(j)
        if a != b:
            parent[max(a, b)] = min(a, b)
    sources, reviews, exact = {}, {}, {}
    texts = [_compact(e["content"]) for e in events]
    for i, event in enumerate(events):
        for cache, key in ((sources, event["source_key"]), (reviews, event["review_link"])):
            if key in cache:
                join(i, cache[key])
            else:
                cache[key] = i
        if len(texts[i]) >= POLICY["near_copy_min_chars"]:
            if texts[i] in exact:
                join(i, exact[texts[i]])
            else:
                exact[texts[i]] = i
    for i, a in enumerate(texts):
        if len(a) < POLICY["near_copy_min_chars"]:
            continue
        for j in range(i):
            b = texts[j]
            if root(i) == root(j) or len(b) < POLICY["near_copy_min_chars"]:
                continue
            if min(len(a), len(b)) / max(len(a), len(b)) >= .9 and SequenceMatcher(None, a, b, autojunk=False).ratio() >= POLICY["near_copy_similarity"]:
                join(i, j)
    groups = {}
    for i, event in enumerate(events):
        unit = groups.setdefault(root(i), {"event_keys": set(), "review_ids": set(), "identity_known": True})
        unit["event_keys"].add(event["event_key"])
        unit["review_ids"].add(event["review_id"])
        unit["identity_known"] = unit["identity_known"] and event["identity_known"]
    return list(groups.values())


def _probability_cohorts(reviews, groups, extraction, as_of, days, product_id):
    """Build window/SKU cohorts once, rather than once for every problem."""
    extraction_by_id = _extraction_index(extraction)
    append_events, problem_rows = {}, {}
    for group in groups.values():
        for row in group["rows"]:
            problem_rows.setdefault(row["review_id"], []).append(row)
            if row["specific"] and _day(row["date"]):
                append_events.setdefault(row["review_id"], set()).add(_day(row["date"]))
    cohorts, excluded = {}, Counter()
    raw_count, complete_count, metadata_only = 0, 0, 0
    for review in reviews:
        rid = _s(review.get("review_id"))
        if not rid or review.get("valid_for_analysis") is False or not _s(review.get("text") or review.get("content")):
            excluded["ineligible_review"] += 1
            continue
        if product_id and _s(review.get("product_id")) and _s(review["product_id"]) != _s(product_id):
            excluded["wrong_product"] += 1
            continue
        if not _is_low_star(review):
            excluded["not_confirmed_low_star"] += 1
            continue
        sku_key, sku_label = _sku(review)
        if not sku_key:
            excluded["unknown_sku"] += 1
            continue
        record_dates = {_day(review.get("date"))}
        appended = _day(review.get("append_date"))
        if appended and appended in append_events.get(rid, set()) and _s(review.get("append_content")):
            # A new date alone cannot promote an old initial complaint. Only
            # attributed, concrete append evidence creates an append event.
            record_dates.add(appended)
        usable_dates = set()
        for day in record_dates:
            if not day or not as_of:
                excluded["unknown_date"] += 1
            elif day > as_of:
                excluded["future_date"] += 1
            elif day < as_of - timedelta(days=days-1):
                excluded["outside_window"] += 1
            else:
                usable_dates.add(day)
        if not usable_dates:
            continue
        raw_count += 1
        records = extraction_by_id.get(rid, [])
        status = _extraction_status(review, records)
        if status != "complete":
            excluded[status] += 1
            continue
        if not _issue_snapshot_matches(records, problem_rows.get(rid, [])):
            excluded["snapshot_mismatch"] += 1
            continue
        # Real extraction snapshots echo the initial body. A separate append
        # must also be represented in that snapshot; status-only fixtures carry
        # an explicit metadata-only provenance limitation below.
        if appended in usable_dates and appended != _day(review.get("date")):
            echo_records = [r for r in records if "content" in r or "text" in r]
            if echo_records:
                append_text = _compact(review.get("append_content"))
                covered = any(append_text and append_text in _compact(r.get("content") or r.get("text")) for r in echo_records)
                covered = covered or any(_compact(i.get("evidence")) and _compact(i.get("evidence")) in append_text
                    for r in records for i in r.get("issues") or [] if isinstance(i, dict))
                if not covered:
                    usable_dates.discard(appended)
                    excluded["append_not_in_extraction_snapshot"] += 1
        if not usable_dates:
            continue
        complete_count += 1
        metadata_only += not any("content" in r or "text" in r for r in records)
        cohort = cohorts.setdefault(sku_key, {"sku_label": sku_label, "events": []})
        cohort["sku_label"] = min(cohort["sku_label"], sku_label,
            key=lambda label: (label == sku_key.split(":", 1)[-1], label))
        for day in usable_dates:
            cohort["events"].append({"review_id": rid, "event_key": (rid, day.isoformat()),
                "content": _s(review.get("text") or review.get("content")) + " 追评：" + _s(review.get("append_content")),
                "source_key": _source(review), "review_link": _s(review.get("parent_review_id") or review.get("original_review_id")) or rid,
                "identity_known": any(_s(review.get(k)) for k in ("reviewer_id", "buyer_id", "user_id"))})
    for cohort in cohorts.values():
        cohort["units"] = _cohort_units(cohort.pop("events"))
    return {"cohorts": cohorts, "excluded": dict(excluded), "candidate_review_count": raw_count,
            "completed_review_count": complete_count, "metadata_only_review_count": metadata_only,
            "extraction_provided": extraction is not None}


def _probability_for_problem(rows, assessment, context):
    positive_events, historical_support = {}, Counter()
    for row in rows:
        if row["specific"] and row["sku_key"]:
            historical_support[row["sku_key"]] += 1
            day = _day(row["date"])
            if day:
                positive_events.setdefault(row["sku_key"], set()).add((row["review_id"], day.isoformat()))
    scope = "在已完成问题抽取的同规格可分析低星反馈中"
    base = {"target": "next_comparable_low_star_review_mentions_problem", "label": "同类问题复现概率",
            "scope_label": scope, "purchase_probability": None, "sku_label": "", "sku_key": "",
            "per_sku": [], "small_sample": True, "tone": "none",
            "coverage": {key: value for key, value in context.items() if key != "cohorts"}}
    if not context["extraction_provided"] or not historical_support:
        return {**beta_estimate(0, 0), **base, "reason": "缺少逐评论抽取完成状态，不能以未抽取当作未提及。" if not context["extraction_provided"] else "本问题缺少可核对的具体证据或已知规格，暂不估计概率。"}
    per_sku = []
    for sku_key in sorted(set(context["cohorts"]) | set(historical_support)):
        cohort = context["cohorts"].get(sku_key, {"sku_label": sku_key.split(":", 1)[-1], "units": []})
        units = cohort["units"]
        events = positive_events.get(sku_key, set())
        positives = [u for u in units if u["event_keys"] & events]
        result = beta_estimate(len(positives), len(units))
        point = result["estimate"]
        small = len(units) < 5
        tone = "none" if point is None else "low" if point < .3 else "high" if point >= .6 else "medium"
        result.update(sku_key=sku_key, sku_label=cohort["sku_label"], small_sample=small, tone=tone,
            review_ids=sorted({rid for unit in units for rid in unit["review_ids"]}),
            positive_review_ids=sorted({event[0] for unit in positives for event in unit["event_keys"] & events}),
            identified_source_count=len(units) if units and all(u["identity_known"] for u in units) else None)
        per_sku.append(result)
    # Preserve the support-based scope selection. Do not cherry-pick a tiny SKU
    # because its posterior percentage happens to be the largest.
    support = assessment["sku_counts"] or historical_support
    chosen_key = min(support, key=lambda key: (-support[key], -next((p["total"] for p in per_sku if p["sku_key"] == key), 0), key))
    chosen = next(p for p in per_sku if p["sku_key"] == chosen_key)
    result = {**chosen, **base, "sku_key": chosen_key, "sku_label": chosen["sku_label"], "per_sku": per_sku,
              "small_sample": chosen["small_sample"], "tone": chosen["tone"]}
    if assessment["attention_level"] == "safety":
        result["display_tone_basis"] = "safety_warning_separate_from_probability_color"
    result["reason"] = (f"所选规格{chosen['sku_label']}：{chosen['successes']}个反馈单元提及本问题 / {chosen['total']}个完成抽取的可比低星反馈单元。"
                        "按支持条数选择规格，不合并不同规格；采用Jeffreys先验平滑估计。" if chosen["total"] else
                        "本问题最受支持的规格在当前窗口缺少完成抽取的可比低星分母，暂不估计概率。")
    result["assumptions"] = [*chosen["assumptions"], "来源和长文案去重后仍假设反馈单元独立；脱敏昵称不能验证这一假设，未作任意权重修正。",
        "低星中未识别到本问题仅代表本次抽取未识别，不证明购买者没有该问题。"]
    if context["metadata_only_review_count"]:
        result["assumptions"].append("部分完成状态未附正文回显，依据所提供的评论标识和状态关联，无法额外核对抽取快照正文。")
    result["assumptions"].append("若完成记录未提供具体问题清单，仅按评论标识关联现有问题；显式清单与问题证据冲突的记录已排除。")
    return result


def _dedupe(rows):
    units, reasons = [], Counter()
    for row in sorted(rows, key=lambda r: (_s(r.get("date")), r["review_id"])):
        duplicate = None
        for unit in units:
            if row["source_key"] == unit["source_key"]:
                duplicate = "same_source_or_review"
            else:
                a, b = _compact(row["content"]), _compact(unit["content"])
                if len(a) >= POLICY["near_copy_min_chars"] and len(b) >= POLICY["near_copy_min_chars"]:
                    if a == b or (min(len(a), len(b)) / max(len(a), len(b)) >= .9 and
                                  SequenceMatcher(None, a, b, autojunk=False).ratio() >= POLICY["near_copy_similarity"]):
                        duplicate = "same_or_near_copy_long_text"
            if duplicate:
                reasons[duplicate] += 1
                break
        if not duplicate:
            units.append(row)
    return units, dict(reasons)


def _assessment(rows, as_of, days, probability_context=None):
    ids = sorted({r["review_id"] for r in rows})
    recent, unknown, future, old = [], 0, 0, 0
    for row in rows:
        day = _day(row.get("date"))
        if not day or not as_of:
            unknown += 1
        elif day > as_of:
            future += 1
        elif day < as_of - timedelta(days=days-1):
            old += 1
        else:
            recent.append(row)
    units, duplicate_reasons = _dedupe([r for r in recent if r["specific"]])
    counts = Counter(r["sku_key"] for r in units if r["sku_key"])
    max_sku = max(counts.values(), default=0)
    count = len(units)
    identity_known = all(r["identity_known"] for r in units) and bool(units)
    source_count = len({r["source_key"] for r in units}) if identity_known else None
    safety = any(r["specific"] and r["severity"] == "critical" for r in recent)
    if safety:
        level, tone, label = "safety", "high", "存在安全相关线索，请优先核查"
    elif max_sku >= POLICY["repeat_threshold"]:
        level, tone, label = "repeat", "high" if identity_known else "medium", "近期同一规格同一问题反复出现"
    elif count >= POLICY["repeat_threshold"] and len(counts) > 1:
        level, tone, label = "cross_sku", "medium", "存在跨规格重复线索，单一规格未达四条"
    elif max_sku >= 2:
        level, tone, label = "repeated", "medium", "近期有多条同一具体问题反馈"
    elif count >= 2 and len(counts) > 1:
        level, tone, label = "repeated", "medium", "多个规格出现同一具体问题反馈"
    elif count and not max_sku:
        level, tone, label = "incomplete", "medium", "存在近期问题反馈，规格待核实"
    elif count:
        level, tone, label = "single", "low", "近期有具体问题反馈，需结合原文"
    elif recent or unknown or future:
        level, tone, label = "incomplete", "none", "证据条件不完整，暂不能判断近期重复"
    elif old:
        level, tone, label = "historical", "none", "仅有窗口外反馈，保留历史线索"
    else:
        level, tone, label = "none", "none", "暂无可核验的具体问题证据"
    limitations = []
    if source_count is None:
        limitations.append("只有评论标识或脱敏昵称，是否来自不同买家无法核实；去重反馈不等于独立买家。")
    else:
        limitations.append("按已提供的稳定来源标识去重；不同平台账号不保证是真实独立购买者。")
    missing_sku = sum(not r["sku_key"] for r in recent if r["specific"])
    if missing_sku:
        limitations.append(f"{missing_sku}条近期具体反馈缺少规格，不计入已知同一规格条数。")
    if unknown or future:
        limitations.append(f"日期缺失或截止日未知{unknown}条、未来日期{future}条，均未计入确定的近期条数。")
    if duplicate_reasons:
        limitations.append("相同来源或相同/近似长文案已合并计数；不据此判断评论造假。")
    if any(not r["specific"] for r in rows):
        limitations.append("笼统描述、推断或未核对上原文的引用保留供查看，未计入具体重复反馈。")
    severe = max((r["severity"] for r in recent if r["specific"]), key=lambda s: {"unknown": 0, "minor": 1, "moderate": 2, "major": 3, "critical": 4}.get(s, 0), default="unknown")
    result = {"attention_level": level, "tone": tone, "attention_label": label,
            "repeat_count": count, "max_same_sku_count": max_sku, "recent_review_count": len({r['review_id'] for r in recent}),
            "review_count": len(ids), "specific_count": count, "identified_source_count": source_count,
            "identity_label": "按稳定来源标识去重" if source_count is not None else "去重反馈，是否不同买家无法核实",
            "window_label": f"截至{as_of.isoformat()}的近{days}天" if as_of else f"近{days}天（截止日未知）",
            "impact_label": risk_model.SEVERITY_LABELS.get(severe, "未分级"), "severity": severe,
            "reason": f"近期具体去重反馈{count}条；已知同一规格最多{max_sku}条。" + label + "。",
            "limitations": limitations, "review_ids": ids, "recent_review_ids": sorted({r['review_id'] for r in units}),
            "rule_progress": min(1.0, max_sku / POLICY['repeat_threshold']),
            "probability": None, "purchase_probability": None, "calibrated": False, "sku_counts": dict(counts),
            "excluded": {"unknown_date": unknown, "future_date": future, "outside_window": old,
                         "unknown_sku": missing_sku, "duplicates": duplicate_reasons}}
    context = probability_context or {"cohorts": {}, "excluded": {}, "extraction_provided": False,
        "candidate_review_count": 0, "completed_review_count": 0, "metadata_only_review_count": 0}
    result["probability_estimate"] = _probability_for_problem(rows, result, context)
    result["probability"] = result["probability_estimate"]["estimate"]
    return result


def build(reviews, clusters, issues=None, *, product_id="", claim_aspects=(), collection=None, summary=None, extraction=None):
    """Keep all evidence IDs while counting only attributable, concrete repeats."""
    collection, summary = collection or {}, summary or {}
    as_of, as_of_source = None, "unknown"
    for value, source in ((collection.get("finished_at"), "collection.finished_at"),
                          (summary.get("created_at"), "summary.created_at")):
        if _day(value):
            as_of, as_of_source = _day(value), source
            break
    if as_of is None:
        dates = [_day(r.get(field)) for r in reviews for field in ("date", "append_date") if _day(r.get(field))
                 and r.get("valid_for_analysis") is not False
                 and (not product_id or not _s(r.get("product_id")) or _s(r.get("product_id")) == _s(product_id))]
        if dates:
            as_of, as_of_source = max(dates), "latest_review_date_fallback"
    index = {_s(r.get("review_id")): r for r in reviews}
    groups, rejected = {}, Counter()
    for issue in _records(clusters, issues):
        rid = _s(issue.get("review_id"))
        review = index.get(rid)
        if not review or review.get("valid_for_analysis") is False:
            rejected["missing_or_ineligible_review"] += 1
            continue
        if product_id and any(_s(row.get("product_id")) and _s(row.get("product_id")) != _s(product_id) for row in (review, issue)):
            rejected["wrong_product"] += 1
            continue
        quote = _s(issue.get("evidence"))
        initial = _s(review.get("content") or review.get("text"))
        append = _s(review.get("append_content"))
        in_initial = bool(_compact(quote) and _compact(quote) in _compact(initial))
        in_append = bool(_compact(quote) and _compact(quote) in _compact(append))
        signature, label = _signature(quote)
        specific = bool(signature and (in_initial or in_append) and issue.get("basis") != "inferred")
        aspect = _s(issue.get("aspect")) or "其他"
        obj = _s(issue.get("object")) or "整体"
        key = (aspect, obj, signature or "unverified:" + _compact(issue.get("manifestation") or quote or "未核对具体问题"))
        group = groups.setdefault(key, {"name": obj + (label or _s(issue.get("manifestation")) or "待核对问题"),
                    "aspect": aspect, "object": obj, "signature": signature, "rows": [], "cluster_ids": set()})
        group["cluster_ids"].update(filter(None, issue.get("cluster_ids") or []))
        event_date = review.get("date") if in_initial else review.get("append_date") if in_append else None
        if any(r["review_id"] == rid and r["date"] == event_date and r["specific"] == specific and r["quote"] == quote for r in group["rows"]):
            continue
        sku_key, _ = _sku(review)
        severity = risk_model.severity_of(quote) if specific else "unknown"
        if severity == "critical" and signature not in {"allergy", "irritation", "skin_worse", "acne", "electric_leak", "fire", "injury"}:
            severity = "unknown"
        group["rows"].append({"review_id": rid, "date": event_date,
             "content": initial + (" 追评：" + append if append else ""), "quote": quote, "specific": specific,
             "severity": severity,
             "sku_key": sku_key,
             "source_key": _source(review), "identity_known": any(_s(review.get(k)) for k in ("reviewer_id", "buyer_id", "user_id"))})
    windows = {}
    for days in POLICY["window_options"]:
        probability_context = _probability_cohorts(index.values(), groups, extraction, as_of, days, product_id)
        problems = []
        for key, group in groups.items():
            assessment = _assessment(group["rows"], as_of, days, probability_context)
            problems.append({**assessment, "problem_id": "P" + hashlib.sha256(repr(key).encode()).hexdigest()[:12],
                "name": group["name"], "aspect": group["aspect"], "object": group["object"],
                "scope_label": "同一具体问题；规格分开计数", "cluster_ids": sorted(group["cluster_ids"]),
                "unadvertised": group["aspect"] not in set(claim_aspects),
                "evidence": [{"review_id": r["review_id"], "quote": r["quote"], "specific": r["specific"], "date": _s(r["date"])} for r in group["rows"]]})
        problems.sort(key=lambda p: (-_RANK[p["attention_level"]], -p["max_same_sku_count"], -p["repeat_count"], p["problem_id"]))
        empty = _assessment([], as_of, days, probability_context)
        windows[str(days)] = {"overall": dict(problems[0]) if problems else dict(empty),
                             "unadvertised": dict(next((p for p in problems if p["unadvertised"]), empty)), "problems": problems}
    return {"version": VERSION, "probability_method_version": PROBABILITY_VERSION, "as_of": as_of.isoformat() if as_of else None, "as_of_source": as_of_source,
            "default_window_days": POLICY["default_window_days"], "window_options": POLICY["window_options"],
            "policy": dict(POLICY), "windows": windows, "limitations": [NOTE,
            "仅分析已采集并完成问题抽取的低星正文，可能遗漏其他评论及问题；未触发提醒不代表无风险。",
            "未对应宣传仅表示未找到同方面有效主张，不推断商家隐瞒、刷评或删评。"],
            "sources": SOURCES, "rejected": dict(rejected)}
