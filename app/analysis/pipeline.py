"""分析流水线：清洗 → LLM 抽取 → BGE 聚类 → 统计 → 结构化输出。

用法（在工作区根目录）：
  .venv\\Scripts\\python.exe -m analysis.pipeline --product-id 123456789012
需要在 app 目录下运行，或显式设置 PYTHONPATH=app。

没有 LLM 密钥时不会生成假结果：流程会停在 blocked_missing_llm，
但仍输出清洗数据与质量报告。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import time
from pathlib import Path
from typing import Any, Callable

APP_DIR = Path(__file__).resolve().parents[1]
ROOT = APP_DIR.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
from resources import resource_root
ROOT = resource_root()

from analysis import __version__  # noqa: E402
from analysis.categories import CategoryProfile, detect_category, get_profile  # noqa: E402
from analysis.claim_dedup import (  # noqa: E402
    grouped_by_type,
    grouped_conflict_evidence,
    group_near_duplicate_claims,
)
from analysis.claim_extractor import ClaimConfig, load_export, run_claim_extraction  # noqa: E402
from analysis.claim_schema import filter_claim_dicts  # noqa: E402
from analysis.clustering import cluster_issues  # noqa: E402
from analysis.embeddings import ModelUnavailable, embed_issues  # noqa: E402
from analysis.gap_analyzer import GapAnalyzer, GapConfig  # noqa: E402
from analysis.issue_extractor import ExtractConfig, run_extraction  # noqa: E402
from analysis.issue_schema import Issue, validate_issue  # noqa: E402
from analysis.llm_client import ChatClient, LLMConfig  # noqa: E402
from analysis.preprocessing import load_reviews  # noqa: E402
from analysis.statistics import build_statistics  # noqa: E402


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def input_digest(reviews: list[dict[str, Any]]) -> str:
    parts = []
    for review in sorted(reviews, key=lambda item: item["review_id"]):
        text_hash = hashlib.sha1((review.get("text") or "").encode("utf-8")).hexdigest()[:12]
        parts.append(f"{review['review_id']}:{text_hash}")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def clusters_fingerprint(clusters_payload: dict[str, Any]) -> str:
    """问题簇指纹：簇 ID + 成员问题 ID 的稳定哈希。

    落差结果写盘时记录该指纹；下游读取时若与当前 issue_clusters.json 不一致，
    说明聚类已变化，旧的落差结果必须失效重算，不能直接复用。
    """
    pairs = sorted(
        (str(cluster.get("cluster_id")), str(issue_id))
        for cluster in clusters_payload.get("clusters") or []
        for issue_id in cluster.get("member_issue_ids") or []
    )
    return hashlib.sha1(
        json.dumps(pairs, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]


def issues_from_extraction(
    payload: dict[str, Any], reviews: list[dict[str, Any]], profile: CategoryProfile | None = None
) -> tuple[list[Issue], list[dict[str, Any]]]:
    # 复验必须沿用抽取的品类；raw 字段会参与重新规范化和 ID 计算。
    profile = profile or get_profile(str(payload.get("prompt_version") or "").partition(":")[2] or "generic")
    review_by_id = {review["review_id"]: review for review in reviews}
    issues: list[Issue] = []
    errors: list[dict[str, Any]] = []
    for record in payload.get("reviews") or []:
        rid = str(record.get("review_id") or "")
        review = review_by_id.get(rid)
        if not review:
            errors.append({"review_id": rid, "error": "评论不在当前数据集中"})
            continue
        for raw in record.get("issues") or []:
            issue, issue_errors = validate_issue(
                raw, review,
                str(payload.get("model") or raw.get("model") or ""),
                str(payload.get("prompt_version") or raw.get("prompt_version") or ""),
                profile,
            )
            if issue is None:
                errors.append({"review_id": rid, "errors": issue_errors})
            else:
                issues.append(issue)
    return issues, errors


def _load_extracted(
    path: Path, reviews: list[dict[str, Any]], profile: CategoryProfile | None = None
) -> tuple[list[Issue], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    issues, errors = issues_from_extraction(payload, reviews, profile)
    meta = {
        "source": str(path),
        "model": payload.get("model", ""),
        "prompt_version": payload.get("prompt_version", ""),
        "revalidation_errors": errors,
        "revalidated_issue_count": len(issues),
    }
    if any("status" in row for row in payload.get("reviews") or [] if isinstance(row, dict)):
        meta["review_completion"] = extraction_completion(
            payload, [r for r in reviews if r.get("valid_for_analysis")], len(issues)
        )
    return issues, meta


def _extraction_failure(errors: list[Any]) -> tuple[str, str]:
    failure_text = " ".join(str(error).lower() for error in errors)
    if any(word in failure_text for word in ("connection", "remotedisconnected", "timeout", "timed out", "连接", "超时")):
        return "模型服务连接中断，问题抽取未完成，请稍后重试。", "transport"
    if any(word in failure_text for word in ("401", "unauthorized", "api key", "认证", "鉴权")):
        return "模型服务认证失败，请检查 API 配置后重试。", "authentication"
    return "评论问题抽取未完成或未通过校验，请重试分析。", "extraction"


def extraction_completion(
    payload: dict[str, Any], reviews: list[dict[str, Any]], validated_issue_count: int
) -> dict[str, Any]:
    """A returned request is not necessarily a completed extraction.

    Failed/partial/missing per-review outcomes must not be converted into valid
    empty observations. Count only current input IDs and retain usable facts for
    a retryable partial run. The original records remain unchanged on disk.
    """
    expected = {str(row["review_id"]) for row in reviews}
    by_id: dict[str, list[dict[str, Any]]] = {}
    for row in payload.get("reviews") or []:
        if isinstance(row, dict) and str(row.get("review_id") or "") in expected:
            by_id.setdefault(str(row["review_id"]), []).append(row)
    complete = sum(
        bool(by_id.get(rid)) and all(row.get("status") in {"ok", "empty", "cached"}
                                    and not row.get("errors") for row in by_id[rid])
        for rid in expected
    )
    incomplete = len(expected) - complete
    status = ("completed" if not incomplete else "partial"
              if complete or validated_issue_count else "failed")
    message, kind = "", ""
    if status == "failed":
        message, kind = _extraction_failure([error for rows in by_id.values()
                                            for row in rows for error in row.get("errors") or []])
    elif status == "partial":
        message, kind = "部分评论尚未完成问题抽取，已保留可用结果，请重试补全。", "partial_extraction"
    return {"status": status, "expected_review_count": len(expected),
            "completed_review_count": complete, "incomplete_review_count": incomplete,
            "missing_review_count": sum(rid not in by_id for rid in expected),
            "validated_issue_count": validated_issue_count, "retryable": status != "completed",
            "failure_reason": message, "error_kind": kind}


def _payload_stage_status(payload: dict[str, Any], records_key: str, success: str) -> str:
    """Batch APIs may return validation/transport errors without raising."""
    if not payload.get("errors"):
        return success
    usable = [row for row in payload.get(records_key) or [] if isinstance(row, dict)
              and row.get("confidence_basis") != "调用或解析失败"]
    return "partial" if usable else "failed"


def _load_gold(
    path: Path, reviews: list[dict[str, Any]], profile: CategoryProfile | None = None
) -> tuple[list[Issue], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    review_by_id = {review["review_id"]: review for review in reviews if review["valid_for_analysis"]}
    issues: list[Issue] = []
    errors: list[dict[str, Any]] = []
    for record in payload.get("reviews") or []:
        rid = str(record.get("review_id") or "")
        review = review_by_id.get(rid)
        if not review:
            continue
        for raw in record.get("issues") or []:
            issue, issue_errors = validate_issue(raw, review, "manual_gold", "gold-v1", profile)
            if issue is None:
                errors.append({"review_id": rid, "errors": issue_errors})
            else:
                issues.append(issue)
    meta = {
        "mode": "manual_gold",
        "model": "manual_gold",
        "prompt_version": "gold-v1",
        "source": str(path),
        "issue_count": len(issues),
        "revalidation_errors": errors,
        "note": "人工标注实验，仅用于验证聚类/统计链路，不是 LLM 输出",
    }
    return issues, meta


def _package_versions() -> dict[str, str]:
    versions = {"python": platform.python_version(), "analysis": __version__}
    for name in ("numpy", "scipy", "sklearn", "tokenizers", "onnxruntime"):
        try:
            module = __import__(name)
            versions[name] = getattr(module, "__version__", "")
        except Exception:  # noqa: BLE001
            versions[name] = "unavailable"
    return versions


def select_claim_export(
    inputs: list[str], product_id: str, exports_dir: Path
) -> tuple[dict[str, Any], str]:
    """决定宣传主张来自哪份采集 JSON。

    优先使用本次分析显式指定的输入文件：同一商品的多次采集 OCR 文本可能
    不同（详情图数量会变），跨文件组合会让「宣传证据」与「评论证据」
    来自不同快照。指定文件缺失/无 promotional_claims 时，回退到
    load_export 的按信息量挑选。
    """
    for raw_input in inputs or []:
        candidate = Path(raw_input)
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        if not candidate.exists():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("promotional_claims"):
            return data, str(candidate)
    return load_export(product_id, exports_dir)


def run_pipeline(
    product_id: str | None = None,
    inputs: list[str] | None = None,
    out_root: Path | None = None,
    use_llm: bool = True,
    from_extracted: Path | None = None,
    from_gold: Path | None = None,
    sim_threshold: float = 0.78,
    batch_size: int = 4,
    max_chars: int = 2600,
    include_centroid: bool = False,
    skip_claims: bool = False,
    skip_gaps: bool = False,
    reuse_claims: bool = False,
    category: str | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    exports_dir: Path | None = None,
) -> dict[str, Any]:
    started = time.time()
    out_root = out_root or (ROOT / "runtime" / "analysis")
    exports_dir = exports_dir or (ROOT / "runtime" / "exports")
    steps: list[dict[str, Any]] = []
    errors: list[str] = []
    llm_config = LLMConfig.from_env(ROOT)

    def step(name: str, status: str, **extra: Any) -> None:
        entry = {
            "step": name,
            "status": status,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            **extra,
        }
        steps.append(entry)
        if progress is not None:
            try:
                progress(entry)
            except Exception:  # noqa: BLE001 - 进度回调不能影响分析
                pass

    reviews, quality = load_reviews(inputs, product_id)
    product_id = product_id or quality.get("product_id") or ""
    if not product_id:
        raise ValueError("无法确定 product_id")
    if from_gold:
        # 人工标注实验只验证聚类/统计，不应触发宣传分析或 LLM 调用
        skip_claims = True
    out_dir = out_root / str(product_id)
    if from_gold:
        out_dir = out_dir / "experiments" / "gold"
    out_dir.mkdir(parents=True, exist_ok=True)

    write_json(
        out_dir / "cleaned_reviews.json",
        {
            "product_id": product_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "input_digest": input_digest(reviews),
            "review_count": len(reviews),
            "valid_review_count": quality["valid_review_count"],
            "reviews": reviews,
        },
    )
    write_json(out_dir / "data_quality.json", quality)
    step(
        "preprocess",
        "ok",
        unique_reviews=len(reviews),
        valid_reviews=quality["valid_review_count"],
    )

    # 品类识别：只依据真实商品标题/属性，不用商品 ID 推断
    product_meta: dict[str, Any] = {}
    export_path_hint = ""
    try:
        export, export_path_hint = load_export(product_id, exports_dir)
        product_meta = export.get("product") or {}
    except (FileNotFoundError, json.JSONDecodeError):
        export = {"product": {"title": quality.get("product_title", "")}}
    category_info = detect_category(product_meta or {"title": quality.get("product_title", "")})
    if category:
        category_info = {
            "key": category,
            "label": get_profile(category).label,
            "confidence": 1.0,
            "ambiguous": False,
            "evidence": [f"命令行指定品类：{category}"],
        }
    profile = get_profile(category_info["key"])
    step(
        "category",
        "ok",
        key=category_info["key"],
        label=category_info["label"],
        confidence=category_info["confidence"],
        ambiguous=category_info["ambiguous"],
        evidence=category_info.get("evidence", []),
    )

    analysis_status = "completed"
    extraction_status = "skipped"
    failure_reason = ""
    extraction_meta: dict[str, Any] = {}
    issues: list[Issue] = []
    valid_reviews = [review for review in reviews if review["valid_for_analysis"]]

    if from_gold:
        issues, extraction_meta = _load_gold(from_gold, reviews, profile)
        extraction_status = "manual_gold"
        step("extract", "manual_gold", issue_count=len(issues), source=str(from_gold))
    elif from_extracted:
        issues, extraction_meta = _load_extracted(from_extracted, reviews, profile)
        extraction_meta["mode"] = "from_extracted"
        extraction_status = "loaded"
        step("extract", "loaded", issue_count=len(issues), source=str(from_extracted))
    elif use_llm:
        config = llm_config
        if config is None:
            analysis_status = "blocked_missing_llm"
            extraction_status = "blocked"
            errors.append("未配置 LLM_API_KEY / OPENAI_API_KEY，未执行问题抽取")
            step("extract", "blocked", reason="missing_llm_config")
        else:
            try:
                step("extract", "running")
                extraction = run_extraction(
                    valid_reviews,
                    config,
                    out_dir,
                    ExtractConfig(batch_size=batch_size, max_chars_per_batch=max_chars),
                    profile,
                    on_progress=lambda done, total: step(
                        "extract", "running", done=done, total=total
                    ),
                )
                write_json(out_dir / "extracted_issues.json", extraction)
                extraction_meta = {
                    "mode": "llm",
                    "model": extraction["model"],
                    "prompt_version": extraction["prompt_version"],
                    "usage": extraction["usage"],
                    "input_review_count": extraction["input_review_count"],
                    "issue_count": extraction["issue_count"],
                    "validation_failed_count": extraction["validation_failed_count"],
                }
                review_by_id = {review["review_id"]: review for review in reviews}
                issues = []
                for record in extraction["reviews"]:
                    review = review_by_id.get(record["review_id"])
                    if review is None:
                        continue
                    for raw in record.get("issues") or []:
                        issue, _ = validate_issue(
                            raw,
                            review,
                            extraction["model"],
                            extraction["prompt_version"],
                            profile,
                        )
                        if issue is not None:
                            issues.append(issue)
                completion = extraction_completion(extraction, valid_reviews, len(issues))
                extraction_meta["review_completion"] = completion
                extraction_status = "ok" if completion["status"] == "completed" else completion["status"]
                step(
                    "extract",
                    extraction_status,
                    issue_count=len(issues),
                    reviews_with_issues=extraction["reviews_with_issues"],
                    model=extraction["model"],
                    completed_reviews=completion["completed_review_count"],
                    incomplete_reviews=completion["incomplete_review_count"],
                    error=completion["failure_reason"],
                )
            except Exception as exc:  # noqa: BLE001
                analysis_status = "failed"
                extraction_status = "failed"
                failure_reason, error_kind = _extraction_failure([f"{type(exc).__name__}: {exc}"])
                extraction_meta["error_kind"] = error_kind
                errors.append(f"LLM 抽取失败：{type(exc).__name__}: {exc}")
                step("extract", "failed", error=failure_reason)

    else:
        analysis_status = "extraction_skipped"
        step("extract", "skipped")

    completion = extraction_meta.get("review_completion") or {}
    if completion.get("status") in {"failed", "partial"}:
        analysis_status = completion["status"]
        extraction_status = completion["status"]
        failure_reason = completion["failure_reason"]
        errors.append(failure_reason)
        if from_extracted:
            step("extract", extraction_status, error=failure_reason,
                 completed_reviews=completion["completed_review_count"],
                 incomplete_reviews=completion["incomplete_review_count"])

    clusters_payload: dict[str, Any] = {"clusters": [], "metrics": {}, "issue_assignments": []}
    statistics_payload: dict[str, Any] = {"clusters": [], "top_problems": []}

    if analysis_status in {"completed", "partial"} and issues:
        try:
            step("embed", "running")
            texts, vectors, embedding_info = embed_issues(
                issues,
                model_dir=ROOT / "models" / "bge-small-zh-v1.5",
                cache_path=out_dir / "embedding_cache.json",
            )
            step("embed", "ok", **embedding_info)
        except ModelUnavailable as exc:
            analysis_status = "blocked_missing_model"
            errors.append(str(exc))
            step("embed", "blocked", reason=str(exc)[:200])
        except Exception as exc:  # noqa: BLE001
            analysis_status = "failed"
            errors.append(f"向量化失败：{type(exc).__name__}: {exc}")
            step("embed", "failed", error=str(exc)[:300])
        else:
            try:
                clusters_payload = cluster_issues(
                    issues,
                    vectors,
                    {review["review_id"]: review for review in reviews},
                    sim_threshold=sim_threshold,
                    include_centroid=include_centroid,
                    profile=profile,
                )
                clusters_payload["embedding"] = embedding_info
                write_json(out_dir / "issue_clusters.json", clusters_payload)
                step(
                    "cluster",
                    "ok",
                    cluster_count=clusters_payload["metrics"].get("cluster_count"),
                    silhouette=clusters_payload["metrics"].get("silhouette_cosine"),
                )
            except Exception as exc:  # noqa: BLE001
                analysis_status = "failed"
                errors.append(f"聚类失败：{type(exc).__name__}: {exc}")
                step("cluster", "failed", error=str(exc)[:300])

            try:
                statistics_payload = build_statistics(
                    reviews, quality, issues, clusters_payload
                )
                write_json(out_dir / "statistics.json", statistics_payload)
                step(
                    "statistics",
                    "ok",
                    top_problem=(
                        statistics_payload["top_problems"][0]["problem_name"]
                        if statistics_payload.get("top_problems")
                        else ""
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                analysis_status = "failed"
                errors.append(f"统计失败：{type(exc).__name__}: {exc}")
                step("statistics", "failed", error=str(exc)[:300])
    elif analysis_status in {"completed", "partial"}:
        step("cluster", "skipped", reason="no_issues")
        statistics_payload = build_statistics(reviews, quality, [], clusters_payload)
        write_json(out_dir / "statistics.json", statistics_payload)
        step("statistics", "ok", top_problem="")

    # ---------- 第三阶段：宣传主张抽取与落差分析 ----------
    claims_payload: dict[str, Any] = {}
    gaps_payload: dict[str, Any] = {}
    claims_status = "skipped"
    gaps_status = "skipped"

    if not skip_claims and use_llm and extraction_status != "failed":
        claims_file = out_dir / "extracted_claims.json"
        reuse_file = claims_file
        if reuse_claims and not reuse_file.exists():
            fallback = ROOT / "runtime" / "analysis" / str(product_id) / "extracted_claims.json"
            if fallback.exists():
                reuse_file = fallback
        if llm_config is None:
            claims_status = "blocked_missing_llm"
            step("claims", claims_status, reason="missing_llm_config")
        elif reuse_claims and reuse_file.exists():
            try:
                claims_payload = json.loads(reuse_file.read_text(encoding="utf-8"))
                claims_status = _payload_stage_status(claims_payload, "claims", "loaded")
                step(
                    "claims",
                    claims_status,
                    claim_count=claims_payload.get("claim_count", 0),
                    source=str(reuse_file),
                )
            except Exception as exc:  # noqa: BLE001
                claims_status = "failed"
                if analysis_status == "completed":
                    analysis_status = "partial"
                failure_reason = failure_reason or "商品宣传信息抽取未完成，请重试补全。"
                errors.append(f"复用宣传主张失败：{type(exc).__name__}: {exc}")
                step("claims", "failed", error=str(exc)[:300])
        else:
            try:
                step("claims", "running")
                export, export_path = select_claim_export(
                    inputs or [], product_id, exports_dir
                )
                claims_payload = run_claim_extraction(
                    export,
                    ChatClient(llm_config),
                    llm_config.model,
                    out_dir,
                    ClaimConfig(),
                    profile,
                    on_progress=lambda done, total: step(
                        "claims", "running", done=done, total=total
                    ),
                )
                claims_payload["input_export"] = export_path
                write_json(claims_file, claims_payload)
                claims_status = _payload_stage_status(claims_payload, "claims", "ok")
                step(
                    "claims",
                    claims_status,
                    claim_count=claims_payload.get("claim_count", 0),
                    input_text_count=claims_payload.get("input_text_count", 0),
                    model=llm_config.model,
                )
            except Exception as exc:  # noqa: BLE001
                claims_status = "failed"
                if analysis_status == "completed":
                    analysis_status = "partial"
                failure_reason = failure_reason or "商品宣传信息抽取未完成，请重试补全。"
                errors.append(f"宣传主张抽取失败：{type(exc).__name__}: {exc}")
                step("claims", "failed", error=str(exc)[:300])

        if claims_payload.get("errors"):
            if analysis_status == "completed":
                analysis_status = "partial"
            message = "商品宣传信息抽取未完成，请重试补全。"
            failure_reason = failure_reason or message
            errors.append(message)

        if claims_status in {"ok", "loaded", "partial"} and claims_payload.get("claims"):
            # 新抽取已在 extractor 内过滤；复用的历史 JSON 可能仍含平台活动/口号/元叙述，
            # 这里统一再过一遍，保证两条路径的分析维度一致。
            kept_claims, filtered_reasons, filtered_samples = filter_claim_dicts(
                claims_payload.get("claims") or []
            )
            if filtered_reasons:
                claims_payload["claims"] = kept_claims
                claims_payload["claim_count"] = len(kept_claims)
                claims_payload["filtered"] = {
                    "count": sum(filtered_reasons.values()),
                    "reasons": filtered_reasons,
                    "samples": filtered_samples,
                }
            step("claim_groups", "running")
            # 近义分组：同一卖点的多条近义表述只额外打组标签，不删主张。
            # 复用时同样要分组，否则 reuse-claims 路径拿不到 by_type_grouped。
            claim_groups, group_map = group_near_duplicate_claims(
                claims_payload.get("claims") or []
            )
            for claim in claims_payload.get("claims") or []:
                claim["claim_group"] = group_map.get(str(claim.get("claim_id")), "")
            claims_payload["claim_groups"] = claim_groups
            claims_payload["near_duplicate_extra"] = len(
                claims_payload.get("claims") or []
            ) - len(claim_groups)
            # 新抽取（ok）与复用（loaded）两条路径都必须落盘：否则 ok 路径的
            # extracted_claims.json 会缺少 claim_group / claim_groups，与
            # claim_gaps.json 的 by_type_grouped 口径不一致。
            write_json(claims_file, claims_payload)
            step(
                "claim_groups",
                "ok",
                claim_groups=len(claim_groups),
                near_duplicate_extra=claims_payload["near_duplicate_extra"],
            )
    else:
        step(
            "claims",
            "skipped",
            reason="extraction_failed" if extraction_status == "failed" else "flag_skip_claims" if skip_claims else "llm_disabled",
        )

    if skip_gaps or extraction_status == "failed":
        step("gaps", "skipped", reason="extraction_failed" if extraction_status == "failed" else "flag_skip_gaps")
    elif not claims_payload.get("claims"):
        step("gaps", "skipped", reason=f"no_claims:{claims_status}")
    elif llm_config is None:
        gaps_status = "blocked_missing_llm"
        step("gaps", "blocked", reason="missing_llm_config")
    elif not clusters_payload.get("clusters"):
        gaps_status = "skipped"
        step("gaps", "skipped", reason="no_clusters")
    else:
        try:
            step("gaps", "running")
            analyzer = GapAnalyzer(
                ChatClient(llm_config),
                llm_config.model,
                out_dir,
                GapConfig(),
                profile,
            )
            gaps_payload = analyzer.analyze(
                claims_payload["claims"],
                clusters_payload["clusters"],
                {
                    "product_id": product_id,
                    "valid_review_count": quality.get("valid_review_count", 0),
                },
                on_progress=lambda done, total: step(
                    "gaps", "running", done=done, total=total
                ),
            )
            gaps_payload["input_export"] = claims_payload.get("input_export", "")
            gaps_payload["clusters_fingerprint"] = clusters_fingerprint(clusters_payload)
            # 分组计数：一组近义主张只计一次，最强判定代表全组
            gaps_payload["claim_groups"] = claims_payload.get("claim_groups", [])
            gaps_payload["by_type_grouped"] = grouped_by_type(
                gaps_payload.get("gaps") or [],
                {
                    str(c.get("claim_id")): str(c.get("claim_group") or "")
                    for c in (claims_payload.get("claims") or [])
                },
            )
            gaps_payload["grouped_conflict_evidence"] = grouped_conflict_evidence(
                gaps_payload.get("gaps") or [],
                {
                    str(c.get("claim_id")): str(c.get("claim_group") or "")
                    for c in (claims_payload.get("claims") or [])
                },
            )
            write_json(out_dir / "claim_gaps.json", gaps_payload)
            gaps_status = _payload_stage_status(gaps_payload, "gaps", "ok")
            if gaps_payload.get("errors"):
                if analysis_status == "completed":
                    analysis_status = "partial"
                message = "宣传与评论对照未完成，请重试补全。"
                failure_reason = failure_reason or message
                errors.append(message)
            step(
                "gaps",
                gaps_status,
                gap_count=gaps_payload.get("gap_count", 0),
                by_type=gaps_payload.get("by_type", {}),
                by_type_grouped=gaps_payload.get("by_type_grouped", {}),
            )
        except Exception as exc:  # noqa: BLE001
            gaps_status = "failed"
            if analysis_status == "completed":
                analysis_status = "partial"
            failure_reason = failure_reason or "宣传与评论对照未完成，请重试补全。"
            errors.append(f"落差分析失败：{type(exc).__name__}: {exc}")
            step("gaps", "failed", error=str(exc)[:300])

    issues_payload = {
        "product_id": product_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "issue_count": len(issues),
        "reviews_with_issues": len({issue.review_id for issue in issues}),
        "issues": [issue.to_dict() for issue in issues],
        "extraction": extraction_meta,
    }
    if issues:
        write_json(out_dir / "issues_flat.json", issues_payload)

    summary = {
        "product_id": product_id,
        "analysis_status": analysis_status,
        "failure_reason": failure_reason,
        "retryable": bool(failure_reason),
        "category": {
            "key": category_info["key"],
            "label": category_info["label"],
            "confidence": category_info["confidence"],
            "ambiguous": category_info["ambiguous"],
            "evidence": category_info.get("evidence", []),
        },
        "input_review_count": len(reviews),
        "unique_review_count": len(reviews),
        "valid_review_count": quality["valid_review_count"],
        # 当前只入库 ≤3 星正文；将来纳入 4–5 星正文做支持/反向证据时置 True，
        # 风险模型的“多源支持”充分性才可能成立。
        "review_scope": "star_lte_3",
        "reverse_evidence_covered": False,
        "reviews_with_issues": len({issue.review_id for issue in issues}),
        "issue_count": len(issues),
        "cluster_count": len(clusters_payload.get("clusters", [])),
        "top_problems": statistics_payload.get("top_problems", []),
        "claim_count": claims_payload.get("claim_count", 0),
        "claim_by_type": claims_payload.get("by_type", {}),
        "gap_count": gaps_payload.get("gap_count", 0),
        "gap_by_type": gaps_payload.get("by_type", {}),
        "gap_by_type_grouped": gaps_payload.get("by_type_grouped", {}),
        "gap_by_confidence": gaps_payload.get("by_confidence", {}),
        "claim_group_count": len(claims_payload.get("claim_groups") or []),
        "fingerprints": {
            "clusters": clusters_fingerprint(clusters_payload),
            "gaps_clusters": gaps_payload.get("clusters_fingerprint", ""),
            "gaps_fresh": (
                gaps_payload.get("clusters_fingerprint")
                == clusters_fingerprint(clusters_payload)
                if gaps_payload.get("clusters_fingerprint")
                else None
            ),
        },
        "stages": {"extract": extraction_status, "claims": claims_status, "gaps": gaps_status},
        "data_quality": {
            "star_distribution": quality.get("star_distribution"),
            "date_range": quality.get("date_range"),
            "sku_size_distribution": quality.get("sku_size_distribution"),
            "denominators": quality.get("denominators"),
            "warnings": quality.get("warnings"),
        },
        "denominators": statistics_payload.get(
            "denominators", quality.get("denominators", {})
        ),
        "limitations": statistics_payload.get("limitations", []),
        "model": extraction_meta,
        "models": {
            "issues": extraction_meta.get("model", ""),
            "claims": claims_payload.get("model", ""),
            "gaps": gaps_payload.get("model", ""),
        },
        "source": extraction_meta.get("mode", ""),
        "experiment": bool(from_gold),
        "algorithm": clusters_payload.get("algorithm", ""),
        "algorithm_params": clusters_payload.get("params", {}),
        "algorithm_metrics": clusters_payload.get("metrics", {}),
        "baseline": clusters_payload.get("baseline", {}),
        "input": {
            "files": quality.get("input_files", []),
            "digest": input_digest(reviews),
        },
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "errors": errors,
    }
    if analysis_status == "blocked_missing_llm":
        summary["limitations"] = [
            "未配置 LLM API，未执行问题抽取；当前只有数据清洗与质量报告。",
            "配置 LLM_API_KEY / LLM_MODEL / LLM_BASE_URL 后重新运行即可继续。",
        ]
    if from_gold:
        summary["limitations"] = [
            "本目录是人工标注实验，用于验证 BGE 聚类与统计链路，不是 LLM 抽取结果。",
            "不能把本目录的聚类/统计数字当作模型在真实评论上的自动抽取效果。",
            *summary.get("limitations", []),
        ]
    if claims_status == "blocked_missing_llm":
        summary["limitations"] = [
            *summary.get("limitations", []),
            "未配置 LLM，宣传主张抽取与落差分析未执行。",
        ]
    elif claims_status == "failed":
        summary["limitations"] = [
            *summary.get("limitations", []),
            "宣传主张抽取失败，未生成落差分析；已有问题分析结果不受影响。",
        ]
    elif claims_status == "skipped":
        summary["limitations"] = [
            *summary.get("limitations", []),
            "本次未运行宣传主张与落差分析（实验模式、--no-llm 或 --skip-claims）。",
        ]
    if claims_payload.get("claims") and gaps_status != "ok":
        summary["limitations"] = [
            *summary.get("limitations", []),
            f"已抽取宣传主张，但落差分析未完成（{gaps_status}）。",
        ]
    if gaps_status == "ok":
        summary["limitations"] = [
            *summary.get("limitations", []),
            "落差判断只覆盖已采集的低星评价；“未发现直接冲突”不等于宣传已被证实为真。",
            "证据强度按独立评论数分级，不代表统计显著性，也不能推断全体买家。",
        ]
    write_json(out_dir / "analysis_summary.json", summary)

    log = {
        "product_id": product_id,
        "analysis_version": __version__,
        "status": analysis_status,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_seconds": round(time.time() - started, 2),
        "cwd": str(ROOT),
        "versions": _package_versions(),
        "params": {
            "sim_threshold": sim_threshold,
            "batch_size": batch_size,
            "max_chars": max_chars,
            "from_extracted": str(from_extracted) if from_extracted else "",
            "from_gold": str(from_gold) if from_gold else "",
            "skip_claims": skip_claims,
            "skip_gaps": skip_gaps,
            "reuse_claims": reuse_claims,
            "category": category_info["key"],
        },
        "input": {
            "files": quality.get("input_files", []),
            "digest": input_digest(reviews),
            "review_count": len(reviews),
            "valid_review_count": quality["valid_review_count"],
        },
        "steps": steps,
        "errors": errors,
    }
    write_json(out_dir / "analysis_log.json", log)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="商品评论问题分析流水线")
    parser.add_argument("--product-id", default=None)
    parser.add_argument("--input", nargs="*", default=None, help="采集 JSON 路径/通配符")
    parser.add_argument("--out-root", default=None)
    parser.add_argument("--from-extracted", default=None, help="复用已有 extracted_issues.json")
    parser.add_argument("--from-gold", default=None, help="用人工标注做聚类/统计实验（不是 LLM 结果）")
    parser.add_argument("--no-llm", action="store_true", help="只做清洗与质量检查")
    parser.add_argument("--sim-threshold", type=float, default=0.78)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--max-chars", type=int, default=2600)
    parser.add_argument("--include-centroid", action="store_true")
    parser.add_argument("--skip-claims", action="store_true", help="跳过宣传主张与落差分析")
    parser.add_argument("--skip-gaps", action="store_true", help="只抽宣传主张，不做落差匹配")
    parser.add_argument("--reuse-claims", action="store_true", help="复用已有 extracted_claims.json")
    parser.add_argument(
        "--category",
        default=None,
        help="手动指定品类键（shoe/beauty/bag/apparel/food/home/digital/generic），默认自动识别",
    )
    args = parser.parse_args(argv)

    summary = run_pipeline(
        product_id=args.product_id,
        inputs=args.input,
        out_root=Path(args.out_root) if args.out_root else None,
        use_llm=not args.no_llm,
        from_extracted=Path(args.from_extracted) if args.from_extracted else None,
        from_gold=Path(args.from_gold) if args.from_gold else None,
        sim_threshold=args.sim_threshold,
        batch_size=args.batch_size,
        max_chars=args.max_chars,
        include_centroid=args.include_centroid,
        skip_claims=args.skip_claims,
        skip_gaps=args.skip_gaps,
        reuse_claims=args.reuse_claims,
        category=args.category,
    )
    print(json.dumps(
        {
            "status": summary["analysis_status"],
            "product_id": summary["product_id"],
            "valid_reviews": summary["valid_review_count"],
            "issues": summary["issue_count"],
            "clusters": summary["cluster_count"],
            "claims": summary.get("claim_count", 0),
            "gaps": summary.get("gap_count", 0),
            "errors": summary["errors"],
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0 if summary["analysis_status"] in {"completed", "extraction_skipped"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
