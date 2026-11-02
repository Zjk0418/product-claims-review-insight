"""可核验的样本事件占比，和未校准的宣传矛盾指数分开。

百分比回答本次样本中多少评论提及该问题，不推断购买者的真实发生概率。
颜色分档只作展示，不能代替样本量、证据或选择偏差的判断。
"""
from __future__ import annotations

import math


VERSION = "sample-mention-v1"
THRESHOLDS = {"medium_rate": 0.03, "high_rate": 0.10}
NOTE = ("样本负面提及占比只描述本次采集，不是宣传矛盾指数，也不是校准后的购买发生概率。"
        "各问题可能重复引用同一评论；整体与未对应宣传汇总均按评论ID去重。"
        "历史占比分档仅为数据兼容保留，不驱动主提醒；主圆环另行估计同条件低星反馈的问题复现概率，不能当作购买后故障率。"
        "Wilson区间仅为二项比例参考区间，不校正评论自选择、时间顺序、漏采及模型误判。")


def wilson_interval(count: int, total: int, z: float = 1.96) -> list[float] | None:
    if total <= 0:
        return None
    p = count / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return [round(max(0.0, center - margin), 6), round(min(1.0, center + margin), 6)]


def proportion(count: int, total: int) -> dict:
    if count < 0 or total < 0 or total and count > total:
        raise ValueError("样本事件计数超出分母范围")
    rate = round(count / total, 6) if total > 0 else None
    if rate is None:
        band, label = "none", "分母未提供"
    elif count == 0:
        band, label = "none", "样本未检出"
    elif rate >= THRESHOLDS["high_rate"]:
        band, label = "high", "样本占比较高"
    elif rate >= THRESHOLDS["medium_rate"]:
        band, label = "medium", "样本占比需关注"
    else:
        band, label = "low", "样本占比较低"
    return {"numerator": count, "denominator": total, "rate": rate,
            "ci95": wilson_interval(count, total), "band": band, "label": label,
            "small_sample": 0 < total < 30}


def signal(review_ids, scanned_count: int, eligible_count: int, basis: str) -> dict:
    ids = sorted({str(rid) for rid in review_ids})
    return {"version": VERSION, "review_count": len(ids), "review_ids": ids,
            "scanned": proportion(len(ids), scanned_count),
            "eligible": proportion(len(ids), eligible_count),
            "basis": basis, "note": NOTE, "calibrated": False}
