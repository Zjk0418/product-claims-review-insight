"""宣传主张与真实负面体验的落差分析。

流程：方面/关键词候选筛选 → LLM 复核 → 程序校验 → 证据强度分级。
LLM 只判断“给定证据与主张是否矛盾”，不生成统计数字，也不判断是否虚假宣传。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .categories import ALL_CLAIM_OBJECT_TERMS, CategoryProfile, get_profile
from .claim_schema import VERIFIABLE_TYPES
from .parallel import ordered_map
from .issue_schema import aspects_compatible
from .llm_client import LLMError, parse_json_content

PROMPT_VERSION = "gap-analyze-v6"

GAP_TYPES = (
    "potential_conflict",
    "partial_gap",
    "no_direct_conflict_found",
    "insufficient_evidence",
)

# 注意：potential_conflict 是历史命名，Prompt 中的定义是“较明确的矛盾”，
# 展示层统一写作“明确矛盾”，不要按字面理解为“可能冲突”。
GAP_TYPE_LABELS = {
    "potential_conflict": "明确矛盾",
    "partial_gap": "部分体验差异",
    "no_direct_conflict_found": "未发现直接冲突",
    "insufficient_evidence": "证据不足",
}

EVIDENCE_KINDS = ("contradiction", "related_negative", "topic_only", "support", "insufficient")
EVIDENCE_KIND_LABELS = {
    "contradiction": "明确矛盾",
    "related_negative": "具体负面（未构成直接矛盾）",
    "topic_only": "仅提及属性",
    "support": "具体支持",
    "insufficient": "无法判断",
}

STOPWORDS = {
    "商品",
    "具有",
    "性能",
    "功能",
    "材质",
    "问题",
    "体验",
    "用户",
    "评价",
    "认为",
    "出现",
    "存在",
    "比较",
    "有点",
    "非常",
    "以及",
    "可以",
    "这个",
    "这款",
    "产品",
}

SYSTEM_PROMPT = """你是“商品宣传—真实体验”落差审核员。给你一条商家宣传主张，以及若干来自真实低星评价的问题簇（含原文证据），判断是否存在体验落差。

【只依据输入证据】
1. 不得引入外部知识，不得猜测未提供的评论、销量或用户比例。
2. 不得判断广告是否违法、是否虚假宣传；只描述“宣传承诺”与“已采集体验”之间的差异。
3. 没有证据就不能判冲突。“没有发现冲突”不等于“宣传已证实为真”。

【gap_type 只能取以下值】
- potential_conflict：宣传与证据中的具体负面体验存在较明确的矛盾（如宣传“防滑”，评价明确说“很滑”）。名称是历史遗留，含义就是“明确矛盾”。
- partial_gap：宣传描述较宽泛，或差异只在部分 SKU、尺码、使用场景下出现；证据本身必须有具体负面事实。
- no_direct_conflict_found：候选证据与该主张没有直接关系，或未发现矛盾。
- insufficient_evidence：主张抽象不可验证，或证据不足。

【硬性规则】
1. potential_conflict / partial_gap 必须至少选择 1 条 evidence_review_id；没有证据就返回 insufficient_evidence。
2. evidence_review_ids 只能从输入 evidence 中有原文的评论选择；簇的总评论数不是直接证据数。请列出已提供原文中所有直接支持该判断的评论。
3. 只有 1~2 条证据时，finding 必须写明“个别评价”或“少量评价”，不得写成普遍现象。
4. related_cluster_ids 和 evidence_review_ids 只能从输入中选择，不得编造。
5. finding 不超过 120 字，必须基于证据中的具体表现，不得添加输入没有的缺陷。
6. 抽象营销文案（marketing_slogan）或无关文字（irrelevant_noise）返回 insufficient_evidence。
7. “整体没效果”不能直接反驳一个具体功效、成分、作用机制、实验或认证过程；证据必须指向同一对象与同一命题。不要自行补充“该机制暗示某功效”的推论。
8. “经过测试/获得认证/含某成分”与“个人使用没有效果”是不同命题；后者没有直接核查前者时返回 no_direct_conflict_found。没有相关原文则返回 insufficient_evidence。
9. partial_gap 必须有证据明确指出 SKU、使用场景或适用范围的差异；文案宽泛或样本少本身不构成部分落差。
10. 只输出 JSON 对象，不要 markdown，不要解释。
11. 逐条证据必须给出 evidence_kind：
   contradiction=该片段直接与这条承诺不一致；
   related_negative=片段有具体不利事实，但与该承诺是否矛盾还不明确；
   topic_only=只提到相关属性或话题，没有不利事实；
   support=片段具体且条件匹配地支持该承诺；
   insufficient=无法判断。
12. negative_fact 表示该片段是否陈述了具体不利事实。只提到“成分/效果/尺码”等词、
   或只表达关注与疑问，不算不利事实（negative_fact=false）。
13. potential_conflict 必须至少有 1 条 evidence_kind=contradiction；
    只有 related_negative 时改判 partial_gap；只有 topic_only 时改判 no_direct_conflict_found。
    partial_gap 必须至少有 1 条 contradiction 或 related_negative。
    topic_only 不能作为任何冲突判断的证据。

【输出格式】
{"gap_type":"potential_conflict|partial_gap|no_direct_conflict_found|insufficient_evidence",
 "related_cluster_ids":["C01"],
 "evidence_review_ids":["123"],
 "evidence":[{"review_id":"123","relation":"contradiction|indirect|support",
   "evidence_kind":"contradiction|related_negative|topic_only|support|insufficient",
   "negative_fact":true,
   "fact_quality":"concrete|partial|vague","severity":"minor|moderate|major|critical",
   "quote":"评论原文中的证据片段"}],
 "finding":"个别评价反映实际使用效果与宣传承诺存在差异",
 "reasoning":"简要说明判断依据，不超过 80 字"}

【证据明细（可选但优先输出）】
1. evidence 里每个 review_id 必须来自 evidence_review_ids，且 quote 必须是该评论原文中的片段。
2. fact_quality：concrete=给出可核对的具体事实（成分、天数、部件、刻度等）；
   partial=有部分细节但仍笼统；vague=只有结论（如“质量不好”）。
3. severity：minor=轻微体验差异；moderate=一般质量/体验问题；
   major=明显影响使用；critical=核心功能缺陷或安全风险。无法判断时省略该字段。
4. relation 只描述该评论与这条宣传承诺的关系：contradiction=直接不一致；
   indirect=相关但不足以证明矛盾；support=具体且条件匹配地支持该承诺。
   评价维度本身不等于证据关系。"""


class ChatLike(Protocol):
    def chat_json(self, system: str, user: str) -> Any: ...


@dataclass
class GapConfig:
    max_candidates: int = 4
    max_evidence_per_cluster: int = 8
    prompt_version: str = PROMPT_VERSION


def _hash(text: str, length: int = 20) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _evidence_kind(row: dict[str, Any]) -> str:
    """归一化逐条证据类型；旧缓存只有 relation，按 relation 推导。"""
    kind = str(row.get("evidence_kind") or "").strip()
    if kind in EVIDENCE_KINDS:
        return kind
    relation = str(row.get("relation") or "").strip()
    if relation == "contradiction":
        return "contradiction"
    if relation == "support":
        return "support"
    if relation == "indirect":
        # 没给 negative_fact 时按“具体负面”处理，不能凭空降级
        return "topic_only" if row.get("negative_fact") is False else "related_negative"
    return "insufficient"


def _relation_of_kind(kind: str, row: dict[str, Any]) -> str:
    if kind in {"contradiction", "related_negative", "topic_only"}:
        return "contradiction" if kind == "contradiction" else "indirect"
    if kind == "support":
        return "support"
    relation = str(row.get("relation") or "")
    return relation if relation in {"contradiction", "indirect", "support"} else ""


def _negative_fact(row: dict[str, Any], kind: str) -> bool:
    value = row.get("negative_fact")
    if isinstance(value, bool):
        return value
    return kind in {"contradiction", "related_negative"}


def _bigrams(text: str) -> set[str]:
    value = re.sub(r"[\s\W_]+", "", text or "")
    return {value[i : i + 2] for i in range(len(value) - 1)}


def _text_overlap(left: str, right: str) -> float:
    a, b = _bigrams(left), _bigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _claim_object(claim: dict[str, Any], profile: CategoryProfile | None = None) -> str:
    """用品类对象词表识别宣传里提到的具体对象；识别不出返回空串。"""
    text = f"{claim.get('normalized_claim') or ''}{claim.get('original_text') or ''}"
    mapping = profile.claim_object_terms if profile is not None else ALL_CLAIM_OBJECT_TERMS
    for obj, terms in mapping.items():
        if any(term in text for term in terms):
            return obj
    return ""


def _claim_scope_terms(claim: dict[str, Any]) -> tuple[str, ...]:
    """可核查的过程、组成或机制须由同一命题的原文支持，不能用整体无效代替。"""
    text = f"{claim.get('normalized_claim') or ''} {claim.get('original_text') or ''}"
    terms = (
        "检测", "检验", "认证", "测试", "试验", "临床", "专利", "备案",
        "含量", "浓度", "纯度", "色素", "渗透", "转运", "通路", "细胞",
        "优于", "强于", "胜过",
    )
    scopes = [term for term in terms if term in text]
    # “配方温和”“成分能淡印”仍是可由直接体验反驳的功效承诺，
    # 不要求买家也写出“配方/成分”；只有纯组成声明才要求组成证据。
    outcomes = (
        "淡化", "淡印", "祛痘", "去痘", "修复", "温和", "亲肤", "保湿", "滋润",
        "美白", "控油", "抗皱", "防晒", "防滑", "耐磨", "减震", "透气", "抑菌",
        "杀菌", "舒适", "柔软", "吸收", "止痒", "抗敏", "抗过敏",
    )
    if not any(word in text for word in outcomes):
        scopes.extend(term for term in ("成分", "配方") if term in text)
    return tuple(scopes)


def claim_candidates(
    claim: dict[str, Any],
    clusters: list[dict[str, Any]],
    limit: int = 4,
    profile: CategoryProfile | None = None,
) -> list[dict[str, Any]]:
    """用方面兼容 + 对象约束 + 关键词重合做候选筛选，避免把全部评论交给 LLM。"""
    scored: list[tuple[float, dict[str, Any]]] = []
    claim_text = " ".join(
        [
            str(claim.get("normalized_claim") or ""),
            str(claim.get("original_text") or ""),
        ]
    )
    claim_obj = _claim_object(claim, profile)

    for cluster in clusters:
        cluster_obj = str(cluster.get("object") or "")
        if claim_obj and cluster_obj and claim_obj != cluster_obj:
            # 宣传说的是具体部件，就不能拿另一个部件的体验（包括笼统的“整体”）来反驳
            continue

        score = 0.0
        if aspects_compatible(str(claim.get("aspect") or ""), str(cluster.get("aspect") or "")):
            score += 3.0
            if claim.get("aspect") == cluster.get("aspect"):
                score += 1.0
        cluster_text = " ".join(
            [
                str(cluster.get("problem_name") or ""),
                str(cluster.get("description") or ""),
                " ".join(cluster.get("direction_distribution", {}).keys()),
            ]
        )
        overlap = _text_overlap(claim_text, cluster_text)
        if overlap >= 0.1:
            score += 2.0 + min(overlap, 0.5)
        if cluster_obj and cluster_obj in claim_text:
            score += 1.0
        if claim_obj and cluster_obj == claim_obj:
            score += 1.0
        if score >= 3.0:
            scored.append((score, cluster))
    scored.sort(key=lambda item: (item[0], item[1].get("review_count", 0)), reverse=True)
    return [cluster for _, cluster in scored[:limit]]


def _confidence_level(
    gap_type: str,
    claim: dict[str, Any],
    related: list[dict[str, Any]],
    evidence_ids: list[str],
) -> tuple[str, str]:
    if gap_type == "insufficient_evidence":
        return "insufficient", "没有足够的直接评论证据，或主张本身不可验证"
    if gap_type == "no_direct_conflict_found":
        return "insufficient", "未发现直接冲突不构成支持冲突的证据，也不证明宣传为真"
    n = len(set(evidence_ids))
    if n == 0:
        return "insufficient", "未选择任何评论证据"
    direct = any(
        claim.get("aspect") == cluster.get("aspect")
        or aspects_compatible(str(claim.get("aspect") or ""), str(cluster.get("aspect") or ""))
        for cluster in related
    )
    if not direct:
        level, basis = "low", "问题方面与宣传方面仅部分相关"
    elif n >= 4:
        level, basis = "high", f"{n} 条独立评论指向同一方面，证据一致性较高"
    elif n >= 2:
        level, basis = "medium", f"{n} 条独立评论指向同一方面"
    else:
        level, basis = "low", "仅 1 条独立评论，只能作为线索"

    ocr_confidence = claim.get("ocr_confidence")
    if claim.get("source") == "image_ocr" and ocr_confidence is not None and ocr_confidence < 0.8:
        level = "low"
        basis += "；宣传文字来自低置信度 OCR"
    return level, basis


def _limitations(claim: dict[str, Any], sample: dict[str, Any], n: int) -> list[str]:
    items = [
        f"仅覆盖采集到的低星评价（有效样本 {sample.get('valid_review_count', 0)} 条），不能推断全体买家",
        "问题由 LLM 抽取并经规则聚类，方面归类可能存在差异",
    ]
    if claim.get("source") == "image_ocr":
        items.append(f"宣传文字来自图片 OCR，识别置信度 {claim.get('ocr_confidence')}")
    if n <= 2:
        items.append("相关评论数量少，只能作为进一步核查的线索")
    return items


class GapAnalyzer:
    def __init__(
        self,
        client: ChatLike,
        model: str,
        out_dir: Path,
        config: GapConfig | None = None,
        profile: CategoryProfile | None = None,
    ):
        self.client = client
        self.model = model
        self.out_dir = out_dir
        self.config = config or GapConfig()
        self.profile = profile or get_profile("generic")
        self.prompt_version = f"{self.config.prompt_version}:{self.profile.key}"
        self.cache_path = out_dir / "gap_cache.jsonl"
        self.calls_path = out_dir / "gap_calls.jsonl"
        self.raw_path = out_dir / "gap_raw_responses.jsonl"
        self.cache: dict[str, dict[str, Any]] = {}
        self._io_lock = threading.Lock()
        self._usage_lock = threading.Lock()
        self.usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "reasoning_tokens": 0,
            "calls": 0,
            "failed_calls": 0,
        }
        self._load_cache()

    def _load_cache(self) -> None:
        if not self.cache_path.exists():
            return
        with self.cache_path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("key") and item.get("status") == "ok":
                    self.cache[item["key"]] = item

    def _append(self, path: Path, payload: dict[str, Any]) -> None:
        # 并发判断会同时写同一个日志；加锁避免行内容交错。
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with self._io_lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()

    def _build_prompt(
        self,
        claim: dict[str, Any],
        candidates: list[dict[str, Any]],
        sample: dict[str, Any],
    ) -> str:
        payload = {
            "claim": {
                "aspect": claim.get("aspect"),
                "claim_type": claim.get("claim_type"),
                "normalized_claim": claim.get("normalized_claim"),
                "original_text": claim.get("original_text"),
                "source": claim.get("source"),
            },
            "sample": {
                "product_id": sample.get("product_id"),
                "valid_review_count": sample.get("valid_review_count"),
                "note": "仅低星评价，不能代表全体买家",
            },
            "candidates": [
                {
                    "cluster_id": cluster.get("cluster_id"),
                    "aspect": cluster.get("aspect"),
                    "problem_name": cluster.get("problem_name"),
                    "description": cluster.get("description"),
                    "review_count": cluster.get("review_count"),
                    "review_ids": [row["review_id"] for row in self._visible_evidence(claim, cluster)],
                    "directions": list((cluster.get("direction_distribution") or {}).keys()),
                    "evidence": self._visible_evidence(claim, cluster),
                }
                for cluster in candidates
            ],
        }
        return (
            "请判断这条宣传主张与候选评价证据之间是否存在体验落差。\n"
            + json.dumps(payload, ensure_ascii=False)
        )

    def _visible_evidence(
        self, claim: dict[str, Any], cluster: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """只暴露有原文的簇成员，按与主张的字面相关性选取，保持确定顺序。"""
        members = {str(rid) for rid in cluster.get("review_ids") or []}
        rows: dict[str, dict[str, Any]] = {}
        for rep in cluster.get("representative_reviews") or []:
            rid = str(rep.get("review_id"))
            evidence = _clean(rep.get("evidence"))
            if rid in members and evidence:
                rows[rid] = {"review_id": rid, "star": rep.get("star"), "evidence": evidence}
        for rid, text in (cluster.get("review_evidence") or {}).items():
            rid, evidence = str(rid), _clean(text)
            if rid in members and evidence:
                rows[rid] = {
                    "review_id": rid,
                    "star": (rows.get(rid) or {}).get("star"),
                    "evidence": evidence,
                }
        claim_text = f"{claim.get('normalized_claim') or ''} {claim.get('original_text') or ''}"
        ordered = sorted(
            rows.values(), key=lambda row: _text_overlap(claim_text, row["evidence"]), reverse=True
        )
        return ordered[: max(0, self.config.max_evidence_per_cluster)]

    def _call_llm(
        self,
        claim: dict[str, Any],
        candidates: list[dict[str, Any]],
        sample: dict[str, Any],
    ) -> dict[str, Any] | None:
        user_prompt = self._build_prompt(claim, candidates, sample)
        # 同一个簇 ID/成员集合也可能有不同原文；缓存必须绑定实际送入的证据与规则。
        key = _hash("|".join([self.model, self.prompt_version, SYSTEM_PROMPT, user_prompt]))
        cached = self.cache.get(key)
        if cached:
            return cached.get("result")

        for attempt in range(2):
            request_key = _hash("|".join([self.model, self.prompt_version, SYSTEM_PROMPT, user_prompt]))
            if request_key in self.cache:
                return self.cache[request_key].get("result")
            try:
                response = self.client.chat_json(SYSTEM_PROMPT, user_prompt)
            except LLMError as exc:
                with self._usage_lock:
                    self.usage["failed_calls"] += 1
                self._append(
                    self.calls_path,
                    {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "claim_id": claim.get("claim_id"),
                        "status": "error",
                        "attempt": attempt + 1,
                        "error": str(exc)[:300],
                    },
                )
                continue

            usage = response.usage or {}
            with self._usage_lock:
                self.usage["calls"] += 1
                for name in (
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "reasoning_tokens",
                ):
                    value = usage.get(name) or 0
                    if isinstance(value, int):
                        self.usage[name] += value
            self._append(
                self.calls_path,
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "claim_id": claim.get("claim_id"),
                    "status": "ok",
                    "attempt": attempt + 1,
                    "latency_ms": response.latency_ms,
                    "usage": usage,
                },
            )
            self._append(
                self.raw_path,
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "claim_id": claim.get("claim_id"),
                    "content": response.content[:12000],
                },
            )
            try:
                result = parse_json_content(response.content)
            except (ValueError, json.JSONDecodeError):
                user_prompt = (
                    "上一次输出无法解析为 JSON。请严格按格式重新输出，只返回 JSON。\n"
                    + self._build_prompt(claim, candidates, sample)
                )
                continue

            self.cache[request_key] = {
                "key": request_key,
                "request_sha1": _hash(user_prompt),
                "status": "ok",
                "result": result,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self._append(
                self.cache_path,
                {
                    "key": request_key,
                    "request_sha1": _hash(user_prompt),
                    "status": "ok",
                    "result": result,
                    "ts": self.cache[request_key]["ts"],
                },
            )
            return result
        return None

    def _base_gap(self, claim: dict[str, Any]) -> dict[str, Any]:
        return {
            "gap_id": "G" + _hash(str(claim.get("claim_id")), 12),
            "claim_id": claim.get("claim_id"),
            "claim_aspect": claim.get("aspect"),
            "claim_type": claim.get("claim_type"),
            "claim_text": claim.get("normalized_claim"),
            "claim_original_text": claim.get("original_text"),
            "claim_source": claim.get("source"),
            "claim_source_image": claim.get("source_image"),
            "related_clusters": [],
            "gap_type": "insufficient_evidence",
            "evidence_review_ids": [],
            "independent_review_count": 0,
            "finding": "",
            "confidence_level": "insufficient",
            "confidence_basis": "",
            "limitations": [],
            "reviewed_by": "rules",
        }

    def _rule_only_gap(self, claim: dict[str, Any]) -> dict[str, Any] | None:
        """不调用模型就能确定的判断；返回 None 表示需要证据匹配。

        这里的规则必须和 analyze 中的分支保持一致：预取阶段用它判断
        哪些主张真的需要 LLM，主循环用它直接产出结果。
        """
        claim_type = str(claim.get("claim_type") or "")
        aspect = str(claim.get("aspect") or "")
        claim_text = f"{claim.get('normalized_claim') or ''}{claim.get('original_text') or ''}"
        if claim_type not in VERIFIABLE_TYPES:
            return {
                "finding": "该文案属于抽象营销表达或无关文字，无法用评论证据验证",
                "confidence_basis": "不可验证的宣传类型",
                "limitations": ["未做证据匹配"],
            }
        if claim_type == "objective_param" and aspect == "其他":
            return {
                "finding": "该文案是款式/品类等客观描述，不构成可验证的性能承诺",
                "confidence_basis": "客观参数，无需与体验证据对比",
                "limitations": ["未做证据匹配"],
            }
        if claim_type == "size_spec" and not any(
            word in claim_text
            for word in ("标准", "正码", "不偏", "合脚", "偏大", "偏小", "码数")
        ):
            return {
                "gap_type": "no_direct_conflict_found",
                "finding": "该主张只说明尺寸、容量或组合规格，未承诺具体使用体验；相关体验评价不构成对该规格描述的直接反驳",
                "confidence_basis": "规格或组合选项与具体使用体验属于不同命题",
            }
        if claim_type == "price_value" and not any(
            word in claim_text for word in ("实惠", "性价比", "划算", "便宜", "贵", "值")
        ):
            return {
                "gap_type": "no_direct_conflict_found",
                "finding": "该主张只陈述价格数值，评论对价格高低的感受不构成对该事实的冲突",
                "confidence_basis": "价格事实与价格评价属于不同命题",
            }
        if (
            claim_type == "objective_param"
            and aspect == "外观与色差"
            and any(word in claim_text for word in ("包含", "可选", "颜色分类"))
        ):
            return {
                "gap_type": "no_direct_conflict_found",
                "finding": "该主张只说明可选颜色，未对颜色一致性或实物色差作出承诺",
                "confidence_basis": "规格描述与颜色体验属于不同命题",
            }
        return None

    def analyze(
        self,
        claims: list[dict[str, Any]],
        clusters: list[dict[str, Any]],
        sample: dict[str, Any],
        on_progress: Any = None,
    ) -> dict[str, Any]:
        started = time.time()
        gaps: list[dict[str, Any]] = []
        errors: list[str] = []
        cluster_by_id = {str(cluster.get("cluster_id")): cluster for cluster in clusters}
        claims_with_candidates = 0

        # 预取：把真正需要模型判断的主张并发跑掉（单条 5–15 秒，串行会拖到几分钟）。
        # 规则类判断与“无候选簇”不需要模型，仍在主循环里按原顺序产出。
        prefetch: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        for claim in claims:
            if self._rule_only_gap(claim) is not None:
                continue
            candidates = claim_candidates(
                claim,
                clusters,
                limit=self.config.max_candidates,
                profile=self.profile,
            )
            if candidates:
                prefetch[str(claim.get("claim_id"))] = (claim, candidates)
        llm_results: dict[str, Any] = {}
        if prefetch:
            keys = list(prefetch)
            total = len(keys)
            progress_state = {"done": 0}
            progress_lock = threading.Lock()

            def run_one(key: str):
                result = self._call_llm(prefetch[key][0], prefetch[key][1], sample)
                if on_progress is not None:
                    with progress_lock:
                        progress_state["done"] += 1
                        try:
                            on_progress(progress_state["done"], total)
                        except Exception:  # noqa: BLE001 - 进度回调不能影响判断
                            pass
                return result

            ordered = ordered_map(run_one, keys)
            llm_results = dict(zip(keys, ordered))

        for claim in claims:
            gap = self._base_gap(claim)
            rule_only = self._rule_only_gap(claim)
            if rule_only is not None:
                gap.update(rule_only)
                if "gap_type" not in rule_only:
                    gap["limitations"] = ["未做证据匹配"]
                    gaps.append(gap)
                    continue
                gap["limitations"] = _limitations(claim, sample, 0)
                gaps.append(gap)
                continue

            candidates = claim_candidates(
                claim,
                clusters,
                limit=self.config.max_candidates,
                profile=self.profile,
            )
            if not candidates:
                gap["gap_type"] = "no_direct_conflict_found"
                gap["finding"] = "当前采集到的低星评价中，未发现与该主张直接相关的具体问题"
                gap["confidence_level"] = "insufficient"
                gap["confidence_basis"] = "没有匹配到相关方面或问题簇，属于“未发现”，不等于已证实"
                gap["limitations"] = _limitations(claim, sample, 0)
                gaps.append(gap)
                continue

            claims_with_candidates += 1
            result = llm_results.get(str(claim.get("claim_id")))
            if not isinstance(result, dict):
                gap["finding"] = "LLM 复核失败，未生成落差判断"
                gap["confidence_basis"] = "调用或解析失败"
                gap["limitations"] = _limitations(claim, sample, 0)
                errors.append(f"{claim.get('claim_id')}: LLM 复核失败")
                gaps.append(gap)
                continue

            gap_type = str(result.get("gap_type") or "").strip()
            if gap_type not in GAP_TYPES:
                gap_type = "insufficient_evidence"
                errors.append(f"{claim.get('claim_id')}: 非法 gap_type，已降级")

            related_ids = [
                str(value)
                for value in (result.get("related_cluster_ids") or [])
                if str(value) in {str(c.get("cluster_id")) for c in candidates}
            ]
            allowed_reviews: set[str] = set()
            for cluster_id in related_ids:
                allowed_reviews.update(
                    row["review_id"]
                    for row in self._visible_evidence(claim, cluster_by_id[cluster_id])
                )
            requested_ids = list(dict.fromkeys(
                str(value) for value in (result.get("evidence_review_ids") or [])
            ))
            evidence_ids = [
                value for value in requested_ids if value in allowed_reviews
            ]
            rejected_ids = [value for value in requested_ids if value not in allowed_reviews]

            if gap_type in {"potential_conflict", "partial_gap"} and (
                not related_ids or not evidence_ids
            ):
                gap_type = "insufficient_evidence"
                errors.append(f"{claim.get('claim_id')}: 冲突判断缺少有效证据，已降级")

            # 逐条证据分类：只有“明确矛盾/具体负面”才能支撑冲突判断。
            topic_only_downgraded = False
            detail_rows = []
            for row in result.get("evidence") or []:
                if not isinstance(row, dict):
                    continue
                rid = str(row.get("review_id") or "")
                if rid not in evidence_ids:
                    continue
                kind = _evidence_kind(row)
                detail_rows.append({
                    "review_id": rid,
                    "relation": _relation_of_kind(kind, row),
                    "evidence_kind": kind,
                    "negative_fact": _negative_fact(row, kind),
                    "quality": str(row.get("fact_quality") or "")
                    if str(row.get("fact_quality") or "") in {"concrete", "partial", "vague"} else "",
                    "severity": str(row.get("severity") or "")
                    if str(row.get("severity") or "") in {"minor", "moderate", "major", "critical"} else "",
                    "quote": _clean(row.get("quote"))[:120],
                })

            if detail_rows and gap_type in {"potential_conflict", "partial_gap"}:
                kinds = {row["evidence_kind"] for row in detail_rows}
                if not kinds & {"contradiction", "related_negative"}:
                    gap.setdefault("llm_finding_raw", _clean(result.get("finding")))
                    gap["decision_validation"] = {
                        "reason": "evidence_is_topic_mention_only",
                        "evidence_kinds": sorted(kinds),
                        "rejected_gap_type": gap_type,
                    }
                    gap_type = "no_direct_conflict_found"
                    evidence_ids = []
                    topic_only_downgraded = True
                elif gap_type == "potential_conflict" and "contradiction" not in kinds:
                    gap_type = "partial_gap"

            finding = _clean(result.get("finding"))[:160]
            if not finding:
                finding = "LLM 未给出可读结论，已降级为证据不足"
                gap_type = "insufficient_evidence"
            if topic_only_downgraded:
                finding = ("已提供的评论只提到相关属性，没有陈述具体不利事实，"
                           "未发现对该承诺的直接反驳；这不证明宣传为真")

            if rejected_ids:
                gap["llm_finding_raw"] = finding
                finding = (
                    f"仅保留 {len(evidence_ids)} 条已提供原文的评论作为核查线索；原判断含未展示的证据引用，需人工复核"
                    if evidence_ids
                    else "原判断没有引用已提供原文的有效证据，当前证据不足，需人工核查"
                )
                gap["evidence_validation"] = {
                    "rejected_review_ids": rejected_ids,
                    "reason": "not_in_visible_evidence_of_related_clusters",
                }

            scope_terms = _claim_scope_terms(claim)
            quoted_text = " ".join(
                row["evidence"] for cid in related_ids
                for row in self._visible_evidence(claim, cluster_by_id[cid])
                if row["review_id"] in evidence_ids
            )
            if (
                gap_type in {"potential_conflict", "partial_gap"}
                and scope_terms and not any(term in quoted_text for term in scope_terms)
            ):
                gap.setdefault("llm_finding_raw", finding)
                gap["decision_validation"] = {
                    "reason": "evidence_does_not_address_claim_scope",
                    "required_scope_terms": list(scope_terms),
                    "rejected_gap_type": gap_type,
                }
                gap_type = "no_direct_conflict_found"
                evidence_ids = []
                finding = "已提供的评论原文未直接核查该宣传所述的过程、组成或机制，未发现同一命题的直接反驳；这不证明宣传为真"

            related = [cluster_by_id[cid] for cid in related_ids]
            level, basis = _confidence_level(gap_type, claim, related, evidence_ids)

            gap["related_clusters"] = related_ids
            gap["gap_type"] = gap_type
            gap["gap_type_label"] = GAP_TYPE_LABELS.get(gap_type, gap_type)
            gap["evidence_review_ids"] = evidence_ids
            gap["independent_review_count"] = len(set(evidence_ids))
            gap["finding"] = finding
            gap["llm_reasoning"] = _clean(result.get("reasoning"))[:120]
            gap["confidence_level"] = level
            gap["confidence_basis"] = basis
            gap["limitations"] = _limitations(claim, sample, len(evidence_ids))
            gap["reviewed_by"] = "llm"
            if detail_rows:
                gap["evidence_detail"] = detail_rows
            evidence_index: dict[str, dict[str, Any]] = {}
            for cluster_id in related_ids:
                cluster = cluster_by_id[cluster_id]
                for row in self._visible_evidence(claim, cluster):
                    rid = row["review_id"]
                    evidence_index.setdefault(
                        rid,
                        {
                            "review_id": rid,
                            "cluster_id": cluster_id,
                            "star": row.get("star"),
                            "evidence": row["evidence"],
                        },
                    )
            gap["review_evidence"] = [
                evidence_index[rid] for rid in evidence_ids if rid in evidence_index
            ]
            gaps.append(gap)

        by_type: dict[str, int] = {}
        by_confidence: dict[str, int] = {}
        for gap in gaps:
            gap["gap_type_label"] = GAP_TYPE_LABELS.get(gap["gap_type"], gap["gap_type"])
            by_type[gap["gap_type"]] = by_type.get(gap["gap_type"], 0) + 1
            by_confidence[gap["confidence_level"]] = (
                by_confidence.get(gap["confidence_level"], 0) + 1
            )

        return {
            "prompt_version": self.prompt_version,
            "model": self.model,
            "system_prompt": SYSTEM_PROMPT,
            "claim_count": len(claims),
            "verifiable_claim_count": sum(
                1 for claim in claims if claim.get("claim_type") in VERIFIABLE_TYPES
            ),
            "claims_with_candidates": claims_with_candidates,
            "gap_count": len(gaps),
            "by_type": by_type,
            "by_confidence": by_confidence,
            "usage": self.usage,
            "elapsed_seconds": round(time.time() - started, 2),
            "errors": errors,
            "gaps": gaps,
        }
