---
name: taobao-tmall-review-exporter
description: Export Taobao and Tmall product reviews through the MTOP rate API using an already-running, logged-in Chrome DevTools session. Use for single-product review downloads, default or newest-first sorting, Excel or JSONL delivery, reviews with real buyer-uploaded images, Feishu/Lark Base batch collection, partial retries, result merging, or crawl monitoring. Never bypass captcha or substitute product images for review images.
---

# Taobao/Tmall Review Exporter

通过淘宝/天猫 MTOP 评价接口采集真实商品评价，支持单商品、飞书多维表格批量、断点补抓和进度监控。

当前版本：v2.2.0（2026-07-30）。本版本增加“时间排序”，并统一技能名为 `taobao-tmall-review-exporter`。

## 执行原则

1. 先确认用户的四个要求：商品链接或商品 ID、数量、排序、交付格式。
2. 排序只有两种：
   - **默认排序**：不传 `--time-sort`，对应接口 `orderType=""`。
   - **最新排序**：传 `--time-sort`，对应页面“时间排序”和接口 `orderType="feedbackdate"`。
3. 格式支持 Excel、JSONL 或两者。脚本会同时生成两种文件；最终只向用户交付其指定的格式。
4. 用户要求“带图片”时，只使用每条评价原始 JSON 中实际返回的买家评价图片。忽略空 URL，不把头像、商品主图、SKU 图或重复 URL 当作评价图片。
5. 用户未说明排序或格式时，先问清楚；已经说明则直接执行，不重复确认。
6. 只连接已经运行且已经登录的 Chrome 调试会话。不要自行启动新浏览器，除非用户明确要求。
7. 单线程、受控翻页。遇到验证码只允许用户在原会话中手动完成，不绕过风控。

## 路由任务

- 单个淘宝/天猫商品链接或 ID：`scripts/tmall_rate_auto.py`
- 飞书/Lark Base 或 Wiki Base 中的一批商品：`scripts/tmall_reviews_from_feishu_base.py`
- 对旧工作簿中的零条或不完整商品补抓：`scripts/rerun_missing_tmall_reviews.py`
- 查看补抓进度并合并新旧 Excel：`scripts/tmall_review_dashboard.py`

不要使用小旺神、坐标点击、SKU 截图流程或验证码绕过技巧。

## 运行前检查

定位本技能目录并安装依赖：

```powershell
$skill = "$env:USERPROFILE\.codex\skills\taobao-tmall-review-exporter"
python -m pip install -r "$skill\scripts\requirements.txt"
```

确认 Chrome 调试端口可用且该浏览器已登录淘宝/天猫：

```powershell
Invoke-RestMethod "http://127.0.0.1:9222/json/version"
```

若端口不可达，说明当前 Chrome 没有启用远程调试。此时向用户报告，不要擅自打开另一个浏览器。

默认输出位于淘宝项目自己的 `runtime/reviews/`。从当前项目目录运行时可这样指定：

```powershell
$out = Join-Path (Get-Location) "runtime\reviews"
```

不要让两个抓取进程同时连接同一个 Chrome 会话。

## 单商品采集

默认排序：

```powershell
python "$skill\scripts\tmall_rate_auto.py" "<商品链接或ID>" --max-reviews <数量> --page-size 50 --min-delay 12 --max-delay 25 --manual-captcha --manual-captcha-wait 180 --out-dir "$out"
```

最新排序只增加一个参数：

```powershell
python "$skill\scripts\tmall_rate_auto.py" "<商品链接或ID>" --max-reviews <数量> --page-size 50 --time-sort --min-delay 12 --max-delay 25 --manual-captcha --manual-captcha-wait 180 --out-dir "$out"
```

脚本会：

- 从链接提取商品 ID，并在已有 Chrome 会话中打开商品页；
- 从真实浏览器 Cookie 读取 `_m_h5_tk`，优先使用 `.tmall.com` token；
- 在页面上下文中签名并请求 MTOP，而不是直接复用他人的 Cookie；
- 每成功获取一页就保存 partial Excel/JSONL；
- 按评价 ID 去重，在达到数量上限或没有下一页时停止；
- 生成状态 JSON，记录状态、停止原因、数量和下一页页码。

断点续抓：

```powershell
python "$skill\scripts\tmall_rate_auto.py" "<商品链接或ID>" --resume-excel "<已有单品Excel>" --max-reviews <数量> --out-dir "$out"
```

续抓时必须沿用上一轮的排序方式；最新排序仍需传 `--time-sort`。

## 图片处理

单品脚本的 JSONL 每行保存一条接口原始评价对象。用户要求图片时：

1. 从该评价对象自身的图片字段中提取 URL；兼容字段名包含 `pic`、`photo`、`image` 的字符串、列表或嵌套对象。
2. 补全以 `//` 开头的 URL 为 `https://`，对 URL 去重，并过滤空值。
3. 只保留实际存在图片的评价；“最新六条带图片”表示在最新排序结果中向后扫描，直到找到六条确有评价图片的记录，而不是只检查前六条。
4. 下载时按 `<out-dir>\images\<评价ID>\01.<扩展名>` 保存，并在交付表中保留“评价图片URL”和“本地图片路径”。
5. 下载失败时保留原 URL，并明确标记失败；禁止用商品图、头像或其他评价的图片补位。

若用户只说“评价带上图片”，默认同时交付评价数据和图片文件夹；若用户只要 URL，则无需下载图片。

## 飞书 Base 批量采集

先确保 `lark-cli` 已登录，然后运行：

```powershell
python "$skill\scripts\tmall_reviews_from_feishu_base.py" "<飞书或Lark Base链接>" --max-reviews-per-product 1500 --page-size 50 --min-delay 12 --max-delay 25 --product-min-delay 60 --product-max-delay 120 --manual-captcha --manual-captcha-wait 180 --product-attempts 2 --product-retry-delay 900 --blocked-streak-limit 2 --global-cooldown 1800 --out-dir "$out"
```

先做不抓评价的结构检查：

```powershell
python "$skill\scripts\tmall_reviews_from_feishu_base.py" "<飞书或Lark Base链接>" --dry-run
```

批量脚本默认读取字段 `商品链接`，可用 `--link-field "<字段名>"` 修改。它会解析 Base/Wiki 链接、读取表名和记录、从单元格中提取商品链接、按商品 ID 去重，并顺序调用单品采集器。

最终工作簿包含 `reviews` 和 `products` 两个工作表。文件名默认取数据表名：末尾的 `四维分类` 或 `分类` 替换为 `评论`，否则追加 `评论`；可用 `--output-name "<名称>"` 覆盖。

注意：当前批量入口没有独立的 `--time-sort` 参数。用户要求整批按最新排序时，不要假称已支持；应先扩展批量脚本并验证，或逐个调用单品脚本。

## 补抓与监控

只补抓旧结果中 `抓取条数 == 0` 的商品：

```powershell
python "$skill\scripts\rerun_missing_tmall_reviews.py" "<旧批量Excel>" --only-zero --max-reviews-per-product 1500 --manual-captcha --out-dir "$out"
```

补抓所有少于指定条数的商品：

```powershell
python "$skill\scripts\rerun_missing_tmall_reviews.py" "<旧批量Excel>" --only-under 1500 --max-reviews-per-product 1500 --manual-captcha --out-dir "$out"
```

已有补抓目录时，用 `--resume-run-dir "<目录>"` 继续，避免重新抓已完成页面。保持单线程；频繁出现验证时应增加延迟或等待全局冷却。

启动本地只读监控页：

```powershell
python "$skill\scripts\tmall_review_dashboard.py" --out-dir "$out" --host 127.0.0.1 --port 8765
```

监控页地址为 `http://127.0.0.1:8765/`，实时合并文件位于 `<out-dir>\dashboard\combined_tmall_reviews_live.xlsx`。

## 接口约束

接口：

```text
https://h5api.m.tmall.com/h5/mtop.taobao.rate.detaillist.get/6.0/
api=mtop.taobao.rate.detaillist.get
v=6.0
appKey=12574478
```

完整评价请求的核心 `data`：

```json
{
  "showTrueCount": false,
  "auctionNumId": "ITEM_ID",
  "pageNo": 1,
  "pageSize": 50,
  "rateType": "",
  "searchImpr": "-8",
  "orderType": "",
  "expression": "",
  "rateSrc": "pc_rate_list"
}
```

最新排序仅把 `orderType` 改为 `feedbackdate`。默认不要增加 `skuVids` 或 `foldFlag`，这两个参数可能把完整评价范围缩窄成某个 SKU 或折叠集合。

## 验收与交付

完成前检查：

- 条数不超过用户要求，且评价 ID 无重复；
- 最新排序时，输出日期应整体从新到旧；若异常，检查是否实际传入 `--time-sort`；
- 图片数量按非空且去重后的真实评价图片计算；
- Excel 可正常打开，JSONL 每行都是合法 JSON；
- 状态不是 `blocked_validate` 或 `api_error`；若是 partial，必须明确说明已抓条数和停止原因；
- 最终回复提供用户指定格式及图片目录的可点击绝对路径。

脚本生成的单商品文件：

```text
tmall_reviews_<item_id>_<count>条_<timestamp>.xlsx
tmall_reviews_<item_id>_<count>条_<timestamp>.jsonl
tmall_reviews_<item_id>_partial_<timestamp>.xlsx
tmall_reviews_<item_id>_partial_<timestamp>.jsonl
tmall_reviews_<item_id>_status_<timestamp>.json
```

## 故障处理

- `FAIL_SYS_ILLEGAL_ACCESS`：通常是 token 域或签名状态失效；重新读取 `.tmall.com` token 后重试。
- `FAIL_SYS_USER_VALIDATE` / `RGV587`：在当前 Chrome 中打开返回的验证地址，等待用户手动完成。
- 总数异常偏低：检查并移除 `skuVids`、`foldFlag`。
- `rateList` 为空：正常停止，除非用户要求尝试其他参数。
- Base 无法解析 table/view：检查 URL 的 `table=`、`view=`，必要时请用户提供精确视图链接。
- 找不到 Base 字段：使用 `--link-field` 指定实际字段名。
