---
name: jd-review-summary-collector
description: Collect the two JD review-summary fields—评价数 and 好评率—directly from SPUs or product URLs through a real Chrome CDP session, with optional Feishu Base date input and writeback. Use for standalone JD collection without Feishu, batch jobs, checkpoint recovery, or safe Feishu updates without bypassing verification.
---

# JD Review Summary Collector

通过已运行的 Chrome 页面上下文采集京东商品页的 `评价数` 和 `好评率`。核心直采接受 SPU/商品链接且不依赖飞书；需要时也可按日期读取飞书 `京东自营` 表并回写原记录。

## 固定口径

- 直采公共入口是 `collect_spus(spus, run_name=None)`；飞书入口继续保持 `run(dt)`。
- 飞书模式的来源字段只使用 `SPU` 和 `日期`；直采模式接受 SPU 或标准京东商品链接。
- 飞书模式只回写 `评价数` 和 `好评率`；直采模式只写本地结果文件。
- `评价数` 保留京东展示文本，例如 `10万+`；不要伪造精确整数。
- `好评率` 写京东页面完整展示文案，例如 `超99%买家赞不绝口`；不要只写 `99%`。
- 失败或空响应不写 `0`，也不覆盖已有有效值。

## 运行前检查

1. 直采模式不需要飞书配置。仅使用飞书模式时，才从京东项目根目录的 `.env` 或同名系统环境变量读取自建应用凭证、Base Token、Table ID、View ID；变量名见 `jd/.env.example`。
2. 飞书模式需确保该应用拥有目标 Base 的读取和编辑权限。
3. 脚本会检查 Chrome 调试端口 `9222`；未开启时自动使用固定 profile 启动专用 Chrome：

```powershell
Invoke-RestMethod "http://127.0.0.1:9222/json/version"
```

4. 首次运行需在该专用 Chrome 中登录京东；之后复用固定 profile。脚本找不到京东页时会在同一会话中新开目标页。
5. 安装依赖：

```powershell
python -m pip install -r "<skill-dir>\scripts\requirements.txt"
```

遇到登录、验证码或风险页时暂停，让用户在专用浏览器中人工处理；禁止绕过。

## 执行

不依赖飞书的直采：

```powershell
python "<skill-dir>\scripts\jd_review_summary.py" --spu 100047005216 100012043978 --run-name direct-run
```

Python调用：

```python
from jd_review_summary import collect_spus

result = collect_spus(["100047005216", "https://item.jd.com/100012043978.html"])
```

下面是可选的飞书模式。

Python 调用：

```python
from jd_review_summary import run

result = run("2026-08-16")
```

命令行：

```powershell
python "<skill-dir>\scripts\jd_review_summary.py" --date 2026-08-16
```

只读预检（不采集、不写回）：

```powershell
python "<skill-dir>\scripts\jd_review_summary.py" --date 2026-08-16 --preflight
```

脚本先按每批 20 个 SPU 请求批量评价摘要接口，并把百分比按京东模板生成完整好评文案；批量失败或缺失项再使用商品页自身的 `PSign` 调用 `getLegoWareDetailComment` 补偿。不得复制或持久化 Cookie、`h5st`、token 或设备标识。

## 批量与恢复

- 飞书 OpenAPI 读取每页最多 500 条并串行翻页。
- 先按 SPU 去重采集，再把同一 SPU 的结果映射回全部目标记录。
- 京东批量请求受控串行；单条补偿请求也受控串行。
- 若上一轮因人工验证暂停，续跑时自动跳过再次触发风控的旧批量接口，改用当前签名接口补剩余 SPU。
- 结果实时追加到京东项目自己的 `runtime/review-summary/<日期>/checkpoint.jsonl`。
- 直采结果写入 `runtime/direct/<run-name>/`；同名重跑复用 checkpoint，不需要飞书凭证。
- 重跑同一日期时复用成功 checkpoint，只补失败或缺失 SPU。
- 飞书按 record_id 构造更新记录，每批最多 500 条串行写入，批次间延迟。

## 验收

- 直采范围必须严格等于用户提供的 SPU；飞书模式的读取范围必须严格等于指定日期。
- 每个成功 SPU 同时具备非空 `评价数` 和 `好评率`。
- 不把接口报错、`系统繁忙`、验证码响应当成业务值。
- 飞书模式本轮成功记录全部更新；失败记录保留旧值并进入失败清单。直采模式不得访问飞书。
- `status.json` 中的目标数、成功数、失败数与 checkpoint 一致。
- 返回绝对输出目录和失败 SPU，便于继续补跑。

接口证据、字段映射及回放约束见 [references/api-contract.md](references/api-contract.md)。
