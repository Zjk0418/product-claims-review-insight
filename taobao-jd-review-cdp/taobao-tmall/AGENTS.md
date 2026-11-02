# AI 使用指引

这个目录是可独立拉取的淘宝/天猫项目。默认使用单商品 CDP 采集，不要假设用户拥有飞书。

## 核心入口

- 源码：`taobao-tmall-review-exporter/scripts/tmall_rate_auto.py`
- Python：`crawl_from_url(...)`
- CLI：传商品链接/ID及 `--max-reviews`
- 输出：`runtime/reviews/` 下的 Excel、JSONL 和状态文件

核心流程是连接 `127.0.0.1:9222` 的已登录真实 Chrome，读取 `_m_h5_tk`，计算 MTOP MD5 签名，再在页面上下文发送 JSONP 评价请求。遇到验证只允许人工处理。

## 可选飞书适配

只有用户明确提供飞书 Base 批量任务时才使用 `tmall_reviews_from_feishu_base.py` 和 `lark-cli`。飞书不是单商品采集的依赖。

不要读取、输出或提交 `.env`、runtime、Cookie、token 或买家隐私数据。
