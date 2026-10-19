# 安装与连接

这份说明用于安装和维护 ToneRelay for Lightroom。应用示例见 [README](README.md)，日常修图见 [使用指南](USAGE.md)。

## 安装前

准备 Lightroom Classic、支持本地 MCP 的 Agent 客户端，以及 Python 3.10 或以上。使用本仓一个已经核验的固定 commit 或版本；把这个固定版本交给 Agent，避免安装过程中切换到不同源码。

希望由 Agent 完成安装时，将 [公开安装提示词](https://github.com/ReefRuan/tonerelay-portal/blob/main/docs/install-prompt.md) 发给它。需要平台和客户端的详细步骤时，读取随源码提供的 [安装 Skill 指南](plugins/tonerelay-lightroom/skills/tonerelay-lightroom/references/installation.md)。

## 安装整套组件

完整安装包含 Lightroom 插件、ToneRelay MCP 和客户端启动 Skill。先选择一个 ToneRelay 专用安装目录，源码下载、软件和运行文件均放在专用目录中；已有开发仓库可以作为安装输入。

macOS 默认安装目录为 `~/Library/Application Support/ToneRelay`。将核验后的固定版本源码放在该目录的 `downloads/source/`，进入这份源码目录后运行；下例的路径应与实际下载位置一致：

```sh
python3 plugins/tonerelay-lightroom/scripts/tonerelay_bootstrap_macos.py install \
  --source-root "$HOME/Library/Application Support/ToneRelay/downloads/source" --version "fixed-version-label"
```

Windows PowerShell 使用对应平台的入口：

```powershell
python plugins/tonerelay-lightroom/scripts/tonerelay_bootstrap.py install `
  --source-root "C:\path\to\verified-source" --version "fixed-version-label"
```

按安装结果中的实际路径继续，重点查看：

| 返回项 | 接下来做什么 |
| --- | --- |
| `lightroom_plugin` | 在 Lightroom Plug-in Manager 中确认插件已注册并启用。需要加载新版本时，先保存工作，再正常退出并重新打开 Lightroom。 |
| `mcp_server` | 将这份配置加入目标客户端的官方 MCP 配置入口，保留已有服务。每个客户端分别接入。 |
| `control` | 保存这个维护入口，用于查看状态和恢复连接。 |
| `verification` | 查看已经完成的检查与仍需操作的步骤。 |

安装完整客户端 Plugin 时应包含 `skills/`。仅手动添加 MCP 时，还需要按 [客户端 Skill 安装步骤](plugins/tonerelay-lightroom/skills/tonerelay-lightroom/references/installation.md#deliver-the-startup-skill-to-each-client) 安装同一份启动 Skill。仓库附带的 MCP Plugin 入口面向 macOS；Windows 使用安装器返回的配置。

## 验证能否开始使用

在 Lightroom 打开目标 Catalog 并选中一张照片，在目标客户端说“开始 Lightroom 修图”。按提示确认首次 Catalog 绑定，查看实际连接状态和当前照片名称。能够读到本次选片，才说明这份客户端连接已经可用。

需要查看配置或排查时，可以说：

> 查看 ToneRelay 当前配置和连接状态。

安装期间的验证保持只读。修改照片由后续明确的修图任务发起。

## 连接失败与恢复

先用已安装版本给出的 `control` 查询状态；macOS 可按需要使用 `bridge status`、`bridge start`、`lightroom-plugin status` 和 `mcp status`。在真实客户端中刷新或重连后，再验证当前选片。

| 现象 | 排查入口 |
| --- | --- |
| 客户端没有 ToneRelay 工具 | 核对 MCP 与 Skill 是否分别启用，以及启动命令是否仍指向实际安装位置。 |
| Bridge 不可达 | 使用安装器返回的维护入口查看 Bridge 状态。 |
| Lightroom 插件没有响应 | 确认 Lightroom 界面可用和插件启用状态；升级后按需要正常退出并重新打开。 |
| Catalog 授权未完成 | 按实际 Catalog 名称完成首次确认，或处理已有绑定冲突。 |
| Python 依赖下载失败 | 检查安装输出与网络；使用 [公开 FAQ](https://github.com/ReefRuan/tonerelay-portal/blob/main/docs/faq.md) 的依赖排障入口。 |

客户端配置文件可由源码中的 `tools/check_client_mcp.py` 检查（JSON MCP 清单；TOML 配置需要 Python 3.11+）。它用于检查启动路径，不会代替客户端的真实连接验证。

## 升级与卸载

使用已经核验的新固定版本执行相应平台的升级或修复步骤，并保留原有配置与编辑数据。完成后重新验证 Lightroom 实际加载版本与客户端连接。

卸载的完整步骤见 [卸载指南](plugins/tonerelay-lightroom/skills/tonerelay-lightroom/references/uninstallation.md)。macOS 维护入口的 `uninstall` 默认先给出计划；核对客户端接入、进程、Lightroom 注册和软件清单，再按指南执行删除。Windows 使用其原安装入口，整根卸载会删除根内数据，应先确认备份和实际文件归属。

[返回介绍页](README.md) · [开始修图](USAGE.md) · [ToneRelay Portal](https://reefruan.github.io/tonerelay-portal/)
