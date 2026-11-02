"""商品宣传主张抽取：把标题、属性、OCR 宣传语拆成可比较的具体主张。

只做文本结构化，不判断宣传真假；真假判断在 gap_analyzer 中基于评论证据完成。
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

from .categories import CategoryProfile, get_profile
from .claim_schema import (
    Claim,
    SOURCE_RANK,
    dedupe_claims,
    filter_actionable_claims,
    validate_claim,
)
from .llm_client import LLMError, parse_json_content
from .parallel import ordered_map

PROMPT_VERSION = "claim-extract-v4"

SYSTEM_PROMPT_TEMPLATE = """你是电商商品宣传文案的结构化抽取器。你只把原文拆成可比较的宣传主张，不判断真假、不补充原文没有的信息。

【任务】
输入若干条宣传文本 texts，每条有 text_id、text、source。输出 claims 数组，把每条文本拆成 0~N 条具体主张。

【当前商品品类】{category_label}

【硬性规则】
1. 复合宣传必须拆开：如“防滑耐磨减震透气”应拆成防滑、耐磨、减震、透气四条，不能合并成一条。
2. original_text 必须是该条 text 中的连续片段，逐字复制；不得改写、拼接、加省略号。找不到依据就不要输出。
3. normalized_claim 是规范化命题，不超过 40 字，不含品牌名、店铺名、促销话术；例如“商品具有透气性能”“商品含烟酰胺成分”“规格覆盖 39-44”。
4. 不凭空补充原文没有的功能、材质、参数或承诺。
5. 每条输入文本至少返回一条 claim；纯品牌名、店铺名、收藏加购、发货说明、推荐指数、货号等返回 claim_type=irrelevant_noise。
6. 抽象口号（时尚潮流、百搭、品质生活等）返回 claim_type=marketing_slogan，aspect=其他，不得包装成可验证的功能。
7. 图片 OCR 可能有错字，只有语义清楚时才抽取；不确定的按 irrelevant_noise 处理。
8. 规格类信息（尺码、颜色、容量、型号、净含量）属于同一规格，各输出一条主张，不要按每个选项拆条。
9. 同一批次内意思重复的主张只保留一条，例如“深层补水”“持久补水”若指同一承诺，只保留一条。
10. 品类词、风格词（男鞋/老爹鞋/大容量/ins风）属于 objective_param，aspect=其他，不是功能承诺。
11. confidence 是“该主张确实来自这段原文”的抽取置信度（0~1），不是宣传真实性。

【aspect 只能取以下值】
{aspect_list}

【claim_type 只能取以下值】
functional（功能/性能承诺，如防滑、续航、祛痘）
material_craft（材质/成分/工艺）
size_spec（尺寸/规格/容量/型号）
comfort_experience（舒适/肤感/口感/使用体验）
price_value（价格/性价比/优惠）
objective_param（型号/货号/颜色/重量/客观参数）
marketing_slogan（抽象营销文案）
irrelevant_noise（品牌/店铺/促销/噪声）

【输出格式】
{{"claims":[{{"text_id":"T01","aspect":"功能与性能","claim_type":"functional","normalized_claim":"商品具有防滑性能","original_text":"防滑","confidence":0.95}}]}}

只输出 JSON 对象，不要 markdown，不要解释。"""


def build_system_prompt(profile: CategoryProfile) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        category_label=profile.label,
        aspect_list="、".join(profile.aspects),
    )


class ChatLike(Protocol):
    def chat_json(self, system: str, user: str) -> Any: ...


@dataclass
class ClaimConfig:
    batch_size: int = 5
    max_chars_per_batch: int = 1800
    repair_retries: int = 1
    prompt_version: str = PROMPT_VERSION


def _hash(text: str, length: int = 20) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def build_raw_texts(export: dict[str, Any]) -> list[dict[str, Any]]:
    """从采集 JSON 构建待抽取文本，按文本去重并保留更可靠的来源。"""
    product = export.get("product") or {}
    entries: list[dict[str, Any]] = []

    def add(text: str, source: str, confidence: float | None = None, image: str = "") -> None:
        value = _clean_text(text)
        if value:
            entries.append(
                {
                    "text": value,
                    "source": source,
                    "confidence": confidence,
                    "image": image,
                }
            )

    add(product.get("title", ""), "title", 1.0)
    add(product.get("subtitle", ""), "detail_text", 1.0)

    price_text = _clean_text(product.get("price_text"))
    if price_text:
        add(f"价格：{price_text} 元", "attribute", 1.0)

    for name, values in (product.get("attributes") or {}).items():
        if not (isinstance(values, list) and values):
            continue
        cleaned = [_clean_text(item) for item in values if _clean_text(item)]
        if not cleaned:
            continue
        if any(tag in str(name) for tag in ("尺码", "规格", "尺寸", "型号")):
            sizes = []
            for item in cleaned:
                for token in re.findall(r"\d{2,3}", item):
                    if token not in sizes:
                        sizes.append(token)
            extra = "；标准运动码" if any("标准运动码" in item for item in cleaned) else ""
            rendered = f"尺码：{'、'.join(sizes)}{extra}" if sizes else "、".join(cleaned)
        elif "颜色" in str(name):
            colors = []
            for item in cleaned:
                base = re.sub(r"【[^】]*】", "", item).strip(" .。")
                if base and base not in colors:
                    colors.append(base)
            rendered = f"颜色分类：{'、'.join(colors)}"
        else:
            rendered = "、".join(cleaned)
        add(f"{name}：{rendered}", "attribute", 1.0)

    for claim in export.get("promotional_claims") or []:
        if not isinstance(claim, dict):
            continue
        add(
            claim.get("text", ""),
            str(claim.get("source") or "image_ocr"),
            claim.get("confidence"),
            str(claim.get("image") or ""),
        )

    best: dict[str, dict[str, Any]] = {}
    for entry in entries:
        key = entry["text"]
        current = best.get(key)
        if current is None:
            best[key] = entry
            continue
        new_rank = (SOURCE_RANK.get(entry["source"], 0), entry.get("confidence") or 0)
        old_rank = (SOURCE_RANK.get(current["source"], 0), current.get("confidence") or 0)
        if new_rank > old_rank:
            best[key] = entry

    ordered = sorted(
        best.values(),
        key=lambda item: (-SOURCE_RANK.get(item["source"], 0), -float(item.get("confidence") or 0)),
    )
    return [
        {"text_id": f"T{index:02d}", **entry} for index, entry in enumerate(ordered, 1)
    ]


def build_user_prompt(texts: list[dict[str, Any]]) -> str:
    payload = {
        "texts": [
            {
                "text_id": item["text_id"],
                "text": item["text"],
                "source": item["source"],
            }
            for item in texts
        ]
    }
    return (
        "请按系统规则抽取每条文本中的宣传主张；每条文本至少返回一条 claim。\n"
        + json.dumps(payload, ensure_ascii=False)
    )


class ClaimExtractor:
    def __init__(
        self,
        client: ChatLike,
        model: str,
        out_dir: Path,
        config: ClaimConfig | None = None,
        profile: CategoryProfile | None = None,
    ):
        self.client = client
        self.model = model
        self.out_dir = out_dir
        self.config = config or ClaimConfig()
        self.profile = profile or get_profile("generic")
        self.prompt_version = f"{self.config.prompt_version}:{self.profile.key}"
        self.system_prompt = build_system_prompt(self.profile)
        self.cache_path = out_dir / "claim_cache.jsonl"
        self.calls_path = out_dir / "claim_calls.jsonl"
        self.raw_path = out_dir / "claim_raw_responses.jsonl"
        self.errors_path = out_dir / "claim_validation_errors.jsonl"
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
        # 并发批次会同时写同一个日志；加锁避免行内容交错。
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with self._io_lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()

    def _batch_key(self, batch: list[dict[str, Any]]) -> str:
        raw = "|".join(f"{item['text_id']}:{_hash(item['text'], 16)}" for item in batch)
        return _hash(f"{raw}|{self.model}|{self.prompt_version}")

    def _make_batches(self, texts: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        batches: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        chars = 0
        for item in texts:
            length = len(item["text"])
            if current and (
                len(current) >= self.config.batch_size
                or chars + length > self.config.max_chars_per_batch
            ):
                batches.append(current)
                current, chars = [], 0
            current.append(item)
            chars += length
        if current:
            batches.append(current)
        return batches

    def _call_batch(
        self, batch: list[dict[str, Any]], raw_texts: dict[str, dict[str, Any]]
    ) -> tuple[list[Claim], list[str], dict[str, Any] | None]:
        user_prompt = build_user_prompt(batch)
        last_error = ""
        for attempt in range(self.config.repair_retries + 1):
            try:
                response = self.client.chat_json(self.system_prompt, user_prompt)
            except LLMError as exc:
                with self._usage_lock:
                    self.usage["failed_calls"] += 1
                self._append(
                    self.calls_path,
                    {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "text_ids": [item["text_id"] for item in batch],
                        "status": "error",
                        "attempt": attempt + 1,
                        "error": str(exc)[:300],
                    },
                )
                last_error = str(exc)
                continue

            usage = response.usage or {}
            with self._usage_lock:
                self.usage["calls"] += 1
                for key in (
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "reasoning_tokens",
                ):
                    value = usage.get(key) or 0
                    if isinstance(value, int):
                        self.usage[key] += value
            self._append(
                self.calls_path,
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "text_ids": [item["text_id"] for item in batch],
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
                    "text_ids": [item["text_id"] for item in batch],
                    "content": response.content[:30000],
                },
            )

            try:
                data = parse_json_content(response.content)
                claims_raw = data.get("claims")
                if not isinstance(claims_raw, list):
                    raise ValueError("缺少 claims 数组")
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = f"JSON 解析失败：{exc}"
                user_prompt = (
                    "上一次输出无法解析为要求的 JSON。请严格按格式重新输出，只返回 JSON。\n"
                    + build_user_prompt(batch)
                )
                continue

            claims: list[Claim] = []
            errors: list[str] = []
            for raw in claims_raw:
                claim, claim_errors = validate_claim(
                    raw, raw_texts, self.model, self.prompt_version, self.profile
                )
                if claim is not None and not claim_errors:
                    claims.append(claim)
                else:
                    errors.extend(claim_errors)
                    self._append(
                        self.errors_path,
                        {
                            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "errors": claim_errors,
                            "raw_claim": raw,
                        },
                    )

            covered = {claim.text_id for claim in claims}
            missing = [item["text_id"] for item in batch if item["text_id"] not in covered]
            if missing and attempt < self.config.repair_retries:
                last_error = f"缺少文本结果：{', '.join(missing)}"
                user_prompt = (
                    f"上一次输出缺少这些 text_id 的主张：{', '.join(missing)}。"
                    "请为每条文本至少返回一条 claim，只返回 JSON。\n"
                    + build_user_prompt(batch)
                )
                continue

            cache_key = self._batch_key(batch)
            self.cache[cache_key] = {
                "key": cache_key,
                "status": "ok",
                "claims": [claim.to_dict() for claim in claims],
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self._append(
                self.cache_path,
                {
                    "key": cache_key,
                    "status": "ok",
                    "claims": self.cache[cache_key]["claims"],
                    "ts": self.cache[cache_key]["ts"],
                },
            )
            return claims, errors, self.usage

        return [], [last_error or "调用失败"], self.usage

    def run(self, texts: list[dict[str, Any]], on_progress: Any = None) -> dict[str, Any]:
        started = time.time()
        raw_texts = {item["text_id"]: item for item in texts}
        claims: list[Claim] = []
        errors: list[str] = []

        batches = self._make_batches(texts)
        cached_batches: dict[int, list[Claim]] = {}
        pending_indices: list[int] = []
        pending_batches: list[list[dict[str, Any]]] = []
        for index, batch in enumerate(batches):
            cached = self.cache.get(self._batch_key(batch))
            if cached:
                cached_claims = []
                for raw in cached.get("claims") or []:
                    claim, claim_errors = validate_claim(
                        raw, raw_texts, self.model, self.prompt_version, self.profile
                    )
                    if claim is not None and not claim_errors:
                        cached_claims.append(claim)
                cached_batches[index] = cached_claims
            else:
                pending_indices.append(index)
                pending_batches.append(batch)

        # 批次之间互相独立，并发调用；结果顺序与串行版本一致。
        # 进度按“已返回的批次数”推进，因此能反映真实耗时，而不是只在结束时跳一下。
        total_batches = len(pending_batches)
        progress_state = {"done": 0}
        progress_lock = threading.Lock()

        def run_batch(batch: list[dict[str, Any]]):
            result = self._call_batch(batch, raw_texts)
            if on_progress is not None:
                with progress_lock:
                    progress_state["done"] += 1
                    try:
                        on_progress(progress_state["done"], total_batches)
                    except Exception:  # noqa: BLE001 - 进度回调不能影响抽取
                        pass
            return result

        batch_results = ordered_map(run_batch, pending_batches)
        pending_results = dict(zip(pending_indices, batch_results))
        for index in range(len(batches)):
            if index in cached_batches:
                claims.extend(cached_batches[index])
            else:
                batch_claims, batch_errors, _ = pending_results[index]
                claims.extend(batch_claims)
                errors.extend(batch_errors)

        before = len(claims)
        claims = dedupe_claims(claims)
        # 只保留对商品本身的承诺（规格/容量/质量/功效/体验/价格）；
        # 活动时间、平台活动名、描述图文本身的元叙述不进分析与报告。
        claims, filtered_reasons, filtered_samples = filter_actionable_claims(claims)
        by_type: dict[str, int] = {}
        for claim in claims:
            by_type[claim.claim_type] = by_type.get(claim.claim_type, 0) + 1

        return {
            "prompt_version": self.prompt_version,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "input_text_count": len(texts),
            "claim_count": len(claims),
            "deduped_count": before - len(claims) - sum(filtered_reasons.values()),
            "filtered": {
                "count": sum(filtered_reasons.values()),
                "reasons": filtered_reasons,
                "samples": filtered_samples,
            },
            "by_type": by_type,
            "usage": self.usage,
            "elapsed_seconds": round(time.time() - started, 2),
            "errors": errors,
            "claims": [claim.to_dict() for claim in claims],
        }


def run_claim_extraction(
    export: dict[str, Any],
    client: ChatLike,
    model: str,
    out_dir: Path,
    config: ClaimConfig | None = None,
    profile: CategoryProfile | None = None,
    on_progress: Any = None,
) -> dict[str, Any]:
    texts = build_raw_texts(export)
    extractor = ClaimExtractor(client, model, out_dir, config, profile)
    return extractor.run(texts, on_progress=on_progress)


def load_export(product_id: str, exports_dir: Path) -> tuple[dict[str, Any], str]:
    """读取该商品信息最完整的一份采集 JSON。

    不能简单取“最新文件”：采集失败（如被平台验证拦截）也会写出一份空结果，
    按 mtime 取最新会把宣传文本丢成只剩标题。这里按信息量排序，同等信息量再取最新。
    """
    candidates = sorted(
        exports_dir.glob(f"tmall_{product_id}_*.json"), key=lambda path: path.stat().st_mtime
    )
    if not candidates:
        raise FileNotFoundError(f"未找到商品 {product_id} 的采集 JSON：{exports_dir}")

    best: tuple[int, float, dict[str, Any], str] | None = None
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        product = data.get("product") or {}
        score = (
            len(data.get("promotional_claims") or []) * 2
            + len(product.get("images") or [])
            + (2 if product.get("title") else 0)
            + (1 if product.get("attributes") else 0)
        )
        mtime = path.stat().st_mtime
        if best is None or (score, mtime) > (best[0], best[1]):
            best = (score, mtime, data, str(path))
    if best is None:
        raise FileNotFoundError(f"商品 {product_id} 的采集 JSON 都无法解析")
    return best[2], best[3]
