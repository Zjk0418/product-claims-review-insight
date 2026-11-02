"""商品品类识别与品类规则配置。

设计原则：
1. 通用 Schema 不随品类变化，品类只补充“对象同义词 / 方面提示 / 方向提示”；
2. 品类由商品标题、属性等真实信息推断，推断不出来就返回 generic（unknown），
   不根据商品 ID 或 URL 参数硬编码；
3. 每个品类配置都是纯数据，新增品类不改代码逻辑；
4. 规则只影响归类与提示，不修改评论事实。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# 通用方面：所有品类都可用
GENERIC_ASPECTS = [
    "尺寸与规格",
    "使用舒适性",
    "功效与效果",
    "做工质量",
    "材质与用料",
    "外观与色差",
    "功能与性能",
    "耐用性",
    "气味",
    "物流与包装",
    "客服与售后",
    "价格与性价比",
    "其他",
]

# 历史标签：鞋类商品继续沿用，保证既有标注集与报告可比
LEGACY_ASPECTS = ["尺码与鞋型", "穿着舒适性"]

# 品类扩展方面
EXTRA_ASPECTS = [
    "口感与味道",
    "新鲜度与保质期",
    "安装与结构",
    "连接与兼容",
    "版型与合身度",
]


@dataclass(frozen=True)
class CategoryProfile:
    key: str
    label: str
    # 该品类允许出现的方面（顺序即 Prompt 中的展示顺序）
    aspects: tuple[str, ...]
    # 通用方面 -> 该品类惯用标签（如鞋类用“尺码与鞋型”）
    aspect_preferred: dict[str, str] = field(default_factory=dict)
    # 该品类优先匹配的方面关键词（先于通用词表）
    aspect_keywords: tuple[tuple[tuple[str, ...], str], ...] = ()
    object_synonyms: dict[str, tuple[str, ...]] = field(default_factory=dict)
    prompt_object_hints: str = ""
    prompt_aspect_hints: str = ""
    # 宣传主张里用于识别对象的词（落差候选筛选）
    claim_object_terms: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def preferred(self, canonical: str) -> str:
        """把通用方面名换成该品类的惯用标签。"""
        mapped = self.aspect_preferred.get(canonical, canonical)
        return mapped if mapped in self.aspects else "其他"

    @property
    def allowed_aspects(self) -> set[str]:
        return set(self.aspects)


# 通用对象词表：所有品类共用
BASE_OBJECT_SYNONYMS: dict[str, tuple[str, ...]] = {
    "整体": ("整体", "商品", "产品", "这款", "东西"),
    "包装": ("包装", "外包装", "盒子", "纸箱"),
    "物流": ("物流", "快递", "发货", "配送"),
    "客服": ("客服", "售后", "商家", "店铺"),
    "价格": ("价格", "价位", "售价", "价钱"),
    "气味": ("气味", "味道", "异味"),
    "颜色": ("颜色", "色差", "配色"),
    "做工": ("做工", "走线", "胶水", "工艺", "缝线", "针脚"),
    "外观": ("外观", "款式", "颜值", "造型"),
    "尺寸": ("尺寸", "大小", "规格", "尺码", "码数"),
}


GENERIC_ASPECT_HINTS = (
    "按商品实际部件与表现归类；能归入具体方面就不要放进“其他”；"
    "“其他”只用于确实无法归类的具体问题"
)
GENERIC_OBJECT_HINTS = (
    "具体部件或对象（如：屏幕、电池、面料、瓶身、包装、客服、价格……）；"
    "说不清具体对象时用“整体”，不要把所有问题都写成“商品”"
)


PROFILES: dict[str, CategoryProfile] = {
    "generic": CategoryProfile(
        key="generic",
        label="未识别品类",
        aspects=tuple(GENERIC_ASPECTS + EXTRA_ASPECTS),
        object_synonyms=dict(BASE_OBJECT_SYNONYMS),
        prompt_object_hints=GENERIC_OBJECT_HINTS,
        prompt_aspect_hints=GENERIC_ASPECT_HINTS,
    ),
    "shoe": CategoryProfile(
        key="shoe",
        label="鞋类",
        aspects=tuple(
            ["尺码与鞋型", "穿着舒适性"]
            + [a for a in GENERIC_ASPECTS if a not in {"尺寸与规格", "使用舒适性", "功效与效果"}]
        ),
        aspect_preferred={"尺寸与规格": "尺码与鞋型", "使用舒适性": "穿着舒适性"},
        aspect_keywords=((("磨脚", "挤脚", "脚感", "脚疼", "累脚", "不跟脚", "掉跟"), "穿着舒适性"),),
        object_synonyms={
            **BASE_OBJECT_SYNONYMS,
            "整体": ("整体", "鞋子", "这双鞋", "鞋", "商品", "产品"),
            "鞋头": ("鞋头", "鞋尖", "前掌", "前脚掌", "脚趾", "楦头"),
            "后跟": ("后跟", "脚跟", "后脚跟", "脚踝", "后帮"),
            "鞋底": ("鞋底", "大底", "中底", "鞋底面"),
            "鞋面": ("鞋面", "帮面", "鞋帮"),
            "鞋垫": ("鞋垫", "内垫", "鞋内垫"),
            "尺码": ("尺码", "码数", "号码", "码"),
            "鞋带": ("鞋带",),
            "包装": ("包装", "鞋盒", "盒子"),
        },
        prompt_object_hints=(
            "鞋头、后跟、鞋底、鞋面、鞋垫、尺码、鞋带、包装、物流、客服、价格、气味、整体"
        ),
        prompt_aspect_hints=(
            "- 软硬、轻重、磨脚、挤脚、脚疼、不跟脚、掉跟 → 穿着舒适性；\n"
            "- 尺码与鞋型只用于尺码本身偏大/偏小/偏窄、码数不标准；\n"
            "- 防滑、透气、缓震、防水 → 功能与性能；\n"
            "- 材质与用料只用于具体材质、面料、内里成分或用料好坏；\n"
            "- 开胶、走线、左右不对称、做工粗糙 → 做工质量。"
        ),
        claim_object_terms={
            "鞋头": ("鞋头", "鞋尖", "前掌", "脚趾"),
            "后跟": ("后跟", "脚跟", "后帮"),
            "鞋底": ("鞋底", "大底", "中底"),
            "鞋面": ("鞋面", "帮面", "网面"),
            "鞋垫": ("鞋垫", "内垫"),
            "尺码": ("尺码", "码数", "标准码"),
        },
    ),
    "beauty": CategoryProfile(
        key="beauty",
        label="美妆个护",
        aspects=GENERIC_ASPECTS,
        aspect_keywords=(
            (("淡印", "祛痘", "痘印", "美白", "保湿", "补水", "修复", "抗皱", "控油", "效果"), "功效与效果"),
        ),
        object_synonyms={
            **BASE_OBJECT_SYNONYMS,
            "面部": ("面部", "脸上", "脸", "脸颊"),
            "皮肤": ("皮肤", "肌肤"),
            "痘印": ("痘印", "痘痕", "印子"),
            "痘痘": ("痘痘", "粉刺", "闭口"),
            "精华液": ("精华", "精华液", "凝胶", "原液", "面霜", "乳液"),
            "瓶身": ("瓶身", "瓶子", "小瓶", "包装瓶", "泵头"),
            "质地": ("质地", "肤感", "上脸", "搓泥"),
            "成分": ("成分", "配方", "烟酰胺", "传明酸", "壬二酸"),
        },
        prompt_object_hints=(
            "面部、皮肤、痘印、痘痘、精华液、瓶身、质地、成分、包装、物流、客服、价格、气味、整体"
        ),
        prompt_aspect_hints=(
            "- 泛红、刺痛、过敏、长痘等**使用后不良反应** → 功效与效果（与宣传的功效/安全性直接相关）；\n"
            "- 搓泥、粘腻、拔干等**肤感**问题 → 使用舒适性；\n"
            "- 淡印、祛痘、保湿、美白等实际效果与宣传不符 → 功效与效果；\n"
            "- 成分、配方、烟酰胺等成分宣称 → 材质与用料；\n"
            "- 漏液、干涸、瓶身破损 → 做工质量或耐用性；\n"
            "- 尺寸与规格用于净含量、容量、规格不符。"
        ),
        claim_object_terms={
            "面部": ("面部", "脸", "肌肤", "皮肤"),
            "痘印": ("痘印", "痘痕", "印子"),
            "精华液": ("精华", "凝胶", "原液", "面霜", "乳液"),
        },
    ),
    "bag": CategoryProfile(
        key="bag",
        label="箱包",
        aspects=GENERIC_ASPECTS,
        aspect_keywords=((("容量", "空间", "装不下", "太小", "太大"), "尺寸与规格"),),
        object_synonyms={
            **BASE_OBJECT_SYNONYMS,
            "容量": ("容量", "空间", "容积"),
            "拉链": ("拉链", "拉锁"),
            "背带": ("背带", "肩带", "提手"),
            "内袋": ("内袋", "隔层", "分区", "口袋"),
            "轮子": ("轮子", "滚轮"),
            "拉杆": ("拉杆",),
            "面料": ("面料", "布料", "材质"),
            "箱体": ("箱体", "包体", "主体"),
        },
        prompt_object_hints=(
            "容量、拉链、背带、提手、内袋、轮子、拉杆、面料、箱体、包装、物流、客服、价格、气味、整体"
        ),
        prompt_aspect_hints=(
            "- 容量、大小、装不下 → 尺寸与规格；\n"
            "- 拉链卡顿、线头、开线、缝线粗糙 → 做工质量；\n"
            "- 承重断裂、掉轮、拉杆变形 → 耐用性；\n"
            "- 背着累、勒肩、压肩 → 使用舒适性；\n"
            "- 防水、耐磨 → 功能与性能。"
        ),
        claim_object_terms={
            "容量": ("容量", "空间", "升"),
            "拉链": ("拉链", "拉锁"),
            "背带": ("背带", "肩带", "提手"),
        },
    ),
    "apparel": CategoryProfile(
        key="apparel",
        label="服饰",
        aspects=tuple(GENERIC_ASPECTS + ["版型与合身度"]),
        aspect_keywords=((("版型", "合身", "修身", "肥瘦", "长短"), "版型与合身度"),),
        object_synonyms={
            **BASE_OBJECT_SYNONYMS,
            "领口": ("领口", "领子"),
            "袖口": ("袖口", "袖子"),
            "拉链": ("拉链", "拉锁"),
            "纽扣": ("纽扣", "扣子"),
            "面料": ("面料", "布料"),
            "版型": ("版型", "剪裁"),
        },
        prompt_object_hints=(
            "领口、袖口、拉链、纽扣、面料、版型、包装、物流、客服、价格、气味、整体"
        ),
        prompt_aspect_hints=(
            "- 版型、合身度、长短、肥瘦 → 版型与合身度；\n"
            "- 起球、掉色、缩水、变形 → 耐用性；\n"
            "- 面料、成分、手感 → 材质与用料。"
        ),
        claim_object_terms={"面料": ("面料", "布料", "棉", "聚酯")},
    ),
    "food": CategoryProfile(
        key="food",
        label="食品",
        aspects=tuple(GENERIC_ASPECTS + ["口感与味道", "新鲜度与保质期"]),
        aspect_keywords=(
            (("味道", "口感", "好吃", "难吃", "太咸", "太甜", "太淡", "油腻"), "口感与味道"),
            (("新鲜", "保质期", "过期", "变质", "日期"), "新鲜度与保质期"),
        ),
        object_synonyms={
            **BASE_OBJECT_SYNONYMS,
            "味道": ("味道", "口感", "口味"),
            "分量": ("分量", "份量", "克数", "净含量"),
            "保质期": ("保质期", "生产日期", "日期"),
        },
        prompt_object_hints="味道、口感、分量、保质期、包装、物流、客服、价格、整体",
        prompt_aspect_hints=(
            "- 味道、口感、咸甜 → 口感与味道；\n"
            "- 过期、变质、日期不符 → 新鲜度与保质期；\n"
            "- 缺斤少两、净含量不足 → 尺寸与规格。"
        ),
        claim_object_terms={"味道": ("味道", "口感", "口味"), "分量": ("分量", "克", "净含量")},
    ),
    "home": CategoryProfile(
        key="home",
        label="家居",
        aspects=tuple(GENERIC_ASPECTS + ["安装与结构"]),
        aspect_keywords=((("安装", "组装", "结构", "稳固", "晃动", "承重"), "安装与结构"),),
        object_synonyms={
            **BASE_OBJECT_SYNONYMS,
            "安装": ("安装", "组装"),
            "结构": ("结构", "支架", "骨架"),
            "五金": ("五金", "螺丝", "合页"),
            "材质": ("材质", "用料"),
        },
        prompt_object_hints="安装、结构、五金、材质、尺寸、包装、物流、客服、价格、气味、整体",
        prompt_aspect_hints=(
            "- 安装困难、孔位不准、晃动 → 安装与结构；\n"
            "- 承重不足、变形 → 耐用性；\n"
            "- 尺寸不符、放不下 → 尺寸与规格。"
        ),
        claim_object_terms={"结构": ("结构", "支架", "承重")},
    ),
    "digital": CategoryProfile(
        key="digital",
        label="数码电器",
        aspects=tuple(GENERIC_ASPECTS + ["连接与兼容"]),
        aspect_keywords=((("连接", "断连", "配对", "兼容", "信号", "延迟"), "连接与兼容"),),
        object_synonyms={
            **BASE_OBJECT_SYNONYMS,
            "电池": ("电池", "续航", "电量"),
            "屏幕": ("屏幕", "显示"),
            "接口": ("接口", "充电口", "插口"),
            "连接": ("连接", "配对", "蓝牙", "信号"),
            "发热": ("发热", "发烫"),
        },
        prompt_object_hints="电池、屏幕、接口、连接、发热、包装、物流、客服、价格、整体",
        prompt_aspect_hints=(
            "- 断连、配对失败、兼容性 → 连接与兼容；\n"
            "- 续航缩水、充电慢 → 功能与性能；\n"
            "- 发烫、异响 → 使用舒适性或功能与性能。"
        ),
        claim_object_terms={"电池": ("电池", "续航"), "连接": ("连接", "蓝牙", "配对")},
    ),
}

DEFAULT_PROFILE = PROFILES["generic"]

# 未指定品类时的兜底：合并所有品类的“宣传对象词”，避免默认只认某一品类
ALL_CLAIM_OBJECT_TERMS: dict[str, tuple[str, ...]] = {}
for _profile in PROFILES.values():
    for _obj, _terms in _profile.claim_object_terms.items():
        ALL_CLAIM_OBJECT_TERMS.setdefault(_obj, tuple())
        ALL_CLAIM_OBJECT_TERMS[_obj] = tuple(
            dict.fromkeys(ALL_CLAIM_OBJECT_TERMS[_obj] + tuple(_terms))
        )


def get_profile(key: str | None) -> CategoryProfile:
    return PROFILES.get(str(key or "").strip().lower(), DEFAULT_PROFILE)


# 品类关键词：命中标题/属性即累计权重，取最高分
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "shoe": (
        "运动鞋", "跑步鞋", "板鞋", "老爹鞋", "帆布鞋", "皮鞋", "靴", "拖鞋", "凉鞋",
        "鞋", "鞋垫", "鞋带",
    ),
    "beauty": (
        "精华", "面膜", "面霜", "乳液", "洁面", "洗面奶", "爽肤水", "防晒", "口红",
        "粉底", "祛痘", "痘印", "烟酰胺", "护肤", "化妆品", "凝胶", "眼霜",
    ),
    "bag": (
        "旅行包", "双肩包", "背包", "行李包", "手提包", "单肩包", "箱包", "拉杆箱",
        "行李箱", "钱包", "腰包", "收纳包",
    ),
    "apparel": (
        "T恤", "卫衣", "外套", "羽绒服", "裤", "裙", "衬衫", "毛衣", "内衣", "袜",
        "夹克", "风衣", "套装",
    ),
    "food": (
        "零食", "干脆面", "方便面", "坚果", "饼干", "饮料", "咖啡", "茶叶", "糕点",
        "糖果", "肉干", "水果", "食品", "速食",
    ),
    "home": (
        "收纳", "置物架", "床品", "四件套", "枕头", "被子", "厨具", "锅", "餐具",
        "清洁", "拖把", "椅子", "桌子", "灯",
    ),
    "digital": (
        "手机", "耳机", "充电", "数据线", "电脑", "键盘", "鼠标", "相机", "音箱",
        "数码", "电器", "电池",
    ),
}


def _product_text(product: dict[str, Any]) -> str:
    parts = [
        str(product.get("title") or ""),
        str(product.get("subtitle") or ""),
        " ".join(str(k) for k in (product.get("attributes") or {}).keys()),
        " ".join(
            str(v)
            for values in (product.get("attributes") or {}).values()
            if isinstance(values, list)
            for v in values[:5]
        ),
    ]
    return " ".join(parts)


def detect_category(product: dict[str, Any] | None) -> dict[str, Any]:
    """从真实商品信息推断品类；返回 key/label/confidence/evidence。

    置信度是“关键词命中的相对强度”，不是概率。多个品类得分接近时标记 ambiguous，
    交由上层决定是否人工复核，不做强制归类。
    """
    product = product or {}
    text = _product_text(product)
    if not text.strip():
        return {
            "key": "generic",
            "label": DEFAULT_PROFILE.label,
            "confidence": 0.0,
            "ambiguous": False,
            "evidence": ["无商品标题或属性，无法识别品类"],
        }

    scores: dict[str, int] = {}
    hits: dict[str, list[str]] = {}
    for key, words in CATEGORY_KEYWORDS.items():
        matched = [w for w in words if w and w in text]
        if matched:
            # 长词更有区分度（“旅行包”优先于“包”）
            scores[key] = sum(len(w) for w in matched)
            hits[key] = matched

    if not scores:
        return {
            "key": "generic",
            "label": DEFAULT_PROFILE.label,
            "confidence": 0.0,
            "ambiguous": False,
            "evidence": [f"标题与属性未命中任何品类词：{text[:60]}"],
        }

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    key, top = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0
    total = sum(scores.values())
    confidence = round(top / total, 2) if total else 0.0
    ambiguous = bool(second and top - second <= 2 and confidence < 0.6)
    profile = get_profile(key)
    return {
        "key": key,
        "label": profile.label,
        "confidence": confidence,
        "ambiguous": ambiguous,
        "evidence": [f"命中「{w}」" for w in hits.get(key, [])[:6]],
        "scores": scores,
    }
