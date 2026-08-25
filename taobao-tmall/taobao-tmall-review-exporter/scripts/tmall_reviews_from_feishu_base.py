import argparse
import asyncio
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "runtime" / "reviews"

from tmall_rate_auto import crawl_from_url, extract_item_id


DEFAULT_CONTEXT_FIELDS = [
    "TOP类别",
    "排名",
    "来源工作表",
    "品牌",
    "商品名称",
    "商品链接",
    "销量（件）",
    "销售额（元）",
    "售价",
    "价格段",
    "年龄段",
    "场景",
    "核心价值",
    "渠道",
    "年份",
]


def decode_process_output(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def run_lark_cli(args: list[str]) -> dict[str, Any]:
    cli = (
        os.environ.get("LARK_CLI")
        or shutil.which("lark-cli.cmd")
        or shutil.which("lark-cli.exe")
        or shutil.which("lark-cli.ps1")
        or shutil.which("lark-cli")
    )
    if not cli:
        raise RuntimeError("没有找到 lark-cli。请先安装并登录，或通过 LARK_CLI 环境变量指定路径。")

    cli_path = Path(cli)
    node = os.environ.get("LARK_CLI_NODE") or shutil.which("node")
    run_js = os.environ.get("LARK_CLI_RUN_JS")
    if not run_js:
        candidate = cli_path.parent / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js"
        run_js = str(candidate) if candidate.exists() else ""

    if node and run_js and Path(run_js).exists():
        command = [str(node), str(run_js), *args]
    elif cli_path.suffix.lower() == ".ps1":
        command = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(cli_path),
            *args,
        ]
    elif cli_path.suffix.lower() in {".cmd", ".bat"}:
        command = ["cmd", "/d", "/s", "/c", str(cli_path), *args]
    else:
        command = [str(cli_path), *args]

    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    stdout = decode_process_output(completed.stdout)
    stderr = decode_process_output(completed.stderr)
    if completed.returncode != 0:
        raise RuntimeError(f"lark-cli failed: {' '.join(args)}\n{stderr or stdout}")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"lark-cli did not return JSON: {' '.join(args)}\n{stdout[:1000]}") from exc
    if payload.get("ok") is False:
        raise RuntimeError(f"lark-cli returned ok=false: {json.dumps(payload, ensure_ascii=False)[:1000]}")
    return payload


def resolve_base_url(base_url: str) -> tuple[str, str, str]:
    payload = run_lark_cli(["base", "+url-resolve", "--url", base_url, "--as", "user", "--json"])
    data = payload.get("data") or {}
    base_token = data.get("base_token") or data.get("app_token")

    parsed = urlparse(base_url)
    query = parse_qs(parsed.query)
    table_id = data.get("table_id") or (query.get("table") or [""])[0]
    view_id = data.get("view_id") or (query.get("view") or [""])[0]

    if not base_token:
        raise ValueError("没有从飞书链接解析到 base_token")
    if not table_id:
        raise ValueError("没有从飞书链接解析到 table_id；请确认 URL 带 table= 参数")
    if not view_id:
        print("[WARN] 链接里没有 view= 参数，将读取整张表。")

    return str(base_token), str(table_id), str(view_id)


def list_fields(base_token: str, table_id: str) -> list[dict[str, Any]]:
    payload = run_lark_cli(
        [
            "base",
            "+field-list",
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--as",
            "user",
            "--json",
        ]
    )
    return list((payload.get("data") or {}).get("fields") or [])


def get_table_name(base_token: str, table_id: str) -> str:
    payload = run_lark_cli(
        [
            "base",
            "+table-get",
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--as",
            "user",
            "--json",
        ]
    )
    table = (payload.get("data") or {}).get("table") or {}
    return scalar_to_text(table.get("name")) or table_id


def sanitize_filename(value: str, fallback: str = "feishu_base_tmall_reviews") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:120] or fallback


def make_review_output_stem(table_name: str) -> str:
    name = table_name.strip()
    for suffix in ("四维分类", "分类"):
        if name.endswith(suffix):
            name = name[: -len(suffix)] + "评论"
            break
    else:
        if "评论" not in name and "评价" not in name:
            name += "评论"
    return sanitize_filename(name)


def scalar_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "；".join(scalar_to_text(item) for item in value if scalar_to_text(item))
    if isinstance(value, dict):
        if "text" in value:
            return scalar_to_text(value["text"])
        if "name" in value and len(value) <= 4:
            return scalar_to_text(value["name"])
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def collect_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (int, float, bool)):
        return [str(value)]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(collect_strings(item))
        return out
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(collect_strings(item))
        return out
    return [str(value)]


def extract_urls(value: Any) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for text in collect_strings(value):
        for match in re.findall(r"https?://[^\s\]\)<>\"']+", text):
            url = match.replace("\\u0026", "&").rstrip("，。；;,.")
            if ("detail.tmall.com" in url or "item.taobao.com" in url or "detail.tmall.hk" in url) and url not in seen:
                urls.append(url)
                seen.add(url)
    return urls


def list_base_records(
    base_token: str,
    table_id: str,
    view_id: str,
    field_names: list[str],
    page_size: int,
    limit_records: int | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0

    while True:
        current_limit = page_size
        if limit_records is not None:
            remaining = limit_records - len(rows)
            if remaining <= 0:
                break
            current_limit = min(current_limit, remaining)

        args = [
            "base",
            "+record-list",
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--limit",
            str(current_limit),
            "--offset",
            str(offset),
            "--as",
            "user",
            "--json",
        ]
        if view_id:
            args.extend(["--view-id", view_id])
        for field in field_names:
            args.extend(["--field-id", field])

        payload = run_lark_cli(args)
        data = payload.get("data") or {}
        fields = list(data.get("fields") or [])
        values = list(data.get("data") or [])
        record_ids = list(data.get("record_id_list") or [])

        for i, value_row in enumerate(values):
            record = {field: value_row[j] if j < len(value_row) else None for j, field in enumerate(fields)}
            record["_record_id"] = record_ids[i] if i < len(record_ids) else ""
            rows.append(record)

        if not data.get("has_more") or not values:
            break
        offset += len(values)

    return rows


def normalize_products(records: list[dict[str, Any]], link_field: str) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    seen_item_ids: set[str] = set()

    for record in records:
        urls = extract_urls(record.get(link_field))
        if not urls:
            for key, value in record.items():
                if key.startswith("_") or key == link_field:
                    continue
                urls.extend(extract_urls(value))

        for url in urls:
            try:
                item_id = extract_item_id(url)
            except Exception:
                continue
            if item_id in seen_item_ids:
                continue
            seen_item_ids.add(item_id)
            products.append(
                {
                    "record_id": record.get("_record_id", ""),
                    "item_id": item_id,
                    "url": url,
                    "source": record,
                }
            )

    return products


def source_columns(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "来源记录ID": source.get("_record_id", ""),
        "来源TOP类别": scalar_to_text(source.get("TOP类别")),
        "来源排名": scalar_to_text(source.get("排名")),
        "来源工作表": scalar_to_text(source.get("来源工作表")),
        "来源品牌": scalar_to_text(source.get("品牌")),
        "来源商品名称": scalar_to_text(source.get("商品名称")),
        "来源商品链接": scalar_to_text(source.get("商品链接")),
        "来源销量（件）": scalar_to_text(source.get("销量（件）")),
        "来源销售额（元）": scalar_to_text(source.get("销售额（元）")),
        "来源售价": scalar_to_text(source.get("售价")),
        "来源价格段": scalar_to_text(source.get("价格段")),
        "来源年龄段": scalar_to_text(source.get("年龄段")),
        "来源场景": scalar_to_text(source.get("场景")),
        "来源核心价值": scalar_to_text(source.get("核心价值")),
        "来源渠道": scalar_to_text(source.get("渠道")),
        "来源年份": scalar_to_text(source.get("年份")),
    }


def write_workbook(
    excel_path: Path,
    review_frames: list[pd.DataFrame],
    product_summary: list[dict[str, Any]],
) -> None:
    reviews = pd.concat(review_frames, ignore_index=True) if review_frames else pd.DataFrame()
    products = pd.DataFrame(product_summary)
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        reviews.to_excel(writer, sheet_name="reviews", index=False)
        products.to_excel(writer, sheet_name="products", index=False)


def append_jsonl(jsonl_path: Path, rows: list[dict[str, Any]]) -> None:
    with jsonl_path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


async def crawl_products(args: argparse.Namespace, products: list[dict[str, Any]]) -> tuple[Path, Path, int]:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir = out_dir / f"feishu_base_reviews_{stamp}"
    per_product_dir = run_dir / "per_product"
    per_product_dir.mkdir(parents=True, exist_ok=True)

    output_stem = sanitize_filename(getattr(args, "output_stem", "") or f"feishu_base_tmall_reviews_{stamp}")
    final_excel = run_dir / f"{output_stem}.xlsx"
    final_jsonl = run_dir / f"{output_stem}.jsonl"
    partial_excel = run_dir / f"{output_stem}_partial_{stamp}.xlsx"

    review_frames: list[pd.DataFrame] = []
    product_summary: list[dict[str, Any]] = []
    total_reviews = 0
    blocked_streak = 0

    for index, product in enumerate(products, start=1):
        item_id = product["item_id"]
        url = product["url"]
        source = product["source"]
        print(f"[PRODUCT] {index}/{len(products)} item={item_id} {scalar_to_text(source.get('品牌'))} {scalar_to_text(source.get('商品名称'))[:40]}")

        summary = {
            **source_columns(source),
            "商品ID": item_id,
            "抓取链接": url,
            "抓取状态": "pending",
            "抓取条数": 0,
            "单品Excel": "",
            "单品JSONL": "",
            "错误": "",
        }

        try:
            result = None
            resume_excel: str | None = None
            for attempt in range(1, args.product_attempts + 1):
                print(f"[ATTEMPT] item={item_id} {attempt}/{args.product_attempts}")
                try:
                    result = await crawl_from_url(
                        item_or_url=url,
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
                prefix = source_columns(source)
                prefix["抓取商品ID"] = item_id
                prefix["抓取链接"] = url
                for key, value in reversed(list(prefix.items())):
                    frame.insert(0, key, value)
                review_frames.append(frame)
                append_jsonl(final_jsonl, frame.to_dict(orient="records"))
                total_reviews += len(frame)

            summary.update(
                {
                    "抓取状态": (
                        result.status
                        if count == 0 or result.status.startswith("complete")
                        else f"partial_{result.status}"
                    ),
                    "抓取条数": count,
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
            summary.update({"抓取状态": "error", "错误": repr(exc)})
            print(f"[ERROR] item={item_id} {exc}")
            if args.stop_on_error:
                product_summary.append(summary)
                write_workbook(partial_excel, review_frames, product_summary)
                raise

        product_summary.append(summary)
        write_workbook(partial_excel, review_frames, product_summary)
        print(f"[PARTIAL] 已汇总 {total_reviews} 条评价：{partial_excel}")

        if blocked_streak >= args.blocked_streak_limit:
            print(
                f"[CIRCUIT] 连续 {blocked_streak} 个商品触发风控，"
                f"全局冷却 {args.global_cooldown:.0f} 秒"
            )
            await asyncio.sleep(args.global_cooldown)
            blocked_streak = 0

        if index < len(products):
            delay = random.uniform(args.product_min_delay, args.product_max_delay)
            print(f"[SLEEP] 商品间隔 {delay:.1f} 秒")
            await asyncio.sleep(delay)

    write_workbook(final_excel, review_frames, product_summary)
    return final_excel, final_jsonl, total_reviews


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从飞书多维表格读取商品链接，逐个抓取淘宝/天猫评价，并汇总到一个 Excel。"
    )
    parser.add_argument("base_url", help="飞书多维表格 / Wiki Base 链接，URL 中最好包含 table= 和 view=")
    parser.add_argument("--link-field", default="商品链接", help="商品链接字段名，默认：商品链接")
    parser.add_argument("--max-reviews-per-product", type=int, default=1500, help="每个商品最多抓取评价数，默认 1500")
    parser.add_argument("--page-size", type=int, default=50, help="MTOP 每页评价数，默认 50")
    parser.add_argument("--min-delay", type=float, default=12.0, help="同一商品翻页最小等待秒数，默认 12")
    parser.add_argument("--max-delay", type=float, default=25.0, help="同一商品翻页最大等待秒数，默认 25")
    parser.add_argument("--product-min-delay", type=float, default=60.0, help="商品之间最小等待秒数，默认 60")
    parser.add_argument("--product-max-delay", type=float, default=120.0, help="商品之间最大等待秒数，默认 120")
    parser.add_argument("--manual-captcha", action="store_true", help="遇到验证码时在当前 Chrome 打开验证页并等待人工完成")
    parser.add_argument("--manual-captcha-wait", type=float, default=180.0, help="验证码人工处理等待秒数，默认 180")
    parser.add_argument("--validate-retries", type=int, default=0, help="风控冷却重试次数，默认 0")
    parser.add_argument("--validate-cooldown", type=float, default=180.0, help="风控冷却秒数，默认 180")
    parser.add_argument("--product-attempts", type=int, default=2, help="每个商品最多执行轮数，默认 2；后续轮次断点续页")
    parser.add_argument("--product-retry-delay", type=float, default=900.0, help="商品失败后再次尝试前的冷却秒数，默认 900")
    parser.add_argument("--blocked-streak-limit", type=int, default=2, help="连续多少个商品触发风控后进入全局冷却，默认 2")
    parser.add_argument("--global-cooldown", type=float, default=1800.0, help="连续风控后的全局冷却秒数，默认 1800")
    parser.add_argument("--record-page-size", type=int, default=200, help="读取飞书记录的分页大小，默认 200")
    parser.add_argument("--limit-records", type=int, default=None, help="只读取前 N 条飞书记录，用于测试")
    parser.add_argument("--limit-products", type=int, default=None, help="只抓前 N 个商品，用于测试")
    parser.add_argument("--dry-run", action="store_true", help="只读取飞书表并列出待抓商品，不抓评价")
    parser.add_argument("--stop-on-error", action="store_true", help="任一商品失败后立即停止")
    parser.add_argument("--output-name", default=None, help="最终 Excel/JSONL 文件名前缀；默认按飞书数据表名生成")
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_OUTPUT_ROOT),
        help=f"输出目录，默认 {DEFAULT_OUTPUT_ROOT}",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    base_token, table_id, view_id = resolve_base_url(args.base_url)
    table_name = get_table_name(base_token, table_id)
    args.output_stem = sanitize_filename(args.output_name) if args.output_name else make_review_output_stem(table_name)
    fields = list_fields(base_token, table_id)
    field_names_available = [field.get("name") for field in fields if field.get("name")]
    field_names = [field for field in DEFAULT_CONTEXT_FIELDS if field in field_names_available]

    if args.link_field not in field_names_available:
        raise ValueError(f"表里没有字段：{args.link_field}。现有字段：{', '.join(field_names_available)}")
    if args.link_field not in field_names:
        field_names.append(args.link_field)

    print(f"[BASE] base_token={base_token} table_id={table_id} view_id={view_id or '(all)'}")
    print(f"[BASE] table_name={table_name}")
    print(f"[OUTPUT] final_name={args.output_stem}.xlsx / {args.output_stem}.jsonl")
    print(f"[BASE] 读取字段：{', '.join(field_names)}")

    records = list_base_records(
        base_token=base_token,
        table_id=table_id,
        view_id=view_id,
        field_names=field_names,
        page_size=args.record_page_size,
        limit_records=args.limit_records,
    )
    products = normalize_products(records, args.link_field)
    if args.limit_products is not None:
        products = products[: args.limit_products]

    print(f"[BASE] 读取记录 {len(records)} 条，识别商品 {len(products)} 个")
    for i, product in enumerate(products[:20], start=1):
        source = product["source"]
        print(
            f"[ITEM] {i}. {product['item_id']} | {scalar_to_text(source.get('品牌'))} | "
            f"{scalar_to_text(source.get('商品名称'))[:60]}"
        )
    if len(products) > 20:
        print(f"[ITEM] ... 还有 {len(products) - 20} 个")

    if args.dry_run:
        print("[DRY-RUN] 只做读取和识别，未开始抓评价。")
        return
    if not products:
        raise RuntimeError("没有识别到可抓取的淘宝/天猫商品链接。")

    excel_file, jsonl_file, count = asyncio.run(crawl_products(args, products))
    print(f"[DONE] 成功汇总 {count} 条评价")
    print(f"[DONE] Excel: {excel_file}")
    print(f"[DONE] JSONL: {jsonl_file}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[STOP] 用户中断。", file=sys.stderr)
        raise
