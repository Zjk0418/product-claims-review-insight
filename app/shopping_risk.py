"""评论侧购物提醒，与宣传是否作出承诺无关。

只聚合已通过抽取校验的问题簇和负面证据，不用关键词重新给评论分类。
提醒级别表示当前样本中反馈的重复程度，不能解释为购买发生概率。
"""
from __future__ import annotations

import risk_model


VERSION = "shopping-evidence-v1"
POLICY = {
    "repeated_min_reviews": 2,
    "concentrated_min_reviews": 3,
    "concentrated_min_effective_share": 0.2,
}
NOTE = ("占扫描样本 = 该方面负面去重评论数 / 扫描评论数；"
        "占可分析低星 = 该方面负面去重评论数 / 可分析低星正文数。"
        "这是本次样本的反馈占比，未经随机抽样与概率校准，不能当作购买后出问题的概率。"
        "无对应宣传承诺也会展示购物提醒；不同方面可能引用同一条评论。")


def _rate(count: int, total: int) -> float | None:
    return round(count / total, 6) if total > 0 else None


def assess_aspect(*, problems: list[dict], negative_ids: set[str], reviews: dict[str, dict],
                  scanned_count: int, low_star_count: int, effective_count: int) -> dict:
    """仅接受经过上游校验的负面 ID；逐方面去重，保留全部可展开依据。"""
    ids = sorted(rid for rid in negative_ids if rid in reviews and reviews[rid].get("valid_for_analysis"))
    count = len(ids)
    effective_share = _rate(count, effective_count)
    # 问题的严重性只看问题簇描述，避免同一评论的其他问题污染本方面。
    safety = any(risk_model.severity_of(f"{p.get('name', '')} {p.get('description', '')}") == "critical"
                 for p in problems if set(p.get("review_ids") or []) & set(ids))
    if safety and count:
        level, label = "safety", "安全相关线索"
    elif count >= POLICY["concentrated_min_reviews"] and (effective_share or 0) >= POLICY["concentrated_min_effective_share"]:
        level, label = "concentrated", "重点关注"
    elif count >= POLICY["repeated_min_reviews"]:
        level, label = "repeated", "多条负面反馈"
    elif count:
        level, label = "single", "个别负面反馈"
    else:
        level, label = "none", "暂无已归类负面证据"
    return {
        "version": VERSION, "negative_count": count,
        "scanned_rate": _rate(count, scanned_count),
        "low_star_share": _rate(count, low_star_count),
        "effective_share": effective_share,
        "attention_level": level, "attention_label": label,
        "review_ids": ids,
        "problems": [{"name": p.get("name", ""), "description": p.get("description", ""),
                      "review_ids": sorted(set(p.get("review_ids") or []) & set(ids))}
                     for p in problems if set(p.get("review_ids") or []) & set(ids)],
        "evidence": [{key: reviews[rid].get(key) for key in ("review_id", "content", "star", "date", "sku")}
                     for rid in ids],
        "note": NOTE,
    }


def summarize(aspects: list[dict], effective_count: int) -> dict:
    rows = [a for a in aspects if (a.get("shopping") or {}).get("negative_count")]
    ids = {rid for a in rows for rid in a["shopping"]["review_ids"]}
    repeated = [a for a in rows if a["shopping"]["negative_count"] >= POLICY["repeated_min_reviews"]]
    safety = [a["aspect"] for a in rows if a["shopping"]["attention_level"] == "safety"]
    label = ("存在安全相关线索，请核查" if safety else
             "存在多条负面反馈，建议重点核查" if repeated else
             "已发现负面反馈，建议核查" if rows else
             "未检出已归类负面问题，请核查低星正文" if effective_count else
             "当前样本不足以判断购物风险")
    return {"version": VERSION, "negative_review_count": len(ids), "aspect_count": len(rows),
            "repeated_aspect_count": len(repeated), "safety_aspects": safety,
            "attention_label": label, "unclassified_effective_count": max(0, effective_count - len(ids)),
            "review_ids": sorted(ids), "note": NOTE, "policy": POLICY.copy(),
            "probability_calibrated": False}
