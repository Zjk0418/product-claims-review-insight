"""Small, event-neutral probability calculations with explicit assumptions.

These functions estimate a *defined observed Bernoulli event*. They cannot turn
selected reviews into an identified purchasing-outcome probability. No I/O,
classification, scoring weights, seller assumptions or UI policy live here.
"""
from __future__ import annotations

import math
from numbers import Integral, Real

VERSION = "jeffreys-binomial-v1"
ASSUMPTIONS = (
    "样本在明确的同一条件下可视为独立伯努利观测，事件概率在观察和预测期间保持稳定。",
    "事件定义、分母资格与事件识别有效；未提及只能按已定义的观测事件解释。",
    "可信区间仅描述该模型内的参数不确定性，不校正评论自选择、不可见评论、漏采或分类错误。",
    "模型未经购买后结局数据校准，不能自动解释为买家购买后遇到问题的概率。",
)


def _finite_real(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite real number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite real number")
    return result


def beta_estimate(successes: int, total: int, *, confidence: float = .95) -> dict:
    """Jeffreys posterior mean and equal-tailed credible interval.

    For k events in n eligible observations, posterior = Beta(k+.5,n-k+.5).
    Its mean is also the posterior predictive probability of the next event
    under the stated exchangeability/stability assumptions. No data returns
    ``None`` rather than presenting the prior's 50% as an empirical estimate.
    """
    if any(isinstance(value, bool) or not isinstance(value, Integral)
           for value in (successes, total)):
        raise ValueError("successes and total must be integer counts")
    successes, total = int(successes), int(total)
    if not 0 <= successes <= total:
        raise ValueError("counts must satisfy 0 <= successes <= total")
    confidence = _finite_real(confidence, "confidence")
    if not 0 < confidence < 1:
        raise ValueError("confidence must be strictly between zero and one")
    alpha, beta = successes + .5, total - successes + .5
    result = {
        "version": VERSION, "status": "estimated" if total else "insufficient",
        "estimate": None, "interval": None, "credible_level": confidence,
        "successes": successes, "total": total,
        "prior": {"name": "Jeffreys", "alpha": .5, "beta": .5},
        "posterior": {"alpha": alpha, "beta": beta},
        "calibrated": False, "method": "Beta-Bernoulli posterior predictive mean; equal-tailed posterior credible interval",
        "assumptions": list(ASSUMPTIONS),
    }
    if not total:
        return result
    # scipy already belongs to the analysis runtime. Import lazily so empty
    # reports and metadata-only views need not initialize its numerical modules.
    from scipy.special import betaincinv

    tail = (1 - confidence) / 2
    result["estimate"] = alpha / (total + 1)
    result["interval"] = [float(betaincinv(alpha, beta, tail)),
                          float(betaincinv(alpha, beta, 1 - tail))]
    return result


def selection_scenario(observed_estimate: float | dict | None, visibility_ratio: float) -> dict:
    """Transform q to p=q/[r(1-q)+q] under an explicitly supplied scenario.

    r = P(observed|event)/P(observed|no event). Accept a probability point or
    a beta_estimate-style dict, optionally containing ``interval``. A supplied
    interval is transformed endpoint-wise because this map is monotone in q.
    r is a hypothetical input, never inferred from reviews or seller identity.
    The output is a scenario, not an identified or bias-corrected real-world rate.
    """
    ratio = _finite_real(visibility_ratio, "visibility_ratio")
    if ratio <= 0:
        raise ValueError("visibility_ratio must be greater than zero")
    point = observed_estimate.get("estimate") if isinstance(observed_estimate, dict) else observed_estimate
    interval = observed_estimate.get("interval") if isinstance(observed_estimate, dict) else None

    def transform(value):
        value = _finite_real(value, "observed probability")
        if not 0 <= value <= 1:
            raise ValueError("observed probability must lie in [0,1]")
        if value in (0, 1):
            return value
        # Algebraically equivalent form avoids r*(1-q) overflowing when r is
        # finite but very large. r extremely small legitimately approaches one.
        if ratio >= 1:
            return (value / ratio) / ((1 - value) + value / ratio)
        return value / (ratio * (1 - value) + value)

    result = {
        "version": "visibility-scenario-v1", "status": "scenario" if point is not None else "insufficient",
        "estimate": transform(point) if point is not None else None, "interval": None,
        "visibility_ratio": ratio, "observed_estimate": point,
        "calibrated": False, "identified": False,
        "method": "q / [r * (1 - q) + q]",
        "assumptions": [
            "r由使用者明确假设，是发生事件与未发生事件两组的可见概率之比，并非从现有数据测得。",
            "该二组选择模型假设一人一购一评、事件分类有效且可见性只按这两组区分。",
            "区间条件于固定r；它不包含r本身的不确定性，也不证明任何商家操纵评论。",
        ],
    }
    if interval is not None:
        if not isinstance(interval, (list, tuple)) or len(interval) != 2:
            raise ValueError("interval must contain two probability endpoints")
        lo, hi = transform(interval[0]), transform(interval[1])
        if lo > hi:
            raise ValueError("interval endpoints must be ordered")
        if point is None:
            raise ValueError("an interval requires an observed point estimate")
        result["interval"] = [lo, hi]
    return result
