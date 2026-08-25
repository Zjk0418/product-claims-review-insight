# 京东评价数和好评率采集

这个项目可以读取京东商品的：

- 评价数，例如 `10万+`
- 好评率文案，例如 `超99%买家赞不绝口`

最简单的用法不需要飞书，只要给商品 SPU 或京东商品链接即可。

## 第一次使用

先进入这个目录：

```powershell
cd <你的京东项目目录>
```

安装依赖：

```powershell
python -m pip install -r .\jd-review-summary-collector\scripts\requirements.txt
```

## 直接抓商品，不用飞书

抓一个或多个 SPU：

```powershell
python .\jd-review-summary-collector\scripts\jd_review_summary.py --spu 100047005216 100012043978 --run-name my-run
```

也可以直接传京东商品链接：

```powershell
python .\jd-review-summary-collector\scripts\jd_review_summary.py --spu "https://item.jd.com/100047005216.html" --run-name my-run
```

商品很多时，可以准备一个 UTF-8 文本文件，每行放一个 SPU 或商品链接：

```powershell
python .\jd-review-summary-collector\scripts\jd_review_summary.py --spu-file .\spus.txt --run-name my-run
```

结果会放在：

```text
runtime/direct/my-run/
├─ results.json       最终结果
├─ checkpoint.jsonl   已经抓成功的商品
└─ status.json        本次运行状态
```

同一个任务继续使用相同的 `--run-name`，程序会跳过已经成功的商品，只补没有抓到的部分。

这个用法不会读取 `.env`，也不会连接飞书。

## 浏览器要怎么准备？

程序会连接本机 `9222` 端口上的 Chrome。如果没有找到，它会尝试启动一个专用 Chrome，并把登录资料保存在：

```text
runtime/browser-profile/
```

第一次打开时可能需要你手动登录京东。遇到验证码或风险验证，也要在这个 Chrome 中手动完成，程序不会绕过验证。

如果 Chrome 安装在特殊位置，可以设置环境变量 `JD_CHROME_PATH`。

## 可选：从飞书批量读取并回写

只有使用飞书时才需要 `.env`。

新电脑先复制示例文件：

```powershell
Copy-Item .\.env.example .\.env
```

然后在 `.env` 中填写自己的飞书应用和多维表格信息。

先检查配置，不采集也不写回：

```powershell
python .\jd-review-summary-collector\scripts\jd_review_summary.py --date 2026-08-16 --preflight
```

正式按日期采集并写回飞书：

```powershell
python .\jd-review-summary-collector\scripts\jd_review_summary.py --date 2026-08-16
```

飞书模式的断点放在：

```text
runtime/review-summary/<日期>/
```

## 哪些文件不要上传？

下面这些文件已经写进 `.gitignore`：

- `.env`：里面是飞书密钥和表格信息。
- `runtime/`：里面可能有浏览器登录资料和采集结果。
- `.pytest_cache/`、`__pycache__/`：测试和 Python 自动生成的缓存。

可以上传 `.env.example`，它只有字段名和示例值，没有真实密钥。

影刀版本的使用方法见 `yingdao/影刀部署说明.md`。
