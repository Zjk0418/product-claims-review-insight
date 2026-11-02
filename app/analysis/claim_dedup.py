"""宣传主张的近义重复分组。

同一卖点常被 LLM 拆成多条近义表述（例如“淡化痘印”“精准淡化痘印”
“逐层淡化痘印”），逐条做落差判断会把一个真实冲突计成多次。这里在
进入落差分析前，用 BGE 向量对 normalized_claim 做语义分组：

- 保留全部原始主张与证据，不删除、不合并文本；
- 只额外给出 claim_group / claim_groups 两个字段；
- 统计时可按组去重计数（一组取最强判定），原始逐条计数仍然保留。
"""
from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

from resources import resource_path

DEFAULT_MODEL_DIR = resource_path("models", "bge-small-zh-v1.5")
DEFAULT_THRESHOLD = 0.90

# 一组内同时出现多种判定时，取“最强”的代表判定
_STRENGTH_ORDER = [
    "potential_conflict",
    "partial_gap",
    "no_direct_conflict_found",
    "insufficient_evidence",
]


def group_near_duplicate_claims(
    claims: list[dict[str, Any]],
    threshold: float = DEFAULT_THRESHOLD,
    model_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """返回 (分组列表, claim_id -> group_id)。

    模型不可用或文本为空时保留全部主张、每条自成一组；每组的 grouping
    元数据明确记录分组是否执行，降级不会伪装成成功的语义分组。
    """
    claims = [c for c in claims if str(c.get("claim_id") or "")]
    if not claims:
        return [], {}

    norms = [str(c.get("normalized_claim") or "") for c in claims]
    usable = [i for i, text in enumerate(norms) if text.strip()]
    vectors = None
    grouping: dict[str, Any] = {
        "status": "not_applicable", "method": "singleton_empty_text",
        "model": "BAAI/bge-small-zh-v1.5", "threshold": float(threshold),
    }
    if usable:
        stage = "model_load"
        try:
            from analysis.embeddings import BGEEmbedder
            import numpy as np

            embedder = BGEEmbedder(model_dir=Path(model_dir or DEFAULT_MODEL_DIR))
            stage = "encode"
            vectors = embedder.encode([norms[i] for i in usable])
            stage = "vector_validation"
            vectors = np.asarray(vectors)
            if (vectors.ndim != 2 or vectors.shape[0] != len(usable) or vectors.shape[1] == 0
                    or not np.isfinite(vectors).all() or np.any(np.linalg.norm(vectors, axis=1) <= 0)):
                raise ValueError("BGE 返回了不可用于比较的向量")
            grouping.update(status="ok", method="bge_cosine")
        except Exception as exc:  # noqa: BLE001 - 保留原主张，并明确披露降级。
            vectors = None
            grouping.update(status="degraded", method="singleton_fallback",
                            failure_stage=stage, error_type=type(exc).__name__)
            # 异常原文可能含用户路径；元数据和日志只暴露阶段与异常类型。
            warnings.warn(f"BGE 近义主张分组未执行（{stage}: {type(exc).__name__}），当前逐条成组。",
                          RuntimeWarning, stacklevel=2)

    n = len(claims)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    if vectors is not None:
        import numpy as np

        sims = vectors @ vectors.T
        for a in range(len(usable)):
            for b in range(a + 1, len(usable)):
                if float(sims[a, b]) >= threshold:
                    union(usable[a], usable[b])

    buckets: dict[int, list[int]] = {}
    for i in range(n):
        buckets.setdefault(find(i), []).append(i)

    groups: list[dict[str, Any]] = []
    group_map: dict[str, str] = {}
    for index, (root, members) in enumerate(
        sorted(buckets.items(), key=lambda kv: min(kv[1])), start=1
    ):
        group_id = f"CG{index:02d}"
        member_ids = [str(claims[i]["claim_id"]) for i in members]
        representative = max(
            (norms[i] for i in members if norms[i].strip()),
            key=len,
            default="",
        )
        groups.append(
            {
                "group_id": group_id,
                "size": len(members),
                "member_claim_ids": member_ids,
                "representative": representative,
                "grouping": dict(grouping),
            }
        )
        for claim_id in member_ids:
            group_map[claim_id] = group_id
    return groups, group_map


def grouped_by_type(
    gaps: list[dict[str, Any]], group_map: dict[str, str]
) -> dict[str, int]:
    """按组统计落差类型：同一组只计一次，取最强判定。"""
    best: dict[str, str] = {}
    for gap in gaps:
        group_id = group_map.get(str(gap.get("claim_id") or ""))
        if not group_id:
            group_id = f"__solo__{gap.get('gap_id')}"
        gap_type = str(gap.get("gap_type") or "insufficient_evidence")
        current = best.get(group_id)
        if current is None or _STRENGTH_ORDER.index(gap_type) < _STRENGTH_ORDER.index(
            current
        ):
            best[group_id] = gap_type
    counts: dict[str, int] = {}
    for gap_type in best.values():
        counts[gap_type] = counts.get(gap_type, 0) + 1
    return counts


def grouped_conflict_evidence(
    gaps: list[dict[str, Any]], group_map: dict[str, str]
) -> dict[str, list[str]]:
    """每组去重后的证据评论 ID 集合，用于核对冲突计数变化是否来自证据变化。"""
    per_group: dict[str, set[str]] = {}
    for gap in gaps:
        group_id = group_map.get(str(gap.get("claim_id") or ""), "")
        if not group_id:
            continue
        bucket = per_group.setdefault(group_id, set())
        for rid in gap.get("evidence_review_ids") or []:
            bucket.add(str(rid))
    return {key: sorted(value) for key, value in per_group.items()}


def dump_groups(groups: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"groups": groups}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
