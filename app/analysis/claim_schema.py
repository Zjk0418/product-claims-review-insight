"""商品宣传主张的字段定义、规范化与校验。

原则：
1. 原始宣传文字必须保留，且能在输入文本中逐字定位；
2. 主张拆分为可比较的最小命题，不把“防滑耐磨减震透气”混成一条；
3. 抽象口号与 OCR 噪声单独分类，不伪装成可验证的功能承诺。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from .categories import CategoryProfile, get_profile
from .issue_schema import normalize_aspect

CLAIM_TYPES = (
    "functional",
    "material_craft",
    "size_spec",
    "comfort_experience",
    "price_value",
    "objective_param",
    "marketing_slogan",
    "irrelevant_noise",
)

CLAIM_TYPE_LABELS = {
    "functional": "产品功能",
    "material_craft": "材质与工艺",
    "size_spec": "尺码与规格",
    "comfort_experience": "舒适与体验",
    "price_value": "价格与性价比",
    "objective_param": "客观参数",
    "marketing_slogan": "抽象营销文案",
    "irrelevant_noise": "无关文字/噪声",
}

# 只有这些类型参与“宣传—体验”匹配
VERIFIABLE_TYPES = {
    "functional",
    "material_craft",
    "size_spec",
    "comfort_experience",
    "price_value",
    "objective_param",
}

SOURCE_RANK = {"title": 4, "attribute": 3, "detail_text": 2, "image_ocr": 1}

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def _clean(value: Any) -> str:
    return _WS_RE.sub(" ", str(value or "")).strip()


def _norm_for_contains(text: str) -> str:
    return _PUNCT_RE.sub("", unicodedata.normalize("NFKC", text or ""))


def normalize_claim_type(raw: str) -> str:
    value = _clean(raw).lower()
    return value if value in CLAIM_TYPES else "irrelevant_noise"


def make_claim_id(text_id: str, aspect: str, normalized_claim: str, original_text: str) -> str:
    raw = "|".join([text_id, aspect, normalized_claim, original_text])
    return "PC" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


@dataclass
class Claim:
    claim_id: str
    text_id: str
    aspect: str
    aspect_raw: str
    claim_type: str
    normalized_claim: str
    original_text: str
    source: str
    source_image: str = ""
    ocr_confidence: float | None = None
    confidence: float | None = None
    verification_status: str = "unverified"
    model: str = ""
    prompt_version: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "text_id": self.text_id,
            "aspect": self.aspect,
            "aspect_raw": self.aspect_raw,
            "claim_type": self.claim_type,
            "claim_type_label": CLAIM_TYPE_LABELS.get(self.claim_type, self.claim_type),
            "normalized_claim": self.normalized_claim,
            "original_text": self.original_text,
            "source": self.source,
            "source_image": self.source_image,
            "ocr_confidence": self.ocr_confidence,
            "confidence": self.confidence,
            "verification_status": self.verification_status,
            "verifiable": self.claim_type in VERIFIABLE_TYPES,
            "model": self.model,
            "prompt_version": self.prompt_version,
        }


def validate_claim(
    payload: dict[str, Any],
    raw_texts: dict[str, dict[str, Any]],
    model: str = "",
    prompt_version: str = "",
    profile: CategoryProfile | None = None,
) -> tuple[Claim | None, list[str]]:
    """校验单条模型输出；original_text 必须能在对应输入文本中定位。"""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return None, ["claim 不是对象"]

    text_id = _clean(payload.get("text_id"))
    raw = raw_texts.get(text_id)
    if raw is None:
        return None, [f"未知 text_id：{text_id}"]

    original = _clean(payload.get("original_text"))
    if not original:
        errors.append("original_text 为空")
    elif _norm_for_contains(original) not in _norm_for_contains(raw.get("text", "")):
        errors.append("original_text 无法在输入文本中定位")

    normalized_claim = _clean(payload.get("normalized_claim"))
    if not normalized_claim:
        errors.append("normalized_claim 为空")
    elif len(normalized_claim) > 60:
        errors.append("normalized_claim 超过 60 字")

    claim_type = normalize_claim_type(payload.get("claim_type"))
    # 短促属性词常被当成“抽象口号”，但它们是可核验的承诺：升格为功能主张
    if claim_type == "marketing_slogan" and _ATTRIBUTE_CUES.search(
        f"{_clean(payload.get('original_text'))} {_clean(payload.get('normalized_claim'))}"
    ):
        claim_type = "functional"
    aspect_raw = _clean(payload.get("aspect")) or "其他"
    aspect_profile = profile or get_profile("generic")
    aspect = normalize_aspect(aspect_raw, aspect_profile)
    if aspect not in aspect_profile.allowed_aspects:
        aspect = "其他"
    if claim_type == "irrelevant_noise":
        aspect = "其他"
    else:
        if claim_type == "objective_param" and aspect == "其他":
            # 颜色属于可对比的外观信息，保留为外观方面；纯品类/款式仍归“其他”
            if any(word in f"{original}{normalized_claim}" for word in ("颜色", "配色", "米色", "白色", "黑色", "红色", "蓝色")):
                aspect = "外观与色差"
            # 版型/厚度/材质虽然常被标成客观参数，但它们是可被体验反驳的承诺，
            # 不能落到“其他”后被 rule-only 分支直接跳过。
            elif any(word in f"{original}{normalized_claim}" for word in
                     ("薄款", "加厚", "厚度", "克重", "面料", "材质", "里料", "填充", "成分")):
                if "材质与用料" in aspect_profile.allowed_aspects:
                    aspect = "材质与用料"
            elif any(word in f"{original}{normalized_claim}" for word in
                     ("直筒", "宽松", "修身", "紧身", "版型", "剪裁", "高腰", "阔腿", "束脚", "显瘦")):
                if "版型与合身度" in aspect_profile.allowed_aspects:
                    aspect = "版型与合身度"
        if aspect == "其他":
            # 口号升格的功能主张（也可能模型没给出方面），按属性词落到可对比维度
            for cues, target in _ASPECT_BY_CUE:
                if any(cue in f"{original}{normalized_claim}" for cue in cues):
                    if target in aspect_profile.allowed_aspects:
                        aspect = target
                    break
    if any(word in f"{original}{normalized_claim}" for word in ("耐磨", "耐穿", "耐用", "寿命")):
        aspect = "耐用性"

    confidence = payload.get("confidence")
    if confidence is not None:
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            errors.append("confidence 不是数值")
            confidence = None

    if errors:
        return None, errors

    claim = Claim(
        claim_id=make_claim_id(text_id, aspect, normalized_claim, original),
        text_id=text_id,
        aspect=aspect,
        aspect_raw=aspect_raw,
        claim_type=claim_type,
        normalized_claim=normalized_claim,
        original_text=original,
        source=str(raw.get("source") or ""),
        source_image=str(raw.get("image") or ""),
        ocr_confidence=raw.get("confidence") if raw.get("source") == "image_ocr" else None,
        confidence=confidence,
        model=model,
        prompt_version=prompt_version,
    )
    return claim, []


def dedupe_claims(claims: list[Claim]) -> list[Claim]:
    """同一方面下，规范化命题相同或存在包含关系的，只保留信息更完整的一条。"""
    ordered = sorted(
        claims,
        key=lambda c: (SOURCE_RANK.get(c.source, 0), c.confidence or 0, len(c.normalized_claim)),
        reverse=True,
    )
    kept: list[Claim] = []
    for claim in ordered:
        norm = _norm_for_contains(claim.normalized_claim)
        duplicate_index = None
        for index, current in enumerate(kept):
            if current.aspect != claim.aspect:
                continue
            other = _norm_for_contains(current.normalized_claim)
            if not norm or not other:
                continue
            if norm == other or norm in other or other in norm:
                duplicate_index = index
                break
        if duplicate_index is None:
            kept.append(claim)
            continue
        current = kept[duplicate_index]
        if len(norm) > len(_norm_for_contains(current.normalized_claim)):
            kept[duplicate_index] = claim
    return kept


# 只保留“对商品本身的承诺”：规格、容量、材质工艺、功能效果、使用体验、价格。
# 以下三类对分析没有价值，且会污染报告：
#   1. 描述文本/图片本身的元叙述（“文本为尺码推荐标题”“无具体规格信息”）；
#   2. 活动时间、日期（“活动时间：10月15日-11月13日”）；
#   3. 平台活动/渠道名（“天猫超市双11”“618”）。
# 注意：不能因为规范化文本以“商品为…”开头就判为元叙述——
# 模型习惯把标题里的“直筒/宽松/薄款”写成“商品为直筒版型”，
# 这些正是服饰品类最需要核对的版型与材质承诺。只有“商品为<纯品类>”
# 这类没有设计/材质信息的身份描述才算元叙述。
_META_RE = re.compile(
    r"^(商品|产品|文本|图片|画面|图中|该文案|此文案|本图|本页)\s*(为|是)"
    r"|无具体.{0,8}信息|平台促销活动信息|促销活动信息",
    re.I,
)
_DESIGN_OR_MATERIAL_RE = re.compile(
    r"直筒|宽松|修身|紧身|版型|剪裁|廓形|高腰|中腰|低腰|阔腿|束脚|显瘦|显高|"
    r"薄款|加厚|加绒|厚度|克重|弹力|微弹|垂感|挺括|硬挺|柔软|亲肤|透气|"
    r"面料|材质|成分|里料|填充|纯棉|棉|羊毛|羊绒|聚酯|涤纶|锦纶|氨纶|真丝|皮革|"
    r"防水|防风|防滑|耐磨|耐穿|速干|凉感|保暖|冰丝",
    re.I,
)

# 短促的属性词（透气/显瘦/抗皱/弹力…）经常被模型归为“抽象口号”，
# 但它们正是买家最能感知、也最容易被体验反驳的承诺，不能当噪声丢掉。
_ATTRIBUTE_CUES = re.compile(
    r"透气|显瘦|显高|遮臀|修饰腿部|修饰身材|抗皱|免烫|弹力|微弹|回弹|亲肤|柔软|"
    r"挺括|有型|不粘毛|不起球|不易变形|坚牢|耐用|耐磨|保暖|速干|吸汗|"
    r"防滑|防水|防风|轻盈|垂感|舒适|宽松|修身|直筒|高腰|阔腿",
    re.I,
)
# 从口号升格为功能主张后，把方面落到具体的可对比维度（仅当模型没给或给了“其他”）
_ASPECT_BY_CUE: tuple[tuple[tuple[str, ...], str], ...] = (
    (("显瘦", "显高", "遮臀", "修饰腿部", "修饰身材", "高腰", "阔腿"), "版型与合身度"),
    (("弹力", "微弹", "回弹", "亲肤", "面料", "材质"), "材质与用料"),
    (("抗皱", "免烫", "耐用", "耐磨", "坚牢", "不粘毛", "不起球", "不易变形"), "耐用性"),
    (("透气", "柔软", "舒适", "保暖", "速干", "吸汗"), "使用舒适性"),
    (("挺括", "有型", "外观"), "外观与色差"),
    (("防滑", "防水", "防风"), "功能与性能"),
)
_DATE_RE = re.compile(r"^活动时间|^\d{1,2}月\d{1,2}日|^\d{4}年\d{1,2}月|^\d{1,2}[./]\d{1,2}\s*[-~至]")
_PLATFORM_RE = re.compile(
    r"^(天猫超市|天猫|淘宝|京东|拼多多|抖音)(\s*(双11|双十一|618|年货节|聚划算|百亿补贴|限时秒杀))*[\s·]*$"
    r"|^(双11|双十一|618|年货节|聚划算|百亿补贴|限时秒杀)$",
    re.I,
)

# 平台价格解释是页面规则，不是这件商品的价格承诺。只在没有混合实际
# 到手价、量值规格或产品性能时排除，避免把同一行里的有效卖点丢掉。
_PRICE_EXPLANATION_RE = re.compile(
    r"(?:未划线|非划线|划线)(?:价|价格).{0,30}(?:指|是|为|并非|非原价|非成交价|不代表|不等于|不作为|仅供|参考|可能)"
    r"|(?:价格|价钱)(?:说明|解释|规则)"
    r"|(?:成交|结算|支付|付款|到手)(?:价|价格).{0,20}以.{0,25}(?:结算|订单|支付|付款|页面).{0,10}为准"
    r"|(?:最终|具体|实际).{0,8}(?:价格|价).{0,12}以.{0,25}(?:结算|订单|支付|付款).{0,10}为准",
    re.I,
)
_ACTUAL_PRICE_RE = re.compile(
    r"(?:到手(?:价格|价)|券后(?:价格|价)|实付价|成交(?:价格|价)|售价|现价|活动价|单价|商品价格)"
    r"\s*(?:[:：]|为|是|约|低至|仅需|仅)?\s*[¥￥]?\s*\d+(?:\.\d+)?"
    r"|(?:^|[\s，,；;])价格\s*[:：]\s*[¥￥]?\s*\d+(?:\.\d+)?",
    re.I,
)
_QUANTIFIED_PRODUCT_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:ml|mg|kg|g|l|cm|mm|mah|wh|w|%)(?![A-Za-z])"
    r"|\d+(?:\.\d+)?\s*(?:毫升|千克|公斤|厘米|毫米|克|斤|升|小时|分钟|秒)"
    r"|(?:尺码|码数|型号|净含量|容量|净重|重量|克重|腰围|臀围|胸围|衣长|裤长|肩宽|袖长).{0,12}\d"
    r"|均码|标准尺码|标准码|(?<![A-Za-z])(?:XXXL|XXL|XL|XXS|XS|S|M|L)(?![A-Za-z])",
    re.I,
)
_PRODUCT_BENEFIT_RE = re.compile(
    r"补水|保湿|祛痘|淡化|留香|护色|除菌|杀菌|抑菌|抗菌|清洁|去污|去渍|去油|"
    r"温和|不伤|不刺激|抗敏|无添加|无香|无酒精|防晒|防腐|防霉|防漏|防摔|耐摔|"
    r"食品级|正品|保真|纯度|续航|快充|降噪|音质|防伪",
    re.I,
)
_BARE_SIZE_LABEL_RE = re.compile(
    r"(?:(?:尺码推荐表|尺码对照表|尺码推荐|参考尺码|建议尺码|推荐尺码|建议码数|推荐码数|"
    r"尺码表|尺码|码数|尺寸|身高|体重|腰围|臀围|胸围|衣长|裤长|肩宽|袖长|size|height|weight)"
    r"(?:cm|kg|mm|斤|厘米|千克|公斤)?)+",
    re.I,
)


def _bare_size_label(text: str) -> bool:
    compact = re.sub(r"[\W_]+|[与和]", "", text, flags=re.UNICODE)
    return bool(compact and _BARE_SIZE_LABEL_RE.fullmatch(compact))


def _has_concrete_product_claim(text: str) -> bool:
    return bool(_ACTUAL_PRICE_RE.search(text) or _QUANTIFIED_PRODUCT_RE.search(text)
                or _DESIGN_OR_MATERIAL_RE.search(text) or _PRODUCT_BENEFIT_RE.search(text))


def non_actionable_reason_dict(claim: dict[str, Any]) -> str:
    """按 dict 形式判断（复用的历史 claims 也是 dict）。"""
    claim_type = str(claim.get("claim_type") or "")
    text = f"{claim.get('normalized_claim') or ''} {claim.get('original_text') or ''}"
    if not _has_concrete_product_claim(text):
        if _PRICE_EXPLANATION_RE.search(text):
            return "平台价格说明或成交规则"
        if any(_bare_size_label(str(claim.get(key) or "")) for key in ("normalized_claim", "original_text")):
            return "尺码表标题或裸标签"
    if claim_type in {"marketing_slogan", "irrelevant_noise"}:
        # 含属性词的口号是可核验承诺，不算噪声
        if not _ATTRIBUTE_CUES.search(text):
            return "抽象口号或无关文字"
    # “商品为680g×2/补水保湿配方”仍有实质规格/功效；不能只保护服装材质词。
    # 只检查规范化命题，避免原文里别的卖点让纯品类“商品为沐浴露”误过关。
    if _META_RE.search(text) and not _has_concrete_product_claim(str(claim.get("normalized_claim") or "")):
        return "描述文本或图片本身"
    stripped = str(claim.get("normalized_claim") or "").strip()
    if _DATE_RE.search(stripped):
        return "活动时间或日期"
    if _PLATFORM_RE.search(stripped):
        return "平台活动或渠道名称"
    return ""


def non_actionable_reason(claim: Claim) -> str:
    """返回不可分析的原因；空串表示这是一条值得分析的主张。"""
    return non_actionable_reason_dict(
        {
            "claim_type": claim.claim_type,
            "normalized_claim": claim.normalized_claim,
            "original_text": claim.original_text,
        }
    )


def filter_actionable_claims(claims: list[Claim]) -> tuple[list[Claim], dict[str, int], list[dict[str, str]]]:
    """按“是否值得分析”过滤主张，返回 (保留, 原因计数, 被丢弃样例)。"""
    kept: list[Claim] = []
    reasons: dict[str, int] = {}
    samples: list[dict[str, str]] = []
    for claim in claims:
        reason = non_actionable_reason(claim)
        if not reason:
            kept.append(claim)
            continue
        reasons[reason] = reasons.get(reason, 0) + 1
        if len(samples) < 8:
            samples.append(
                {
                    "claim": claim.normalized_claim,
                    "original": claim.original_text,
                    "source": claim.source,
                    "reason": reason,
                }
            )
    return kept, reasons, samples


def filter_claim_dicts(claims: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, str]]]:
    """过滤 dict 形式的主张（用于复用历史 extracted_claims.json 的路径）。"""
    kept: list[dict[str, Any]] = []
    reasons: dict[str, int] = {}
    samples: list[dict[str, str]] = []
    for claim in claims:
        reason = non_actionable_reason_dict(claim)
        if not reason:
            kept.append(claim)
            continue
        reasons[reason] = reasons.get(reason, 0) + 1
        if len(samples) < 8:
            samples.append(
                {
                    "claim": str(claim.get("normalized_claim") or ""),
                    "original": str(claim.get("original_text") or ""),
                    "source": str(claim.get("source") or ""),
                    "reason": reason,
                }
            )
    return kept, reasons, samples
