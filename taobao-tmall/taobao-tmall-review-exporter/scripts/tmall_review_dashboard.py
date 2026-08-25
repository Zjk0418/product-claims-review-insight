import argparse
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd
import psutil


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "runtime" / "reviews"
OUTPUT_ROOT = Path(
    os.environ.get(
        "TMALL_REVIEWS_OUTPUT",
        DEFAULT_OUTPUT_ROOT,
    )
).resolve()
RUN_LOGS = OUTPUT_ROOT / "run_logs"
DASHBOARD_DIR = OUTPUT_ROOT / "dashboard"
COMBINED_XLSX = DASHBOARD_DIR / "combined_tmall_reviews_live.xlsx"
MERGE_META = DASHBOARD_DIR / "combined_tmall_reviews_live.json"

STATE_LOCK = threading.Lock()
MERGE_LOCK = threading.Lock()
STATUS_CACHE: dict[str, Any] = {"at": 0.0, "value": None}
MERGE_CACHE: dict[str, Any] = {"signature": None, "meta": None}


def configure_paths(out_dir: str | Path) -> None:
    global OUTPUT_ROOT, RUN_LOGS, DASHBOARD_DIR, COMBINED_XLSX, MERGE_META
    OUTPUT_ROOT = Path(out_dir).expanduser().resolve()
    RUN_LOGS = OUTPUT_ROOT / "run_logs"
    DASHBOARD_DIR = OUTPUT_ROOT / "dashboard"
    COMBINED_XLSX = DASHBOARD_DIR / "combined_tmall_reviews_live.xlsx"
    MERGE_META = DASHBOARD_DIR / "combined_tmall_reviews_live.json"
    STATUS_CACHE.update({"at": 0.0, "value": None})
    MERGE_CACHE.update({"signature": None, "meta": None})


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>天猫评价采集监控</title>
  <style>
    :root{--bg:#f4f6fb;--card:#fff;--ink:#172033;--muted:#72809a;--line:#e6eaf2;--blue:#356df3;--green:#16a36a;--orange:#f59e0b;--red:#dc4c64}
    *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,"PingFang SC","Microsoft YaHei",sans-serif}
    .wrap{max-width:1380px;margin:auto;padding:24px}.top{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:18px}
    h1{font-size:24px;margin:0 0 7px}.sub{color:var(--muted);font-size:13px}.right{display:flex;gap:10px;align-items:center}
    .pill{padding:8px 12px;border-radius:999px;font-size:13px;font-weight:700;background:#e9f8f1;color:var(--green)}.pill.stop{background:#feecef;color:var(--red)}
    .btn{display:inline-block;text-decoration:none;background:var(--blue);color:#fff;padding:9px 14px;border-radius:9px;font-size:13px;font-weight:700}
    .grid{display:grid;grid-template-columns:repeat(6,1fr);gap:12px}.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;box-shadow:0 3px 14px rgba(27,39,71,.04)}
    .label{font-size:12px;color:var(--muted);margin-bottom:8px}.num{font-size:27px;font-weight:800;letter-spacing:-.6px}.hint{font-size:12px;color:var(--muted);margin-top:5px}
    .wide{grid-column:span 4}.side{grid-column:span 2}.section{margin-top:14px}.section h2{font-size:16px;margin:0 0 12px}
    .bar{height:13px;background:#edf0f6;border-radius:99px;overflow:hidden}.fill{height:100%;width:0;background:linear-gradient(90deg,#356df3,#6f95f8);transition:width .5s}
    .barrow{display:flex;justify-content:space-between;font-size:13px;margin:8px 0;color:var(--muted)}
    .etagrid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:16px}.etabox{background:#f7f9fd;border:1px solid var(--line);border-radius:10px;padding:12px}.etabox span{display:block;color:var(--muted);font-size:12px;margin-bottom:6px}.etabox strong{font-size:18px}.etanote{font-size:12px;color:var(--muted);margin-top:9px}
    .reason{line-height:1.75;font-size:14px}.reason strong{color:var(--ink)}
    .stack{height:22px;display:flex;border-radius:7px;overflow:hidden;margin:15px 0 8px}.old{background:#8ba8f8}.new{background:#25bd81}.cur{background:#f5b74f}
    .legend{display:flex;gap:18px;flex-wrap:wrap;color:var(--muted);font-size:12px}.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
    table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:10px 9px;border-bottom:1px solid var(--line)}th{color:var(--muted);font-weight:650;background:#fafbfe;position:sticky;top:0}
    .tablebox{max-height:480px;overflow:auto;border:1px solid var(--line);border-radius:10px}.status{font-weight:700}.complete{color:var(--green)}.running{color:var(--blue)}.pending{color:var(--muted)}.blocked{color:var(--red)}
    .fresh{font-size:12px;color:var(--muted);margin-top:10px}.warn{color:#9a6700;background:#fff7df;border:1px solid #f6df9f;padding:10px 12px;border-radius:9px;font-size:13px;margin-top:12px}
    @media(max-width:980px){.grid{grid-template-columns:repeat(2,1fr)}.wide,.side{grid-column:span 2}.top{flex-direction:column}.right{flex-wrap:wrap}.etagrid{grid-template-columns:1fr}}
  </style>
</head>
<body><div class="wrap">
  <div class="top"><div><h1>天猫评价采集监控</h1><div class="sub">本地实时读取抓取日志与 Excel，页面每 3 秒刷新</div></div><div class="right"><span id="proc" class="pill">读取中</span><a class="btn" href="/download/combined.xlsx">下载实时合并 Excel</a></div></div>
  <div class="grid">
    <div class="card"><div class="label">待补抓商品</div><div class="num" id="target">-</div><div class="hint">首轮为 0 的商品</div></div>
    <div class="card"><div class="label">已完成补抓</div><div class="num" id="done">-</div><div class="hint" id="doneHint">-</div></div>
    <div class="card"><div class="label">当前商品</div><div class="num" id="current">-</div><div class="hint" id="page">-</div></div>
    <div class="card"><div class="label">昨天评价</div><div class="num" id="oldCount">-</div><div class="hint">首轮汇总</div></div>
    <div class="card"><div class="label">本次新增</div><div class="num" id="newCount">-</div><div class="hint">含当前 partial</div></div>
    <div class="card"><div class="label">去重合计</div><div class="num" id="merged">-</div><div class="hint" id="dedup">-</div></div>
  </div>
  <div class="grid section">
    <div class="card wide"><h2>总体进度</h2><div class="bar"><div class="fill" id="progress"></div></div><div class="barrow"><span id="progressText">-</span><span id="phase">-</span></div>
      <div class="etagrid"><div class="etabox"><span>已运行</span><strong id="elapsed">-</strong></div><div class="etabox"><span>预计剩余</span><strong id="eta">-</strong></div><div class="etabox"><span>预计完成时间</span><strong id="finish">-</strong></div></div><div class="etanote">根据本次任务当前平均商品耗时动态估算，验证码等待和风控冷却会计入预测。</div>
      <h2 style="margin-top:22px">评价数据构成</h2><div class="stack"><div class="old" id="oldSeg"></div><div class="new" id="newSeg"></div><div class="cur" id="curSeg"></div></div>
      <div class="legend"><span><i class="dot" style="background:#8ba8f8"></i>昨天</span><span><i class="dot" style="background:#25bd81"></i>本次已完成</span><span><i class="dot" style="background:#f5b74f"></i>当前商品</span></div>
    </div>
    <div class="card side"><h2>验证码状态</h2><div class="num" id="captcha">-</div><div class="hint">本次挑战次数</div><div class="reason" id="reason" style="margin-top:13px">-</div><div class="warn">没有弹出验证码只表示当前请求未收到 RGV587，不代表存在固定的永久放行。</div></div>
  </div>
  <div class="card section"><h2>商品队列</h2><div class="tablebox"><table><thead><tr><th>#</th><th>商品ID</th><th>品牌 / 商品</th><th>状态</th><th>评价条数</th><th>当前页</th><th>验证码</th></tr></thead><tbody id="rows"></tbody></table></div><div class="fresh" id="fresh">-</div></div>
</div>
<script>
const el=id=>document.getElementById(id);const n=v=>Number(v||0).toLocaleString('zh-CN');
function cls(s){if(s.includes('完成'))return'complete';if(s.includes('进行'))return'running';if(s.includes('风控')||s.includes('错误'))return'blocked';return'pending'}
async function refresh(){
 try{const r=await fetch('/api/status',{cache:'no-store'});const d=await r.json();
  const p=el('proc');p.textContent=d.process.running?'采集中':'已停止';p.className='pill'+(d.process.running?'':' stop');
  el('target').textContent=d.run.target_products;el('done').textContent=d.run.completed_products;el('doneHint').textContent=`完成评价 ${n(d.run.completed_reviews)} 条`;
  el('current').textContent=d.run.current_item||'-';el('page').textContent=d.run.current_item?`第 ${d.run.current_page}/${d.run.max_pages} 页 · ${n(d.run.current_reviews)} 条`:'等待中';
  el('oldCount').textContent=n(d.data.old_reviews);el('newCount').textContent=n(d.data.new_reviews_live);el('merged').textContent=n(d.data.merged_reviews);el('dedup').textContent=`去重 ${n(d.data.duplicates_removed)} 条`;
  el('progress').style.width=`${d.run.progress_percent}%`;el('progressText').textContent=`${d.run.progress_percent.toFixed(1)}% · ${d.run.completed_products}/${d.run.target_products} 个完成`;el('phase').textContent=d.run.phase;
  el('elapsed').textContent=d.run.elapsed_text;el('eta').textContent=d.run.eta_text;el('finish').textContent=d.run.expected_finish;
  el('captcha').textContent=d.captcha.events;el('reason').innerHTML=d.captcha.explanation;
  const total=Math.max(1,d.data.old_reviews+d.data.completed_new_reviews+d.run.current_reviews);el('oldSeg').style.width=`${100*d.data.old_reviews/total}%`;el('newSeg').style.width=`${100*d.data.completed_new_reviews/total}%`;el('curSeg').style.width=`${100*d.run.current_reviews/total}%`;
  el('rows').innerHTML=d.products.map((x,i)=>`<tr><td>${i+1}</td><td>${x.item_id}</td><td>${x.brand?x.brand+' · ':''}${x.title||''}</td><td class="status ${cls(x.status)}">${x.status}</td><td>${n(x.count)}</td><td>${x.page||'-'}</td><td>${x.captcha}</td></tr>`).join('');
  el('fresh').textContent=`数据更新时间：${d.updated_at} · 日志最后写入：${d.process.log_updated_at} · 合并文件：${d.data.merge_updated_at||'准备中'}`;
 }catch(e){el('proc').textContent='读取失败';el('proc').className='pill stop';el('fresh').textContent=String(e)}}
refresh();setInterval(refresh,3000);
</script></body></html>"""


def read_latest_run() -> dict[str, Any]:
    path = RUN_LOGS / "latest_rerun_missing.json"
    return json.loads(path.read_text(encoding="utf-8-sig"))


def source_xlsx(run: dict[str, Any]) -> Path:
    configured = run.get("input_xlsx")
    if configured and Path(configured).exists():
        return Path(configured)
    candidates = [
        path
        for path in OUTPUT_ROOT.glob("feishu_base_reviews_*/feishu_base_tmall_reviews_*.xlsx")
        if "partial" not in path.name
    ]
    if not candidates:
        candidates = [
            path
            for path in OUTPUT_ROOT.glob("feishu_base_reviews_*/*.xlsx")
            if "partial" not in path.name and path.parent.name.startswith("feishu_base_reviews_")
        ]
    if not candidates:
        raise FileNotFoundError("未找到首轮汇总 Excel")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def read_log(path: Path) -> tuple[list[str], str]:
    raw = path.read_bytes() if path.exists() else b""
    for encoding in ("utf-8", "gb18030"):
        try:
            text = raw.decode(encoding)
            return text.splitlines(), text
        except UnicodeDecodeError:
            continue
    text = raw.decode("utf-8", errors="replace")
    return text.splitlines(), text


def process_running(pid: int) -> bool:
    try:
        return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
    except (psutil.Error, OSError):
        return False


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "计算中"
    total_minutes = max(0, int(round(seconds / 60)))
    days, rem = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours or days:
        parts.append(f"{hours}小时")
    parts.append(f"{minutes}分钟")
    return " ".join(parts)


def add_eta(run: dict[str, Any], parsed: dict[str, Any], running: bool) -> None:
    now = datetime.now()
    try:
        started = datetime.fromisoformat(str(run.get("started_at", "")))
    except ValueError:
        started = now
    elapsed = max(0.0, (now - started).total_seconds())
    eta_seconds: float | None = None
    current_remaining = 0
    if parsed["current_index"] > parsed["completed_products"]:
        current_remaining = max(0, parsed["max_pages"] * 50 - parsed["current_reviews"])
    remaining_reviews = max(
        0,
        (parsed["target_products"] - parsed["completed_products"] - (1 if current_remaining else 0))
        * parsed["max_pages"]
        * 50
        + current_remaining,
    )
    fetched = parsed.get("fetched_reviews_this_run", 0)
    if not running and remaining_reviews == 0:
        eta_seconds = 0.0
    elif fetched >= 50 and elapsed > 0:
        eta_seconds = elapsed / fetched * remaining_reviews

    parsed["elapsed_seconds"] = int(elapsed)
    parsed["elapsed_text"] = format_duration(elapsed)
    parsed["eta_seconds"] = int(eta_seconds) if eta_seconds is not None else None
    parsed["eta_text"] = format_duration(eta_seconds)
    parsed["expected_finish"] = (
        (now + timedelta(seconds=eta_seconds)).strftime("%m月%d日 %H:%M")
        if eta_seconds is not None
        else "计算中"
    )


def parse_log(lines: list[str], target: int) -> dict[str, Any]:
    current_item = ""
    current_index = 0
    current_page = 0
    max_pages = 30
    current_reviews = 0
    completed_products = 0
    completed_reviews = 0
    captcha_by_item: dict[str, int] = {}
    product_order: list[str] = []
    last_signal = "启动中"
    fetched_reviews_this_run = 0

    for line in lines:
        m = re.search(r"\[PRODUCT\]\s+(\d+)/(\d+)\s+item=(\d+)", line)
        if m:
            current_index = int(m.group(1))
            target = int(m.group(2))
            current_item = m.group(3)
            current_page = 0
            current_reviews = 0
            if current_item not in product_order:
                product_order.append(current_item)
            last_signal = "正在打开商品"
            continue
        m = re.search(r"\[FETCH\]\s+item=(\d+)\s+page=(\d+)/(\d+)", line)
        if m:
            current_item = m.group(1)
            current_page = int(m.group(2))
            max_pages = int(m.group(3))
            last_signal = "正在抓取"
            continue
        m = re.search(r"\[OK\].*got=(\d+).*total_got=(\d+)", line)
        if m:
            fetched_reviews_this_run += int(m.group(1))
            current_reviews = int(m.group(2))
            last_signal = "正在抓取"
            continue
        m = re.search(r"products_done=(\d+)\s+total_reviews=(\d+)", line)
        if m:
            completed_products = int(m.group(1))
            completed_reviews = int(m.group(2))
            last_signal = "商品间冷却"
            continue
        m = re.search(r"\[RESUME-RUN\]\s+carried_products=(\d+)", line)
        if m:
            completed_products = max(completed_products, int(m.group(1)))
            continue
        if line.startswith("[CAPTCHA]") and "http" in line:
            captcha_by_item[current_item] = captcha_by_item.get(current_item, 0) + 1
            last_signal = "等待人工验证码"
            continue
        if line.startswith("[SLEEP]"):
            last_signal = "商品间冷却"
        elif line.startswith("[ERROR]"):
            last_signal = "发生错误，准备继续"
        elif line.startswith("[DONE]"):
            last_signal = "已完成"

    fractional = min(1.0, current_reviews / max(1, max_pages * 50)) if current_index > completed_products else 0.0
    progress = min(100.0, 100.0 * (completed_products + fractional) / max(1, target))
    return {
        "target_products": target,
        "completed_products": completed_products,
        "completed_reviews": completed_reviews,
        "current_item": current_item,
        "current_index": current_index,
        "current_page": current_page,
        "max_pages": max_pages,
        "current_reviews": current_reviews,
        "progress_percent": round(progress, 2),
        "phase": last_signal,
        "captcha_by_item": captcha_by_item,
        "captcha_events": sum(captcha_by_item.values()),
        "product_order": product_order,
        "fetched_reviews_this_run": fetched_reviews_this_run,
    }


def latest_rerun_dir() -> Path:
    dirs = sorted(OUTPUT_ROOT.glob("rerun_missing_reviews_*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not dirs:
        raise FileNotFoundError("未找到补抓输出目录")
    return dirs[0]


def latest_summary_xlsx(run_dir: Path) -> Path | None:
    candidates = sorted(run_dir.glob("rerun_missing_tmall_reviews_partial_*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        candidates = sorted(run_dir.glob("rerun_missing_tmall_reviews_*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def latest_item_xlsx(run_dir: Path, item_id: str, resume_run_dir: Path | None = None) -> Path | None:
    if not item_id:
        return None
    files = list((run_dir / "per_product").glob(f"tmall_reviews_{item_id}_*.xlsx"))
    if resume_run_dir:
        files.extend((resume_run_dir / "per_product").glob(f"tmall_reviews_{item_id}_*.xlsx"))
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def normalize_new_reviews(df: pd.DataFrame) -> pd.DataFrame:
    rename = {c: c.replace("重跑来源", "来源", 1) for c in df.columns if str(c).startswith("重跑来源")}
    rename.update({"重跑商品ID": "抓取商品ID", "重跑链接": "抓取链接"})
    return df.rename(columns=rename)


def review_keys(df: pd.DataFrame) -> pd.Series:
    item = df.get("商品ID", pd.Series("", index=df.index)).fillna("").astype(str)
    rid = df.get("评价ID", pd.Series("", index=df.index)).fillna("").astype(str)
    fallback = (
        df.get("评价日期", pd.Series("", index=df.index)).fillna("").astype(str)
        + "|" + df.get("评价内容", pd.Series("", index=df.index)).fillna("").astype(str)
        + "|" + df.get("完整SKU", pd.Series("", index=df.index)).fillna("").astype(str)
    )
    return item + "|" + rid.where(rid.ne(""), fallback)


def source_prefix_from_old_products(old_products: pd.DataFrame, item_id: str) -> dict[str, Any]:
    match = old_products[old_products["商品ID"].astype(str).eq(str(item_id))]
    if match.empty:
        return {"抓取商品ID": item_id}
    row = match.iloc[0]
    prefix = {c: row.get(c, "") for c in old_products.columns if str(c).startswith("来源")}
    prefix["抓取商品ID"] = item_id
    prefix["抓取链接"] = row.get("抓取链接", "")
    return prefix


def build_merged(run: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    old_xlsx = source_xlsx(run)
    run_dir = latest_rerun_dir()
    resume_run_dir = Path(run["resume_run_dir"]) if run.get("resume_run_dir") else None
    summary = latest_summary_xlsx(run_dir)
    current = latest_item_xlsx(run_dir, parsed["current_item"], resume_run_dir)
    signature = (
        old_xlsx.stat().st_mtime_ns,
        summary.stat().st_mtime_ns if summary else 0,
        current.stat().st_mtime_ns if current else 0,
        parsed["current_item"],
        parsed["current_reviews"],
    )
    if MERGE_CACHE.get("signature") == signature and MERGE_CACHE.get("meta"):
        return MERGE_CACHE["meta"]

    with MERGE_LOCK:
        if MERGE_CACHE.get("signature") == signature and MERGE_CACHE.get("meta"):
            return MERGE_CACHE["meta"]
        old_reviews = pd.read_excel(old_xlsx, sheet_name="reviews")
        old_products = pd.read_excel(old_xlsx, sheet_name="products")
        frames = [old_reviews]
        new_products = pd.DataFrame()
        completed_new_reviews = 0
        if summary and summary.exists():
            new_reviews = pd.read_excel(summary, sheet_name="reviews")
            new_products = pd.read_excel(summary, sheet_name="products")
            new_reviews = normalize_new_reviews(new_reviews)
            completed_new_reviews = len(new_reviews)
            frames.append(new_reviews)

        current_in_summary = False
        if not new_products.empty and "重跑商品ID" in new_products:
            current_in_summary = new_products["重跑商品ID"].astype(str).eq(str(parsed["current_item"])).any()
        current_rows = 0
        if current and current.exists() and not current_in_summary:
            current_df = pd.read_excel(current)
            current_rows = len(current_df)
            prefix = source_prefix_from_old_products(old_products, parsed["current_item"])
            for key, value in reversed(list(prefix.items())):
                if key not in current_df.columns:
                    current_df.insert(0, key, value)
            frames.append(current_df)

        all_columns: list[str] = []
        for frame in frames:
            for col in frame.columns:
                if col not in all_columns:
                    all_columns.append(col)
        combined = pd.concat([f.reindex(columns=all_columns) for f in frames], ignore_index=True)
        before = len(combined)
        combined.insert(0, "数据批次", ["昨天首轮"] * len(old_reviews) + ["本次补抓"] * (len(combined) - len(old_reviews)))
        combined.insert(1, "_去重键", review_keys(combined))
        combined = combined.drop_duplicates("_去重键", keep="last").drop(columns="_去重键")

        product_rows = old_products.copy()
        product_rows["昨天抓取条数"] = pd.to_numeric(product_rows["抓取条数"], errors="coerce").fillna(0).astype(int)
        product_rows["本次补抓条数"] = 0
        if not new_products.empty:
            counts = dict(zip(new_products["重跑商品ID"].astype(str), pd.to_numeric(new_products["重跑条数"], errors="coerce").fillna(0).astype(int)))
            product_rows["本次补抓条数"] = product_rows["商品ID"].astype(str).map(counts).fillna(0).astype(int)
        if parsed["current_item"] and not current_in_summary:
            mask = product_rows["商品ID"].astype(str).eq(str(parsed["current_item"]))
            product_rows.loc[mask, "本次补抓条数"] = current_rows
        product_rows["合并条数"] = product_rows[["昨天抓取条数", "本次补抓条数"]].max(axis=1)

        temp = DASHBOARD_DIR / "combined_tmall_reviews_live.tmp.xlsx"
        with pd.ExcelWriter(temp, engine="openpyxl") as writer:
            combined.to_excel(writer, sheet_name="reviews", index=False)
            product_rows.to_excel(writer, sheet_name="products", index=False)
        try:
            os.replace(temp, COMBINED_XLSX)
        except PermissionError:
            temp.unlink(missing_ok=True)

        meta = {
            "old_reviews": len(old_reviews),
            "completed_new_reviews": completed_new_reviews,
            "completed_new_products": int(
                pd.to_numeric(new_products.get("重跑条数", pd.Series(dtype=float)), errors="coerce").fillna(0).gt(0).sum()
            ),
            "current_partial_reviews": current_rows,
            "new_reviews_live": completed_new_reviews + current_rows,
            "merged_reviews": len(combined),
            "duplicates_removed": before - len(combined),
            "merge_updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "combined_file": str(COMBINED_XLSX),
        }
        MERGE_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        MERGE_CACHE.update({"signature": signature, "meta": meta})
        return meta


def build_products(run: dict[str, Any], parsed: dict[str, Any]) -> list[dict[str, Any]]:
    old = pd.read_excel(source_xlsx(run), sheet_name="products")
    old_counts = pd.to_numeric(old["抓取条数"], errors="coerce").fillna(0).astype(int)
    selected = old[old_counts.eq(0)].copy()
    run_dir = latest_rerun_dir()
    summary = latest_summary_xlsx(run_dir)
    done_counts: dict[str, int] = {}
    done_status: dict[str, str] = {}
    if summary:
        newp = pd.read_excel(summary, sheet_name="products")
        done_counts = dict(zip(newp["重跑商品ID"].astype(str), pd.to_numeric(newp["重跑条数"], errors="coerce").fillna(0).astype(int)))
        done_status = dict(zip(newp["重跑商品ID"].astype(str), newp["重跑状态"].astype(str)))

    rows = []
    for _, row in selected.iterrows():
        item = str(row["商品ID"])
        count = done_counts.get(item, 0)
        page = 0
        if item == parsed["current_item"] and item not in done_counts:
            count = parsed["current_reviews"]
            page = parsed["current_page"]
            status = "进行中"
        elif item in done_counts:
            status = "已完成" if count > 0 else ("风控" if "blocked" in done_status.get(item, "") else "已结束 0条")
        else:
            status = "待处理"
        rows.append({
            "item_id": item,
            "brand": str(row.get("来源品牌", "") or ""),
            "title": str(row.get("来源商品名称", "") or ""),
            "status": status,
            "count": int(count),
            "page": page,
            "captcha": parsed["captcha_by_item"].get(item, 0),
        })
    return rows


def status_payload(force: bool = False) -> dict[str, Any]:
    now = time.time()
    with STATE_LOCK:
        if not force and STATUS_CACHE["value"] and now - STATUS_CACHE["at"] < 2.0:
            return STATUS_CACHE["value"]
    run = read_latest_run()
    log_path = Path(run["stdout_log"])
    lines, _ = read_log(log_path)
    parsed = parse_log(lines, int(run.get("selected_products", 0)))
    merge = build_merged(run, parsed)
    parsed["completed_products"] = max(parsed["completed_products"], merge["completed_new_products"])
    parsed["completed_reviews"] = max(parsed["completed_reviews"], merge["completed_new_reviews"])
    if merge["current_partial_reviews"] > parsed["current_reviews"]:
        parsed["current_reviews"] = merge["current_partial_reviews"]
        fractional = min(1.0, parsed["current_reviews"] / max(1, parsed["max_pages"] * 50))
        parsed["progress_percent"] = round(
            min(100.0, 100.0 * (parsed["completed_products"] + fractional) / max(1, parsed["target_products"])),
            2,
        )
    running = process_running(int(run["pid"]))
    add_eta(run, parsed, running)
    events = parsed["captcha_events"]
    current_events = parsed["captcha_by_item"].get(parsed["current_item"], 0)
    if parsed["phase"] == "等待人工验证码":
        explanation = (
            f"当前商品第 <strong>{parsed['current_page']} 页</strong>正在等待验证码。"
            f"本次任务累计 <strong>{events} 次</strong>，当前商品已触发 {current_events} 次。"
        )
    elif events:
        explanation = (
            f"本次实际已触发 <strong>{events} 次</strong>验证码。"
            "人工验证完成后，截至当前日志，后续请求没有再返回 RGV587。"
        )
    else:
        explanation = "截至当前日志，接口尚未返回 RGV587，因此没有打开验证码页面。"
    payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "process": {
            "running": running,
            "pid": int(run["pid"]),
            "log_updated_at": datetime.fromtimestamp(log_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        },
        "run": {k: v for k, v in parsed.items() if k not in {"captcha_by_item", "captcha_events", "product_order"}},
        "captcha": {"events": events, "explanation": explanation},
        "data": merge,
        "products": build_products(run, parsed),
    }
    with STATE_LOCK:
        STATUS_CACHE.update({"at": now, "value": payload})
    return payload


class Handler(BaseHTTPRequestHandler):
    def send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/":
                self.send_bytes(HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/status":
                body = json.dumps(status_payload(), ensure_ascii=False).encode("utf-8")
                self.send_bytes(body, "application/json; charset=utf-8")
            elif path == "/download/combined.xlsx":
                status_payload(force=True)
                self.send_bytes(
                    COMBINED_XLSX.read_bytes(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            elif path == "/health":
                self.send_bytes(b'{"ok":true}', "application/json")
            else:
                self.send_bytes(b"not found", "text/plain", 404)
        except Exception as exc:
            body = json.dumps({"error": repr(exc)}, ensure_ascii=False).encode("utf-8")
            self.send_bytes(body, "application/json; charset=utf-8", 500)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{datetime.now():%H:%M:%S}] {self.address_string()} {fmt % args}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="天猫评价抓取实时看板")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--out-dir",
        default=os.environ.get(
            "TMALL_REVIEWS_OUTPUT",
            str(DEFAULT_OUTPUT_ROOT),
        ),
        help="抓取输出根目录，需包含 run_logs/latest_rerun_missing.json",
    )
    args = parser.parse_args()
    configure_paths(args.out_dir)
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    status_payload(force=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Dashboard: http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
