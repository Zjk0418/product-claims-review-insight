"""共性问题统计。

所有占比都以“本次进入分析的有效低星评论数”为分母，并显式写入输出。
同一评论在同一个问题簇中只计一次；一条评论可以出现在多个问题簇中。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .issue_schema import Issue

MIN_SKU_SAMPLE = 3


def _month(date: str) -> str:
    return date[:7] if date else ""


def build_statistics(
    reviews: list[dict[str, Any]],
    quality: dict[str, Any],
    issues: list[Issue],
    clusters_payload: dict[str, Any],
) -> dict[str, Any]:
    valid_reviews = [review for review in reviews if review.get("valid_for_analysis")]
    valid_ids = {review["review_id"] for review in valid_reviews}
    review_by_id = {review["review_id"]: review for review in valid_reviews}

    issues = [issue for issue in issues if issue.review_id in valid_ids]
    issues_by_review: dict[str, list[Issue]] = defaultdict(list)
    for issue in issues:
        issues_by_review[issue.review_id].append(issue)

    reviews_with_issues = len(issues_by_review)
    valid_count = len(valid_reviews)

    star_stats: dict[str, dict[str, int]] = {}
    for star in range(1, 6):
        subset = [review for review in valid_reviews if review.get("star") == star]
        with_issues = sum(1 for review in subset if review["review_id"] in issues_by_review)
        issue_count = sum(len(issues_by_review.get(review["review_id"], [])) for review in subset)
        if subset:
            star_stats[str(star)] = {
                "review_count": len(subset),
                "reviews_with_issues": with_issues,
                "issue_count": issue_count,
            }

    size_stats: dict[str, dict[str, Any]] = {}
    for size in sorted({review.get("sku_size") for review in valid_reviews if review.get("sku_size")}):
        subset = [review for review in valid_reviews if review.get("sku_size") == size]
        with_issues = [review for review in subset if review["review_id"] in issues_by_review]
        size_stats[str(size)] = {
            "review_count": len(subset),
            "reviews_with_issues": len(with_issues),
            "issue_count": sum(len(issues_by_review.get(review["review_id"], [])) for review in subset),
            "insufficient_sample": len(subset) < MIN_SKU_SAMPLE,
        }

    month_stats: dict[str, dict[str, int]] = {}
    for review in valid_reviews:
        month = _month(review.get("date", ""))
        if not month:
            continue
        month_stats.setdefault(month, {"review_count": 0, "reviews_with_issues": 0})
        month_stats[month]["review_count"] += 1
        if review["review_id"] in issues_by_review:
            month_stats[month]["reviews_with_issues"] += 1
    month_stats = dict(sorted(month_stats.items()))

    enriched_clusters: list[dict[str, Any]] = []
    for cluster in clusters_payload.get("clusters", []):
        review_ids = [rid for rid in cluster.get("review_ids", []) if rid in valid_ids]
        review_subset = [review_by_id[rid] for rid in review_ids if rid in review_by_id]
        stars = Counter(review.get("star") for review in review_subset if review.get("star"))
        sizes = Counter(review.get("sku_size") for review in review_subset if review.get("sku_size"))
        sku_detail = []
        for sku_id, count in list(cluster.get("sku_distribution", {}).items())[:10]:
            sku_reviews = [review for review in review_subset if review.get("sku_id") == sku_id]
            sku_detail.append(
                {
                    "sku_id": sku_id,
                    "sku": sku_reviews[0].get("sku", "") if sku_reviews else "",
                    "count": count,
                    "insufficient_sample": count < MIN_SKU_SAMPLE,
                }
            )
        item = dict(cluster)
        item["review_ids"] = review_ids
        item["review_count"] = len(review_ids)
        item["share_of_valid_reviews"] = (
            round(len(review_ids) / valid_count, 4) if valid_count else 0.0
        )
        item["share_of_reviews_with_issues"] = (
            round(len(review_ids) / reviews_with_issues, 4) if reviews_with_issues else 0.0
        )
        item["star_distribution"] = {str(key): value for key, value in sorted(stars.items())}
        item["sku_size_distribution"] = dict(sizes.most_common())
        item["sku_detail"] = sku_detail
        item["high_severity_review_count"] = len(
            {
                issue.review_id
                for issue in issues
                if issue.issue_id in set(cluster.get("member_issue_ids", []))
                and issue.severity == "high"
            }
        )
        enriched_clusters.append(item)

    enriched_clusters.sort(
        key=lambda item: (item["review_count"], item.get("issue_count", 0)), reverse=True
    )
    for rank, item in enumerate(enriched_clusters, 1):
        item["frequency_rank"] = rank

    top_problems = [
        {
            "rank": item["frequency_rank"],
            "cluster_id": item["cluster_id"],
            "problem_name": item["problem_name"],
            "aspect": item["aspect"],
            "review_count": item["review_count"],
            "share_of_valid_reviews": item["share_of_valid_reviews"],
            "high_severity_review_count": item["high_severity_review_count"],
            "mean_confidence": item.get("mean_confidence"),
        }
        for item in enriched_clusters[:10]
    ]

    denominators = {
        **quality.get("denominators", {}),
        "valid_for_analysis": valid_count,
        "reviews_with_issues": reviews_with_issues,
        "issue_count": len(issues),
        "cluster_count": len(enriched_clusters),
    }

    limitations = [
        "样本仅包含本次采集到的 ≤3 星评价，不代表全体买家，也不能推断商品真实不良率。",
        "占比分母是“本次进入分析的有效低星评论数”，不是平台评价总量。",
        "同一评论可归入多个问题簇，因此各问题占比相加可能超过 100%。",
        "LLM 抽取可能存在漏提或误提；已用原文证据校验，但仍建议人工抽查。",
        "SKU 子样本普遍较小，单个 SKU 的问题分布只能作为线索，不能作为质量结论。",
        "未纳入 4–5 星但正文负面的评价，这类问题需要后续补充分析。",
    ]
    if any(item.get("sku_detail") for item in enriched_clusters):
        limitations.append(f"SKU 计数小于 {MIN_SKU_SAMPLE} 时标记为 insufficient_sample。")

    return {
        "denominators": denominators,
        "coverage": {
            "valid_reviews": valid_count,
            "reviews_with_issues": reviews_with_issues,
            "reviews_without_issues": valid_count - reviews_with_issues,
            "issue_count": len(issues),
            "avg_issues_per_review_with_issues": round(
                len(issues) / reviews_with_issues, 2
            )
            if reviews_with_issues
            else 0.0,
        },
        "star_breakdown": star_stats,
        "sku_size_breakdown": size_stats,
        "month_breakdown": month_stats,
        "clusters": enriched_clusters,
        "top_problems": top_problems,
        "limitations": limitations,
    }
