# 把既有区域库接入增量流程

区域库是包含 `papers/chunks/page_sources/lexical` 的SQLite、两路NPY向量和固定嵌入模型收据的资料目录。通用关键词页库仍可单独使用；不需要区域功能时不安装可选依赖。

```bash
python -m pip install -e '.[mcp,regional]'
journalflow --config /path/to/private.toml baseline --index /path/to/library/index --library-root /path/to/library
```

基线只登记外部来源、DOI、文件哈希、文章身份和质量状态，不复制PDF，不改原库。缺少DOI的来源单独保存；DOI数、文章身份数与PDF版本数分别记账。重复导入不新增，来源身份冲突拒绝并回滚。元数据更新不会把已下载或基线已有的论文重新排入下载队列。

在私有配置中添加以下路径，均相对该配置文件，可使用自己的绝对路径。不要提交个人配置、论文或数据库。

```toml
[regional]
base_index = "library/index"
library_root = "library"
output_root = "generations"
active_pointer = "active-index.json"
model_dir = "models/multilingual-e5-small"

[automation]
publication_since = "2021-10-30"
download_enabled = false
vision_enabled = false
weekday = 0
hour = 10
minute = 0
timezone = "Asia/Shanghai"
```

`publication_since`是可选的全文获取范围。查新接口的近期更新可能来自很久以前的论文；这些元数据可以保留，但不应全部误当作新论文下载。范围过滤不删除已有PDF，未来日期记录可在日期到达后重新排队。

审核清单包含完整物理页、所选Markdown、页及片段哈希、复核标记、确认区域和来源版本。`regional.build_incremental`不识别PDF、不猜数字，也不会自行把隔离区改为正确。

```bash
journalflow --config /path/to/private.toml regional-build \
  --base-index /path/to/library/index \
  --manifest /path/to/reviewed-batch.json \
  --output-root /path/to/generations \
  --model-dir /path/to/models/multilingual-e5-small \
  --publish-pointer /path/to/active-index.json
```

更新在独立generation中构建，源索引不改。保留全部旧片段ID与向量；替代的旧正文仅降为不可回答，审核过的新正文追加新ID。两路向量分别使用短标题加正文、仅正文，同一模型及revision才能混用。没有变化的文本不重编码，重复批次复用已有结果。

发布前检查SQLite/外键、FTS对应、向量ID与形状、新向量归一化、旧向量字节、来源页覆盖及哈希。通过后才原子更新私有pointer。旧generation保留，回滚是重新验证并发布旧generation，不回写论文或清空历史。MCP每个请求选择一个完整快照。

历史真实语料回放使用 `export-holdout` 导出已选页；可在独立影子索引中移除该批，再用真实固定模型重新增量加入，检查新增、幂等和隔离规则。该验证必须标为 `baseline_holdout`、新下载数0，不能签发真实下载转换的每周启用凭据。

`pipeline --limit 10`执行受配置约束的查新与批次。关闭下载/视觉时相应工作只保留待办，不租用服务器。新识别页需要完整来源收据；身份无法核对的论文不进入默认回答。这里的自动质检不等于人工逐字校对。

公共仓库及程序包只包含程序、配置示例和合成测试。组内既有资料包独立提供；开源许可不包含真实期刊全文或个人机构访问权限。
