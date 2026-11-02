"""LLM 抽取与聚类的人工评测。

匹配标准（写在文档里，避免同义改写造成不合理误判）：
- 同一 review_id；
- 对象规范化后一致；
- 方面一致；
- 方向不冲突（偏大 vs 偏小、过硬 vs 过软判为不匹配）；
- 证据片段有实质重叠（去标点后包含，或字符二元组 Jaccard ≥ 0.5）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from analysis.categories import CategoryProfile, get_profile  # noqa: E402
from analysis.issue_schema import (  # noqa: E402
    direction_polarity,
    make_issue_id,
    normalize_aspect,
    normalize_direction,
    object_key,
)
from analysis.normalization import normalize_issue_labels  # noqa: E402

PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)

MATCH_MODES = ("raw", "normalized", "semantic")


def _norm(text: str) -> str:
    return PUNCT_RE.sub("", text or "")


def _bigrams(text: str) -> set[str]:
    value = _norm(text)
    return {value[i : i + 2] for i in range(len(value) - 1)} if len(value) > 1 else {value}


def _jaccard(a: str, b: str) -> float:
    left, right = _bigrams(a), _bigrams(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def direction_conflict(left: str, right: str) -> bool:
    a = direction_polarity(left)
    b = direction_polarity(right)
    return bool(a and b and a[0] == b[0] and a[1] != b[1])


def _raw_object(issue: dict[str, Any]) -> str:
    return _norm(str(issue.get("object_raw") or issue.get("object") or ""))


def _raw_aspect(issue: dict[str, Any]) -> str:
    return _norm(str(issue.get("aspect_raw") or issue.get("aspect") or ""))


def _raw_direction(issue: dict[str, Any]) -> str:
    value = str(issue.get("direction_raw") or issue.get("direction") or "")
    return normalize_direction(value)[0]


def match_score(
    pred: dict[str, Any],
    gold: dict[str, Any],
    mode: str = "normalized",
    profile: CategoryProfile | None = None,
) -> float:
    """按指定口径计算匹配分；0 表示不匹配。

    - raw：对象/方面按原始字面严格比较（不做同义词归并）；
    - normalized：对象/方面按 normalization 模块归一后比较（默认，线上口径）；
    - semantic：只看证据片段与方向，不看对象/方面（用于量化标签口径分歧）。
    """
    if mode not in MATCH_MODES:
        raise ValueError(f"未知匹配口径：{mode}")
    profile = profile or get_profile("generic")

    if mode == "raw":
        pred_object, gold_object = _raw_object(pred), _raw_object(gold)
        pred_aspect, gold_aspect = _raw_aspect(pred), _raw_aspect(gold)
        pred_direction, gold_direction = _raw_direction(pred), _raw_direction(gold)
    else:
        pred_norm = normalize_issue_labels(pred, profile)
        gold_norm = normalize_issue_labels(gold, profile)
        pred_object, gold_object = pred_norm["object"], gold_norm["object"]
        pred_aspect, gold_aspect = pred_norm["aspect"], gold_norm["aspect"]
        pred_direction, gold_direction = pred_norm["direction"], gold_norm["direction"]

    if mode != "semantic":
        if pred_object != gold_object:
            # 允许“整体”与具体部件之间的宽泛匹配（沿用既有规则，两种口径一致）
            if "整体" not in {pred_object, gold_object}:
                return 0.0
        if pred_aspect != gold_aspect:
            return 0.0
    if direction_conflict(pred_direction, gold_direction):
        return 0.0

    evidence_score = _evidence_score(pred, gold)
    if evidence_score < 0.5:
        return 0.0

    score = 1.0 + evidence_score
    if pred_object == gold_object:
        score += 1.0
    if mode == "semantic" and pred_aspect == gold_aspect:
        score += 0.5
    return score


def _evidence_score(pred: dict[str, Any], gold: dict[str, Any]) -> float:
    pred_evidence = pred.get("evidence", "")
    gold_evidence = gold.get("evidence", "")
    if not (_norm(pred_evidence) and _norm(gold_evidence)):
        return 0.0
    if _norm(pred_evidence) in _norm(gold_evidence) or _norm(gold_evidence) in _norm(pred_evidence):
        return 1.0
    return _jaccard(pred_evidence, gold_evidence)


def semantic_match_score(
    pred: dict[str, Any],
    gold: dict[str, Any],
    profile: CategoryProfile | None = None,
) -> float:
    """不要求 aspect 相同，用于单独衡量“方面判断”是否正确。"""
    profile = profile or get_profile("generic")
    pred_norm = normalize_issue_labels(pred, profile)
    gold_norm = normalize_issue_labels(gold, profile)
    pred_object, gold_object = pred_norm["object"], gold_norm["object"]
    if pred_object != gold_object:
        if "整体" not in {pred_object, gold_object}:
            return 0.0
    if direction_conflict(pred_norm["direction"], gold_norm["direction"]):
        return 0.0
    evidence_score = _evidence_score(pred, gold)
    if evidence_score < 0.5:
        return 0.0
    score = 1.0 + evidence_score
    if pred_object == gold_object:
        score += 1.0
    if pred_norm["aspect"] == gold_norm["aspect"]:
        score += 0.5
    return score


def evaluate_reviews(
    predictions: dict[str, list[dict[str, Any]]],
    gold: dict[str, list[dict[str, Any]]],
    only_predicted: bool = False,
    mode: str = "normalized",
    profile_resolver: Any = None,
) -> dict[str, Any]:
    """only_predicted=True 时只评预测覆盖到的评论（用于分阶段小样本测试）。

    mode 见 MATCH_MODES；profile_resolver(review_id) -> CategoryProfile，
    不传时统一用 generic 词表（独立集需按品类传入）。
    """
    tp = fp = fn = 0
    matched_rows: list[dict[str, Any]] = []
    false_positives: list[dict[str, Any]] = []
    false_negatives: list[dict[str, Any]] = []

    def profile_for(review_id: str) -> CategoryProfile:
        if callable(profile_resolver):
            return profile_resolver(review_id)
        return get_profile("generic")

    review_ids = set(predictions) if only_predicted else (set(predictions) | set(gold))
    for review_id in sorted(review_ids):
        profile = profile_for(review_id)
        preds = list(predictions.get(review_id, []))
        golds = list(gold.get(review_id, []))
        used_gold: set[int] = set()
        used_pred: set[int] = set()
        candidates = []
        for p_index, pred in enumerate(preds):
            for g_index, gold_issue in enumerate(golds):
                score = match_score(pred, gold_issue, mode=mode, profile=profile)
                if score > 0:
                    candidates.append((score, p_index, g_index))
        for score, p_index, g_index in sorted(candidates, reverse=True):
            if p_index in used_pred or g_index in used_gold:
                continue
            used_pred.add(p_index)
            used_gold.add(g_index)
            pred = preds[p_index]
            gold_issue = golds[g_index]
            pred_norm = normalize_issue_labels(pred, profile)
            gold_norm = normalize_issue_labels(gold_issue, profile)
            matched_rows.append(
                {
                    "review_id": review_id,
                    "predicted": pred,
                    "gold": gold_issue,
                    "match_score": round(score, 2),
                    "aspect_ok": pred_norm["aspect"] == gold_norm["aspect"],
                    "object_ok": pred_norm["object"] == gold_norm["object"],
                    "taxonomy_ok": (
                        pred_norm["aspect"] == gold_norm["aspect"]
                        and pred_norm["object"] == gold_norm["object"]
                    ),
                    "pred_normalized": pred_norm,
                    "gold_normalized": gold_norm,
                }
            )
        tp += len(used_pred)
        for index, pred in enumerate(preds):
            if index not in used_pred:
                fp += 1
                false_positives.append({"review_id": review_id, **pred})
        for index, gold_issue in enumerate(golds):
            if index not in used_gold:
                fn += 1
                false_negatives.append({"review_id": review_id, **gold_issue})

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    # 方面准确率：先用“对象 + 证据 + 方向”做语义匹配，不要求方面相同
    semantic_pairs = 0
    semantic_aspect_hits = 0
    for review_id in sorted(review_ids):
        profile = profile_for(review_id)
        preds = list(predictions.get(review_id, []))
        golds = list(gold.get(review_id, []))
        used_pred: set[int] = set()
        used_gold: set[int] = set()
        candidates = []
        for p_index, pred in enumerate(preds):
            for g_index, gold_issue in enumerate(golds):
                score = semantic_match_score(pred, gold_issue, profile)
                if score > 0:
                    candidates.append((score, p_index, g_index))
        for _, p_index, g_index in sorted(candidates, reverse=True):
            if p_index in used_pred or g_index in used_gold:
                continue
            used_pred.add(p_index)
            used_gold.add(g_index)
            semantic_pairs += 1
            pred_norm = normalize_issue_labels(preds[p_index], profile)
            gold_norm = normalize_issue_labels(golds[g_index], profile)
            if pred_norm["aspect"] == gold_norm["aspect"]:
                semantic_aspect_hits += 1

    exact_count = within_one = over_split = under_split = 0
    for review_id in review_ids:
        pred_n = len(predictions.get(review_id, []))
        gold_n = len(gold.get(review_id, []))
        if pred_n == gold_n:
            exact_count += 1
        if abs(pred_n - gold_n) <= 1:
            within_one += 1
        if pred_n > gold_n:
            over_split += 1
        elif pred_n < gold_n:
            under_split += 1

    all_pred = [issue for issues in predictions.values() for issue in issues]
    evidence_valid = sum(
        1 for issue in all_pred if issue.get("evidence_check") in {"exact", "normalized"}
    )

    return {
        "mode": mode,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "aspect_accuracy_on_matched": round(
            sum(1 for row in matched_rows if row["aspect_ok"]) / len(matched_rows), 4
        )
        if matched_rows
        else 0.0,
        "object_accuracy_on_matched": round(
            sum(1 for row in matched_rows if row["object_ok"]) / len(matched_rows), 4
        )
        if matched_rows
        else 0.0,
        "taxonomy_agreement_on_matched": round(
            sum(1 for row in matched_rows if row["taxonomy_ok"]) / len(matched_rows), 4
        )
        if matched_rows
        else 0.0,
        "aspect_accuracy_on_semantic_match": round(semantic_aspect_hits / semantic_pairs, 4)
        if semantic_pairs
        else 0.0,
        "semantic_match_count": semantic_pairs,
        "review_count_evaluated": len(review_ids),
        "exact_issue_count_match_reviews": exact_count,
        "within_one_issue_count_reviews": within_one,
        "over_split_reviews": over_split,
        "under_split_reviews": under_split,
        "evidence_issue_count": len(all_pred),
        "evidence_valid_rate": round(evidence_valid / len(all_pred), 4) if all_pred else 0.0,
        "matched": len(matched_rows),
        "false_positives": false_positives[:100],
        "false_negatives": false_negatives[:100],
    }


def evaluate_all_modes(
    predictions: dict[str, list[dict[str, Any]]],
    gold: dict[str, list[dict[str, Any]]],
    profile_resolver: Any = None,
) -> dict[str, Any]:
    """一次跑三种口径，返回 {"raw": {...}, "normalized": {...}, "semantic": {...}}。

    三份结果除 mode 外结构一致，便于直接对比；不修改任何标注。
    """
    results: dict[str, Any] = {}
    for mode in MATCH_MODES:
        result = evaluate_reviews(
            predictions, gold, mode=mode, profile_resolver=profile_resolver
        )
        results[mode] = result
    baseline = results["raw"]
    normalized = results["normalized"]
    semantic = results["semantic"]
    results["deltas"] = {
        "normalized_minus_raw": {
            "precision": round(normalized["precision"] - baseline["precision"], 4),
            "recall": round(normalized["recall"] - baseline["recall"], 4),
            "f1": round(normalized["f1"] - baseline["f1"], 4),
        },
        "semantic_minus_normalized": {
            "precision": round(semantic["precision"] - normalized["precision"], 4),
            "recall": round(semantic["recall"] - normalized["recall"], 4),
            "f1": round(semantic["f1"] - normalized["f1"], 4),
        },
        "semantic_taxonomy_agreement": semantic["taxonomy_agreement_on_matched"],
    }
    return results


def load_predictions(path: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    by_review: dict[str, list[dict[str, Any]]] = {}
    for record in payload.get("reviews") or []:
        review_id = str(record.get("review_id"))
        issues = []
        for raw in record.get("issues") or []:
            issue = dict(raw)
            if not issue.get("issue_id"):
                direction, _ = normalize_direction(issue.get("direction", ""))
                issue["issue_id"] = make_issue_id(
                    review_id,
                    object_key(issue.get("object", "")),
                    normalize_aspect(issue.get("aspect", "")),
                    issue.get("manifestation", ""),
                    direction,
                )
            issues.append(issue)
        by_review[review_id] = issues
    meta = {
        "model": payload.get("model", ""),
        "prompt_version": payload.get("prompt_version", ""),
        "processed_review_count": payload.get("processed_review_count", len(by_review)),
        "validation_failed_count": payload.get("validation_failed_count", 0),
        "usage": payload.get("usage", {}),
    }
    return by_review, meta


def load_gold(path: Path) -> dict[str, list[dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(record["review_id"]): list(record.get("issues") or [])
        for record in payload.get("reviews") or []
    }


def cluster_metrics(
    clusters_path: Path,
    matched_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = json.loads(clusters_path.read_text(encoding="utf-8"))
    assignment = payload.get("issue_assignments") or []
    issue_to_cluster = {row["issue_id"]: row["cluster_id"] for row in assignment}
    cluster_members: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cluster in payload.get("clusters") or []:
        cluster_members[cluster["cluster_id"]] = list(cluster.get("member_issue_ids") or [])

    conflicts = []
    for cluster_id, members in cluster_members.items():
        directions = []
        for row in payload.get("clusters") or []:
            if row["cluster_id"] == cluster_id:
                directions = list(row.get("direction_distribution", {}).keys())
                break
        if any(word in directions for word in ("偏大",)) and any(
            word in directions for word in ("偏小", "偏窄")
        ):
            conflicts.append({"cluster_id": cluster_id, "directions": directions})
        if "偏硬" in directions and "偏软" in directions:
            conflicts.append({"cluster_id": cluster_id, "directions": directions})

    result: dict[str, Any] = {
        "cluster_count": len(cluster_members),
        "opposite_direction_clusters": conflicts,
        "algorithm_metrics": payload.get("metrics", {}),
        "baseline": payload.get("baseline", {}),
    }

    # 用“匹配上的问题”做 ARI/NMI：聚类 vs 人工方面标签
    labels_true: list[str] = []
    labels_pred: list[str] = []
    for row in matched_rows:
        pred = row["predicted"]
        issue_id = pred.get("issue_id")
        cluster_id = issue_to_cluster.get(issue_id)
        if cluster_id:
            labels_true.append(normalize_aspect(row["gold"].get("aspect", "")))
            labels_pred.append(cluster_id)
    if len(labels_true) >= 3 and len(set(labels_pred)) >= 2:
        try:
            from sklearn.metrics import adjusted_rand_score, normalized_mutual_info_score

            result["ari_vs_gold_aspect"] = round(
                adjusted_rand_score(labels_true, labels_pred), 4
            )
            result["nmi_vs_gold_aspect"] = round(
                normalized_mutual_info_score(labels_true, labels_pred), 4
            )
            result["labeled_issue_count"] = len(labels_true)
        except Exception as exc:  # noqa: BLE001
            result["cluster_metric_error"] = f"{type(exc).__name__}: {exc}"
    else:
        result["ari_vs_gold_aspect"] = None
        result["nmi_vs_gold_aspect"] = None
        result["cluster_metric_note"] = "匹配上的问题不足或聚类数不足，未计算 ARI/NMI"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="评测 LLM 抽取与聚类结果")
    parser.add_argument("--pred", required=True)
    parser.add_argument("--gold", required=True)
    parser.add_argument("--clusters", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument(
        "--only-predicted",
        action="store_true",
        help="只评预测覆盖的评论（小样本测试用，避免未预测评论被算成漏检）",
    )
    parser.add_argument(
        "--mode",
        choices=[*MATCH_MODES, "all"],
        default="normalized",
        help="匹配口径：raw=原始字面 / normalized=规范化 / semantic=只看证据与方向 / all=三种都跑",
    )
    args = parser.parse_args(argv)

    predictions, meta = load_predictions(Path(args.pred))
    gold = load_gold(Path(args.gold))
    if args.mode == "all":
        modes = evaluate_all_modes(predictions, gold)
        result: dict[str, Any] = {"modes": modes, "mode": "all"}
    else:
        result = evaluate_reviews(
            predictions, gold, only_predicted=args.only_predicted, mode=args.mode
        )
    result["prediction_meta"] = meta
    result["gold_review_count"] = len(gold)
    result["predicted_review_count"] = len(predictions)
    result["predicted_issue_count"] = sum(len(value) for value in predictions.values())
    result["gold_issue_count"] = sum(len(value) for value in gold.values())
    processed = meta.get("processed_review_count") or 0
    failed = meta.get("validation_failed_count") or 0
    result["json_valid_rate"] = round((processed - failed) / processed, 4) if processed else None
    if args.clusters:
        # 复用 evaluate_reviews 的匹配结果
        matched_rows: list[dict[str, Any]] = []
        for review_id in sorted(set(predictions) | set(gold)):
            preds = list(predictions.get(review_id, []))
            golds = list(gold.get(review_id, []))
            used_gold: set[int] = set()
            used_pred: set[int] = set()
            candidates = []
            for p_index, pred in enumerate(preds):
                for g_index, gold_issue in enumerate(golds):
                    score = match_score(pred, gold_issue)
                    if score > 0:
                        candidates.append((score, p_index, g_index))
            for score, p_index, g_index in sorted(candidates, reverse=True):
                if p_index in used_pred or g_index in used_gold:
                    continue
                used_pred.add(p_index)
                used_gold.add(g_index)
                matched_rows.append(
                    {"review_id": review_id, "predicted": preds[p_index], "gold": golds[g_index]}
                )
        result["clustering"] = cluster_metrics(Path(args.clusters), matched_rows)

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"written {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
