"""有序并发执行 LLM 批次。

三个阶段（问题抽取、宣传主张抽取、落差判断）都是一批一批调用模型，
串行时单次 5–15 秒会累积成几分钟。这里统一用线程池并发，并保持输入顺序，
使产物顺序、断点语义和串行版本完全一致。

并发数由 `LLM_CONCURRENCY` 控制（默认 6，上限 8）：
- 太小提速有限；太大容易触发接口限流（429 会自动退避重试）。
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")
R = TypeVar("R")

DEFAULT_CONCURRENCY = 6
MAX_CONCURRENCY = 8


def default_workers() -> int:
    raw = os.getenv("LLM_CONCURRENCY", "")
    try:
        value = int(raw) if raw else DEFAULT_CONCURRENCY
    except ValueError:
        value = DEFAULT_CONCURRENCY
    return max(1, min(value, MAX_CONCURRENCY))


def ordered_map(fn: Callable[[T], R], items: Iterable[T], workers: int | None = None) -> list[R]:
    """等价于 [fn(x) for x in items]，但最多 workers 路并发且结果顺序不变。

    fn 抛出的异常会原样传播（和串行版本一致）；任一批次失败时，其余批次可能已经发出，
    这正是并发带来的可接受代价——调用方本来就允许单批失败后继续。
    """
    values = list(items)
    if not values:
        return []
    count = max(1, min(workers if workers is not None else default_workers(), MAX_CONCURRENCY))
    if count <= 1 or len(values) == 1:
        return [fn(value) for value in values]
    with ThreadPoolExecutor(max_workers=count, thread_name_prefix="llm-batch") as pool:
        return list(pool.map(fn, values))
