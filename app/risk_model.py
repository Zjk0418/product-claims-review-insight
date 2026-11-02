"""宣传落差的风险指数模型（工程启发式，非概率）。

risk-v2 相对 risk-v1 的变化（对应审查意见 P0–P2）：

1. 证据分类从 3 类扩为 5 类：``contradiction``（明确矛盾）、
   ``related_negative``（具体负面但未构成直接矛盾）、``topic_only``（仅提及属性）、
   ``support``（具体支持）、``insufficient``（无法判断）。仅提及不加分，
   避免“关键词命中”推动风险。
2. 事实具体性与可核验性分开：``has_media`` 不再自动把评论提升为“具体事实”；
   引用能否与原文逐字对上单独记录为 ``quote_status``，并作为权重系数。
3. 聚合从指数饱和 ``1-e^{-kW}`` 改为 Hill 型“佐证曲线” ``W^n/(K^n+W^n)``：
   单条证据只能形成线索，需要多条独立证据才进入高风险区间；仍保留边际递减。
4. 证据充分性拆成 ``multi_source / preliminary / limited / none``，输出理由与
   未覆盖项；未采集 4–5 星正文（反向证据）时封顶 ``preliminary``。
5. 严重程度不再等同于真实性：安全相关描述单独触发 ``safety_alert``，
   不并入指数；“问题是否成立”与“问题有多严重”分开输出。
6. 没有可计分证据时返回 ``None`` 并给出原因，不写成 0。

以下参数均为**未校准的工程启发式**，不是统计估计：
- 事实具体性权重、严重程度权重、关联/支持权重、Hill 的 n 与 K、支持抵扣上限；
- 充分性阈值与分档阈值。
这些参数集中在 :data:`PARAMS`，版本号随模型输出，可用人工标注数据重新拟合。
``assess(..., model_version="v1")`` 保留旧算法，供对照与复现。

本模块不读配置、不访问网络、不调用模型，只做纯函数计算。
"""

from __future__ import annotations

import math
import re
from typing import Any

# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------

PARAMS: dict[str, Any] = {
    "version": "risk-v2",
    # 评论事实具体性：是否给出可核查的细节（不等于真实性）
    "quality_weight": {"concrete": 1.0, "partial": 0.62, "vague": 0.3, "unknown": 0.5},
    # 问题一旦成立对消费者的影响；区间比 v1 收窄，避免单条评论被严重度放大
    "severity_weight": {"critical": 1.35, "major": 1.15, "moderate": 1.0,
                        "minor": 0.8, "unknown": 0.9},
    # 证据与宣传承诺的关系权重
    "relation_weight": {
        "contradiction": 1.0,
        "related_negative": 0.3,
        "topic_only": 0.0,
        "support": 0.25,
        "insufficient": 0.0,
    },
    # 引用与原文的核对状态系数：逐字可核对才按满权重计
    "quote_status_weight": {
        "verbatim": 1.0,
        "original_review": 0.9,
        "paraphrase": 0.6,
        "unknown": 1.0,
    },
    # 支持性证据最多下调 30%（v1 为 35%，上限同为未校准参数）
    "support_cap": 0.3,
    "support_decay": 0.35,
    # Hill 佐证曲线：W^n / (K^n + W^n)
    "hill_n": 1.4,
    "hill_k": 3.0,
    # 只有关联、没有明确矛盾时的上限
    "indirect_only_cap": 30.0,
    # 分档
    "band_high": 65.0,
    "band_medium": 35.0,
    # 证据充分性
    "sufficiency": {
        "preliminary_min_units": 3,
        "preliminary_min_quality": 0.55,
        "multi_source_min_units": 5,
        "multi_source_min_quality": 0.65,
        "multi_source_min_quote_rate": 0.8,
    },
}

# risk-v1 原始参数，仅用于复算对照，不参与新报告的主评分
PARAMS_V1: dict[str, Any] = {
    "version": "risk-v1",
    "quality_weight": {"concrete": 1.0, "partial": 0.62, "vague": 0.3, "unknown": 0.5},
    "severity_weight": {"critical": 1.5, "major": 1.28, "moderate": 1.0,
                        "minor": 0.7, "unknown": 0.85},
    "indirect_weight": 0.3,
    "support_weight": 0.25,
    "support_cap": 0.35,
    "saturation_k": 0.55,
    "indirect_only_cap": 30.0,
    "band_high": 65.0,
    "band_medium": 35.0,
    "sufficient_min_units": 3,
    "sufficient_min_quality": 0.6,
}

RELATION_LABELS = {
    "contradiction": "明确矛盾",
    "related_negative": "具体关联",
    "topic_only": "仅提及属性",
    "support": "支持性证据",
    "insufficient": "无法判断",
    # v1 兼容标签
    "partial": "间接关联",
    "unrelated": "无关",
    "unknown": "无法判断",
}
QUALITY_LABELS = {"concrete": "具体事实", "partial": "部分细节",
                  "vague": "笼统表述", "unknown": "未判断"}
SEVERITY_LABELS = {"critical": "重大（安全/核心功能）", "major": "明显影响使用",
                   "moderate": "一般问题", "minor": "轻微体验差异",
                   "unknown": "未分级"}
SUFFICIENCY_LABELS = {
    "multi_source": "多源支持",
    "preliminary": "初步",
    "limited": "有限",
    "none": "未形成",
    # v1 兼容
    "sufficient": "充分（v1 口径）",
    "insufficient": "不足（v1 口径）",
}
BAND_LABELS = {"high": "高", "medium": "中", "low": "低", "none": "暂未形成指数"}

# ---------------------------------------------------------------------------
# 文本启发式
# ---------------------------------------------------------------------------

# 具体事实的线索：数字、成分/材质、时间、尺度、可核对对象
_CONCRETE_HINTS = re.compile(
    r"\d|厘米|cm|毫米|mm|克|g\b|毫升|ml|天|周|个月|小时|分钟|次|"
    r"成分|标签|材质|纯棉|涤纶|聚酯|羊毛|皮革|不锈钢|"
    r"起球|掉色|开线|漏水|断裂|异味|过敏|红肿|刺痒|褪色|变形|缩水|"
    r"色差|偏大|偏小|尺码|型号|批次|生产日期|保质期",
    re.I,
)
# 笼统差评：只有结论，没有事实
_VAGUE_PATTERNS = re.compile(
    r"^(质量不好|质量差|一般般?|不推荐|失望|很差|不好用|垃圾|踩雷|不怎么样|还行|无语)[。！!…\s]*$"
)

# 明确否定“缺陷存在”的说法，先剔除，避免“没有掉色”被当成“掉色”
_NEGATED_DEFECT = re.compile(
    r"(?:没有|没|不|未|无)(?:出现|发生|任何|什么)?"
    r"(?:掉色|褪色|起球|缩水|变形|开线|脱线|漏水|漏液|异味|发霉|过敏|红肿|刺痒|"
    r"断裂|破损|掉漆|生锈|色差|磨脚|扎人)",
    re.I,
)
# 直接缺陷 / 负面事实词
_NEGATIVE_TERMS = re.compile(
    r"差评|差劲|太差|很差|不好|不行|不满意|失望|后悔|踩雷|翻车|退货|退款|投诉|"
    r"掉色|褪色|起球|缩水|变形|开线|脱线|脱胶|开胶|漏水|漏液|漏气|不密封|"
    r"异味|臭味|发霉|过期|变质|破损|压坏|划痕|掉漆|生锈|断裂|裂了|破了|坏了|"
    r"过敏|红肿|刺痒|瘙痒|疼痛|脱皮|爆痘|长痘|烂脸|刺激|"
    r"色差|偏大|偏小|偏色|不对称|线头|毛边|粗糙|扎人|磨脚|硌脚|"
    r"太紧|太松|太硬|太软|太薄|太厚|太重|太轻|太大|太小|太贵|太慢|太少|"
    r"有点薄|有点硬|有点小|有点大|有点紧|有点松|有点贵|有点糙|"
    r"不划算|不值|性价比低|缺件|少发|漏发|发错|"
    r"没效果|没有效果|无效|没用|不管用|不吸收|不持久|不防晒|不防滑|不防水|"
    r"卡顿|死机|闪退|黑屏|花屏|失灵|不灵敏|异响|噪音|发烫|过热|"
    r"掉电快|续航短|充不进电|吸不住|粘不住|不粘|不牢|松动|晃动",
    re.I,
)
# “不 + 正向形容词”同样构成负面事实
# “不 + 正向形容词/评价”同样构成负面事实（不顺手、不合理、不耐用……）
_NEGATED_POSITIVE = re.compile(
    r"不(?:是|太|够|怎么|很|甚)?(?:好|行|对|满意|舒服|舒适|合适|合身|合脚|耐用|结实|牢固|"
    r"稳|平整|匀|均匀|新鲜|干净|整齐|清晰|准确|准|足|够|到位|达标|如|值|划算|推荐|"
    r"喜欢|明显|理想|符合|一致|一样|对版|正品|顺手|方便|实用|透气|防滑|防水|"
    r"持久|耐脏|好洗|好闻|好吃|好喝|好用|顺滑|细腻|服帖|显瘦|好看|香|亮|响|灵|"
    r"热|凉快|暖和|合理|协调|对称|饱满|挺括|柔软|轻便|省力|省电|静音)",
    re.I,
)
# 规格/数量与宣传不符：不一定含负面词，但属于可核对的反驳
_QUANTITY_MISMATCH = re.compile(
    r"(?:只有|仅有|才|实际只有|缩水到|不到)\s*\d+(?:\.\d+)?\s*"
    r"(?:ml|毫升|l|升|g|克|kg|千克|cm|厘米|mm|毫米|片|粒|包|瓶|支|件)?",
    re.I,
)
# 正向表达，用于排除“不错”“不贵”等假阴性
_POSITIVE_EXCEPTIONS = re.compile(r"不错|不贵|不差|不赖|不闷|不勒|不掉|不滑|不重|不厚|不粘手")

_SEVERITY_HINTS = (
    ("critical", re.compile(r"过敏|红肿|刺痒|灼伤|漏电|起火|爆炸|中毒|安全隐患|受伤|医疗|烂脸", re.I)),
    ("major", re.compile(r"断裂|漏水|漏液|漏气|开胶|脱胶|无法使用|不能用|坏了|失效|退货|退款|发霉|异味|"
                         r"起球|掉色|褪色|缩水|变形|色牢度|没效果|没有效果|"
                         r"无改善|没改善|没有改善|没淡|没变淡|没有变淡|没去掉|没有去掉|"
                         r"根本没用|完全没用|没啥用|智商税|刺激|泛红|破了|破洞|破损|辱骂|威胁|拉黑|欺骗|诈骗|"
                         r"疼|痛|顶脚|挤脚|磨破|破皮|烂了|烂掉|开裂|不透气|不咋透气|闷脚|闷汗|"
                         r"掉跟|不跟脚|滑倒|摔", re.I)),
    ("moderate", re.compile(r"线头|毛边|做工|粗糙|僵硬|走线|开线|脱线|异味|臭味|发黄|泛黄|"
                            r"质量差|质量不好|品质差|诱导|骚扰|态度差|不给退|不处理|推诿|敷衍|针脚|"
                            r"不合身|版型不对|不整齐|不对称", re.I)),
    ("minor", re.compile(r"色差|偏大|偏小|包装|物流|有点|稍微|略微|不太|不咋", re.I)),
)

_RELATION_ALIASES = {
    "contradiction": "contradiction",
    "contradict": "contradiction",
    "conflict": "contradiction",
    "related_negative": "related_negative",
    "negative": "related_negative",
    "partial": "related_negative",
    "indirect": "related_negative",
    "topic_only": "topic_only",
    "topic": "topic_only",
    "mention": "topic_only",
    "support": "support",
    "supporting": "support",
    "insufficient": "insufficient",
    "unrelated": "insufficient",
    "unknown": "insufficient",
    "": "insufficient",
}
_RELATION_RANK = {"insufficient": 0, "topic_only": 1, "support": 2,
                  "related_negative": 3, "contradiction": 4}
_SEVERITY_RANK = {"unknown": 0, "minor": 1, "moderate": 2, "major": 3, "critical": 4}


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def has_negative_fact(text: str) -> bool:
    """判断文本是否包含具体的不利事实，而不是只提到某个属性。

    这是规则兜底，不是语义理解：先剔除“没有掉色”这类否定缺陷的说法，
    再匹配缺陷词与“不+正向词”。宁可漏判（不计分），也不把主题提及当证据。
    """
    body = _text(text)
    if not body:
        return False
    cleaned = _NEGATED_DEFECT.sub("", body)
    cleaned = _POSITIVE_EXCEPTIONS.sub("", cleaned)
    return bool(_NEGATIVE_TERMS.search(cleaned)
                or _NEGATED_POSITIVE.search(cleaned)
                or _QUANTITY_MISMATCH.search(cleaned))


def quality_of(text: str, has_media: bool = False, provided: str | None = None) -> str:
    """判断一条证据的事实具体程度。

    ``has_media`` 仅为兼容旧调用保留：**带图不再自动算“具体事实”**。
    图片是否存在与图片是否真的支持该缺陷是两件事，未经视觉核验不加分。
    """
    if provided in {"concrete", "partial", "vague"}:
        return provided
    body = _text(text)
    if not body:
        return "unknown"
    if _VAGUE_PATTERNS.match(body) or len(body) < 8:
        return "vague"
    hits = len(_CONCRETE_HINTS.findall(body))
    if hits >= 2 or (len(body) >= 24 and hits >= 1):
        return "concrete"
    if len(body) >= 12 or hits >= 1:
        return "partial"
    return "vague"


def severity_of(text: str, provided: str | None = None) -> str:
    if provided in SEVERITY_LABELS and provided != "unknown":
        return provided
    # 先剔除“没有过敏/不起球”这类否定缺陷的说法，避免把“没过敏”判成重大
    body = _NEGATED_DEFECT.sub("", _text(text))
    for level, pattern in _SEVERITY_HINTS:
        if pattern.search(body):
            return level
    return "unknown"


def relation_of(value: Any, text: str = "") -> str:
    """把模型标签或旧标签归一为 v2 的五类关系。

    ``partial`` / ``indirect`` 只有在文本确实包含具体负面事实时才升为
    ``related_negative``；否则降为 ``topic_only``，不参与评分。
    """
    raw = _text(value).strip().lower()
    relation = _RELATION_ALIASES.get(raw, "insufficient")
    body = _text(text)
    if relation == "contradiction":
        # 明确矛盾是模型给出的直接判断；只有文本为空时才降级，
        # 笼统与否交给事实具体性权重处理，不能把“质量不好”当成主题提及。
        if not body:
            return "insufficient"
        return "contradiction"
    if relation == "related_negative" and not has_negative_fact(text):
        return "topic_only"
    return relation


def _normalize(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?~…\-—\"'“”‘’]+", "", _text(text))


def _bigrams(text: str) -> set[str]:
    value = re.sub(r"[\s\W_]+", "", _text(text))
    return {value[i: i + 2] for i in range(len(value) - 1)}


def _similar(left: str, right: str, threshold: float = 0.9) -> bool:
    a, b = _bigrams(left), _bigrams(right)
    if not a or not b:
        return _normalize(left) == _normalize(right)
    return len(a & b) / len(a | b) >= threshold


def dedupe_units(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一评论（含追评）只算一个证据单元；近重复文本也只保留权重最高的一条。"""
    by_id: dict[str, dict[str, Any]] = {}
    for unit in units:
        review_id = _text(unit.get("review_id"))
        if not review_id:
            continue
        current = by_id.get(review_id)
        if current is None:
            by_id[review_id] = dict(unit)
            continue
        # 同一评论可能来自初评与追评：合并文本、保留更强的证据关系
        current["text"] = (current.get("text", "") + " " + unit.get("text", "")).strip()
        if _RELATION_RANK.get(unit.get("relation"), 0) > _RELATION_RANK.get(current.get("relation"), 0):
            current["relation"] = unit.get("relation")
        if _SEVERITY_RANK.get(unit.get("severity"), 0) > _SEVERITY_RANK.get(current.get("severity"), 0):
            current["severity"] = unit.get("severity")
        current["quality"] = quality_of(current["text"], bool(current.get("has_media")))
        current["appended"] = True

    kept: list[dict[str, Any]] = []
    seen_text: dict[str, dict[str, Any]] = {}
    for unit in by_id.values():
        norm = _normalize(unit.get("text", ""))
        if not norm:
            continue
        duplicate = seen_text.get(norm)
        if duplicate is not None:
            if _unit_weight(unit) > _unit_weight(duplicate):
                seen_text[norm] = unit
            continue
        seen_text[norm] = unit
        kept.append(unit)

    # 近重复（同模板刷评 / 复制粘贴）按文本相似度合并，只保留权重更高的一条
    unique: list[dict[str, Any]] = []
    for unit in kept:
        for index, existing in enumerate(unique):
            left, right = unit.get("text", ""), existing.get("text", "")
            # 只对较长文本做近重复合并：短评论本来就可能措辞相同，
            # 不能因为都写“质量太差”就当成同一个证据。
            if len(left) >= 24 and len(right) >= 24 and _similar(left, right, 0.94):
                if _unit_weight(unit) > _unit_weight(existing):
                    unique[index] = unit
                break
        else:
            unique.append(unit)
    return unique


def _quote_factor(unit: dict[str, Any]) -> float:
    status = _text(unit.get("quote_status")) or "unknown"
    return PARAMS["quote_status_weight"].get(status, 1.0)


def _quality_value(unit: dict[str, Any]) -> float:
    return PARAMS["quality_weight"].get(unit.get("quality") or "unknown", 0.5)


def _unit_weight(unit: dict[str, Any]) -> float:
    relation = unit.get("relation") or "insufficient"
    quality = _quality_value(unit) * _quote_factor(unit)
    if relation == "contradiction":
        severity = PARAMS["severity_weight"].get(unit.get("severity") or "unknown", 0.9)
        return quality * severity
    if relation == "related_negative":
        return quality * PARAMS["relation_weight"]["related_negative"]
    if relation == "support":
        # 无实质内容的好评不能抵扣风险
        if unit.get("quality") == "vague":
            return 0.0
        return quality * PARAMS["relation_weight"]["support"]
    return 0.0


def _corroboration(total: float) -> float:
    """Hill 曲线：少量证据增长慢，多条独立证据才接近上限。"""
    if total <= 0:
        return 0.0
    n = PARAMS["hill_n"]
    k = PARAMS["hill_k"]
    powered = total ** n
    return powered / (k ** n + powered)


def _saturate_v1(total: float) -> float:
    return 1.0 - math.exp(-PARAMS_V1["saturation_k"] * max(0.0, total))


def _prepare(units: list[dict[str, Any]], model_version: str = "v2") -> list[dict[str, Any]]:
    """补齐关系、具体性与严重程度：模型没给时用评论原文做启发式判断。"""
    prepared = []
    for unit in units:
        row = dict(unit)
        text = _text(row.get("text"))
        row["quality"] = quality_of(text, bool(row.get("has_media")), row.get("quality"))
        row["severity"] = severity_of(text, row.get("severity"))
        if model_version == "v2":
            row["relation"] = relation_of(row.get("relation"), text)
            row["negative_fact"] = has_negative_fact(text)
            row["quote_status"] = _text(row.get("quote_status")) or "unknown"
        else:
            # v1 只有 contradiction / partial / support 三类；
            # 新版新增的 topic_only 不属于旧口径，不能悄悄并入 partial。
            legacy_relation = row.get("relation")
            if legacy_relation in {"related_negative", "partial", "indirect"}:
                legacy_relation = "partial"
            elif legacy_relation == "topic_only":
                legacy_relation = "insufficient"
            row["relation"] = legacy_relation
        prepared.append(row)
    return prepared


def _band(risk_index: float | None, params: dict[str, Any] | None = None) -> str:
    settings = params or PARAMS
    if risk_index is None:
        return "none"
    if risk_index >= settings["band_high"]:
        return "high"
    if risk_index >= settings["band_medium"]:
        return "medium"
    return "low"


def _max_severity(units: list[dict[str, Any]]) -> str:
    best = "unknown"
    for unit in units:
        current = unit.get("severity") or "unknown"
        if _SEVERITY_RANK.get(current, 0) > _SEVERITY_RANK.get(best, 0):
            best = current
    return best


def _public_evidence(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for unit in units:
        rows.append({
            "review_id": _text(unit.get("review_id")),
            "relation": unit.get("relation"),
            "relation_label": RELATION_LABELS.get(unit.get("relation") or "", ""),
            "quality": unit.get("quality"),
            "quality_label": QUALITY_LABELS.get(unit.get("quality") or "", ""),
            "severity": unit.get("severity"),
            "severity_label": SEVERITY_LABELS.get(unit.get("severity") or "", ""),
            "quote_status": unit.get("quote_status") or "unknown",
            "negative_fact": bool(unit.get("negative_fact")),
            "star": unit.get("star"),
            "sku": unit.get("sku") or "",
            "claim_id": unit.get("claim_id") or "",
            "text": _text(unit.get("text"))[:300],
        })
    return rows


def _sufficiency(
    contradictions: list[dict[str, Any]],
    related: list[dict[str, Any]],
    supports: list[dict[str, Any]],
    topic_only: list[dict[str, Any]],
    *,
    scanned_count: int,
    effective_review_count: int,
    coverage_state: str,
    reverse_evidence_covered: bool,
) -> tuple[str, list[str], dict[str, Any]]:
    """证据充分性：与风险强度分开，输出可解释的理由和未覆盖项。"""
    settings = PARAMS["sufficiency"]
    n = len(contradictions)
    qualities = [_quality_value(unit) * _quote_factor(unit) for unit in contradictions]
    average = round(sum(qualities) / len(qualities), 3) if qualities else 0.0
    verified = sum(1 for unit in contradictions if unit.get("quote_status") in {"verbatim", "original_review"})
    checked = sum(1 for unit in contradictions
                  if unit.get("quote_status") in {"verbatim", "original_review", "paraphrase"})
    quote_rate = round(verified / checked, 3) if checked else None
    signals = {
        "independent_contradictions": n,
        "independent_related_negative": len(related),
        "support_units": len(supports),
        "topic_only_units": len(topic_only),
        "average_specificity": average,
        "quote_verified_rate": quote_rate,
        "coverage_state": coverage_state,
        "scan_complete": coverage_state == "full",
        "reverse_evidence_covered": bool(reverse_evidence_covered),
        "scanned_review_count": int(scanned_count or 0),
        "effective_review_count": int(effective_review_count or 0),
    }
    reasons: list[str] = []
    if n == 0 and not related:
        reasons.append("没有可计分的矛盾或具体关联证据")
        if topic_only:
            reasons.append(f"{len(topic_only)} 条仅提及相关属性，未计分")
        return "none", reasons, signals

    level = "limited"
    if n >= settings["preliminary_min_units"] and average >= settings["preliminary_min_quality"]:
        level = "preliminary"
    if (
        n >= settings["multi_source_min_units"]
        and average >= settings["multi_source_min_quality"]
        and quote_rate is not None
        and quote_rate >= settings["multi_source_min_quote_rate"]
        and coverage_state == "full"
        and reverse_evidence_covered
    ):
        level = "multi_source"

    if n:
        reasons.append(f"{n} 条独立评论构成明确矛盾")
        reasons.append(f"平均事实具体性 {average:.2f}")
        if quote_rate is None:
            reasons.append("未返回逐条引用，无法逐字核对")
        elif quote_rate < 0.8:
            reasons.append("部分引用未能与原文逐字核对")
    if related:
        reasons.append(f"{len(related)} 条具体负面但未构成直接矛盾，仅作辅助")
    if topic_only:
        reasons.append(f"{len(topic_only)} 条仅提及相关属性，未计分")
    if not reverse_evidence_covered:
        reasons.append("未采集 4–5 星正文，反向证据未覆盖")
    if coverage_state == "bounded":
        reasons.append("采集范围有明确上限，仅覆盖部分评论（区间已知，非中断）")
    elif coverage_state != "full":
        reasons.append("采集未完成或状态未知，覆盖范围不确定")

    # 封顶：没有反向证据时，不宣称“多源支持”
    if level == "multi_source" and not reverse_evidence_covered:
        level = "preliminary"
    # 有明确上限（扫描上限/时间范围/样本不足）时，可以到“初步”但不能到“多源支持”
    if level == "multi_source" and coverage_state != "full":
        level = "preliminary"
    # 中断或未知：覆盖范围本身不确定，最高“有限”
    if level in {"multi_source", "preliminary"} and coverage_state in {"interrupted", "unknown"}:
        level = "limited"
    if level in {"multi_source", "preliminary"} and quote_rate is not None and quote_rate < 0.5 and n:
        level = "limited"
        reasons.append("可逐字核对的引用比例过低，降为有限")
    return level, reasons, signals


def assess(
    units: list[dict[str, Any]],
    *,
    scanned_count: int = 0,
    effective_review_count: int = 0,
    scan_complete: bool = False,
    coverage_state: str | None = None,
    reverse_evidence_covered: bool = False,
    model_version: str = "v2",
) -> dict[str, Any]:
    """按宣传维度聚合证据，输出风险指数与证据充分性。

    units: ``[{review_id, relation, text, severity?, quality?, quote_status?,
    has_media?, star?, sku?, date?, claim_id?}]``。
    ``relation`` 取 contradiction / related_negative / topic_only / support /
    insufficient，兼容旧标签 partial / indirect / unrelated。
    """
    if model_version == "v1":
        return assess_v1(
            units,
            scanned_count=scanned_count,
            effective_review_count=effective_review_count,
            scan_complete=scan_complete,
        )

    if coverage_state not in {"full", "bounded", "interrupted", "unknown"}:
        coverage_state = "full" if scan_complete else "unknown"

    independent = dedupe_units(_prepare(units, "v2"))
    contradictions = [u for u in independent if u.get("relation") == "contradiction"]
    related = [u for u in independent if u.get("relation") == "related_negative"]
    supports = [u for u in independent if u.get("relation") == "support"]
    topic_only = [u for u in independent if u.get("relation") == "topic_only"]
    insufficient = [u for u in independent if u.get("relation") == "insufficient"]

    weight_contradiction = sum(_unit_weight(u) for u in contradictions)
    weight_related = sum(_unit_weight(u) for u in related)
    weight_support = sum(_unit_weight(u) for u in supports)
    negative_mass = weight_contradiction + weight_related

    raw = _corroboration(negative_mass) if negative_mass > 0 else 0.0
    support_factor = 1.0 - PARAMS["support_cap"] * (
        1.0 - math.exp(-PARAMS["support_decay"] * weight_support)
    )
    score = raw * support_factor

    capped = False
    if not contradictions and related:
        limit = PARAMS["indirect_only_cap"] / 100.0
        if score > limit:
            score, capped = limit, True

    has_scoring_evidence = bool(contradictions or related)
    risk_index = round(score * 100, 1) if has_scoring_evidence else None
    band = _band(risk_index)
    severity = _max_severity(contradictions or related)
    severity_distribution = {"critical": 0, "major": 0, "moderate": 0, "minor": 0, "unknown": 0}
    for unit in contradictions + related:
        key = unit.get("severity") if unit.get("severity") in severity_distribution else "unknown"
        severity_distribution[key] += 1
    safety_alert = severity == "critical" and has_scoring_evidence
    sufficiency, reasons, signals = _sufficiency(
        contradictions, related, supports, topic_only,
        scanned_count=scanned_count,
        effective_review_count=effective_review_count,
        coverage_state=coverage_state,
        reverse_evidence_covered=reverse_evidence_covered,
    )

    structure = {
        "contradiction": len(contradictions),
        "related_negative": len(related),
        "topic_only": len(topic_only),
        "support": len(supports),
        "insufficient": len(insufficient),
        "scored": len(contradictions) + len(related),
    }
    return {
        "risk_index": risk_index,
        "risk_band": band,
        "risk_band_label": BAND_LABELS[band],
        "risk_model_version": PARAMS["version"],
        "risk_index_calibrated": False,
        "evidence_sufficiency": sufficiency,
        "evidence_sufficiency_label": SUFFICIENCY_LABELS[sufficiency],
        "sufficiency_reasons": reasons,
        "sufficiency_signals": signals,
        "severity": severity,
        "severity_label": SEVERITY_LABELS.get(severity, "未分级"),
        "severity_distribution": severity_distribution,
        "safety_alert": safety_alert,
        "contradiction_count": len(contradictions),
        "related_negative_count": len(related),
        "partial_count": len(related),  # 兼容旧字段名，含义为“具体关联”
        "support_count": len(supports),
        "topic_only_count": len(topic_only),
        "relation_structure": structure,
        "contradiction_review_ids": sorted({_text(u.get("review_id")) for u in contradictions}),
        "partial_review_ids": sorted({_text(u.get("review_id")) for u in related}),
        "support_review_ids": sorted({_text(u.get("review_id")) for u in supports}),
        "evidence_units": independent,
        "risk_evidence": _public_evidence(contradictions + related + supports),
        "null_reason": None if has_scoring_evidence else "no_scoring_evidence",
        "calculation": {
            "params_version": PARAMS["version"],
            "weight_contradiction": round(weight_contradiction, 4),
            "weight_related_negative": round(weight_related, 4),
            "weight_partial": round(weight_related, 4),  # 兼容旧字段
            "weight_support": round(weight_support, 4),
            "negative_mass": round(negative_mass, 4),
            "raw_score": round(raw * 100, 2),
            "support_factor": round(support_factor, 4),
            "indirect_only_capped": capped,
            "aggregation": "hill",
            "hill_n": PARAMS["hill_n"],
            "hill_k": PARAMS["hill_k"],
            "formula": "risk = 100 × W^n/(K^n+W^n) × 支持抵扣；W 为矛盾与具体关联的加权证据量；仅关联时封顶",
            "calibrated": False,
        },
    }


def assess_v1(
    units: list[dict[str, Any]],
    *,
    scanned_count: int = 0,
    effective_review_count: int = 0,
    scan_complete: bool = False,
) -> dict[str, Any]:
    """risk-v1 原始算法，保留用于对照复算，不再作为报告主口径。"""
    independent = dedupe_units(_prepare(units, "v1"))
    contradictions = [u for u in independent if u.get("relation") == "contradiction"]
    partials = [u for u in independent if u.get("relation") == "partial"]
    supports = [u for u in independent if u.get("relation") == "support"]

    def quality(unit: dict[str, Any]) -> float:
        return PARAMS_V1["quality_weight"].get(unit.get("quality") or "unknown", 0.5)

    weight_contradiction = sum(
        quality(u) * PARAMS_V1["severity_weight"].get(u.get("severity") or "unknown", 0.85)
        for u in contradictions
    )
    weight_partial = sum(quality(u) * PARAMS_V1["indirect_weight"] for u in partials)
    weight_support = sum(quality(u) * PARAMS_V1["support_weight"] for u in supports)

    raw = _saturate_v1(weight_contradiction + weight_partial)
    support_factor = 1.0 - PARAMS_V1["support_cap"] * (1.0 - math.exp(-0.35 * weight_support))
    score = raw * support_factor

    capped = False
    if not contradictions and partials:
        limit = PARAMS_V1["indirect_only_cap"] / 100.0
        if score > limit:
            score, capped = limit, True

    risk_index = round(score * 100, 1) if (contradictions or partials) else None
    band = _band(risk_index, PARAMS_V1)
    severity = _max_severity(contradictions or partials)

    # v1 的充分性口径，按原样复现
    if not contradictions and not partials:
        sufficiency = "insufficient"
    else:
        qualities = [PARAMS_V1["quality_weight"].get(u.get("quality") or "unknown", 0.5)
                     for u in contradictions] or [0.5]
        average = sum(qualities) / len(qualities)
        if len(contradictions) >= PARAMS_V1["sufficient_min_units"] and \
                average >= PARAMS_V1["sufficient_min_quality"]:
            sufficiency = "sufficient"
        else:
            sufficiency = "limited"
        coverage = (len(contradictions) / scanned_count) if scanned_count else 0.0
        if sufficiency == "sufficient" and (not scan_complete or coverage < 0.001):
            sufficiency = "limited"

    return {
        "risk_index": risk_index,
        "risk_band": band,
        "risk_band_label": BAND_LABELS[band],
        "risk_model_version": PARAMS_V1["version"],
        "evidence_sufficiency": sufficiency,
        "evidence_sufficiency_label": SUFFICIENCY_LABELS[sufficiency],
        "severity": severity,
        "severity_label": SEVERITY_LABELS.get(severity, "未分级"),
        "contradiction_count": len(contradictions),
        "partial_count": len(partials),
        "support_count": len(supports),
        "contradiction_review_ids": sorted({_text(u.get("review_id")) for u in contradictions}),
        "partial_review_ids": sorted({_text(u.get("review_id")) for u in partials}),
        "support_review_ids": sorted({_text(u.get("review_id")) for u in supports}),
        "evidence_units": independent,
        "calculation": {
            "params_version": PARAMS_V1["version"],
            "weight_contradiction": round(weight_contradiction, 4),
            "weight_partial": round(weight_partial, 4),
            "weight_support": round(weight_support, 4),
            "raw_score": round(raw * 100, 2),
            "support_factor": round(support_factor, 4),
            "indirect_only_capped": capped,
            "formula": "risk = 100 × (1 − e^(−k·Σ证据权重)) × 支持抵扣，仅间接证据时封顶",
            "sat": PARAMS_V1["saturation_k"],
        },
    }


def summarize(aspects: list[dict[str, Any]]) -> dict[str, Any]:
    """整体汇总：按方面取最高风险，不把各维度证据相加（同一评论可能跨维度）。"""
    scored = [a for a in aspects if a.get("risk_index") is not None]
    safety = [a.get("aspect") for a in aspects if a.get("safety_alert")]
    structure = {"contradiction": 0, "related_negative": 0, "topic_only": 0, "support": 0}
    for aspect in aspects:
        for key in structure:
            structure[key] += int((aspect.get("evidence_structure") or {}).get(key, 0) or 0)
    if not scored:
        return {"risk_index": None, "risk_band": "none",
                "risk_band_label": BAND_LABELS["none"],
                "evidence_sufficiency": "none",
                "evidence_sufficiency_label": SUFFICIENCY_LABELS["none"],
                "top_aspect": "", "bands": {"high": 0, "medium": 0, "low": 0},
                "scored_aspect_count": 0, "evidence_structure": structure,
                "safety_alert": bool(safety), "safety_aspects": [s for s in safety if s]}
    top = max(scored, key=lambda a: a["risk_index"])
    bands = {"high": 0, "medium": 0, "low": 0}
    for aspect in scored:
        bands[aspect["risk_band"]] = bands.get(aspect["risk_band"], 0) + 1
    # 只有具体关联、没有明确矛盾时，整体不抬到“高”
    level = top["risk_band"]
    if level == "high" and top.get("contradiction_count", 0) == 0:
        level = "medium"
    return {
        "risk_index": top["risk_index"],
        "risk_band": level,
        "risk_band_label": BAND_LABELS[level],
        "evidence_sufficiency": top["evidence_sufficiency"],
        "evidence_sufficiency_label": top["evidence_sufficiency_label"],
        "top_aspect": top["aspect"],
        "bands": bands,
        "scored_aspect_count": len(scored),
        "evidence_structure": structure,
        "safety_alert": bool(safety),
        "safety_aspects": [s for s in safety if s],
    }
