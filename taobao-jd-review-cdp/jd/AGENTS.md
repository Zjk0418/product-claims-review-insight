# AI 使用指引

这个目录是可独立拉取的京东项目。默认先使用核心 CDP 直采，不要假设用户拥有飞书。

## 核心入口

- 源码：`jd-review-summary-collector/scripts/jd_review_summary.py`
- Python：`collect_spus(spus, run_name=None)`
- CLI：`--spu ...` 或 `--spu-file ...`
- 输出：`runtime/direct/<run-name>/results.json` 与 `checkpoint.jsonl`

核心流程是连接 `127.0.0.1:9222` 的真实 Chrome，在京东页面上下文先请求批量摘要接口，缺失项再调用页面自带 `window.PSign` 生成签名并请求 `getLegoWareDetailComment`。遇到验证只允许人工处理。

## 可选飞书适配

只有用户明确提供飞书日期/Base工作流时才使用 `run(dt)` 或 CLI `--date`。此时才要求根目录 `.env`；变量清单见 `.env.example`。不要把飞书配置作为使用CDP核心的前置条件。

不要读取、输出或提交 `.env`、`runtime/browser-profile`、Cookie、`h5st`、token 或设备标识。
