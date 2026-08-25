# 淘宝、天猫评价采集

这个项目可以抓淘宝和天猫商品的评价明细，包括：

- 评价内容
- 评价时间
- 买家昵称
- SKU信息
- 商家回复
- 接口返回的原始评价数据

最简单的用法不需要飞书，只要给商品链接或商品ID即可。

## 第一次使用

先进入这个目录：

```powershell
cd <你的淘宝项目目录>
```

安装依赖：

```powershell
python -m pip install -r .\taobao-tmall-review-exporter\scripts\requirements.txt
```

## 直接抓一个商品，不用飞书

```powershell
python .\taobao-tmall-review-exporter\scripts\tmall_rate_auto.py "<商品链接或商品ID>" --max-reviews 500
```

程序会把结果放在：

```text
runtime/reviews/
```

主要会生成：

- Excel：方便直接查看和整理。
- JSONL：每行一条原始评价，方便程序或AI继续分析。
- status.json：记录抓了多少条、为什么停止、下一页是多少。
- partial文件：运行中途保存的结果，防止中断后全部重来。

## 默认排序和最新排序

默认排序不需要额外参数：

```powershell
python .\taobao-tmall-review-exporter\scripts\tmall_rate_auto.py "<商品链接或ID>" --max-reviews 500
```

如果要按时间从新到旧，加上 `--time-sort`：

```powershell
python .\taobao-tmall-review-exporter\scripts\tmall_rate_auto.py "<商品链接或ID>" --max-reviews 500 --time-sort
```

## 浏览器要怎么准备？

先启动一个开启了 `9222` 调试端口的 Chrome，并在里面登录淘宝或天猫、打开任意淘宝/天猫页面。

程序会连接这个真实 Chrome，使用浏览器当前的登录状态发送评价请求。它不会把登录信息写死在源码里。

遇到验证码时需要你在这个 Chrome 中手动处理，程序不会绕过验证。

## 可选：从飞书批量读取商品

只有要从飞书多维表格读取一批商品时，才需要 `lark-cli`：

```powershell
python .\taobao-tmall-review-exporter\scripts\tmall_reviews_from_feishu_base.py "<飞书多维表格链接>"
```

单商品采集不需要飞书，也不需要 `lark-cli`。

## 补抓和查看进度

- `rerun_missing_tmall_reviews.py`：补抓零条或数量不足的商品。
- `tmall_review_dashboard.py`：在浏览器里查看进度并合并结果。

完整参数和特殊情况处理见 `taobao-tmall-review-exporter/SKILL.md`。

## 哪些文件不要上传？

下面这些文件已经写进 `.gitignore`：

- `.env`：以后如果增加本地密钥，可以放这里。
- `runtime/`：里面是采集结果和运行状态。
- `.pytest_cache/`、`__pycache__/`：测试和 Python 自动生成的缓存。

当前淘宝/天猫核心程序没有写死私密应用密钥。
