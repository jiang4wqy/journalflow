# 本地工作台与每周增量更新

当前公开交付仅为 GitHub 源码及源码 ZIP，不含免安装运行时、论文、索引、账号或 API Key。已经交付的四刊离线资料包保持独立，不会因打开这个项目被修改。工作台是使用者可主动运行的本地入口；发布、下载或解压源码不会启动程序，也不会创建每周任务。程序不会租服务器、启动暂停的云服务器或替你登录机构账号。

## 可选：使用者自行启动工作台

- Windows：双击 `scripts/打开JournalFlow.cmd`。
- macOS：双击 `scripts/打开JournalFlow.command`。若解压软件未保留执行权限，可在项目目录运行 `chmod +x scripts/打开JournalFlow.command`。本机未做 Mac 真机验收。

源码 ZIP 没有附带独立 Python。正常使用需要先安装 Python 3.11 或 3.12；入口会创建或复用项目 `.venv`，首次缺少依赖时需要网络安装 `[mcp,regional]`。区域索引所需的模型文件、论文数据与视觉模型服务需要另行配置，启动入口不安装或启动视觉模型、CUDA 或 GPU 环境。脚本保留了优先检测相对 `runtime/` 目录的能力，供使用者自行准备完整运行时；这不表示本次源码发布已经包含该运行时。

首次生成示例 `journalflow.toml`，之后保留已有配置。浏览器打开随机端口的 `http://127.0.0.1:端口/`。终端需保持打开；Ctrl+C 停止工作台。关闭浏览器不取消已经启动的 CLI 批次，运行日志保存在数据目录的 `automation/desktop-logs`。CLI 自身的流水线锁防止多个入口同时修改同一个库。

工作台提供：

- **查新**：调用 `discover`，更新论文目录，不获取全文。
- **处理批次**：调用 `pipeline --limit 10`，遵守配置中的访问方式、视觉暂停状态与验证要求。没有获取权限的 PDF 保留等待，不绕过机构限制。
- **状态**：显示入站 PDF 收件箱、配置与区域索引路径、视觉暂停状态、队列数量、当前窗口发起的批次及日志位置。邮箱只显示是否配置，不展示地址或环境变量内容。
- **接入 Codex**：调用 `connect-codex`，仅生成当前配置目录的项目 `.codex/config.toml`，已有不同配置拒绝覆盖。完成后在 Codex 打开配置所在目录并信任项目，不修改全局账号，不触发下载或视觉识别。

当前下载与视觉阶段均暂停，`download_enabled=false`、`vision_enabled=false`。批次可继续元数据查新和排队，不会把排队当作完成下载。没有提供 Unpaywall 邮箱时也不会假定已经具备 OA 获取条件。等待视觉转换的文档不计为已成功完成；历史十篇回放不是十篇真实下载与高精度转换试运行。

界面只监听 loopback。所有启动操作必须使用 POST、匹配的本地 Origin/Host 和每次启动随机生成的 token；GET 不触发批次。CLI 子进程通过参数数组执行，不拼接 shell 命令。工作台不提供文件上传、任意命令执行或远程管理接口。

## 配置周期

配置文件中可以加入：

```toml
[automation]
download_enabled = false
vision_enabled = false
weekday = 0  # Monday=0 ... Sunday=6
hour = 10
minute = 0
timezone = "Asia/Shanghai"
```

**周一 10:00 已作为默认配置，当前定时下载待启用。** 改时间不会自动创建系统任务。Windows Task Scheduler 和 macOS launchd 的日历触发使用操作系统本地时间，安装前检查本机时区偏移是否与配置一致；应先把本机时区设为配置时区。额外 IANA 时区可能需要安装 `tzdata`。改变时区后请卸载并重新安装本项目任务。

## 启用条件

代码已接入真实试运行的凭据签发：`pipeline` 记录本轮配置哈希、识别策略和新增 PDF ID，只有下载与视觉开关都开启、同一批至少十个不同 DOI 的新 PDF 有 HTTP(S) 来源、在本轮完成转换、完整进入发布的区域索引且保留可回答证据，并且既有基线仍存在、区域索引重新验证通过、当前入口指向该版本时，才原子生成 `数据目录/automation/gate.json`。旧积压、历史回放、仅有“下载十篇/转换十篇”两个计数都不能签发。未通过时报告 `pending`，不改写已有凭据。

**签发代码已提供，但本项目目前未完成真实十篇新下载验收，也未因此启用任何系统任务。** 签发只生成凭据；之后仍需显式安装任务。下面只展示主要字段，实际凭据还包含该批 DOI、PDF ID、来源哈希及区域索引哈希；不要手工填写为通过。安装以及每次调度执行均重新检查：

```json
{
  "schema": 1,
  "config_sha256": "当前配置文件的 SHA256",
  "baseline": {"passed": true},
  "trial": {"passed": true, "documents": 10, "kind": "live_download_conversion"},
  "regional": {"passed": true}
}
```

请不要手工把未完成的阶段改为通过。`baseline_holdout`、不足十篇、缺少区域验证或配置哈希改变都会阻止安装和运行。通过历史回放只能证明回放路径，不能解除真实增量下载的启用条件。

可先查看状态，或者只导出可审阅的任务定义：

```bash
python -m journalflow.scheduling --config journalflow.toml status
python -m journalflow.scheduling --config journalflow.toml export --platform windows --output weekly-task.xml
python -m journalflow.scheduling --config journalflow.toml export --platform macos --output weekly-task.plist
```

导出不会启用任务，也不要求 gate 已通过。指定已存在的导出文件会拒绝覆盖。Mac 的 plist 只适合在 Mac 上安装。

待真实试运行通过、明确决定启用后：

```bash
python -m journalflow.scheduling --config journalflow.toml install
```

也可用 `--weekday 2 --hour 9 --minute 30 --timezone Asia/Shanghai` 覆盖本次安装时间。配置路径与解释器路径固定在任务中；移动项目后先卸载旧任务，再从新路径安装。Windows 任务使用当前用户、最低权限、交互登录令牌，不保存账户密码。Mac 使用当前用户的 LaunchAgent。它们适合常用个人电脑，关机、休眠或未登录时不会保证准点执行；系统恢复后的补跑行为由系统调度器决定。

任务定义实际使用独立解释器直接执行本项目 `scheduling.py ... run --limit 10`，自行定位同一源码包，不依赖安装终端的临时 `PYTHONPATH`。它重验 gate 后再调用 `pipeline --limit 10`。手动也可使用 `python -m journalflow.scheduling ... run --limit 10`。不创建用于监控某次任务的 Codex 自动化聊天。系统任务本身不会获得额外的论文访问权限，也不会上传本地文件。

## 卸载与改时间

```bash
python -m journalflow.scheduling --config journalflow.toml uninstall
```

任务标识是 `org.journalflow.weekly.<配置路径哈希>`。安装遇到已有任务时拒绝覆盖；卸载核对本项目收据、任务标识和执行命令，只删除该任务，不清理其他系统任务。若任务被外部修改，保留现场并报错。改时间请先卸载，再安装新时间。卸载不会删除论文、转换结果或索引。

本机配置、`data/`、`.venv/`、运行日志与真实 gate 不应进入公开 GitHub 或源码分享 ZIP。配置示例只能使用占位路径；论文与完整离线资料包继续单独私下交付。
