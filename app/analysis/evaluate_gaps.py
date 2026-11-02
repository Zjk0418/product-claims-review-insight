"""人工标注的宣传—体验配对评测。

只评“落差类型”和“证据强度等级”，不把模型生成的 finding 当作新事实。

v2.0 起补充：四分类逐类 Precision / Recall / F1、Macro-F1、完整混淆矩阵，
以及（人工 Gold 提供 `expected_evidence_review_ids` 时）证据引用命中率。
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .eval_identity import make_identity

GAP_TYPES = (
    "potential_conflict",
    "partial_gap",
    "no_direct_conflict_found",
    "insufficient_evidence",
)
ROOT = Path(__file__).resolve().parents[2]


def load_gaps(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    gaps = {str(item.get("claim_id")): item for item in payload.get("gaps") or []}
    return gaps, payload


def evaluate(gaps: dict[str, dict[str, Any]], gold: dict[str, Any]) -> dict[str, Any]:
    cases = list(gold.get("cases") or [])
    correct = 0
    confidence_correct = 0
    confidence_total = 0
    per_type: dict[str, dict[str, int]] = defaultdict(lambda: {"support": 0, "correct": 0})
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    mismatches: list[dict[str, Any]] = []
    missing: list[str] = []

    for case in cases:
        claim_id = str(case.get("claim_id"))
        expected = case.get("expected_gap_type")
        gap = gaps.get(claim_id)
        if gap is None:
            missing.append(claim_id)
            continue
        predicted = gap.get("gap_type")
        per_type[expected]["support"] += 1
        confusion[expected][predicted] += 1
        if predicted == expected:
            correct += 1
            per_type[expected]["correct"] += 1
        else:
            mismatches.append(
                {
                    "claim_id": claim_id,
                    "claim_text": case.get("claim_text"),
                    "expected": expected,
                    "predicted": predicted,
                    "note": case.get("note"),
                    "model_finding": gap.get("finding"),
                    "evidence_review_ids": gap.get("evidence_review_ids"),
                }
            )
        expected_conf = case.get("expected_confidence")
        if expected_conf:
            confidence_total += 1
            if gap.get("confidence_level") == expected_conf:
                confidence_correct += 1
            else:
                mismatches.append(
                    {
                        "claim_id": claim_id,
                        "claim_text": case.get("claim_text"),
                        "expected_confidence": expected_conf,
                        "predicted_confidence": gap.get("confidence_level"),
                        "note": "证据强度等级不一致",
                    }
                )

    total = len(cases) - len(missing)

    classification = classification_metrics(cases, gaps, missing)
    evidence = evidence_metrics(cases, gaps, missing)

    return {
        "label_count": len(cases),
        "evaluated": total,
        "missing_claim_ids": missing,
        "accuracy": round(correct / total, 4) if total else 0.0,
        "correct": correct,
        "confidence_accuracy": round(confidence_correct / confidence_total, 4)
        if confidence_total
        else None,
        "confidence_evaluated": confidence_total,
        "per_type": {key: dict(value) for key, value in per_type.items()},
        "confusion": {key: dict(value) for key, value in confusion.items()},
        "classification": classification,
        "evidence": evidence,
        "mismatches": mismatches,
        "criteria": gold.get("criteria", ""),
    }


def classification_metrics(
    cases: list[dict[str, Any]],
    gaps: dict[str, dict[str, Any]],
    missing: list[str],
) -> dict[str, Any]:
    """四分类逐类 P/R/F1 + Macro-F1 + 完整混淆矩阵。

    分母口径写死并回传，便于复核：
    - 某类 recall 的分母 = 该类在人工 Gold 中的条数；
    - 某类 precision 的分母 = 模型判为该类的条数；
    - Macro-F1 只对 Gold 中出现过的类别取平均（同时给出全四类版本）。
    """
    missing_set = {str(item) for item in missing}
    labels = list(GAP_TYPES)
    tp = Counter()
    support = Counter()
    predicted = Counter()
    confusion: dict[str, Counter[str]] = {label: Counter() for label in labels}
    unknown_gold = 0
    unknown_pred = 0

    for case in cases:
        claim_id = str(case.get("claim_id"))
        if claim_id in missing_set:
            continue
        expected = case.get("expected_gap_type")
        gap = gaps.get(claim_id) or {}
        got = gap.get("gap_type")
        if expected not in labels:
            unknown_gold += 1
        if got not in labels:
            unknown_pred += 1
        if expected in labels:
            support[expected] += 1
            confusion[expected][str(got)] += 1
        if got in labels:
            predicted[got] += 1
        if expected in labels and expected == got:
            tp[expected] += 1

    per_class = {}
    for label in labels:
        t = tp[label]
        s = support[label]
        p_den = predicted[label]
        precision = t / p_den if p_den else None
        recall = t / s if s else None
        if precision is not None and recall is not None and (precision + recall) > 0:
            f1 = 2 * precision * recall / (precision + recall)
        elif precision is None or recall is None:
            f1 = None
        else:
            f1 = 0.0
        per_class[label] = {
            "support": s,
            "predicted": p_den,
            "tp": t,
            "fp": p_den - t,
            "fn": s - t,
            "precision": round(precision, 4) if precision is not None else None,
            "recall": round(recall, 4) if recall is not None else None,
            "f1": round(f1, 4) if f1 is not None else None,
        }

    present = [label for label in labels if support[label] > 0]
    macro_present = (
        round(
            sum(per_class[label]["f1"] for label in present if per_class[label]["f1"] is not None)
            / len([label for label in present if per_class[label]["f1"] is not None]),
            4,
        )
        if any(per_class[label]["f1"] is not None for label in present)
        else None
    )
    macro_all = (
        round(
            sum(per_class[label]["f1"] for label in labels if per_class[label]["f1"] is not None)
            / len([label for label in labels if per_class[label]["f1"] is not None]),
            4,
        )
        if any(per_class[label]["f1"] is not None for label in labels)
        else None
    )
    return {
        "labels": labels,
        "per_class": per_class,
        "macro_f1_present_classes": macro_present,
        "macro_f1_all_classes": macro_all,
        "macro_f1_note": "present_classes 只对 Gold 中出现过的类别平均；all_classes 把未出现的类按现有 F1 一并平均",
        "confusion_matrix": {label: dict(confusion[label]) for label in labels},
        "unknown_gold_labels": unknown_gold,
        "unknown_predicted_labels": unknown_pred,
        "evaluated": sum(support.values()),
    }


def evidence_metrics(
    cases: list[dict[str, Any]],
    gaps: dict[str, dict[str, Any]],
    missing: list[str],
) -> dict[str, Any]:
    """证据引用命中情况；只在 Gold 提供 expected_evidence_review_ids 时计算。"""
    missing_set = {str(item) for item in missing}
    cases_with_gold = 0
    any_overlap = 0
    recall_sum = 0.0
    precision_sum = 0.0
    rows: list[dict[str, Any]] = []
    for case in cases:
        claim_id = str(case.get("claim_id"))
        if claim_id in missing_set:
            continue
        expected = case.get("expected_evidence_review_ids")
        if not expected:
            continue
        expected_set = {str(item) for item in expected}
        gap = gaps.get(claim_id) or {}
        got_set = {str(item) for item in (gap.get("evidence_review_ids") or [])}
        cases_with_gold += 1
        overlap = expected_set & got_set
        if overlap:
            any_overlap += 1
        recall_sum += len(overlap) / len(expected_set)
        precision_sum += len(overlap) / len(got_set) if got_set else 0.0
        rows.append(
            {
                "claim_id": claim_id,
                "expected": sorted(expected_set),
                "predicted": sorted(got_set),
                "overlap": sorted(overlap),
            }
        )
    if not cases_with_gold:
        return {
            "available": False,
            "note": "Gold 未提供 expected_evidence_review_ids，本轮不计算证据命中率",
        }
    return {
        "available": True,
        "cases": cases_with_gold,
        "any_overlap_rate": round(any_overlap / cases_with_gold, 4),
        "mean_recall": round(recall_sum / cases_with_gold, 4),
        "mean_precision": round(precision_sum / cases_with_gold, 4),
        "rows": rows,
    }


def _display(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def _identity_from_gold(gold: dict[str, Any], gold_path: Path, args) -> dict[str, Any]:
    annotator = str(gold.get("annotator") or "")
    if gold.get("human_verified"):
        labeler_type = "human"
    elif annotator.lower() in {"manual", "human", "author"} or "人工" in annotator:
        labeler_type = "human_unverified"
    elif "assistant" in annotator.lower() or "draft" in annotator.lower() or "草稿" in annotator:
        labeler_type = "assistant_draft"
    else:
        labeler_type = "unknown"
    role = args.role or gold.get("dataset_role") or "development"
    used_for_tuning = (
        args.used_for_tuning
        if args.used_for_tuning is not None
        else bool(gold.get("used_for_tuning", role == "development"))
    )
    reportable = bool(gold.get("reportable")) if args.reportable is None else args.reportable
    return make_identity(
        dataset_name=args.dataset_name or gold.get("dataset_name") or gold_path.stem,
        dataset_version=args.dataset_version
        or gold.get("annotation_version")
        or "unknown",
        role=role,
        review_count=int(gold.get("review_count") or len(gold.get("cases") or [])),
        issue_count=len(gold.get("cases") or []),
        label_file=_display(gold_path),
        labeler_type=labeler_type,
        labeler_count=int(
            gold.get("labeler_count")
            or (1 if gold.get("human_verified") or labeler_type == "human_unverified" else 0)
        ),
        human_verified=bool(gold.get("human_verified")),
        blind=bool(gold.get("blind_to_model", False)),
        used_for_tuning=used_for_tuning,
        model=str(args.model or ""),
        prompt_version=str(args.prompt_version or ""),
        metric_scope="gap_type 四分类（accuracy / 逐类 P/R/F1 / Macro-F1 / 混淆矩阵）与证据强度",
        reportable=reportable,
        reportable_note=(
            "人工盲标且未参与调参时才可正式引用；开发集只能报基线。"
            if reportable
            else "非人工盲标或开发集：只能作为开发集基线/诊断，不得写成泛化准确率。"
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="评测宣传—体验落差判断")
    parser.add_argument("--gaps", required=True)
    parser.add_argument("--gold", required=True)
    parser.add_argument("--out", default=None)
    parser.add_argument("--dataset-name", default=None)
    parser.add_argument("--dataset-version", default=None)
    parser.add_argument("--role", default=None, choices=["development", "validation", "test", "diagnostic"])
    parser.add_argument("--reportable", action="store_true", default=None)
    parser.add_argument("--used-for-tuning", action="store_true", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--prompt-version", default=None)
    args = parser.parse_args(argv)

    gaps_path = Path(args.gaps)
    gold_path = Path(args.gold)
    gaps, payload = load_gaps(gaps_path)
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    result = evaluate(gaps, gold)
    result["prediction_meta"] = {
        "prompt_version": payload.get("prompt_version"),
        "model": payload.get("model"),
        "claim_count": payload.get("claim_count"),
    }
    result["gold_meta"] = {
        "annotation_version": gold.get("annotation_version"),
        "annotator": gold.get("annotator"),
        "labeler_count": gold.get("labeler_count"),
        "human_verified": gold.get("human_verified"),
        "blind_to_model": gold.get("blind_to_model"),
        "reportable": gold.get("reportable"),
        "source_csv_sha1": gold.get("source_csv_sha1"),
        "label_counts": gold.get("label_counts"),
        "class_support": gold.get("class_support"),
        "case_count": len(gold.get("cases") or []),
    }
    result["dataset"] = _identity_from_gold(
        gold,
        gold_path,
        argparse.Namespace(
            dataset_name=args.dataset_name,
            dataset_version=args.dataset_version,
            role=args.role,
            reportable=args.reportable,
            used_for_tuning=args.used_for_tuning,
            model=args.model or payload.get("model") or "",
            prompt_version=args.prompt_version or payload.get("prompt_version") or "",
        ),
    )
    if not result["dataset"]["human_verified"]:
        result["warning"] = (
            "这套 Gold 未标记为“人工填写 + 盲标”，其准确率/P/R/F1 只能作为开发集基线或试标，"
            "不得作为独立评测结论或泛化能力。"
        )
    elif result["dataset"]["role"] == "development":
        result["warning"] = (
            "这是开发集（参与过规则/Prompt 迭代），只能报开发集基线，不能称为泛化准确率。"
        )
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"written {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
