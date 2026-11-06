# 用 Codex 检索自己的论文

JournalFlow 在本机保存 PDF 和页索引。Codex 通过两个只读工具搜索原文和查看指定页。每个结果带论文标题、DOI、来源版本、PDF 物理页码和本地链接。

## Windows 快速试用

在包含 `pyproject.toml` 的目录打开 PowerShell。下面的安装脚本使用 Python 3.12，并保留已有配置。也可用 Python 3.11 手动创建 `.venv`。确认 `codex --version` 可运行。

```powershell
& .\scripts\install-codex.ps1
$journalflow = (Resolve-Path .\.venv\Scripts\journalflow.exe).Path
& $journalflow import-pdf .\examples\demo.pdf --doi 10.99999/journalflow-demo --journal IJOC --year 2025 --title "Synthetic routing demo"
& $journalflow index-text
$journalflowConfig = (Resolve-Path .\journalflow.toml).Path
codex mcp add journalflow -- $journalflow --config $journalflowConfig mcp
codex mcp list
```

示例是专为测试制作的两页 PDF，DOI、期刊归类和实验内容都是演示数据。在新的本机 Codex 对话中问「用 JournalFlow 查 route disruption，只看 2025 年。打开命中页，给出原文和 PDF 页码。」应命中示例的 PDF 第 2 页。如果该界面不打开本地文件链接，可将工具返回的 `file:///…#page=2` 地址复制到本机浏览器地址栏。

macOS/Linux 使用 `python3.12 -m venv .venv` 和 `.venv/bin/` 下的程序，注册 MCP 时同样传入程序和配置的绝对路径。在支持项目配置的本机 Codex 上，也可以将以下内容加到受信任项目的 `.codex/config.toml`。

```toml
[mcp_servers.journalflow]
command = "/absolute/path/to/.venv/bin/journalflow"
args = ["--config", "/absolute/path/to/journalflow.toml", "mcp"]
```

Windows 模板中的路径可用正斜杠，例如 `C:/papers/.venv/Scripts/journalflow.exe`。

## 导入自己的文献

使用 `import-pdf` 导入你有权使用的 PDF。期刊缩写来自本机配置，首次导入未知 DOI 时填写标题及期刊，有出版年份时传 `--year`。可以导入多篇，再运行 `index-text --limit 20` 分批读取 PDF 文字层。扫描件的空白文字页会计入 `pages_without_text`，需要时使用已有的 [视觉转换流程](vision.md)。

这一通用包的检索基于 SQLite FTS5，查英文文献建议用简短英文关键词。视觉转换的页索引也能直接查询，带自动复核标记的视觉页会在 MCP 里跳过。索引结果中的数字、公式和表格应对照原 PDF。

工具返回的片段会进入 Codex 对话。公开发布的包包含工具和合成样例。私有 PDF、Markdown 和含原文的索引可以放在自己的数据目录中，按资料的分享权限交付。

如需解除本机注册，运行 `codex mcp remove journalflow`。已有本地文献和索引可继续用 JournalFlow 查询。

完成首次检索后，可按[论文分析使用说明](research-workflow.md)填写逐篇证据表和跨论文比较。
