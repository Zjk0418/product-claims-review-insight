# 商品宣传与体验落差分析

一个面向淘宝／天猫商品的本地分析工具：采集商品宣传与低星评价，识别具体问题，对照可核验的宣传主张，并在交互报告中展示结论及原文依据。

> 本项目是 Windows 单机桌面工具的源码，不是已部署的多人在线服务。商品页、评价接口和登录验证可能随平台变化而失效。

## 功能

- **信息采集**：支持商品 ID、完整链接及常见分享短链；采集商品信息、图片文字和低星评价，支持暂停与断点续采。
- **结构化分析**：通过用户自配的大模型抽取具体问题和可核验的宣传主张，使用 BGE 中文向量模型归并相近表达，并核对引用证据。
- **可视化报告**：对照宣传与评价，同时展示未对应宣传的商品体验问题，可展开查看评论依据。
- **预览与导出**：在应用内预览 HTML、PDF、XLSX 和 JSON，按需导出。

评分属于证据关注指标；报告中的条件复现估计针对同条件的可分析低星反馈，**不是购买后出现问题的概率**。证据不足不等于商品没有风险。

## 环境与启动

已验证的开发环境为 Windows 10/11、Python 3.10 和本机 Edge／Chrome。需要网络访问电商页面与用户选择的大模型服务。BGE ONNX 模型随本仓库提供，克隆体积约增加 90 MiB；无需 PyTorch 或 GPU。

```powershell
git clone https://github.com/Zjk0418/product-claims-review-insight.git
cd product-claims-review-insight
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python desktop_launcher.py
```

也可在安装依赖后双击 `启动分析应用.bat`。如果系统限制 PowerShell 激活脚本，可使用 `.venv\Scripts\python.exe desktop_launcher.py` 直接运行。

### 配置大模型

首次进入应用后，打开侧栏中的 **API 设置**，自行填写兼容 OpenAI Chat Completions 的服务地址、模型名称和 API Key。密钥默认只在当前会话中使用；在 Windows 上主动选择记住时，通过当前 Windows 用户的 DPAPI 保存。

**桌面模式不读取项目 `.env`，请以应用内设置为准。** `.env.example` 仅供兼容的传统命令行流程参考，仓库不包含任何可用密钥。模型调用会向所选服务商发送分析所需的评论与宣传文本，请自行了解其数据处理规则和费用。

### 采集与导出

开始采集时，程序按需启动独立浏览器实例；登录和验证由使用者在该实例中完成，不复制日常浏览器的 Cookie。平台可能要求重新登录、验证或限制请求，无法保证所有商品均可读取。

工作区与预览数据使用本机临时目录，关闭应用时清理；程序另有本机商品历史缓存，可通过应用管理。报告文件仅在使用者选择导出后保存。历史缓存和 API 配置不会随源码仓库或便携包一起分发。

### 构建 Windows 便携包

在已安装完整依赖的虚拟环境中运行：

```powershell
python tools/build_desktop.py
```

或双击 `打包Windows应用.bat`。打包过程使用 `ReviewInsight.spec` 并检查是否意外包含本地密钥和运行数据。

## 项目结构

```text
app/                         应用服务、界面、采集、分析与报告
models/bge-small-zh-v1.5/   BGE ONNX 模型和分词器
taobao-jd-review-cdp/        采集上游项目及本项目的适配修改
tools/build_desktop.py       Windows 应用打包与检查
desktop_launcher.py          桌面入口
requirements.txt             Python 依赖
```

本仓库不含历史评论、用户登录态、模型 API Key、私人标注或展示材料。依赖这些私人数据的专项评测未一并发布；不应将缺失专项评测视为通过。

## 测试

```powershell
cd app
..\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s analysis/tests -t .
..\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests
```

测试主要验证本地逻辑和模拟数据；实际平台采集、登录验证和在线模型调用仍需在使用者自己的环境中验证。

## 来源与许可

- 本仓库保留了 [flyyang12-rgb/taobao-jd-review-cdp](https://github.com/flyyang12-rgb/taobao-jd-review-cdp) 的上游历史；项目在此基础上扩展了商品信息提取、分析和可视化报告，并对采集脚本作了适配。上游未在仓库根目录声明通用开源许可证，使用其代码时请核查上游授权。
- BGE 原模型来自 [BAAI/bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5)，模型卡标注 MIT；ONNX 转换版来自 [Xenova/bge-small-zh-v1.5](https://huggingface.co/Xenova/bge-small-zh-v1.5)。模型与相关许可说明见 [`models/README.md`](models/README.md)。
- PDF.js 的许可文件位于 `app/static/pdfjs/`。本仓库其余代码尚未单独声明开源许可证；请不要将第三方许可自动视为适用于整个仓库。
