import argparse
import asyncio
import json
import random
import time
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "runtime" / "reviews"

from tmall_rate_auto import crawl_from_url


PRODUCT_COLS_BY_POSITION = {
    "record_id": 0,
    "top_type": 1,
    "rank": 2,
    "source_sheet": 3,
    "brand": 4,
    "title": 5,
    "source_link": 6,
    "sales_units": 7,
    "sales_amount": 8,
    "price": 9,
    "price_segment": 10,
    "age_segment": 11,
    "scene": 12,
    "core_value": 13,
    "channel": 14,
    "year": 15,
    "item_id": 16,
    "crawl_url": 17,
    "status": 18,
    "count": 19,
}


def cell(row: pd.Series, key: str) -> Any:
    idx = PRODUCT_COLS_BY_POSITION[key]
    return row.iloc[idx] if idx < len(row) else ""


def clean(value: Any) -> str:
    if pd.isna(value):
        return ""
    return str(value)


def source_prefix(row: pd.Series) -> dict[str, Any]:
    return {
        "重跑来源记录ID": clean(cell(row, "record_id")),
        "重跑来源TOP类别": clean(cell(row, "top_type")),
        "重跑来源排名": clean(cell(row, "rank")),
        "重跑来源工作表": clean(cell(row, "source_sheet")),
        "重跑来源品牌": clean(cell(row, "brand")),
        "重跑来源商品名称": clean(cell(row, "title")),
        "重跑来源商品链接": clean(cell(row, "source_link")),
        "重跑来源销量（件）": clean(cell(row, "sales_units")),
        "重跑来源销售额（元）": clean(cell(row, "sales_amount")),
        "重跑来源售价": clean(cell(row, "price")),
        "重跑来源价格段": clean(cell(row, "price_segment")),
        "重跑来源年龄段": clean(cell(row, "age_segment")),
        "重跑来源场景": clean(cell(row, "scene")),
        "重跑来源核心价值": clean(cell(row, "core_value")),
        "重跑来源渠道": clean(cell(row, "channel")),
        "重跑来源年份": clean(cell(row, "year")),
        "重跑商品ID": clean(cell(row, "item_id")),
        "重跑链接": clean(cell(row, "crawl_url")),
    }


def write_workbook(path: Path, review_frames: list[pd.DataFrame], product_rows: list[dict[str, Any]]) -> None:
    reviews = pd.concat(review_frames, ignore_index=True) if review_frames else pd.DataFrame()
    products = pd.DataFrame(product_rows)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        reviews.to_excel(writer, sheet_name="reviews", index=False)
        products.to_excel(writer, sheet_name="products", index=False)


def append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_missing_products(input_xlsx: Path, only_zero: bool, only_under: int | None, limit: int | None) -> pd.DataFrame:
    products = pd.read_excel(input_xlsx, sheet_name="products")
    counts = pd.to_numeric(products.iloc[:, PRODUCT_COLS_BY_POSITION["count"]], errors="coerce").fillna(0).astype(int)
    if only_zero:
        selected = products[counts == 0].copy()
    elif only_under is not None:
        selected = products[counts < only_under].copy()
    else:
        selected = products.copy()
    if limit is not None:
        selected = selected.head(limit).copy()
    return selected


def load_resume_state(resume_run_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, set[str]]:
    candidates = sorted(
        list(resume_run_dir.glob("rerun_missing_tmall_reviews_partial_*.xlsx"))
        + list(resume_run_dir.glob("rerun_missing_tmall_reviews_*.xlsx")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return pd.DataFrame(), pd.DataFrame(), set()
    workbook = candidates[0]
    reviews = pd.read_excel(workbook, sheet_name="reviews")
    products = pd.read_excel(workbook, sheet_name="products")
    if products.empty:
        return reviews, products, set()
    counts = pd.to_numeric(products["重跑条数"], errors="coerce").fillna(0).astype(int)
    completed = set(products.loc[counts.gt(0), "重跑商品ID"].astype(str))
    print(f"[RESUME-RUN] 载入旧补抓汇总：{workbook}")
    return reviews, products, completed


def find_resume_excel(resume_run_dir: Path | None, item_id: str) -> str | None:
    if not resume_run_dir:
        return None
    files = sorted(
        (resume_run_dir / "per_product").glob(f"tmall_reviews_{item_id}_*.xlsx"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return str(files[0]) if files else None


async def run(args: argparse.Namespace) -> tuple[Path, Path, int, int]:
    input_xlsx = Path(args.input_xlsx)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir = out_dir / f"rerun_missing_reviews_{stamp}"
    per_product_dir = run_dir / "per_product"
    per_product_dir.mkdir(parents=True, exist_ok=True)

    final_excel = run_dir / f"rerun_missing_tmall_reviews_{stamp}.xlsx"
    final_jsonl = run_dir / f"rerun_missing_tmall_reviews_{stamp}.jsonl"
    partial_excel = run_dir / f"rerun_missing_tmall_reviews_partial_{stamp}.xlsx"

    products = load_missing_products(input_xlsx, args.only_zero, args.only_under, args.limit_products)
    print(f"[LOAD] input={input_xlsx}")

    review_frames: list[pd.DataFrame] = []
    product_rows: list[dict[str, Any]] = []
    total_reviews = 0
    blocked_streak = 0

    resume_run_dir = Path(args.resume_run_dir).resolve() if args.resume_run_dir else None
    if resume_run_dir:
        prior_reviews, prior_products, completed_ids = load_resume_state(resume_run_dir)
        if not prior_reviews.empty:
            review_frames.append(prior_reviews)
            total_reviews = len(prior_reviews)
            append_jsonl(final_jsonl, prior_reviews.to_dict(orient="records"))
        if not prior_products.empty:
            product_rows.extend(prior_products.to_dict(orient="records"))
        item_values = products.iloc[:, PRODUCT_COLS_BY_POSITION["item_id"]].astype(str)
        products = products.loc[~item_values.isin(completed_ids)].copy()

    carried_products = len(product_rows)
    total_selected = carried_products + len(products)
    print(f"[LOAD] selected_products={total_selected}")
    if carried_products:
        print(f"[RESUME-RUN] carried_products={carried_products} remaining_products={len(products)}")
        write_workbook(partial_excel, review_frames, product_rows)

    for i, (_, row) in enumerate(products.iterrows(), start=1):
        display_index = carried_products + i
        item_id = clean(cell(row, "item_id"))
        url = clean(cell(row, "crawl_url"))
        brand = clean(cell(row, "brand"))
        title = clean(cell(row, "title"))
        print(f"[PRODUCT] {display_index}/{total_selected} item={item_id} {brand} {title[:50]}")

        summary = {
            **source_prefix(row),
            "重跑状态": "pending",
            "重跑条数": 0,
            "单品Excel": "",
            "单品JSONL": "",
            "错误": "",
        }

        try:
            result = None
            resume_excel = find_resume_excel(resume_run_dir, item_id)
            if resume_excel:
                print(f"[RESUME-RUN] item={item_id} 使用断点文件：{resume_excel}")
            for attempt in range(1, args.product_attempts + 1):
                print(f"[ATTEMPT] item={item_id} {attempt}/{args.product_attempts}")
                try:
                    result = await crawl_from_url(
                        item_or_url=url or item_id,
                        max_reviews=args.max_reviews_per_product,
                        page_size=args.page_size,
                        start_page=1,
                        min_delay=args.min_delay,
                        max_delay=args.max_delay,
                        out_dir=str(per_product_dir),
                        navigate=True,
                        validate_retries=args.validate_retries,
                        validate_cooldown=args.validate_cooldown,
                        manual_captcha=args.manual_captcha,
                        manual_captcha_wait=args.manual_captcha_wait,
                        resume_excel=resume_excel,
                    )
                except Exception as exc:
                    if attempt >= args.product_attempts:
                        raise
                    print(
                        f"[REQUEUE] item={item_id} exception={exc!r}; "
                        f"冷却 {args.product_retry_delay:.0f} 秒后重试"
                    )
                    await asyncio.sleep(args.product_retry_delay)
                    continue
                if result.status in {"complete_limit", "complete_all"}:
                    break
                resume_excel = str(result.excel_file) if result.count else None
                if attempt < args.product_attempts:
                    print(
                        f"[REQUEUE] item={item_id} status={result.status} count={result.count}; "
                        f"冷却 {args.product_retry_delay:.0f} 秒后断点重试"
                    )
                    await asyncio.sleep(args.product_retry_delay)

            if result is None:
                raise RuntimeError("商品抓取未产生结果")
            excel_file, jsonl_file, count = result

            if count:
                frame = pd.read_excel(excel_file)
                prefix = source_prefix(row)
                for key, value in reversed(list(prefix.items())):
                    frame.insert(0, key, value)
                review_frames.append(frame)
                append_jsonl(final_jsonl, frame.to_dict(orient="records"))
                total_reviews += len(frame)

            summary.update(
                {
                    "重跑状态": (
                        result.status
                        if count == 0 or result.status.startswith("complete")
                        else f"partial_{result.status}"
                    ),
                    "重跑条数": int(count),
                    "单品Excel": str(excel_file),
                    "单品JSONL": str(jsonl_file),
                    "错误": "" if result.status.startswith("complete") else result.stop_reason,
                }
            )
            if result.status == "blocked_validate":
                blocked_streak += 1
            else:
                blocked_streak = 0
        except Exception as exc:
            summary.update({"重跑状态": "error", "错误": repr(exc)})
            print(f"[ERROR] item={item_id} {exc}")
            if args.stop_on_error:
                product_rows.append(summary)
                write_workbook(partial_excel, review_frames, product_rows)
                raise

        product_rows.append(summary)
        write_workbook(partial_excel, review_frames, product_rows)
        print(f"[PARTIAL] products_done={len(product_rows)} total_reviews={total_reviews} file={partial_excel}")

        if blocked_streak >= args.blocked_streak_limit:
            print(
                f"[CIRCUIT] 连续 {blocked_streak} 个商品触发风控，"
                f"全局冷却 {args.global_cooldown:.0f} 秒"
            )
            await asyncio.sleep(args.global_cooldown)
            blocked_streak = 0

        if i < len(products):
            delay = random.uniform(args.product_min_delay, args.product_max_delay)
            print(f"[SLEEP] 商品间隔 {delay:.1f} 秒")
            await asyncio.sleep(delay)

    write_workbook(final_excel, review_frames, product_rows)
    return final_excel, final_jsonl, total_reviews, total_selected


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从上一轮汇总 Excel 的 products sheet 中筛选没抓到的商品并重跑。")
    parser.add_argument("input_xlsx", help="上一轮 feishu_base_tmall_reviews_*.xlsx")
    parser.add_argument("--only-zero", action="store_true", help="只重跑抓取条数为 0 的商品")
    parser.add_argument("--only-under", type=int, default=None, help="重跑抓取条数小于 N 的商品，例如 1500")
    parser.add_argument("--limit-products", type=int, default=None, help="只跑前 N 个，用于测试")
    parser.add_argument("--resume-run-dir", default=None, help="接续上一轮 rerun_missing_reviews_* 目录，继承已完成商品和单品 partial")
    parser.add_argument("--max-reviews-per-product", type=int, default=1500)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--min-delay", type=float, default=20.0)
    parser.add_argument("--max-delay", type=float, default=40.0)
    parser.add_argument("--product-min-delay", type=float, default=120.0)
    parser.add_argument("--product-max-delay", type=float, default=240.0)
    parser.add_argument("--manual-captcha", action="store_true")
    parser.add_argument("--manual-captcha-wait", type=float, default=240.0)
    parser.add_argument("--validate-retries", type=int, default=0)
    parser.add_argument("--validate-cooldown", type=float, default=300.0)
    parser.add_argument("--product-attempts", type=int, default=2, help="每个商品最多执行轮数，默认 2；后续轮次断点续页")
    parser.add_argument("--product-retry-delay", type=float, default=900.0, help="商品失败后再次尝试前的冷却秒数，默认 900")
    parser.add_argument("--blocked-streak-limit", type=int, default=2, help="连续多少个商品触发风控后进入全局冷却，默认 2")
    parser.add_argument("--global-cooldown", type=float, default=1800.0, help="连续风控后的全局冷却秒数，默认 1800")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUTPUT_ROOT))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    excel, jsonl, count, product_count = asyncio.run(run(args))
    print(f"[DONE] rerun_products={product_count} reviews={count}")
    print(f"[DONE] Excel: {excel}")
    print(f"[DONE] JSONL: {jsonl}")


if __name__ == "__main__":
    main()
