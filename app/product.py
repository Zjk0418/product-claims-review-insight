"""商品信息采集：页面 SSR 数据 + 主图下载 + 本地 OCR。

不额外调用商品详情接口，避免触发平台风控；所有数据来自已加载的商品页。
OCR 在本机运行：图片去重（内容哈希）+ 线程并行 + 结果落盘缓存，
详情图上限放宽到 30 张后，整体耗时仍与旧版相当。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import requests

from cdp_tabs import close_tab, create_tab
from resources import resource_root

ROOT = resource_root()


def product_cache_root() -> Path:
    """图片与 OCR 缓存根目录；课堂版可通过环境变量重定向到任务目录，随任务一起清理。"""
    override = os.getenv("REVIEW_PRODUCT_CACHE", "").strip()
    return Path(override) if override else ROOT / "runtime" / "products"
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
    ),
    "Referer": "https://detail.tmall.com/",
}

SSR_EXPRESSION = r"""
(() => {
  try {
    const script = [...document.scripts].find((node) =>
      (node.textContent || '').includes('"skuBase"')
    );
    if (!script) return { ok: false, error: 'script_not_found' };
    const text = script.textContent || '';
    const marker = 'var b = ';
    const start = text.indexOf(marker) + marker.length;
    if (start < marker.length) return { ok: false, error: 'marker_not_found' };

    let depth = 0;
    let inString = false;
    let escaped = false;
    let end = -1;
    for (let i = start; i < text.length; i++) {
      const ch = text[i];
      if (inString) {
        if (escaped) escaped = false;
        else if (ch === '\\') escaped = true;
        else if (ch === '"') inString = false;
      } else if (ch === '"') {
        inString = true;
      } else if (ch === '{') {
        depth++;
      } else if (ch === '}') {
        depth--;
        if (depth === 0) { end = i + 1; break; }
      }
    }
    if (end < 0) return { ok: false, error: 'json_end_not_found' };

    const payload = JSON.parse(text.slice(start, end));
    const res = (((payload.loaderData || {}).home || {}).data || {}).res || {};
    const item = res.item || {};
    const skuBase = res.skuBase || {};
    const sku2info = (res.skuCore || {}).sku2info || {};

    const skus = (skuBase.skus || []).map((sku) => {
      const info = sku2info[sku.skuId] || {};
      const price = info.price || {};
      return {
        sku_id: String(sku.skuId || ''),
        prop_path: String(sku.propPath || ''),
        price_text: String(price.priceText || ''),
        price_title: String(price.priceTitle || ''),
        quantity: info.quantity,
        quantity_text: String(info.quantityText || ''),
        logistics_time: String(info.logisticsTime || '')
      };
    });

    const props = (skuBase.props || []).map((prop) => ({
      name: String(prop.name || ''),
      values: (prop.values || []).map((value) => String(value.name || ''))
    }));

    return {
      ok: true,
      item_id: String(item.itemId || ''),
      spu_id: String(item.spuId || ''),
      title: String(item.title || ''),
      images: (item.images || []).map(String),
      props: props,
      skus: skus
    };
  } catch (error) {
    return { ok: false, error: String((error && error.message) || error) };
  }
})()
"""

FALLBACK_IMAGES_EXPRESSION = (
    "[...document.images].map(i=>i.src)"
    ".filter(x=>x && x.includes('img.alicdn.com') && !x.includes('avatar'))"
)

DESC_URL_TEMPLATE = (
    "https://market.m.taobao.com/app/detail-project/desc/index.html"
    "?id={item_id}&descVersion=7.0&type=1&sellerType=B"
)

DETAIL_PAGE_EXPRESSION = r"""
(() => {
  const urls = new Set();
  for (const img of document.images) {
    for (const attr of ['src', 'data-src', 'data-ks-lazyload', 'data-original']) {
      const value = img.getAttribute(attr);
      if (value) urls.add(value);
    }
    if (img.currentSrc) urls.add(img.currentSrc);
  }
  // 后台标签里懒加载不会把 data-src 换进 src，直接扫一遍页面源码兜底
  const html = document.documentElement.outerHTML || '';
  const matches = html.match(/(?:https?:)?\/\/img\.alicdn\.com\/[^"'\\\s)]+/g) || [];
  for (const url of matches) urls.add(url.replace(/&amp;/g, '&'));
  return {
    text: (document.body.innerText || '').slice(0, 800),
    images: [...urls].filter(
      (url) => url.includes('img.alicdn.com') && !url.includes('avatar')
    ),
  };
})()
"""


_SCROLL_STEP_EXPRESSION = r"""
(() => {
  const step = Math.max(400, Math.floor(window.innerHeight * 0.8));
  window.scrollBy(0, step);
  return JSON.stringify({y: Math.round(window.scrollY),
                         max: Math.round(document.body.scrollHeight - window.innerHeight)});
})()
"""


async def _read_detail_page(
    cdp: Any, item_id: str, wait_seconds: float
) -> tuple[list[str], str]:
    await cdp.call("Page.enable")
    await cdp.call("Runtime.enable")
    await cdp.call("Page.navigate", {"url": DESC_URL_TEMPLATE.format(item_id=item_id)})
    # 后台标签页会被节流，滚动事件/IntersectionObserver 可能不触发；
    # 把视口临时拉成超长，让整页内容一次性进入视口，触发所有懒加载。
    tall_applied = False
    try:
        await cdp.call("Emulation.setDeviceMetricsOverride", {
            "width": 790, "height": 16000, "deviceScaleFactor": 1, "mobile": False,
        })
        tall_applied = True
    except Exception:
        tall_applied = False
    await asyncio.sleep(wait_seconds)
    data = await cdp.eval(DETAIL_PAGE_EXPRESSION)
    if not isinstance(data, dict) or not data.get("images"):
        await asyncio.sleep(3)
        data = await cdp.eval(DETAIL_PAGE_EXPRESSION)

    collected: set[str] = set()
    if isinstance(data, dict):
        collected.update(data.get("images") or [])

    if tall_applied:
        # 超长视口下等待两轮，直到图片集合不再增长
        for _ in range(6):
            await asyncio.sleep(0.6)
            data = await cdp.eval(DETAIL_PAGE_EXPRESSION)
            if isinstance(data, dict):
                before = len(collected)
                collected.update(data.get("images") or [])
                if len(collected) == before:
                    break
        try:
            await cdp.call("Emulation.clearDeviceMetricsOverride")
        except Exception:
            pass
        tall_applied = False

    # 逐屏滚动兜底：万一超长视口没触发完整加载，按屏滚动补齐——
    # 直接跳到底部会跳过中间的懒加载图片。
    stable_rounds = 0
    for _ in range(40):
        try:
            position = await cdp.eval(_SCROLL_STEP_EXPRESSION)
        except Exception:
            break
        await asyncio.sleep(0.35)
        data = await cdp.eval(DETAIL_PAGE_EXPRESSION)
        if isinstance(data, dict):
            collected.update(data.get("images") or [])
        at_bottom = False
        try:
            parsed = json.loads(position) if isinstance(position, str) else {}
            at_bottom = int(parsed.get("y") or 0) >= int(parsed.get("max") or 0)
        except Exception:
            at_bottom = False
        if at_bottom:
            stable_rounds += 1
            await asyncio.sleep(0.3)
            data = await cdp.eval(DETAIL_PAGE_EXPRESSION)
            if isinstance(data, dict):
                before = len(collected)
                collected.update(data.get("images") or [])
                if len(collected) == before:
                    stable_rounds += 1
                    if stable_rounds >= 2:
                        break
        else:
            stable_rounds = 0

    images = list(dict.fromkeys(collected))
    images = [url for url in images if not _is_thumbnail(url)]
    text = _clean(data.get("text") or "") if isinstance(data, dict) else ""
    return images, text


async def scrape_detail_page(
    tmall: Any, item_id: str, wait_seconds: float = 7.0, cdp: Any | None = None
) -> tuple[list[str], str]:
    """打开普通图文详情页，抓取图片与页面文字（不调用 MTOP 详情接口）。

    传入 cdp 时复用当前采集用的后台标签页，不额外开标签；
    否则新建一个后台标签页，用完即关。
    """
    if cdp is not None:
        return await _read_detail_page(cdp, item_id, wait_seconds)

    tab = await create_tab("about:blank", background=True)
    try:
        async with tmall.CDPClient(tab["webSocketDebuggerUrl"]) as local:
            return await _read_detail_page(local, item_id, wait_seconds)
    finally:
        await close_tab(str(tab.get("id") or ""))


def _session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    return session


def _normalize_url(url: str) -> str:
    value = str(url or "").strip()
    if value.startswith("//"):
        return "https:" + value
    return value


# alicdn 缩略图通常带 _60x60 / _100x100 这类后缀，OCR 出来只有噪声
_THUMB_RE = re.compile(r"_(\d{2,3})x(\d{2,3})(?:q\d{2})?\.(?:jpg|jpeg|png|webp|gif)", re.I)


def _is_thumbnail(url: str) -> bool:
    match = _THUMB_RE.search(str(url or ""))
    if not match:
        return False
    width, height = int(match.group(1)), int(match.group(2))
    return max(width, height) <= 200


def _clean(value: Any) -> str:
    return CONTROL_RE.sub("", str(value or "")).strip()


def _relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def download_images(
    urls: list[str],
    target_dir: Path,
    kind: str,
    limit: int,
    *,
    max_workers: int = 6,
) -> list[dict[str, Any]]:
    """并行下载图片；保持“先到先得”的旧命名规则，但整体耗时大幅缩短。

    多取 `limit * 2` 个候选，失败的自动顺延，成功总数仍然受 `limit` 约束。
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    candidates: list[tuple[int, str]] = []
    seen: set[str] = set()
    for index, raw_url in enumerate(urls):
        url = _normalize_url(raw_url)
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        candidates.append((index, url))
        if len(candidates) >= max(limit * 2, limit):
            break

    local = threading.local()

    def _session_for_thread() -> requests.Session:
        session = getattr(local, "session", None)
        if session is None:
            session = _session()
            local.session = session
        return session

    def _fetch(item: tuple[int, str]) -> tuple[int, str, bytes | None, str]:
        index, url = item
        try:
            response = _session_for_thread().get(url, headers=_HTTP_HEADERS, timeout=20)
            if response.status_code == 200 and len(response.content) >= 3000:
                return index, url, response.content, response.headers.get("Content-Type", "")
        except Exception:
            pass
        return index, url, None, ""

    fetched: list[tuple[int, str, bytes | None, str]] = []
    if candidates:
        workers = max(1, min(max_workers, len(candidates)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            fetched = list(pool.map(_fetch, candidates))
    fetched.sort(key=lambda row: row[0])

    results: list[dict[str, Any]] = []
    for _index, _url, content, content_type in fetched:
        if content is None or len(results) >= limit:
            continue
        suffix = ".png" if "png" in content_type else ".webp" if "webp" in content_type else ".jpg"
        path = target_dir / f"{kind}_{len(results) + 1:02d}{suffix}"
        path.write_bytes(content)
        results.append(
            {
                "url": _url,
                "local_path": _relative(path),
                "kind": kind,
                "ocr_text": "",
                "ocr_confidence": None,
            }
        )
    return results


_OCR_STATE = threading.local()


def _ocr_engine():
    engine = getattr(_OCR_STATE, "engine", None)
    if engine is None:
        from rapidocr_onnxruntime import RapidOCR

        engine = RapidOCR()
        _OCR_STATE.engine = engine
    return engine


def _ocr_single(path: Path) -> dict[str, Any] | None:
    try:
        result, _ = _ocr_engine()(str(path))
    except Exception:
        return None
    lines = []
    for item in result or []:
        text = _clean(item[1])
        try:
            score = round(float(item[2]), 3)
        except (TypeError, ValueError):
            score = 0.0
        if text and len(text) >= 2 and score >= 0.5:
            lines.append({"text": text, "score": score})
    if not lines:
        return None
    return {
        "text": " ".join(line["text"] for line in lines),
        "confidence": round(sum(line["score"] for line in lines) / len(lines), 3),
        "lines": lines,
    }


def _content_digest(path: Path) -> str | None:
    try:
        return hashlib.sha1(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def ocr_images(
    paths: list[Path],
    *,
    max_workers: int = 1,
    cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, Any]], str]:
    """OCR 多张图片：内容哈希去重、可复用磁盘缓存。

    - 同一内容（同一张图被详情页重复引用）只识别一次；
    - ``cache`` 是 ``{sha1: 结果}``，由调用方落盘；二次采集时未变化的图
      直接命中缓存，**空结果也会缓存**（模特照没有文字，避免反复白跑）；
    - ``max_workers`` 默认 1：onnxruntime 内部已经多线程，实测再加线程
      反而互相争抢变慢（15 张图：串行 16.4s，4 线程 24.9s），保留参数仅供实验。
    """
    digest_by_path: dict[str, str | None] = {str(path): _content_digest(Path(path)) for path in paths}
    by_digest: dict[str, list[str]] = {}
    for key, digest in digest_by_path.items():
        if not digest:
            continue
        by_digest.setdefault(digest, []).append(key)

    pending: list[tuple[str, Path]] = []
    output: dict[str, dict[str, Any]] = {}
    for digest, keys in by_digest.items():
        cached = (cache or {}).get(digest)
        if cached is not None:
            if not cached.get("empty"):
                for reused in keys:
                    output[reused] = cached
        else:
            pending.append((digest, Path(keys[0])))

    status = "ok" if (output or pending) else "empty"
    if pending:
        if max_workers > 1:
            with ThreadPoolExecutor(max_workers=min(max_workers, len(pending))) as pool:
                done = list(pool.map(lambda item: (item[0], _ocr_single(item[1])), pending))
        else:
            done = [(digest, _ocr_single(path)) for digest, path in pending]

        for digest, data in done:
            if cache is not None:
                # 空结果也要缓存：模特照没有文字，不缓存的话每次采集都会白跑
                cache[digest] = data if data is not None else {"empty": True}
            if data is not None:
                for reused in by_digest.get(digest, []):
                    output[reused] = data
        if output:
            status = "ok"
        elif status == "ok":
            status = "empty"
    return output, status


def _claim_key(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text or "")


def _parse_ssr(payload: dict[str, Any], item_id: str) -> dict[str, Any]:
    skus = payload.get("skus") or []
    prices: list[float] = []
    for sku in skus:
        try:
            prices.append(float(sku.get("price_text")))
        except (TypeError, ValueError):
            continue

    price_min = min(prices) if prices else None
    price_max = max(prices) if prices else None
    if price_min is None:
        price_text = ""
    elif price_min == price_max:
        price_text = f"{price_min:g}"
    else:
        price_text = f"{price_min:g}-{price_max:g}"

    attributes: dict[str, list[str]] = {}
    for prop in payload.get("props") or []:
        name = _clean(prop.get("name"))
        if name:
            attributes[name] = [_clean(value) for value in prop.get("values") or []]

    return {
        "id": str(item_id),
        "title": _clean(payload.get("title")),
        "subtitle": "",
        "price_text": price_text,
        "price_min": price_min,
        "price_max": price_max,
        "price_source": "page_ssr.skuCore.sku2info",
        "sku_count": len(skus),
        "skus": skus,
        "attributes": attributes,
        "images": [],
        "source": "page_ssr",
    }


def finish_detail_images(
    item_id: str,
    detail_urls: list[str],
    detail_text: str = "",
    *,
    max_detail_images: int = 30,
    max_ocr_images: int = 40,
    cancel_check: Callable[[], bool] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """下载并识别详情图（同步，可放在后台线程里跑）。

    与采集评价互不依赖：调用方可以在评价采集期间把这一步丢进后台线程，
    评价结束时 OCR 已经做完，商品信息阶段只剩秒级的 SSR 与主张组装。
    """
    def note(message: str) -> None:
        if log is not None:
            log.append(message)

    def cancelled() -> bool:
        return bool(cancel_check and cancel_check())

    product_dir = product_cache_root() / str(item_id) / "images"
    detail_files = download_images(detail_urls, product_dir, "detail", max_detail_images)
    note(f"详情图下载 {len(detail_files)} 张")
    if cancelled():
        return {"ok": False, "error": "cancelled"}
    if not detail_files:
        # 详情页没抓到图（风控/空页）时必须走主流程回退，不能交出空结果
        return {"ok": False, "error": "desc_no_images"}

    image_paths = [ROOT / entry["local_path"] for entry in detail_files[:max_ocr_images]]
    cache_path = product_dir / "ocr_cache.json"
    cache: dict[str, dict[str, Any]] = {}
    if cache_path.exists():
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cache = loaded
        except Exception:
            cache = {}
    note(f"OCR {len(image_paths)} 张（缓存 {len(cache)}）")
    ocr_result, ocr_status = ocr_images(image_paths, cache=cache)
    if cache:
        try:
            cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    if cancelled():
        return {"ok": False, "error": "cancelled"}

    for entry in detail_files:
        data = ocr_result.get(str(ROOT / entry["local_path"]))
        if data:
            entry["ocr_text"] = data["text"]
            entry["ocr_confidence"] = data["confidence"]

    return {
        "ok": True,
        "detail_files": detail_files,
        "ocr_result": ocr_result,
        "ocr_status": ocr_status,
        "detail_text": _clean(detail_text),
        "detail_count": len(detail_files),
        "ocr_count": len(ocr_result),
    }


async def collect_product_info(
    cdp: Any,
    tmall: Any,
    item_id: str,
    *,
    max_detail_images: int = 30,
    max_ocr_images: int = 40,
    progress: Callable[[str], None] | None = None,
    prepared: dict[str, Any] | None = None,
) -> dict[str, Any]:
    def emit(message: str) -> None:
        if progress:
            progress(message)

    errors: list[str] = []
    emit("正在读取页面商品数据…")
    payload = await cdp.eval(SSR_EXPRESSION)
    ssr_ok = isinstance(payload, dict) and bool(payload.get("ok"))
    if not ssr_ok:
        errors.append(f"页面 SSR：{payload.get('error') if isinstance(payload, dict) else 'unexpected'}")

    product = _parse_ssr(payload if isinstance(payload, dict) else {}, item_id)
    if not product["title"]:
        try:
            title = await cdp.eval("document.title")
            if isinstance(title, str):
                product["title"] = title.replace("-tmall.com天猫", "").replace("-淘宝网", "").strip()
        except Exception:
            pass

    main_urls = [url for url in (payload.get("images") if isinstance(payload, dict) else []) or [] if url]
    if not main_urls:
        try:
            main_urls = await cdp.eval(FALLBACK_IMAGES_EXPRESSION) or []
        except Exception as exc:  # noqa: BLE001
            errors.append(f"页面图片：{type(exc).__name__}: {exc}")

    product_dir = product_cache_root() / str(item_id) / "images"
    emit("正在下载主图…")
    main_files = await asyncio.to_thread(
        download_images, main_urls, product_dir, "main", 5
    )

    prepared = prepared or {}
    prepared_ok = bool(prepared.get("ok")) and isinstance(prepared.get("detail_files"), list)

    detail_urls: list[str] = []
    detail_text = ""
    if prepared_ok:
        # 后台线程已经在评价采集期间完成“抓详情页 + 下载 + OCR”
        detail_files = prepared["detail_files"]
        detail_text = _clean(prepared.get("detail_text") or "")
        ocr_result = prepared.get("ocr_result") or {}
        ocr_status = prepared.get("ocr_status") or "ok"
        emit(f"详情图已在后台识别完成（{len(detail_files)} 张 / OCR {len(ocr_result)} 张）")
    else:
        if tmall is not None:
            emit("正在读取详情图…")
            try:
                detail_urls, detail_text = await scrape_detail_page(tmall, item_id, cdp=cdp)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"详情图：{type(exc).__name__}: {exc}")

        emit("正在下载详情图…")
        detail_files = await asyncio.to_thread(
            download_images, detail_urls, product_dir, "detail", max_detail_images
        )

    product["images"] = main_files + detail_files
    if detail_text and not product.get("subtitle"):
        product["subtitle"] = detail_text

    # OCR 顺序：详情图在前（宣传文案主要在详情图里），主图只是产品照片；
    # 之前“主图+前几张详情”的顺序会把名额耗在主图上，文案图反而排不进去。
    cache_path = product_dir / "ocr_cache.json"
    if prepared_ok:
        # 后台线程已经 OCR 并写入缓存，这里直接复用结果
        ocr_entries: list[dict[str, Any]] = []
        cache: dict[str, dict[str, Any]] = {}
        emit("图片文字识别已由后台完成，直接复用")
    else:
        ocr_entries = detail_files + main_files
        cache: dict[str, dict[str, Any]] = {}
        if cache_path.exists():
            try:
                loaded = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    cache = loaded
            except Exception:
                cache = {}
        image_paths = [ROOT / entry["local_path"] for entry in ocr_entries[:max_ocr_images]]
        emit(f"正在识别图片文字（{len(image_paths)} 张，缓存 {len(cache)}）…")
        ocr_result, ocr_status = await asyncio.to_thread(ocr_images, image_paths, cache=cache)
        if cache:
            try:
                cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass

    for entry in product["images"]:
        data = ocr_result.get(str(ROOT / entry["local_path"]))
        if data:
            entry["ocr_text"] = data["text"]
            entry["ocr_confidence"] = data["confidence"]

    claims: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_claim(text: str, source: str, confidence: float, image: str = "") -> None:
        value = _clean(text)
        key = _claim_key(value)
        if not key or key in seen:
            return
        seen.add(key)
        claims.append(
            {
                "text": value,
                "source": source,
                "confidence": round(float(confidence), 3),
                "image": image,
            }
        )

    add_claim(product.get("title", ""), "title", 1.0)
    add_claim(product.get("subtitle", ""), "detail_text", 1.0)

    text_counts: dict[str, int] = {}
    for entry in product["images"]:
        data = ocr_result.get(str(ROOT / entry["local_path"]))
        if not data:
            continue
        for line in data["lines"]:
            key = _claim_key(line["text"])
            text_counts[key] = text_counts.get(key, 0) + 1

    for entry in product["images"]:
        data = ocr_result.get(str(ROOT / entry["local_path"]))
        if not data:
            continue
        for line in data["lines"]:
            text = line["text"]
            key = _claim_key(text)
            if not re.search(r"[\u4e00-\u9fff]", text) and text_counts.get(key, 0) < 2:
                continue
            add_claim(text, "image_ocr", line["score"], entry["local_path"])

    claim_keys = [_claim_key(claim["text"]) for claim in claims]
    filtered_claims: list[dict[str, Any]] = []
    for index, claim in enumerate(claims):
        key = claim_keys[index]
        if any(
            index != other
            and key in claim_keys[other]
            and len(claim_keys[other]) > len(key)
            for other in range(len(claim_keys))
        ):
            continue
        filtered_claims.append(claim)
    claims = filtered_claims

    return {
        "product": product,
        "promotional_claims": claims,
        "collection_info": {
            "ssr_ok": ssr_ok,
            "main_image_count": len(main_files),
            "detail_image_count": len(detail_files),
            "ocr_image_count": len(ocr_result),
            "ocr_status": ocr_status,
            "source": "page_ssr",
            "errors": errors,
        },
    }
