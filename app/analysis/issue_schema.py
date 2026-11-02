"""负面问题字段定义、规范化与校验。

设计原则：
1. 模型输出的原始值保留在 *_raw 字段，规范化只用于聚类和统计；
2. evidence 必须能在评论文本中找到，找不到就判为无效，不做“修补”；
3. issue_id 由评论 ID + 规范化问题文本决定，便于断点恢复与追踪。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from .categories import (
    EXTRA_ASPECTS,
    GENERIC_ASPECTS,
    LEGACY_ASPECTS,
    CategoryProfile,
    get_profile,
)

# 全量方面标签（通用 + 鞋类历史标签 + 品类扩展）；具体商品可用哪些由品类配置决定
ASPECTS = list(GENERIC_ASPECTS) + list(LEGACY_ASPECTS) + list(EXTRA_ASPECTS)

# 历史标签 -> 通用标签，用于跨品类比较
ASPECT_ALIASES = {
    "尺码与鞋型": "尺寸与规格",
    "穿着舒适性": "使用舒适性",
}

SEVERITIES = ("low", "medium", "high", "unknown")
BASES = ("explicit", "inferred", "unknown")

# 方向：中文/英文别名 -> 规范值
DIRECTION_ALIASES = {
    "偏小": "too_small",
    "太小": "too_small",
    "小了": "too_small",
    "挤脚": "too_small",
    "挤脚趾": "too_small",
    "偏窄": "too_narrow",
    "太窄": "too_narrow",
    "窄": "too_narrow",
    "偏大": "too_large",
    "太大": "too_large",
    "大了": "too_large",
    "偏宽": "too_loose",
    "太宽": "too_loose",
    "宽松": "too_loose",
    "偏硬": "too_hard",
    "过硬": "too_hard",
    "硬": "too_hard",
    "偏软": "too_soft",
    "过软": "too_soft",
    "软": "too_soft",
    "偏薄": "too_thin",
    "太薄": "too_thin",
    "薄": "too_thin",
    "偏厚": "too_thick",
    "太厚": "too_thick",
    "厚重": "too_heavy",
    "偏重": "too_heavy",
    "太重": "too_heavy",
    "偏轻": "too_light",
    "太轻": "too_light",
    "开胶": "unglued",
    "脱胶": "unglued",
    "断裂": "broken",
    "破损": "broken",
    "坏了": "broken",
    "掉色": "color_fading",
    "褪色": "color_fading",
    "色差": "color_mismatch",
    "磨脚": "rubbing",
    "磨脚跟": "rubbing",
    "掉跟": "slipping_off",
    "不跟脚": "slipping_off",
    "不防滑": "slippery",
    "打滑": "slippery",
    "闷脚": "unbreathable",
    "不透气": "unbreathable",
    "臭": "odor",
    "异味": "odor",
    "变形": "deformed",
    "起球": "pilling",
    "线头": "loose_thread",
    "溢胶": "glue_residue",
    "开线": "seam_split",
    # 通用（美妆/食品/箱包/数码等品类）
    "粘腻": "sticky",
    "黏腻": "sticky",
    "油腻": "sticky",
    "拔干": "drying",
    "干燥": "drying",
    "紧绷": "drying",
    "刺痛": "irritation",
    "刺激": "irritation",
    "泛红": "irritation",
    "过敏": "irritation",
    "没用": "ineffective",
    "无效": "ineffective",
    "没效果": "ineffective",
    "没作用": "ineffective",
    "漏液": "leaking",
    "漏出": "leaking",
    "变质": "stale",
    "不新鲜": "stale",
    "过期": "stale",
    "缺斤少两": "short_weight",
    "分量少": "short_weight",
    "卡顿": "jammed",
    "卡住": "jammed",
    "none": "none",
    "无": "none",
}

DIRECTION_LABELS = {
    "too_small": "偏小",
    "too_narrow": "偏窄",
    "too_large": "偏大",
    "too_loose": "偏宽",
    "too_hard": "偏硬",
    "too_soft": "偏软",
    "too_thin": "偏薄",
    "too_thick": "偏厚",
    "too_heavy": "偏重",
    "too_light": "偏轻",
    "unglued": "开胶",
    "broken": "破损",
    "color_fading": "掉色",
    "color_mismatch": "色差",
    "rubbing": "磨脚",
    "slipping_off": "掉跟",
    "slippery": "不防滑",
    "unbreathable": "不透气",
    "odor": "异味",
    "deformed": "变形",
    "pilling": "起球",
    "loose_thread": "线头",
    "glue_residue": "溢胶",
    "seam_split": "开线",
    "sticky": "粘腻",
    "drying": "干燥",
    "irritation": "刺激",
    "ineffective": "无效",
    "leaking": "漏液",
    "stale": "变质",
    "short_weight": "分量不足",
    "jammed": "卡顿",
    "none": "无",
    "other": "其他",
}

# 方向所属维度与极性：同一维度、不同极性绝对不能合并
# too_small / too_narrow 都表示“偏小/偏窄”，视为同一 scale 极性；
# too_large / too_loose 同理。这样“鞋头偏小”和“前脚掌偏窄”能合并，
# 而“偏大”与“偏小”仍然冲突。
DIRECTION_POLARITY = {
    "too_small": ("scale", "small"),
    "too_narrow": ("scale", "small"),
    "too_large": ("scale", "large"),
    "too_loose": ("scale", "large"),
    "too_hard": ("hardness", "hard"),
    "too_soft": ("hardness", "soft"),
    "too_thin": ("thickness", "thin"),
    "too_thick": ("thickness", "thick"),
    "too_heavy": ("weight", "heavy"),
    "too_light": ("weight", "light"),
}

ASPECT_KEYWORDS = [
    (("尺码", "鞋型", "版型", "码数", "规格", "尺寸", "大小", "容量", "净含量", "克数"), "尺寸与规格"),
    (("舒适", "磨脚", "挤脚", "脚感", "疼痛", "脚疼", "累脚", "勒", "闷", "压肩", "顶脚",
      "泛红", "刺痛", "过敏", "拔干", "粘腻", "搓泥"), "使用舒适性"),
    (("效果", "功效", "没用", "无效", "淡印", "祛痘", "痘印", "美白", "保湿", "补水", "修复"), "功效与效果"),
    (("做工", "开胶", "脱胶", "走线", "溢胶", "线头", "开线", "胶水", "针脚", "缝线"), "做工质量"),
    (("材质", "面料", "成分", "配方", "内里", "用料", "布料"), "材质与用料"),
    (("外观", "颜色", "色差", "掉色", "款式", "颜值"), "外观与色差"),
    (("防滑", "透气", "防水", "性能", "功能", "缓震", "抓地", "续航", "吸力", "保温"), "功能与性能"),
    (("耐用", "寿命", "变形", "起球", "穿久", "用久", "坏了", "断裂"), "耐用性"),
    (("气味", "味道", "臭", "异味"), "气味"),
    (("物流", "包装", "快递", "盒子", "发货", "压坏"), "物流与包装"),
    (("客服", "售后", "退货", "退款", "换货"), "客服与售后"),
    (("价格", "性价比", "贵", "便宜", "不值"), "价格与性价比"),
    (("口感", "好吃", "难吃", "太咸", "太甜", "太淡"), "口感与味道"),
    (("新鲜", "保质期", "过期", "变质", "生产日期"), "新鲜度与保质期"),
    (("安装", "组装", "结构", "稳固", "晃动", "承重"), "安装与结构"),
    (("连接", "断连", "配对", "兼容", "信号", "延迟"), "连接与兼容"),
]

# 允许跨方面合并的相近方面（仅限人工确认语义相关的组合）
ASPECT_COMPAT = {
    frozenset(("尺码与鞋型", "穿着舒适性")),
    frozenset(("尺寸与规格", "使用舒适性")),
    frozenset(("尺码与鞋型", "尺寸与规格")),
    frozenset(("穿着舒适性", "使用舒适性")),
    frozenset(("做工质量", "材质与用料")),
    frozenset(("做工质量", "耐用性")),
    frozenset(("材质与用料", "耐用性")),
    frozenset(("功能与性能", "穿着舒适性")),
    frozenset(("功能与性能", "使用舒适性")),
    frozenset(("功能与性能", "功效与效果")),
    frozenset(("功能与性能", "连接与兼容")),
    frozenset(("耐用性", "安装与结构")),
}


def aspects_compatible(left: str, right: str) -> bool:
    if left == right:
        return True
    return frozenset((left, right)) in ASPECT_COMPAT


def directions_conflict(left: str, right: str) -> bool:
    a = direction_polarity(left)
    b = direction_polarity(right)
    return bool(a and b and a[0] == b[0] and a[1] != b[1])

# 兼容旧调用：默认词表 = 通用词表 + 鞋类词表（历史评测集是鞋类）
OBJECT_SYNONYMS = dict(get_profile("shoe").object_synonyms)

_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def canonical_aspect(raw: str, profile: CategoryProfile | None = None) -> str:
    """归一到通用方面名（不带品类偏好）。"""
    value = _clean(raw)
    if not value:
        return "其他"
    if value in GENERIC_ASPECTS or value in EXTRA_ASPECTS:
        return value
    if value in ASPECT_ALIASES:
        return ASPECT_ALIASES[value]
    if value in LEGACY_ASPECTS:
        return ASPECT_ALIASES.get(value, "其他")
    keywords: tuple[tuple[tuple[str, ...], str], ...] = tuple(ASPECT_KEYWORDS)
    if profile is not None:
        keywords = tuple(profile.aspect_keywords) + keywords
    for words, aspect in keywords:
        if any(word in value for word in words):
            return ASPECT_ALIASES.get(aspect, aspect)
    return "其他"


def normalize_aspect(raw: str, profile: CategoryProfile | None = None) -> str:
    """归一化方面；有品类配置时换成该品类的惯用标签。"""
    value = _clean(raw)
    if profile is not None and value in profile.allowed_aspects:
        return value
    canonical = canonical_aspect(value, profile)
    if profile is None:
        return canonical
    return profile.preferred(canonical)


def object_key(raw: str, profile: CategoryProfile | None = None) -> str:
    """按最长命中匹配对象，避免“脸上的痘印”被更短的“脸”抢走。"""
    value = _clean(raw)
    mapping = profile.object_synonyms if profile is not None else OBJECT_SYNONYMS
    # 先看规范名本身是否出现，避免“脸上的痘印”被同义词“脸上”抢走
    canonical_hits = [canonical for canonical in mapping if canonical in value]
    if canonical_hits:
        return max(canonical_hits, key=len)
    best = ""
    best_len = 0
    for canonical, synonyms in mapping.items():
        for word in synonyms:
            if word and word in value and len(word) > best_len:
                best, best_len = canonical, len(word)
    return best or value[:12] or "未知"


def normalize_direction(raw: str) -> tuple[str, str]:
    """返回 (规范方向, 原始方向)。"""
    original = _clean(raw)
    if not original:
        return "other", ""
    key = original.lower().replace("-", "_").replace(" ", "_")
    if key in DIRECTION_ALIASES:
        return DIRECTION_ALIASES[key], original
    if key in DIRECTION_LABELS:
        return key, original
    for alias, canonical in DIRECTION_ALIASES.items():
        if alias and alias in original:
            return canonical, original
    return "other", original


def direction_polarity(direction: str) -> tuple[str, str] | None:
    return DIRECTION_POLARITY.get(direction)


def normalize_severity(raw: str) -> str:
    value = _clean(raw).lower()
    return value if value in SEVERITIES else "unknown"


def normalize_basis(raw: str) -> str:
    value = _clean(raw).lower()
    return value if value in BASES else "unknown"


def _strip_for_evidence(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "")
    return _PUNCT_RE.sub("", normalized)


def check_evidence(evidence: str, content: str) -> str:
    """返回 exact / normalized / failed。

    normalized 指忽略空白与标点后能匹配；不修改 evidence 本身。
    """
    ev = _clean(evidence)
    if not ev:
        return "failed"
    if ev in (content or ""):
        return "exact"
    if _strip_for_evidence(ev) and _strip_for_evidence(ev) in _strip_for_evidence(content):
        return "normalized"
    return "failed"


@dataclass
class Issue:
    review_id: str
    object: str
    aspect: str
    manifestation: str
    direction: str
    severity: str
    evidence: str
    confidence: float | None = None
    basis: str = "unknown"
    object_raw: str = ""
    aspect_raw: str = ""
    direction_raw: str = ""
    object_method: str = ""
    aspect_method: str = ""
    direction_method: str = ""
    normalization_ambiguous: bool = False
    normalization_ambiguous_fields: list[str] = field(default_factory=list)
    evidence_check: str = "failed"
    issue_id: str = ""
    sku_id: str = ""
    star: int | None = None
    date: str = ""
    model: str = ""
    prompt_version: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "review_id": self.review_id,
            "object": self.object,
            "object_raw": self.object_raw,
            "object_normalized": self.object,
            "object_method": self.object_method,
            "aspect": self.aspect,
            "aspect_raw": self.aspect_raw,
            "aspect_normalized": self.aspect,
            "aspect_method": self.aspect_method,
            "manifestation": self.manifestation,
            "direction": self.direction,
            "direction_raw": self.direction_raw,
            "direction_normalized": self.direction,
            "direction_method": self.direction_method,
            "normalization_ambiguous": self.normalization_ambiguous,
            "normalization_ambiguous_fields": self.normalization_ambiguous_fields,
            "severity": self.severity,
            "evidence": self.evidence,
            "evidence_check": self.evidence_check,
            "basis": self.basis,
            "confidence": self.confidence,
            "sku_id": self.sku_id,
            "star": self.star,
            "date": self.date,
            "model": self.model,
            "prompt_version": self.prompt_version,
        }


def make_issue_id(review_id: str, obj: str, aspect: str, manifestation: str, direction: str) -> str:
    raw = "|".join([review_id, obj, aspect, manifestation, direction])
    return "I" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def validate_issue(
    payload: dict[str, Any],
    review: dict[str, Any],
    model: str = "",
    prompt_version: str = "",
    profile: CategoryProfile | None = None,
) -> tuple[Issue | None, list[str]]:
    """校验单条模型输出；不合法返回 None + 错误列表。"""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return None, ["issue 不是对象"]

    obj = _clean(payload.get("object"))
    aspect_raw = _clean(payload.get("aspect"))
    manifestation = _clean(payload.get("manifestation"))
    evidence = _clean(payload.get("evidence"))

    if not obj:
        errors.append("object 为空")
    elif len(obj) > 30:
        errors.append("object 超过 30 字")
    if not manifestation:
        errors.append("manifestation 为空")
    elif len(manifestation) > 100:
        errors.append("manifestation 超过 100 字")
    if not evidence:
        errors.append("evidence 为空")
    elif len(evidence) > 200:
        errors.append("evidence 超过 200 字")

    check = check_evidence(evidence, review.get("text") or review.get("content") or "")
    if evidence and check == "failed":
        errors.append("evidence 无法在原文中定位")

    confidence = payload.get("confidence")
    if confidence is not None:
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            errors.append("confidence 不是数值")
            confidence = None

    if errors:
        return None, errors

    # 规范化统一走 normalization 模块：保留 raw、记录方法与歧义标记
    from .normalization import normalize_issue_labels

    normalized = normalize_issue_labels(
        {
            "object": payload.get("object"),
            "object_raw": payload.get("object_raw"),
            "aspect": payload.get("aspect"),
            "aspect_raw": payload.get("aspect_raw"),
            "direction": payload.get("direction"),
            "direction_raw": payload.get("direction_raw"),
            "manifestation": manifestation,
            "evidence": evidence,
        },
        profile,
    )
    obj_key = normalized["object"]
    aspect = normalized["aspect"]
    direction = normalized["direction"]
    issue = Issue(
        review_id=str(review.get("review_id") or ""),
        object=obj_key,
        object_raw=normalized["object_raw"],
        object_method=normalized["object_method"],
        aspect=aspect,
        aspect_raw=normalized["aspect_raw"],
        aspect_method=normalized["aspect_method"],
        manifestation=manifestation,
        direction=direction,
        direction_raw=normalized["direction_raw"],
        direction_method=normalized["direction_method"],
        normalization_ambiguous=normalized["normalization_ambiguous"],
        normalization_ambiguous_fields=list(normalized["normalization_ambiguous_fields"]),
        severity=normalize_severity(payload.get("severity")),
        evidence=evidence,
        evidence_check=check,
        basis=normalize_basis(payload.get("basis")),
        confidence=confidence,
        sku_id=str(review.get("sku_id") or ""),
        star=review.get("star"),
        date=str(review.get("date") or ""),
        model=model,
        prompt_version=prompt_version,
        issue_id=make_issue_id(str(review.get("review_id") or ""), obj_key, aspect, manifestation, direction),
    )
    return issue, []


def dedupe_issues(issues: list[Issue]) -> list[Issue]:
    """同一评论内按规范化问题去重，保留置信度更高的一条。"""
    best: dict[str, Issue] = {}
    for issue in issues:
        key = "|".join([issue.object, issue.aspect, issue.manifestation, issue.direction])
        current = best.get(key)
        if current is None or (issue.confidence or 0) > (current.confidence or 0):
            best[key] = issue
    return list(best.values())
