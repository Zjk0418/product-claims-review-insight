"""问题语义聚类。

策略：BGE 向量 + 层次聚类作为主方法，K-Means 作为对比基线。
余弦相似度不能单独决定合并：聚类后再按“对象 / 方面 / 方向极性”强制拆分，
避免把“偏大”和“偏小”这类相反问题合并。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable

import numpy as np

from .categories import CategoryProfile, get_profile
from .issue_schema import (
    DIRECTION_LABELS,
    Issue,
    aspects_compatible,
    direction_polarity,
)
from .normalization import (
    DEFECT_LABELS,
    primary_defect_signature,
    signatures_compatible,
)

SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1, "unknown": 0}
CLUSTER_RULE_VERSION = "component-compat-v2"


def compat_key(issue: Issue) -> tuple[str, str, str]:
    polarity = direction_polarity(issue.direction)
    if polarity:
        dimension, value = polarity
        return issue.object, issue.aspect, f"{dimension}:{value}"
    if issue.direction and issue.direction not in {"other", "none"}:
        return issue.object, issue.aspect, f"dir:{issue.direction}"
    return issue.object, issue.aspect, "none"


def direction_family(direction: str) -> str | None:
    """把方向归为可合并的“家族”；中性方向返回 None。"""
    polarity = direction_polarity(direction)
    if polarity:
        return f"{polarity[0]}:{polarity[1]}"
    if not direction or direction in {"other", "none"}:
        return None
    return direction


def _can_merge(
    left: Issue,
    right: Issue,
    left_vector: np.ndarray,
    right_vector: np.ndarray,
    neutral_threshold: float = 0.86,
    use_defect_signature: bool = True,
) -> bool:
    if left.object != right.object:
        return False
    if not aspects_compatible(left.aspect, right.aspect):
        return False
    # 缺陷签名不同 → 不是同一个具体问题（如“长痘”与“过敏”“空瓶”与“分量少”）
    if use_defect_signature and not signatures_compatible(
        left.manifestation, right.manifestation
    ):
        return False

    left_family = direction_family(left.direction)
    right_family = direction_family(right.direction)
    if left_family and right_family and left_family != right_family:
        return False
    if bool(left_family) != bool(right_family):
        # 同一具体缺陷（“没效果/效果一般”）不因一个方向为中性而被拆开
        left_signature = primary_defect_signature(left.manifestation)
        right_signature = primary_defect_signature(right.manifestation)
        if left_signature and left_signature == right_signature:
            return True
        # 中性描述只有在语义足够接近时才并入有方向的问题
        similarity = float(np.dot(left_vector, right_vector))
        return similarity >= neutral_threshold
    return True


def _split_by_rules(
    labels: np.ndarray,
    issues: list[Issue],
    vectors: np.ndarray,
    neutral_threshold: float = 0.86,
    use_defect_signature: bool = True,
) -> np.ndarray:
    """在同一向量簇内，按对象、方面相关性、方向冲突拆分成更小的组。

    使用并查集：只有两两可合并的问题才会留在同一组，避免“中性描述”把
    相反方向的问题间接连在一起。
    """
    new_labels = np.zeros(len(issues), dtype=np.int32)
    next_label = 0
    for raw_label in sorted(set(int(x) for x in labels)):
        indices = [index for index, label in enumerate(labels) if int(label) == raw_label]
        parent = list(range(len(indices)))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra == rb:
                return
            # 单条未知签名不能充当桥，把已知但不相容的缺陷间接串起来。
            # 检查整个组件的交叉成员，而不是只检查触发 union 的一条边。
            left = [pos for pos in range(len(indices)) if find(pos) == ra]
            right = [pos for pos in range(len(indices)) if find(pos) == rb]
            if all(
                _can_merge(
                    issues[indices[x]], issues[indices[y]],
                    vectors[indices[x]], vectors[indices[y]],
                    neutral_threshold, use_defect_signature,
                )
                for x in left for y in right
            ):
                parent[rb] = ra

        for i in range(len(indices)):
            for j in range(i + 1, len(indices)):
                if _can_merge(
                    issues[indices[i]],
                    issues[indices[j]],
                    vectors[indices[i]],
                    vectors[indices[j]],
                    neutral_threshold,
                    use_defect_signature,
                ):
                    union(i, j)

        components: dict[int, list[int]] = defaultdict(list)
        for position, index in enumerate(indices):
            components[find(position)].append(index)

        for component in components.values():
            subset = [issues[i] for i in component]
            families = sorted(
                {family for family in (direction_family(issue.direction) for issue in subset) if family}
            )
            if len(families) <= 1:
                # 只有一个方向家族（或全是中性描述）时保留为一组
                for index in component:
                    new_labels[index] = next_label
                next_label += 1
                continue

            # 存在多个方向家族：按家族拆分，中性项单独成组，避免被错误归并
            family_groups: dict[str, list[int]] = defaultdict(list)
            neutral_group: list[int] = []
            for index in component:
                family = direction_family(issues[index].direction)
                if family:
                    family_groups[family].append(index)
                else:
                    neutral_group.append(index)
            for group in family_groups.values():
                for index in group:
                    new_labels[index] = next_label
                next_label += 1
            for index in neutral_group:
                new_labels[index] = next_label
                next_label += 1
    return new_labels


def _merge_same_key(
    labels: np.ndarray,
    issues: list[Issue],
    vectors: np.ndarray,
    threshold: float = 0.86,
    use_defect_signature: bool = True,
) -> np.ndarray:
    """合并“规则键完全相同”的簇，但要过两道闸：

    1. 合并后组件中所有交叉成员都必须相容，未知签名不能桥接不同缺陷；
    2. 同一已知主缺陷可合并，其余要求质心余弦相似度 ≥ threshold（防止只凭对象/方面相同就把
       “客服骚扰”和“售后拖延”压成一簇）。

    use_defect_signature=False 时回到 v1.8 行为：同 key 无条件合并（仅用于消融对比）。
    """
    if not use_defect_signature:
        key_to_label: dict[tuple[str, str, str], int] = {}
        legacy = np.zeros(len(issues), dtype=np.int32)
        for index, (label, issue) in enumerate(zip(labels, issues)):
            key = compat_key(issue)
            if key not in key_to_label:
                key_to_label[key] = int(label)
            legacy[index] = key_to_label[key]
        return legacy

    clusters: dict[int, list[int]] = defaultdict(list)
    for index, label in enumerate(labels):
        clusters[int(label)].append(index)

    key_groups: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for label, indices in clusters.items():
        key_groups[compat_key(issues[indices[0]])].append(label)

    parent: dict[int, int] = {label: label for label in clusters}

    def find(label: int) -> int:
        while parent[label] != label:
            parent[label] = parent[parent[label]]
            label = parent[label]
        return label

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left == root_right:
            return
        left_indices = [
            index for label, indices in clusters.items() if find(label) == root_left
            for index in indices
        ]
        right_indices = [
            index for label, indices in clusters.items() if find(label) == root_right
            for index in indices
        ]
        if all(
            _can_merge(issues[x], issues[y], vectors[x], vectors[y], threshold, True)
            for x in left_indices for y in right_indices
        ):
            parent[root_right] = root_left

    for labels_in_key in key_groups.values():
        if len(labels_in_key) < 2:
            continue
        centroids = {
            label: vectors[clusters[label]].mean(axis=0) for label in labels_in_key
        }
        primary: dict[int, str | None] = {}
        for label in labels_in_key:
            signatures = [
                primary_defect_signature(issues[index].manifestation)
                for index in clusters[label]
            ]
            signatures = [signature for signature in signatures if signature]
            primary[label] = (
                Counter(signatures).most_common(1)[0][0] if signatures else None
            )
        for i, left in enumerate(labels_in_key):
            for right in labels_in_key[i + 1 :]:
                left_signature, right_signature = primary[left], primary[right]
                if left_signature and right_signature:
                    if left_signature != right_signature:
                        continue
                    # 同一具体缺陷：不需要再比相似度（“没效果/效果一般”表述差异大）
                    union(left, right)
                    continue
                a, b = centroids[left], centroids[right]
                norm = float(np.linalg.norm(a) * np.linalg.norm(b))
                similarity = float(np.dot(a, b) / norm) if norm else 0.0
                if similarity >= threshold:
                    union(left, right)

    root_to_new: dict[int, int] = {}
    new_labels = np.zeros(len(issues), dtype=np.int32)
    for index, label in enumerate(labels):
        root = find(int(label))
        if root not in root_to_new:
            root_to_new[root] = len(root_to_new)
        new_labels[index] = root_to_new[root]
    return new_labels


def _conflict_count(labels: np.ndarray, issues: list[Issue]) -> int:
    """统计同一个簇内出现同维度相反方向的次数。"""
    grouped: dict[int, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for label, issue in zip(labels, issues):
        polarity = direction_polarity(issue.direction)
        if polarity:
            dimension, value = polarity
            grouped[int(label)][dimension].add(value)
    return sum(
        1
        for dimensions in grouped.values()
        if any(len(values) > 1 for values in dimensions.values())
    )


def _problem_name(members: list[Issue], profile: CategoryProfile | None = None) -> str:
    counts = Counter(issue.manifestation for issue in members)
    manifestation, top_count = counts.most_common(1)[0]
    obj = Counter(issue.object for issue in members).most_common(1)[0][0]
    aspect = Counter(issue.aspect for issue in members).most_common(1)[0][0]
    # 食品评论里“气味/味道”常混用；口感类问题的簇名统一用“味道”，避免“气味不好吃”
    if aspect == "口感与味道" and obj in {"气味", "味道"}:
        obj = "味道"

    directions = Counter(
        issue.direction for issue in members if issue.direction not in {"none", "other", ""}
    )
    if directions:
        direction, direction_count = directions.most_common(1)[0]
        if direction_count / len(members) >= 0.6:
            label = DIRECTION_LABELS.get(direction, "")
            if label:
                return f"{obj}{label}"[:40]

    # 缺陷签名主导时用“对象+缺陷”命名，避免同方面下多个簇重名
    signatures = [
        signature
        for signature in (primary_defect_signature(issue.manifestation) for issue in members)
        if signature
    ]
    if signatures:
        signature, signature_count = Counter(signatures).most_common(1)[0]
        if signature_count / len(members) >= 0.6:
            label = DEFECT_LABELS.get(signature, "")
            if label:
                return f"{obj}{label}"[:40]

    # 多个不同表述、没有明显主导时，用“对象+方面问题”概括，避免以偏概全
    if top_count == 1 and (len(members) >= 4 or len(manifestation) > 18):
        if aspect == "做工质量" and any(issue.aspect == "材质与用料" for issue in members):
            return "整体质量与材质问题"
        base = aspect if aspect.startswith(obj) else f"{obj}{aspect}"
        return f"{base}问题"[:40]

    mapping = (profile or get_profile("generic")).object_synonyms
    synonyms = mapping.get(obj, (obj,))
    # 若 manifestation 已以对象或同义词开头（如“鞋底太硬”“拉链卡住”），不再重复加前缀
    starts_with_object = any(
        word and manifestation.startswith(word) for word in (obj, *synonyms)
    )
    if obj and not starts_with_object and not any(
        word and word in manifestation for word in synonyms
    ):
        name = f"{obj}{manifestation}"
    else:
        name = manifestation
    return name[:40]


def _description(members: list[Issue]) -> str:
    manifestations = [text for text, _ in Counter(m.manifestation for m in members).most_common(3)]
    directions = [
        DIRECTION_LABELS.get(direction, direction)
        for direction, _ in Counter(m.direction for m in members).most_common(2)
        if direction and direction != "none"
    ]
    text = "；".join(manifestations)
    if directions:
        text += f"（方向：{'、'.join(directions)}）"
    return text[:160]


def _cluster_record(
    cluster_id: str,
    indices: list[int],
    issues: list[Issue],
    vectors: np.ndarray,
    reviews_by_id: dict[str, dict[str, Any]],
    include_centroid: bool = False,
    profile: CategoryProfile | None = None,
) -> dict[str, Any]:
    members = [issues[i] for i in indices]
    review_ids: list[str] = []
    for issue in members:
        if issue.review_id not in review_ids:
            review_ids.append(issue.review_id)

    severity = Counter(issue.severity for issue in members)
    directions = Counter(issue.direction for issue in members)
    skus = Counter(issue.sku_id for issue in members if issue.sku_id)
    dates = [issue.date for issue in members if issue.date]
    confidence = [issue.confidence for issue in members if issue.confidence is not None]

    representatives: list[dict[str, Any]] = []
    ranked = sorted(
        members,
        key=lambda item: (
            SEVERITY_RANK.get(item.severity, 0),
            item.confidence or 0,
            len(item.evidence),
        ),
        reverse=True,
    )
    evidence_by_review: dict[str, str] = {}
    for issue in ranked:
        current = evidence_by_review.get(issue.review_id, "")
        if len(issue.evidence) > len(current):
            evidence_by_review[issue.review_id] = issue.evidence
    seen_reviews: set[str] = set()
    for issue in ranked:
        if issue.review_id in seen_reviews:
            continue
        seen_reviews.add(issue.review_id)
        review = reviews_by_id.get(issue.review_id, {})
        representatives.append(
            {
                "review_id": issue.review_id,
                "star": issue.star,
                "date": issue.date,
                "sku_id": issue.sku_id,
                "sku_size": review.get("sku_size", ""),
                "content": review.get("text") or review.get("content", ""),
                "evidence": issue.evidence,
            }
        )
        if len(representatives) >= 3:
            break

    subset = vectors[indices]
    centroid = subset.mean(axis=0)
    norm = np.linalg.norm(centroid)
    centroid = centroid / norm if norm > 0 else centroid
    if len(indices) > 1:
        similarity = subset @ subset.T
        upper = similarity[np.triu_indices(len(indices), k=1)]
        cohesion = float(upper.mean())
    else:
        cohesion = 1.0

    record = {
        "cluster_id": cluster_id,
        "object": Counter(issue.object for issue in members).most_common(1)[0][0],
        "aspect": Counter(issue.aspect for issue in members).most_common(1)[0][0],
        "problem_name": _problem_name(members, profile),
        "description": _description(members),
        "member_issue_ids": [issue.issue_id for issue in members],
        "review_ids": review_ids,
        "review_count": len(review_ids),
        "issue_count": len(members),
        "representative_reviews": representatives,
        "review_evidence": evidence_by_review,
        "sku_distribution": dict(skus.most_common()),
        "severity_distribution": dict(severity),
        "direction_distribution": {
            DIRECTION_LABELS.get(key, key): value for key, value in directions.most_common()
        },
        "mean_confidence": round(float(np.mean(confidence)), 3) if confidence else None,
        "date_range": {"min": min(dates) if dates else "", "max": max(dates) if dates else ""},
        "cohesion": round(cohesion, 4),
        "is_singleton": len(members) == 1,
    }
    if include_centroid:
        record["centroid"] = [round(float(value), 5) for value in centroid]
    return record


def cluster_issues(
    issues: list[Issue],
    vectors: np.ndarray,
    reviews_by_id: dict[str, dict[str, Any]],
    sim_threshold: float = 0.78,
    random_state: int = 42,
    include_centroid: bool = False,
    profile: CategoryProfile | None = None,
    use_defect_signature: bool = True,
) -> dict[str, Any]:
    from sklearn.cluster import AgglomerativeClustering, KMeans
    from sklearn.metrics import silhouette_score

    if not issues:
        return {
            "algorithm": "agglomerative",
            "params": {"sim_threshold": sim_threshold, "rule_version": CLUSTER_RULE_VERSION},
            "metrics": {"issue_count": 0, "cluster_count": 0},
            "clusters": [],
            "issue_assignments": [],
        }
    if len(issues) == 1:
        labels = np.zeros(1, dtype=np.int32)
        raw_labels = labels.copy()
    else:
        distance_threshold = max(0.01, 1.0 - sim_threshold)
        model = AgglomerativeClustering(
            n_clusters=None,
            metric="cosine",
            linkage="average",
            distance_threshold=distance_threshold,
        )
        raw_labels = model.fit_predict(vectors)
        split = _split_by_rules(raw_labels, issues, vectors, use_defect_signature=use_defect_signature)
        labels = _merge_same_key(
            split, issues, vectors, use_defect_signature=use_defect_signature
        )

    conflict_before = _conflict_count(raw_labels, issues)
    conflict_after = _conflict_count(labels, issues)

    unique_labels = sorted(set(int(x) for x in labels))
    groups: dict[int, list[int]] = {label: [] for label in unique_labels}
    for index, label in enumerate(labels):
        groups[int(label)].append(index)

    clusters: list[dict[str, Any]] = []
    for label, indices in groups.items():
        clusters.append(
            _cluster_record(
                cluster_id="",
                indices=indices,
                issues=issues,
                vectors=vectors,
                reviews_by_id=reviews_by_id,
                include_centroid=include_centroid,
                profile=profile,
            )
        )
    clusters.sort(key=lambda item: (item["review_count"], item["issue_count"]), reverse=True)
    for index, cluster in enumerate(clusters, 1):
        cluster["cluster_id"] = f"C{index:02d}"

    assignments = []
    cluster_of_issue: dict[str, str] = {}
    for cluster in clusters:
        for issue_id in cluster["member_issue_ids"]:
            cluster_of_issue[issue_id] = cluster["cluster_id"]
    for issue in issues:
        assignments.append(
            {"issue_id": issue.issue_id, "cluster_id": cluster_of_issue.get(issue.issue_id, "")}
        )

    metrics: dict[str, Any] = {
        "issue_count": len(issues),
        "cluster_count": len(clusters),
        "singleton_count": sum(1 for cluster in clusters if cluster["is_singleton"]),
        "conflict_clusters_before_split": conflict_before,
        "conflict_clusters_after_split": conflict_after,
        "algorithm": "agglomerative_average_cosine",
        "sim_threshold": sim_threshold,
    }
    if 2 <= len(clusters) < len(issues):
        try:
            metrics["silhouette_cosine"] = round(
                float(silhouette_score(vectors, labels, metric="cosine")), 4
            )
        except ValueError:
            metrics["silhouette_cosine"] = None

    # K-Means 对比基线：按轮廓系数选 k
    baseline: dict[str, Any] = {"algorithm": "kmeans", "status": "skipped"}
    if 3 <= len(issues) <= 200:
        best: dict[str, Any] | None = None
        for k in range(2, min(10, len(issues) - 1) + 1):
            try:
                model = KMeans(n_clusters=k, random_state=random_state, n_init=10)
                labels_k = model.fit_predict(vectors)
                score = silhouette_score(vectors, labels_k, metric="cosine")
            except (ValueError, RuntimeError):
                continue
            if best is None or score > best["silhouette_cosine"]:
                best = {
                    "k": k,
                    "silhouette_cosine": round(float(score), 4),
                    "conflict_clusters": _conflict_count(labels_k, issues),
                    "cluster_sizes": sorted(
                        [int((labels_k == value).sum()) for value in set(labels_k)], reverse=True
                    ),
                }
        if best:
            baseline = {"algorithm": "kmeans", "status": "ok", **best}

    return {
        "algorithm": "agglomerative_average_cosine + rule_split",
        "params": {
            "rule_version": CLUSTER_RULE_VERSION,
            "sim_threshold": sim_threshold,
            "linkage": "average",
            "metric": "cosine",
            "rule_split": "object + aspect + direction_polarity + defect_signature",
            "use_defect_signature": use_defect_signature,
            "random_state": random_state,
        },
        "metrics": metrics,
        "baseline": baseline,
        "clusters": clusters,
        "issue_assignments": assignments,
    }
