# JournalFlow

[English](README.en.md) · [视觉模型部署](docs/vision.md) · [定时运行](docs/automation.md)

把期刊查新、可用全文获取、PDF 视觉识别和页级检索连接起来的命令行工具。

**0.3：本地工作台、既有区域库基线、审核增量与每周更新。** 当前公开交付为 GitHub 源码及源码 ZIP，不含已打包的免安装运行时。需要 Python 3.11 或 3.12，首次安装依赖需要网络；区域检索模型与视觉模型服务另行配置。下载或解压源码不会启动程序。

如需使用可选工作台，使用者可自行运行 Windows 的 `scripts/打开JournalFlow.cmd` 或 Mac 的 `scripts/打开JournalFlow.command`。工作台在本机浏览器显示状态和待办，点击“接入 Codex”只生成当前项目配置；在 Codex 打开并信任该项目、配置好自己的论文数据后即可检索。

周周期默认周一10:00（Asia/Shanghai），**不会因打开程序而启用**。新配置默认暂停下载与视觉转换。必须先完成基线、至少10篇真实下载/视觉转换和区域索引验收，才能安装每周任务；历史语料回放不能代替真实试运行。详见[每周更新与工作台](docs/weekly-updates.md)和[增量接入](docs/regional-updates.md)。

```text
Crossref 查新 → DOI 去重 → OA PDF / 授权 PDF 导入
                           ↓
                 300 DPI 视觉识别
                           ↓
                 疑难页 450 DPI 精校
                           ↓
                 Markdown + 页级检索
```

四刊示例包含 INFORMS Journal on Computing、MIS Quarterly、Information Systems Research 和 Management Science。期刊可在 TOML 中替换；项目不绑定某所学校、某个云平台或某套个人论文库。

## 已实现

- 按 ISSN 发现论文；首次查询采用发表日期并集，后续采用有重叠窗口的索引更新时间。
- DOI 去重、成功检查点、可持久恢复的任务状态和 HTTP 限流退避。
- 经 Unpaywall 寻找 OA PDF；导入用户已获授权的本地 PDF。
- PDF 真实性、可读性、页数和 SHA256 检查；不同内容版本分别保存。
- 完整 PaddleOCR-VL 1.6 版面与视觉识别流水线，支持独立 vLLM 服务。
- 默认 300 DPI；质检异常、密集表格、复杂公式等页面使用 450 DPI 二次识别。
- 保留两次结果、逐页来源、公式编号、图表图片和待复核标记。
- 自动索引每个 PDF 的选定页版本，提供 SQLite FTS5 关键词检索。
- 单次运行和周期运行；文件锁阻止同一数据目录被多个写入进程同时修改。
- 只读登记既有区域库的DOI/来源版本；完整审核页清单按固定模型空间增量计算双路向量，保留旧ID与向量，验证后原子切换私有入口。
- 本地工作台与项目级Codex接入；带验收门槛的每周任务定义、安装与卸载。源码和示例不附带任何真实论文库或个人配置。

## 安装

Python **3.11 或 3.12**。核心安装适用于 Windows、Linux 和 macOS；模型服务的部署要求另见视觉文档。
目前从本仓库或 GitHub Release 安装，尚未发布到 PyPI。

```bash
git clone https://github.com/jiang4wqy/journalflow.git
cd journalflow
python -m venv .venv
# Linux / macOS
source .venv/bin/activate
# Windows PowerShell 改用：.venv\Scripts\Activate.ps1
python -m pip install -e .
journalflow init
journalflow doctor
```

`init` 生成本地 `journalflow.toml`。数据默认写入配置文件旁的 `data/`，配置和数据均已加入 `.gitignore`。核心安装不安装 Paddle、CUDA 或模型权重。

## 先查新、再获取 PDF

```bash
# 默认首次检查最近 30 天；以后从成功检查点增量查询。
journalflow discover
journalflow status

# 历史日期区间可显式指定，不会修改已经存在的较新检查点。
journalflow discover --since 2025-01-01 --until 2025-12-31
```

首次的 30 天窗口不是永久的发表日期边界；增量检查也可能发现较早发表、最近才更新的记录。

OA 获取需要配置自己的联系邮箱。`.env.example` 是变量示例，程序不会自动加载 `.env`。

```bash
# Linux / macOS
export JOURNALFLOW_CONTACT_EMAIL="your-email@example.org"
export UNPAYWALL_EMAIL="your-email@example.org"
# Windows PowerShell：$env:UNPAYWALL_EMAIL = "your-email@example.org"
journalflow fetch --limit 20
```

没有 OA PDF、缺少访问权限等情况进入 `waiting_access`，不会把 HTML 登录页保存成 PDF。补充邮箱或解决访问问题后，可以显式重查：

```bash
journalflow fetch --retry-access --limit 20
```

学校、图书馆或其他授权渠道下载的 PDF 可导入同一流水线：

```bash
journalflow import-pdf ./paper.pdf --doi 10.1234/example --version published
```

DOI 尚未入目录时，补充期刊键和标题：

```bash
journalflow import-pdf ./paper.pdf --doi 10.1234/example \
  --journal IJOC --title "The paper title" --version accepted
```

`published`、`accepted`、`preprint`、`unknown` 由来源或导入者声明；文件能打开、哈希通过不代表已核验全文完整性或论文身份。

## 视觉识别与检索

按照 [docs/vision.md](docs/vision.md) 安装可选客户端并启动兼容模型服务，然后运行：

```bash
journalflow doctor --vision
journalflow convert --limit 20
journalflow search 'optimization AND uncertainty'
```

源 PDF 不会修改。输出结构如下：

```text
data/
  library.sqlite
  pdfs/<doi-key>/<pdf-sha256>.pdf
  markdown/<doi-key>/<pdf-sha256>/<policy-hash>/
    document.md
    document-refined.md        # 有疑难页时生成
    status.json
    primary/0001/
      page.md
      result.json
      page-status.json
      imgs/...
    refinement/0001/...        # 实际二次识别的页
```

`document-refined.md` 逐页采用通过筛选的二次结果或原结果。索引不会把同一 PDF 的初次页和精校页重复收录。不同 PDF 内容版本会保留并分别可查。检索结果包含 DOI、PDF 哈希、页码和选定 Markdown 路径。

质量检查衡量词语覆盖、表格数字覆盖和异常输出长度，**不是逐字 OCR 准确率，也不能保证公式完全正确**。无可靠文字层的页面保留待复核标记；重要引用、公式和数值请对照原 PDF。曲线图保留图片，不自动编造数字表格。

## 在 Codex 中查论文

安装 MCP 组件后，Codex 可以直接搜索本机论文，并按 PDF 的物理页码读取索引里的原文。普通带文字层的 PDF 可先用 CPU 建立关键词索引，视觉转换生成的页索引也可以接入。[安装和两页示例](docs/codex-mcp.md)

需要整理研究发现时，可以用[证据表和跨论文比较模板](docs/research-workflow.md)。

## 持续更新

```bash
# 一个完整周期：查新 → 获取可用 PDF → 转换/精校 → 更新检索。
journalflow run --limit 20

# 完成一个周期后等待 24 小时，再重复运行。
journalflow watch --interval-hours 24 --limit 20

# 只做目录和 OA 获取，不调用视觉服务。
journalflow run --without-vision --limit 20
```

终端关闭或机器关机会停止 `watch`；长期运行应交给进程管理器。部署示例和恢复方法见 [定时运行文档](docs/automation.md)。程序不会为你租用 GPU、启动云实例或自动登录学校账户。

## 访问范围与数据

Crossref 提供元数据，不授予出版商全文访问权。自动获取的第一版适配器使用 Unpaywall 返回的 OA PDF 地址；机构全文支持用户授权后下载并导入。验证码、机构登录到期和来源的安全验证需要使用者处理。401/403 会暂停相应来源，429 会保存并遵守 `Retry-After`。

仓库仅发布源码、配置示例和合成测试。PDF、模型权重、账号、浏览器会话、运行数据库、日志和个人 API 配置不随项目提供。请按来源许可和机构协议使用下载内容。

## 开发与验证

```bash
python -m pip install -e '.[dev]'
ruff check .
pytest -q
python -m build
```

测试使用合成 PDF、离线 HTTP 响应和仅供测试的识别替身；生产 CLI 没有伪造识别后端。另已用打包后的代码连接真实 PaddleOCR-VL 服务，验证了两页转换、450 DPI 二次识别和检索。该测试不代表论文识别准确率；完整范围见 [验证记录](docs/verification.md)。项目早期版本，不同硬件和出版版式的吞吐量、数值识别表现会变化。

## 许可证与来源

本项目源码采用 [Apache-2.0](LICENSE)。依赖和模型分别遵循其各自许可证；不转授权论文或模型权重。

- [Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)
- [Unpaywall API](https://unpaywall.org/products/api)
- [PaddleOCR / PaddleOCR-VL](https://github.com/PaddlePaddle/PaddleOCR)
- [vLLM](https://github.com/vllm-project/vllm)
- [pypdfium2](https://github.com/pypdfium2-team/pypdfium2)
