"""LLM 负面问题抽取：批量调用、严格校验、断点续跑。

重要：本模块不会“猜”问题。模型输出无法通过校验（尤其是 evidence 不在原文中）时，
该问题会被排除并记录错误，而不是自动修补。
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
from .issue_schema import Issue, dedupe_issues, validate_issue
from .llm_client import ChatClient, LLMConfig, LLMError, parse_json_content
from .parallel import ordered_map

PROMPT_VERSION = "issue-extract-v6"

SYSTEM_PROMPT_TEMPLATE = """你是电商商品评论的“具体负面问题”抽取器。你只做信息抽取，不做总结、不做情感判断、不编造。

【任务】
输入若干条真实评论，对每条评论输出 issues 数组。

【当前商品品类】{category_label}
评论里提到的对象以该品类的实际部件为准；不要套用其他品类的部件名。

【硬性规则】
1. 只抽取评论中明确写出、或可由原文直接支撑的负面问题。低星不代表一定有问题；没有具体问题时 issues=[]。
2. **逐句扫描**：先按句号、分号、逗号、转折词（但是/而且/还/不过/另外/然后/并且/不仅）把评论切成语义单元，逐个单元判断是否含负面信息。长评论不要只抽第一处最明显的问题，后面的并列抱怨同样要抽。
3. 一条评论可以有 0~6 个问题。不同部件、不同表现要拆开，但同一现象不要重复计数：
   - 同一评论里对同一对象给出多个相近的负面形容（如“偏大偏小都试过”“忽软忽硬”），合并成一条，direction=other，不要按每次尝试拆成多条；
   - 某个不适是由同一条评论里已抽取的缺陷直接导致的（如“因为太硬所以疼”），合并进那条问题，不单独成条；只有无法归因时才单列。
4. **商品问题、物流问题、客服问题属于不同对象，必须分开输出**；不得把它们合并成一条“服务差/质量差”。
5. 输出前自查：原文中每一处明确抱怨是否都已覆盖（按规则 3 可合并的重复表述除外）；漏掉任何一处并列抱怨都算错误。
6. 禁止凭空泛化，但原文明确给出的整体评价可以保留为低信息量问题：
   - 原文只说“质量一般/质量差/做工差”时，可输出 object=整体、aspect=做工质量，manifestation 贴近原文；必须出现“质量/做工/材质”等评价词，纯情绪化的“最差的一次/垃圾/别买”不单独成条；
   - 原文只说“不值/有点贵/性价比低/价格偏高”时，可输出 object=价格、aspect=价格与性价比，manifestation 贴近原文；单纯与其他商品比价不单独成条；
   - 不得借此补充原文没有的具体缺陷（如原文只说质量差，不能写成开胶）。
7. 每个问题必须给出 object（具体部件/对象）、aspect（方面）、manifestation（具体表现，含方向）、direction、severity、evidence。
8. evidence 必须是该条评论原文中的连续片段，逐字复制；不得改写、拼接、加省略号或替换标点。找不到原文依据就不要输出该问题。
9. manifestation 只复述原文表达，不得加入原文没有的判断或比较。
10. 不得把中性或正面描述写成负面；不得把“偏大/偏小”“过硬/过软”“磨损/开胶”混为一谈。
11. 不把未赠送运费险、赠品、优惠等权益缺失当作商品问题。
12. severity 依据原文：low=轻微、不影响使用；medium=影响正常体验；high=明显故障、无法正常使用或强烈不适；判断不了用 unknown。
13. basis：explicit=原文直接表达；inferred=需要一步常识推断（仅在依据充分时使用）；不确定用 unknown。
14. confidence 是“该问题确实被原文支持”的参考置信度（0~1），不是统计概率。
15. 只输出 JSON 对象，不要 markdown、不要解释。

【object 建议】{object_hints}

【aspect 只能取以下值】
{aspect_list}

【aspect 判定提示】
{aspect_hints}

【object 判定提示】
- 优先用评论里出现的具体部件名；不要把所有问题都写成“商品”；
- 说不清具体部位时用“整体”。

【direction 建议值】
{direction_list}

【输出格式】
{{"results":[{{"review_id":"...","issues":[{{"object":"...","aspect":"...","manifestation":"...","direction":"...","severity":"low|medium|high|unknown","evidence":"...","basis":"explicit|inferred|unknown","confidence":0.0}}]}}]}}

必须为每条输入评论返回一个 result，issues 可以为空数组。"""

DIRECTION_OPTIONS = (
    "too_small, too_narrow, too_large, too_loose, too_hard, too_soft, too_thin, "
    "too_thick, too_heavy, too_light, unglued, broken, color_fading, color_mismatch, "
    "rubbing, slipping_off, slippery, unbreathable, odor, deformed, pilling, "
    "loose_thread, glue_residue, seam_split, sticky, drying, irritation, ineffective, "
    "leaking, stale, short_weight, jammed, none, other"
)


def build_system_prompt(profile: CategoryProfile) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        category_label=profile.label,
        object_hints=profile.prompt_object_hints,
        aspect_list="、".join(profile.aspects),
        aspect_hints=profile.prompt_aspect_hints,
        direction_list=DIRECTION_OPTIONS,
    )


class ChatLike(Protocol):
    def chat_json(self, system: str, user: str) -> Any: ...


@dataclass
class ExtractConfig:
    batch_size: int = 4
    max_chars_per_batch: int = 2600
    max_tokens_per_batch: int = 1500
    repair_retries: int = 1
    prompt_version: str = PROMPT_VERSION


def estimate_tokens(text: str) -> int:
    """粗略 token 估算：中文约 0.9 token/字，其他约 0.3 token/字符。"""
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text or ""))
    other = max(0, len(text or "") - cjk)
    return int(cjk * 0.9 + other * 0.3) + 1


def _hash(text: str, length: int = 16) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def cache_key(review: dict[str, Any], model: str, prompt_version: str) -> str:
    raw = "|".join(
        [
            str(review.get("review_id") or ""),
            _hash(str(review.get("text") or "")),
            model,
            prompt_version,
        ]
    )
    return _hash(raw, 20)


def build_user_prompt(reviews: list[dict[str, Any]]) -> str:
    payload = {
        "reviews": [
            {
                "review_id": review["review_id"],
                "star": review.get("star"),
                "content": review.get("text") or review.get("content") or "",
            }
            for review in reviews
        ]
    }
    return (
        "请按系统规则抽取每条评论中的具体负面问题。\n"
        "注意：evidence 必须逐字来自对应评论的 content；没有具体问题就返回空数组。\n"
        + json.dumps(payload, ensure_ascii=False)
    )


class IssueExtractor:
    def __init__(
        self,
        client: ChatLike,
        model: str,
        out_dir: Path,
        config: ExtractConfig | None = None,
        profile: CategoryProfile | None = None,
    ):
        self.client = client
        self.model = model
        self.out_dir = out_dir
        self.config = config or ExtractConfig()
        self.profile = profile or get_profile("generic")
        # 品类不同 → Prompt 不同 → 缓存键必须区分，避免跨品类复用
        self.prompt_version = f"{self.config.prompt_version}:{self.profile.key}"
        self.system_prompt = build_system_prompt(self.profile)
        self.cache_path = out_dir / "llm_cache.jsonl"
        self.calls_path = out_dir / "llm_calls.jsonl"
        self.raw_path = out_dir / "llm_raw_responses.jsonl"
        self.cache: dict[str, dict[str, Any]] = {}
        self._io_lock = threading.Lock()
        self._usage_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self.usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "reasoning_tokens": 0,
            "cached_tokens": 0,
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
                key = item.get("key")
                if key and item.get("status") in {"ok", "empty"} and not item.get("errors"):
                    self.cache[key] = item

    def _append(self, path: Path, payload: dict[str, Any]) -> None:
        # 并发批次会同时写同一个日志；加锁避免行内容交错。
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with self._io_lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()

    def _cached_issues(self, review: dict[str, Any]) -> list[Issue] | None:
        key = cache_key(review, self.model, self.prompt_version)
        item = self.cache.get(key)
        if not item:
            return None
        issues: list[Issue] = []
        for raw in item.get("issues") or []:
            issue, errors = validate_issue(
                raw, review, self.model, self.prompt_version, self.profile
            )
            if issue is None or errors:
                return None  # Never turn invalid cached evidence into a valid empty result.
            issues.append(issue)
        return issues

    def _make_batches(self, reviews: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        batches: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        chars = 0
        tokens = 0
        for review in reviews:
            length = len(review.get("text") or "")
            estimated = estimate_tokens(review.get("text") or "")
            if current and (
                len(current) >= self.config.batch_size
                or chars + length > self.config.max_chars_per_batch
                or tokens + estimated > self.config.max_tokens_per_batch
            ):
                batches.append(current)
                current, chars, tokens = [], 0, 0
            current.append(review)
            chars += length
            tokens += estimated
        if current:
            batches.append(current)
        return batches

    def _call_batch(
        self, batch: list[dict[str, Any]]
    ) -> tuple[dict[str, list[Issue]], dict[str, list[str]], list[str], dict[str, Any]]:
        """返回 (issues_by_review, errors_by_review, batch_errors, usage)。"""
        user_prompt = build_user_prompt(batch)
        system_prompt = self.system_prompt
        last_error = ""
        for attempt in range(self.config.repair_retries + 1):
            started = time.time()
            try:
                response = self.client.chat_json(system_prompt, user_prompt)
            except LLMError as exc:
                with self._usage_lock:
                    self.usage["failed_calls"] += 1
                self._append(
                    self.calls_path,
                    {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "review_ids": [r["review_id"] for r in batch],
                        "status": "error",
                        "attempt": attempt + 1,
                        "estimated_prompt_tokens": estimate_tokens(system_prompt + user_prompt),
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
                    "cached_tokens",
                ):
                    value = usage.get(key) or 0
                    if isinstance(value, int):
                        self.usage[key] += value
            self._append(
                self.calls_path,
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "review_ids": [r["review_id"] for r in batch],
                    "status": "ok",
                    "attempt": attempt + 1,
                    "latency_ms": response.latency_ms,
                    "estimated_prompt_tokens": estimate_tokens(system_prompt + user_prompt),
                    "usage": usage,
                },
            )
            self._append(
                self.raw_path,
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "review_ids": [r["review_id"] for r in batch],
                    "content": response.content[:20000],
                },
            )

            try:
                data = parse_json_content(response.content)
                results = data.get("results")
                if not isinstance(results, list):
                    raise ValueError("缺少 results 数组")
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = f"JSON 解析失败：{exc}"
                user_prompt = (
                    "上一次输出无法解析为要求的 JSON。请严格按格式重新输出，只返回 JSON。\n"
                    + build_user_prompt(batch)
                )
                continue

            expected = {r["review_id"] for r in batch}
            by_review: dict[str, list[Issue]] = {rid: [] for rid in expected}
            errors_by_review: dict[str, list[str]] = {}
            global_errors: list[str] = []

            for item in results:
                if not isinstance(item, dict):
                    global_errors.append("result 不是对象")
                    continue
                rid = str(item.get("review_id") or "")
                if rid not in expected:
                    global_errors.append(f"出现未知 review_id：{rid}")
                    continue
                issues_raw = item.get("issues")
                if not isinstance(issues_raw, list):
                    global_errors.append(f"{rid}: issues 不是数组")
                    continue
                review = next(r for r in batch if r["review_id"] == rid)
                for raw_issue in issues_raw:
                    issue, errors = validate_issue(
                        raw_issue, review, self.model, self.prompt_version, self.profile
                    )
                    if issue is not None and not errors:
                        by_review[rid].append(issue)
                    else:
                        errors_by_review.setdefault(rid, []).extend(errors)
                        self._append(
                            self.out_dir / "validation_errors.jsonl",
                            {
                                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                "review_id": rid,
                                "errors": errors,
                                "raw_issue": raw_issue,
                            },
                        )

            missing = expected - {str(item.get("review_id")) for item in results if isinstance(item, dict)}
            if missing:
                global_errors.append(f"缺少评论结果：{', '.join(sorted(missing))}")

            # 出现结构性错误时做一次修复重试
            if global_errors and attempt < self.config.repair_retries:
                last_error = "；".join(global_errors[:3])
                user_prompt = (
                    "上一次输出存在结构问题："
                    + last_error
                    + "。请修正，并为每条评论返回结果，只返回 JSON。\n"
                    + build_user_prompt(batch)
                )
                continue

            for rid in by_review:
                deduped = dedupe_issues(by_review[rid])
                if len(deduped) > 6:
                    errors_by_review.setdefault(rid, []).append("超过 6 个问题，已截断")
                    deduped = deduped[:6]
                by_review[rid] = deduped
            return by_review, errors_by_review, global_errors, self.usage

        return (
            {r["review_id"]: [] for r in batch},
            {r["review_id"]: [last_error or "调用失败"] for r in batch},
            [last_error or "调用失败"],
            self.usage,
        )

    def run(self, reviews: list[dict[str, Any]], on_progress: Any = None) -> dict[str, Any]:
        started = time.time()
        results: dict[str, dict[str, Any]] = {}
        pending: list[dict[str, Any]] = []

        for review in reviews:
            cached = self._cached_issues(review)
            if cached is not None:
                results[review["review_id"]] = {
                    "review_id": review["review_id"],
                    "star": review.get("star"),
                    "date": review.get("date", ""),
                    "sku_id": review.get("sku_id", ""),
                    "content": review.get("text", ""),
                    "issues": [issue.to_dict() for issue in cached],
                    "status": "cached",
                    "errors": [],
                }
            else:
                pending.append(review)

        # Persist each completed batch immediately; a later batch failure must
        # not discard already validated work. Final output remains input ordered.
        batches = self._make_batches(pending)
        total_batches = len(batches)
        progress_state = {"done": 0}
        progress_lock = threading.Lock()

        def run_batch(batch: list[dict[str, Any]]):
            by_review, errors_by_review, global_errors, _ = self._call_batch(batch)
            records = []
            for review in batch:
                rid = review["review_id"]
                issues = by_review.get(rid, [])
                errors = errors_by_review.get(rid, []) + global_errors
                status = "ok" if issues else "empty"
                if errors and not issues:
                    status = "validation_failed"
                elif errors and issues:
                    status = "partial_validation"

                record = {
                    "review_id": rid,
                    "star": review.get("star"),
                    "date": review.get("date", ""),
                    "sku_id": review.get("sku_id", ""),
                    "content": review.get("text", ""),
                    "issues": [issue.to_dict() for issue in issues],
                    "status": status,
                    "errors": errors,
                }
                records.append(record)
                key = cache_key(review, self.model, self.prompt_version)
                cached = {
                    "key": key,
                    "review_id": rid,
                    "status": status if status in {"ok", "empty"} else "failed",
                    "issues": [issue.to_dict() for issue in issues],
                    "errors": errors,
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                with self._cache_lock:
                    self.cache[key] = cached
                self._append(self.cache_path, cached)
            if on_progress is not None:
                with progress_lock:
                    progress_state["done"] += 1
                    try:
                        on_progress(progress_state["done"], total_batches)
                    except Exception:  # noqa: BLE001 - 进度回调不能影响抽取
                        pass
            return records

        batch_results = ordered_map(run_batch, batches)
        for records in batch_results:
            for record in records:
                results[record["review_id"]] = record

        ordered = [results[review["review_id"]] for review in reviews if review["review_id"] in results]
        issue_count = sum(len(item["issues"]) for item in ordered)
        with_issues = sum(1 for item in ordered if item["issues"])
        failed = [item for item in ordered if item["status"] == "validation_failed"]
        partial = [item for item in ordered if item["status"] == "partial_validation"]

        return {
            "prompt_version": self.prompt_version,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "input_review_count": len(reviews),
            "processed_review_count": len(ordered),
            "reviews_with_issues": with_issues,
            "issue_count": issue_count,
            "validation_failed_count": len(failed),
            "partial_validation_count": len(partial),
            "failed_review_ids": [item["review_id"] for item in failed],
            "usage": self.usage,
            "elapsed_seconds": round(time.time() - started, 2),
            "reviews": ordered,
        }


def run_extraction(
    reviews: list[dict[str, Any]],
    config: LLMConfig,
    out_dir: Path,
    extract_config: ExtractConfig | None = None,
    profile: CategoryProfile | None = None,
    on_progress: Any = None,
) -> dict[str, Any]:
    client = ChatClient(config)
    extractor = IssueExtractor(client, config.model, out_dir, extract_config, profile)
    return extractor.run(reviews, on_progress=on_progress)
