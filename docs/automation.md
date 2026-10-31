# 持续运行与恢复

`run` 是一个完整周期，`watch` 在周期结束后等待指定小时数再运行。默认每批获取和转换 20 个 PDF。积压较多时可显式调整 `--limit`；目录查新不受这个 PDF 批量值限制。

```bash
journalflow --config /path/to/journalflow.toml watch --interval-hours 24 --limit 20
```

此命令需要进程和计算机持续运行，关闭终端不会使它自动变成后台服务。项目不创建 Codex 定时任务、不租用云机器，也不自动开关 GPU 实例。

## Linux 服务示例

有 systemd 的 Linux 主机可以把下面内容保存为自己的用户服务文件 `~/.config/systemd/user/journalflow.service`，替换示例路径后启动。它是部署示例，需在自己的系统验证。

```ini
[Unit]
Description=JournalFlow incremental library
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/path/to/journalflow
EnvironmentFile=/path/to/private/journalflow.env
ExecStart=/path/to/venv/bin/journalflow --config /path/to/journalflow.toml watch --interval-hours 24 --limit 20
Restart=on-failure
RestartSec=60

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now journalflow
journalctl --user -u journalflow -f
```

是否允许用户服务在注销后运行由主机配置决定。没有 systemd 的容器可在现有进程管理器下运行同一 `watch` 命令。Windows 可通过任务计划程序定期执行 `run`；计划任务需要使用者自己的运行账户、环境和路径。

## 状态与失败处理

```bash
journalflow status
```

- `pending`：尚未完成全文获取。
- `downloaded`：PDF 已经读取和哈希检查，内容身份及完整性仍取决于来源。
- `waiting_access`：缺少 OA PDF、必要配置或需要使用者解决访问问题。
- `failed`：可重试的失败；有 `retry_at` 时在该时间之前不请求。
- 转换中的 `processing` 和 `failed` 都可以通过重新运行恢复；原 PDF 和已完成页保留。
- 401/403 会持久暂停对应主机，需解决问题后显式重试。
- 429 的 `Retry-After` 在重启后依然有效，手动重试访问也不会清除尚未到期的限流。

```bash
# 解决了 OA 邮箱或来源访问问题后重查。
journalflow fetch --retry-access --limit 20
# 元数据来源的访问问题解决后，只清除访问阻塞，不跳过限流时间。
journalflow discover --retry-source
```

日志输出为 JSON，可由进程管理器保存。仅在新增数据、失败、需要验证或任务完成时通知使用者；项目本身不发送邮件或聊天消息。

## 备份与边界

保存完整 `data/` 和自己的配置。备份 SQLite 时应先停止写入进程，或使用 SQLite 的备份接口。SQLite/WAL 和进程锁按本机持久磁盘设计，不建议把同一状态数据库直接放到多台机器共享的网络盘。

一套 CPU 控制进程可以连接另一台机器上的 GPU 服务。需要多控制进程或跨机器队列时，应另行引入集中任务服务；首版的同一数据目录只允许一个写入者。

机构登录、验证码、学校 VPN 失效仍需要使用者处理；此类验证不会通过自动化绕过。无法确定的识别结果会进入待复核清单。
