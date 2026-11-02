"""对象 / 方面 / 方向的标签规范化（v1.9）。

设计原则（与 v1.8 的 `object_key` 相比更严格）：

1. **保留原始值**：任何规范化都不覆盖模型原始输出，原始值写在 `*_raw`，
   规范化结果写在 `*` / `*_normalized`，并记录 `*_method`。
2. **只做可解释的确定性映射**：同义词表、品类规范名别名、方面关键词；
   不依赖模糊匹配，不针对商品 ID 硬编码。
3. **同义才合并，不同对象绝不合并**：`鞋底` 与 `鞋垫`、`包装` 与 `整体`、
   `尺码` 与 `鞋头` 都有显式测试守住；无法确定时保留原值并标 ambiguous。
4. **品类相关**：同一个词在不同品类含义不同（如「皮」在美妆指皮肤、
   在箱包指皮革），因此跨品类不做粗暴替换；命中其他品类词表时标 ambiguous。
5. **可审计**：返回 (值, 方法, 是否歧义)，评测可以按方法拆分统计。

本模块不改变 LLM 输出，也不改变人工 Gold；评测时对预测与 Gold 对称应用。
"""
from __future__ import annotations

import re
from typing import Any

from .categories import BASE_OBJECT_SYNONYMS, PROFILES, CategoryProfile, get_profile
from .issue_schema import (
    ASPECT_ALIASES,
    ASPECT_KEYWORDS,
    DIRECTION_ALIASES,
    DIRECTION_LABELS,
    EXTRA_ASPECTS,
    GENERIC_ASPECTS,
)

_CLEAN_RE = re.compile(r"\s+")
_NEGATIONS = ("不", "没", "无", "未", "非", "别", "勿")

# 品类内“规范名别名”：把品类配置里并存的两个规范名合并成一个。
# 只影响问题标签，不影响宣传主张的对象识别（claim_object_terms 未改）。
CANONICAL_OBJECT_ALIASES: dict[str, dict[str, str]] = {
    "beauty": {"面部": "皮肤", "脸": "皮肤", "肌肤": "皮肤"},
    "shoe": {"鞋跟": "后跟"},
}

# 方面口径规则：使用后不良反应按“功效与效果”计（与独立集人工标注口径一致）。
# 只在该品类、且原方面为“使用舒适性/其他”时生效；搓泥/粘腻/拔干等肤感不在此列。
CONTEXT_ASPECT_RULES: dict[str, tuple[dict[str, Any], ...]] = {
    "beauty": (
        {
            "from": ("使用舒适性", "其他"),
            "to": "功效与效果",
            "keywords": ("长痘", "起痘", "爆痘", "痘痘", "豆豆", "过敏", "烂脸", "泛红", "刺痛", "发炎"),
            "reason": "使用后皮肤不良反应属于功效/安全性后果，按功效与效果计",
        },
    ),
}


def _clean(value: Any) -> str:
    return _CLEAN_RE.sub(" ", str(value or "")).strip()


def _negated_at(text: str, start: int) -> bool:
    """检查关键词前 1–2 个字符是否有否定词（如“不掉色”“没过敏”）。"""
    prefix = text[max(0, start - 2) : start]
    return any(word in prefix for word in _NEGATIONS)


def _match_surface(value: str, canonical: str, synonyms: tuple[str, ...]) -> tuple[int, int]:
    """返回 (匹配长度, 结束位置)；未命中返回 (0, -1)。

    优先取最长的表面形式，避免“商品包装”被更短的“商品”抢走。
    """
    best_len = 0
    best_end = -1
    for word in (canonical, *synonyms):
        if not word:
            continue
        start = value.find(word)
        while start >= 0:
            if len(word) > best_len or (len(word) == best_len and start + len(word) > best_end):
                best_len = len(word)
                best_end = start + len(word)
            start = value.find(word, start + 1)
    return best_len, best_end


def normalize_object(
    raw: Any,
    profile: CategoryProfile | None = None,
    *,
    allow_other_category: bool = True,
) -> tuple[str, str, bool]:
    """对象规范化，返回 (规范对象, 方法, 是否歧义)。

    方法取值：exact / synonym / canonical_alias / generic_synonym /
    unmapped / unmapped_other_category / empty。
    """
    value = _clean(raw)
    if not value:
        return "未知", "empty", True
    profile = profile or get_profile("generic")
    aliases = CANONICAL_OBJECT_ALIASES.get(profile.key, {})

    matched: dict[str, tuple[int, int]] = {}
    for canonical, synonyms in profile.object_synonyms.items():
        length, end = _match_surface(value, canonical, tuple(synonyms))
        if length:
            matched[canonical] = (length, end)

    if matched:
        best_len = max(length for length, _ in matched.values())
        candidates = {
            canonical: position
            for canonical, (length, position) in matched.items()
            if length == best_len
        }
        # 同长度多个规范名：优先品类专属规范名，其次取在原文中更靠后的
        # （中文中心语通常在后，如“商品包装”→包装、“面部皮肤”→皮肤）
        canonical = max(
            candidates,
            key=lambda key: (key not in BASE_OBJECT_SYNONYMS, candidates[key]),
        )
        ambiguous = len(candidates) > 1
        target = aliases.get(canonical, canonical)
        method = "exact" if target == value else (
            "canonical_alias" if canonical != target else "synonym"
        )
        return target, method, ambiguous

    # 通用词表兜底（包装/物流/客服/价格等所有品类共用）
    generic_len = 0
    generic_canonical = ""
    for canonical, synonyms in BASE_OBJECT_SYNONYMS.items():
        length, _ = _match_surface(value, canonical, tuple(synonyms))
        if length > generic_len:
            generic_len = length
            generic_canonical = canonical
    if generic_canonical:
        target = aliases.get(generic_canonical, generic_canonical)
        return target, "generic_synonym", False

    # 命中其他品类词表：跨品类含义可能不同，保留原值并标歧义
    if allow_other_category:
        for key, other in PROFILES.items():
            if key == profile.key:
                continue
            for canonical, synonyms in other.object_synonyms.items():
                length, _ = _match_surface(value, canonical, tuple(synonyms))
                if length and canonical not in BASE_OBJECT_SYNONYMS:
                    return value, "unmapped_other_category", True

    return value, "unmapped", True


def normalize_aspect_label(
    raw: Any,
    profile: CategoryProfile | None = None,
    context_text: str = "",
) -> tuple[str, str, bool]:
    """方面规范化，返回 (规范方面, 方法, 是否歧义)。

    方法取值：exact / alias / category_keyword / generic_keyword /
    context_rule / fallback_other / empty。
    """
    value = _clean(raw)
    if not value:
        return "其他", "empty", True
    profile = profile or get_profile("generic")

    resolved = ""
    method = ""
    if value in profile.allowed_aspects:
        resolved, method = value, "exact"
    elif value in ASPECT_ALIASES:
        resolved, method = profile.preferred(ASPECT_ALIASES[value]), "alias"
    elif value in GENERIC_ASPECTS or value in EXTRA_ASPECTS:
        resolved, method = profile.preferred(value), "alias"
    else:
        # 关键词匹配：品类词表优先；多个命中时取最长关键词
        best_len = 0
        best_aspect = ""
        for words, aspect in tuple(profile.aspect_keywords) + tuple(ASPECT_KEYWORDS):
            for word in words:
                if word and word in value and len(word) > best_len:
                    best_len = len(word)
                    best_aspect = aspect
        if best_aspect:
            resolved, method = profile.preferred(ASPECT_ALIASES.get(best_aspect, best_aspect)), (
                "category_keyword" if profile.aspect_keywords else "generic_keyword"
            )

    ambiguous = False
    if not resolved:
        return "其他", "fallback_other", True

    # 上下文规则：使用后不良反应 → 功效与效果（品类限定，关键词带否定保护）
    for rule in CONTEXT_ASPECT_RULES.get(profile.key, ()):
        if resolved not in rule["from"]:
            continue
        haystack = f"{value} {context_text}"
        hit = False
        for word in rule["keywords"]:
            start = haystack.find(word)
            while start >= 0:
                if not _negated_at(haystack, start):
                    hit = True
                    break
                start = haystack.find(word, start + 1)
            if hit:
                break
        if hit:
            return rule["to"], "context_rule", ambiguous

    return resolved, method, ambiguous


def normalize_direction_label(raw: Any) -> tuple[str, str, bool]:
    """方向规范化，返回 (规范方向, 方法, 是否歧义)。

    方法取值：exact / alias / substring / negated / multi_match / other / empty。
    """
    value = _clean(raw)
    if not value:
        return "other", "empty", True
    key = value.lower().replace("-", "_").replace(" ", "_")
    if key in DIRECTION_LABELS:
        return key, "exact", False
    if key in DIRECTION_ALIASES:
        return DIRECTION_ALIASES[key], "alias", False

    hits: list[tuple[str, int, int, str]] = []
    for alias, canonical in DIRECTION_ALIASES.items():
        if not alias:
            continue
        start = value.find(alias)
        while start >= 0:
            hits.append((alias, start, start + len(alias), canonical))
            start = value.find(alias, start + 1)
    if not hits:
        return "other", "other", True

    hits.sort(key=lambda item: (len(item[0]), item[2]), reverse=True)
    alias, start, _end, canonical = hits[0]
    if _negated_at(value, start):
        return "other", "negated", True
    distinct = {canonical for _alias, _start, _end, canonical in hits}
    return canonical, "substring", len(distinct) > 1


def normalize_issue_labels(
    payload: dict[str, Any],
    profile: CategoryProfile | None = None,
) -> dict[str, Any]:
    """对一条问题（预测或 Gold 的 dict）做规范化，返回新 dict。

    不修改入参；原始值保留在 `*_raw`。Gold 与预测都走同一函数，保证对称。
    """
    profile = profile or get_profile("generic")
    context = " ".join(
        str(payload.get(key) or "")
        for key in ("manifestation", "evidence", "claim_text", "text")
    )
    object_raw = _clean(payload.get("object_raw") or payload.get("object"))
    aspect_raw = _clean(payload.get("aspect_raw") or payload.get("aspect"))
    direction_raw = _clean(payload.get("direction_raw") or payload.get("direction"))

    obj, obj_method, obj_ambiguous = normalize_object(object_raw, profile)
    aspect, aspect_method, aspect_ambiguous = normalize_aspect_label(
        aspect_raw, profile, context
    )
    direction, dir_method, dir_ambiguous = normalize_direction_label(direction_raw)

    ambiguous_fields = [
        name
        for name, flag in (
            ("object", obj_ambiguous),
            ("aspect", aspect_ambiguous),
            ("direction", dir_ambiguous),
        )
        if flag
    ]
    result = dict(payload)
    result.update(
        {
            "object_raw": object_raw,
            "object": obj,
            "object_normalized": obj,
            "object_method": obj_method,
            "aspect_raw": aspect_raw,
            "aspect": aspect,
            "aspect_normalized": aspect,
            "aspect_method": aspect_method,
            "direction_raw": direction_raw,
            "direction": direction,
            "direction_normalized": direction,
            "direction_method": dir_method,
            "normalization_ambiguous": bool(ambiguous_fields),
            "normalization_ambiguous_fields": ambiguous_fields,
        }
    )
    return result


# 缺陷签名：把“具体表现”映射到可比较的缺陷类型（确定性关键词，不用模型）。
# 用途：聚类时若两条问题的签名都非空且不相交，则禁止合并——
# 这是“长痘 vs 过敏”“空瓶 vs 分量少”这类高向量相似度但不同缺陷的唯一可靠防线。
# 签名是“缺陷类别”，不是严重度；同一缺陷的不同程度（没效果/效果一般）归同一签名。
DEFECT_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("efficacy_insufficient", ("没效果", "无效", "没用", "没作用", "一点作用", "效果差",
                               "效果一般", "感觉一般", "没什么用", "没啥用", "没一点效果",
                               "没什么效果", "没有啥效果", "没啥效果", "没见效", "没起效",
                               "效果不明显", "改善不明显", "没变化", "没消", "淡化慢",
                               "淡化无效", "一般般", "效果不行", "没有效果", "没有什么效果",
                               "一点效果没有", "一点效果都没有", "效果很差")),
    ("size_small", ("偏小", "太小", "小了", "好小", "没想象的大", "小一号")),
    ("size_large", ("偏大", "太大", "大了", "大一号")),
    ("quantity_short", ("一点点量", "分量少", "量少", "装一点点", "少得可怜", "只有一点")),
    ("empty_unit", ("空瓶", "空的", "里面没有", "没装")),
    ("texture_thin", ("跟水一样", "太稀", "水状", "很稀")),
    ("fake", ("假货", "假冒", "三无", "假一")),
    ("ad_mismatch", ("图不符", "图文不符", "与实物不符", "宣传不符", "和图")),
    ("brand_unknown", ("品牌不明", "牌子不明", "不知道是什么牌子", "不知道什么品牌")),
    ("false_claim", ("虚假宣传", "夸大宣传", "骗人")),
    ("acne", ("长痘", "爆痘", "豆豆", "痘痘越来越多", "起痘", "长了几颗", "痘越多", "越用痘")),
    ("allergy", ("过敏",)),
    ("irritation", ("刺痛", "泛红", "刺激", "发炎", "火辣")),
    ("skin_worse", ("烂脸", "更严重", "更加严重", "加重", "严重了", "皮肤变差", "变差", "烂了")),
    ("sticky", ("黏腻", "粘腻", "油腻")),
    ("dry", ("拔干", "干燥", "紧绷")),
    ("odor", ("刺鼻", "异味", "臭", "味道大")),
    ("upsell", ("推荐", "加购", "配套", "推销", "捆绑", "套路", "再买", "买5套",
                "买几套", "套餐", "转微商", "其他产品", "顾问", "护肤老师", "又花了",
                "让一直买", "购买三四瓶", "又让你买", "引导消费", "诱导消费", "其他平台消费")),
    ("harassment", ("骚扰", "轰炸", "推送广告", "天天发")),
    ("follow_up", ("回访", "反复询问", "反复问")),
    ("attitude", ("态度",)),
    ("after_sales", ("售后", "不给处理", "拖着", "推脱", "不承认", "不给退")),
    ("disappeared", ("消失", "不理人", "不管", "不回复")),
    ("privacy", ("泄露", "电话号码")),
    ("dishonest", ("没一句真话", "说话一套", "不实", "忽悠", "骗")),
    ("price_unworthy", ("不值", "贵", "性价比")),
    ("partition", ("分区", "隔层", "不顺手", "放东西")),
    ("stitching", ("针码", "走线", "线头", "缝线", "开线")),
    ("material_quality", ("质量差", "面料差", "材质差", "质量很差", "品质一般")),
    ("wrong_location", ("送错", "没送到指定", "送错地方")),
    ("not_delivered", ("没到", "未收到", "没收到", "货也没")),
    ("taste_bland", ("味淡", "香味淡", "没味道", "太淡", "有点淡", "偏淡", "淡了")),
    ("taste_bad", ("不好吃", "难吃", "变味")),
)


DEFECT_LABELS = {
    "efficacy_insufficient": "效果不佳",
    "size_small": "偏小",
    "size_large": "偏大",
    "quantity_short": "分量不足",
    "empty_unit": "空瓶少件",
    "texture_thin": "质地太稀",
    "fake": "疑似假货",
    "ad_mismatch": "宣传不符",
    "brand_unknown": "品牌不明",
    "false_claim": "虚假宣传",
    "acne": "长痘",
    "allergy": "过敏",
    "irritation": "刺激不适",
    "skin_worse": "皮肤变差",
    "sticky": "黏腻",
    "dry": "干燥紧绷",
    "odor": "异味",
    "upsell": "诱导加购",
    "harassment": "骚扰推销",
    "follow_up": "频繁回访",
    "attitude": "态度差",
    "after_sales": "售后推诿",
    "disappeared": "卖后失联",
    "privacy": "泄露隐私",
    "dishonest": "说话不实",
    "price_unworthy": "不值",
    "partition": "分区不合理",
    "stitching": "针脚问题",
    "material_quality": "质量差",
    "wrong_location": "送错地点",
    "not_delivered": "未送达",
    "taste_bland": "味道偏淡",
    "taste_bad": "不好吃",
}


def defect_signatures(manifestation: Any) -> set[str]:
    """返回该表现命中的缺陷签名集合；未命中返回空集（未知缺陷，不做限制）。"""
    text = _clean(manifestation)
    if not text:
        return set()
    hits: set[str] = set()
    for signature, words in DEFECT_SIGNATURES:
        if any(word and word in text for word in words):
            hits.add(signature)
    return hits


def primary_defect_signature(manifestation: Any) -> str | None:
    """主缺陷签名：取在文本中出现位置最靠前、同位置取最长关键词的签名。

    一条评论可能同时提到多个缺陷（“长痘还烂脸”），主签名用于判断两条
    问题是否在讲同一个具体缺陷，避免“多签名有交集”把不同缺陷串起来。
    销售/联系方式只是失联或隐私问题的背景时，优先保留后者这一具体后果。
    """
    text = _clean(manifestation)
    if not text:
        return None
    signatures = defect_signatures(text)
    if signatures & {"upsell", "dishonest", "harassment"}:
        outcomes = signatures & {"privacy", "disappeared"}
        if outcomes:
            return min(
                outcomes,
                key=lambda signature: min(
                    text.find(word) for name, words in DEFECT_SIGNATURES if name == signature
                    for word in words if word in text
                ),
            )
    best: tuple[int, int, str] | None = None
    for signature, words in DEFECT_SIGNATURES:
        for word in words:
            if not word:
                continue
            position = text.find(word)
            if position < 0:
                continue
            candidate = (position, -len(word), signature)
            if best is None or candidate < best:
                best = candidate
    return best[2] if best else None


def signatures_compatible(left: Any, right: Any) -> bool:
    """两条表现的主缺陷是否相同：任一未知则不限制，都已知必须相同。"""
    a = primary_defect_signature(left)
    b = primary_defect_signature(right)
    if not a or not b:
        return True
    return a == b


def normalization_summary(issues: list[dict[str, Any]]) -> dict[str, Any]:
    """按规范化方法统计，用于报告“哪些匹配依赖规范化”。"""
    methods = {"object": {}, "aspect": {}, "direction": {}}
    ambiguous = 0
    for issue in issues:
        for field in ("object", "aspect", "direction"):
            method = str(issue.get(f"{field}_method") or "")
            bucket = methods[field]
            bucket[method] = bucket.get(method, 0) + 1
        if issue.get("normalization_ambiguous"):
            ambiguous += 1
    return {"methods": methods, "ambiguous_count": ambiguous, "issue_count": len(issues)}
