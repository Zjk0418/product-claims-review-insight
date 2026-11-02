"""评测数据集身份与可报告性（v2.1）。

任何带指标的评测输出都必须回答七个问题：

1. 数据集叫什么、哪个版本；
2. 有多少条样本、多少个标签；
3. 标注者是谁、几个人、是不是人；
4. 是否盲标；
5. 有没有参与过调参；
6. 用的模型与 Prompt 版本；
7. 这个指标的口径是什么、能不能对外引用。

本模块只做“身份声明 + 机械校验”，不判断指标高低。
`reportable=True` 不会被自动写成 True：调用方必须显式声明，且必须满足
human_verified / labeler_count / role 的最低条件，否则 `identity_gaps()` 会报错。
"""
from __future__ import annotations

from typing import Any

ROLES = {
    "development",   # 开发集：参与过 prompt / 规则迭代
    "validation",    # 验证集：参与过模型选择或调参，但不是最终测试
    "test",          # 最终测试集：在冻结模型后只用一次
    "diagnostic",    # 诊断：标签本身不是人工 Gold，只用于定位问题
    "self_check",    # 工具自检：空数据/流程验证
    "production",    # 线上运行产物，不是评测集
}

LABELER_TYPES = {
    "human",              # 人工标注（需 human_verified=True 且 labeler_count>=1）
    "human_unverified",   # 声明为人工，但没有 attestation/复核，不能按人工 Gold 引用
    "assistant_draft",    # 助手/模型起草，未经人工逐条确认
    "mixed",              # 人工 + 助手混合，需在 note 说明比例
    "none",               # 无标注（如纯预测、纯聚类输出）
    "unknown",
}

REQUIRED_FIELDS = (
    "dataset_name",
    "dataset_version",
    "role",
    "review_count",
    "label_file",
    "labeler_type",
    "labeler_count",
    "human_verified",
    "blind",
    "used_for_tuning",
    "model",
    "prompt_version",
    "metric_scope",
    "reportable",
    "reportable_note",
)


def make_identity(
    *,
    dataset_name: str,
    dataset_version: str,
    role: str,
    review_count: int,
    label_file: str,
    labeler_type: str,
    labeler_count: int,
    human_verified: bool,
    blind: bool | str,
    used_for_tuning: bool,
    model: str,
    prompt_version: str,
    metric_scope: str,
    reportable: bool,
    reportable_note: str,
    issue_count: int | None = None,
    **extra: Any,
) -> dict:
    """构造统一身份块；不在这里“猜”可报告性。"""
    identity = {
        "dataset_name": dataset_name,
        "dataset_version": dataset_version,
        "role": role,
        "review_count": int(review_count),
        "issue_count": issue_count,
        "label_file": label_file,
        "labeler_type": labeler_type,
        "labeler_count": int(labeler_count),
        "human_verified": bool(human_verified),
        "blind": blind,
        "used_for_tuning": bool(used_for_tuning),
        "model": model,
        "prompt_version": prompt_version,
        "metric_scope": metric_scope,
        "reportable": bool(reportable),
        "reportable_note": reportable_note,
    }
    identity.update(extra)
    return identity


def identity_gaps(identity: dict | None) -> list[str]:
    """返回身份块缺失/自相矛盾的字段；空列表表示可以引用。"""
    if not isinstance(identity, dict):
        return ["缺少 dataset 身份块"]
    gaps: list[str] = []
    for field in REQUIRED_FIELDS:
        if field not in identity or identity[field] in (None, ""):
            # 0 / False 是合法值，不能被当成缺失
            if identity.get(field) not in (0, False):
                gaps.append(f"缺少 {field}")

    role = identity.get("role")
    if role and role not in ROLES:
        gaps.append(f"role={role!r} 不在 {sorted(ROLES)}")
    labeler_type = identity.get("labeler_type")
    if labeler_type and labeler_type not in LABELER_TYPES:
        gaps.append(f"labeler_type={labeler_type!r} 不在 {sorted(LABELER_TYPES)}")

    labeler_count = identity.get("labeler_count")
    human_verified = identity.get("human_verified")
    reportable = identity.get("reportable")
    used_for_tuning = identity.get("used_for_tuning")

    if human_verified and (not isinstance(labeler_count, int) or labeler_count < 1):
        gaps.append("human_verified=True 但 labeler_count<1")
    if labeler_type == "human" and not human_verified:
        gaps.append("labeler_type=human 但 human_verified=False")
    if reportable:
        if not human_verified:
            gaps.append("reportable=True 但未经人工确认")
        if not isinstance(labeler_count, int) or labeler_count < 1:
            gaps.append("reportable=True 但 labeler_count<1")
        if role not in {"test", "validation"}:
            gaps.append(f"reportable=True 但 role={role!r}（只允许 test/validation）")
        if not str(identity.get("reportable_note") or "").strip():
            gaps.append("reportable=True 但缺少 reportable_note")
    if role == "test" and used_for_tuning:
        gaps.append("role=test 但 used_for_tuning=True（测试集已被调参污染）")
    return gaps


def is_reportable(identity: dict | None) -> bool:
    return bool(identity) and not identity_gaps(identity) and bool(identity.get("reportable"))


def summarize(identity: dict | None) -> dict:
    """给清单/审计用的短摘要。"""
    if not isinstance(identity, dict):
        return {"present": False, "complete": False, "missing": ["缺少 dataset 身份块"]}
    gaps = identity_gaps(identity)
    return {
        "present": True,
        "complete": not gaps,
        "missing": gaps,
        "dataset_name": identity.get("dataset_name"),
        "dataset_version": identity.get("dataset_version"),
        "role": identity.get("role"),
        "labeler_type": identity.get("labeler_type"),
        "labeler_count": identity.get("labeler_count"),
        "human_verified": identity.get("human_verified"),
        "used_for_tuning": identity.get("used_for_tuning"),
        "reportable": identity.get("reportable"),
    }
