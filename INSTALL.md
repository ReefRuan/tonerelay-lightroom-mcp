# 安装与连接

中文 | [English](INSTALL.en.md)

这份说明用于安装和维护 ToneRelay for Lightroom。应用示例见 [README](README.md)，日常修图见 [使用指南](USAGE.md)。

本页与随包的公开 Skill 指南组成发行版安装说明；开发设计和内部维护记录有意不随发行，安装步骤不依赖它们。

## 安装前

准备 Lightroom Classic、支持本地 MCP 的 Agent 客户端，以及 Python 3.10 或以上。安装源码使用已经核验的完整 commit SHA；版本或标签应先解析到该固定提交，再把它交给 Agent，避免安装过程中切换到不同源码。

| 用途 | GitHub 主源 | Gitee 备用源 |
| --- | --- | --- |
| MCP 完整源码（含安装器、Lightroom 插件和客户端 Plugin／Skill） | [tonerelay-lightroom-mcp](https://github.com/ReefRuan/tonerelay-lightroom-mcp) | [tonerelay-lightroom-mcp](https://gitee.com/ReefRuan/tonerelay-lightroom-mcp) |
| 完整安装提示词（本仓副本） | [INSTALL-PROMPT.md](https://github.com/ReefRuan/tonerelay-lightroom-mcp/blob/main/INSTALL-PROMPT.md) | [INSTALL-PROMPT.md](https://gitee.com/ReefRuan/tonerelay-lightroom-mcp/blob/main/INSTALL-PROMPT.md) |
| FAQ 与 Windows 排障（本仓副本） | [FAQ.md](https://github.com/ReefRuan/tonerelay-lightroom-mcp/blob/main/FAQ.md) | [FAQ.md](https://gitee.com/ReefRuan/tonerelay-lightroom-mcp/blob/main/FAQ.md) |

希望由 Agent 完成安装时，复制 [README 的短提示词](README.md#一键安装)。它只带 MCP 的 GitHub／Gitee 两个入口：先只读检查已有安装；需要源码时固定完整提交，在 ToneRelay 专用安装目录取得完整快照，再完整阅读本地 [INSTALL-PROMPT.md](INSTALL-PROMPT.md)、[FAQ.md](FAQ.md) 和本页。健康兼容的已有安装只新增客户端，不为读取文档而重装或顺便升级。取得这份源码后不依赖 Portal 在线；Portal 是网页与文档维护源，不能用它的 checkout 代替 MCP 源码。

安装提示词与 FAQ 的中英文副本均来自 Portal 的公开文档，[INSTALL-CONTEXT.json](INSTALL-CONTEXT.json)记录其固定来源提交、逐文件路径和 SHA-256；该清单用于检查文档副本一致性，不是安装工件签名，也不单独证明远端来源可信。维护时先更新 Portal，再同步四份文档与清单；不从内部开发资料复制安装说明。旧标签可能不包含这些文件：不要移动已有标签，也不要从缺少文档的旧快照假装读取成功。

需要平台和客户端的详细步骤时，读取所选 MCP 源码版本随附的 [安装 Skill 指南](plugins/tonerelay-lightroom/skills/tonerelay-lightroom/references/installation.md)。

取得源码前核对域名、所有者、仓名与公开访问权限，并从已核验的官方提交或发行信息取得完整 commit SHA。在专用下载目录通过 Git 获取并检出该固定提交；运行安装器前，用 `git rev-parse HEAD` 对照选定 SHA，检查源码完整性，并保留真实来源与核验记录。`FULL_COMMIT_SHA` 只是此处表示“已核验完整提交”的占位名，不是可直接执行的值；`main`、短 SHA 或自行填写的版本标签不能代替核验。

首次安装且用户或安装清单没有既定版本时，若仅上述 Gitee 官方 MCP 镜像可达，可依据镜像中可核验的官方来源、完整 commit SHA 与完整性证据选定并固定该版本，不要求 GitHub 同时在线。明确报告使用的是 Gitee 镜像及其固定版本；未核实主源最新状态时，不宣称它是最新版。

用户或安装清单已有固定版本时，切换到 Gitee MCP 备用源仍须取得**同一已核验 commit** 的完整源码。安装器接受核验后的本地 `--source-root`，不依赖 GitHub 继续下载 Runtime。镜像可能滞后：缺少选定提交时报告镜像尚未同步，不静默降级，也不把镜像 HEAD 宣称为主源最新版。来源或完整性无法核验时停止，不转向内部仓、未知第三方镜像或关闭 TLS 校验。保留完整客户端 Plugin／Skill，并按下文分别完成客户端接入；源码下载完成不等于全部组件已安装。

源码双源不覆盖 Python 软件包下载。pip 依赖仍需可用的可信下载源；必要时通过 `PIP_INDEX_URL` 指定已经核验的软件包源，不禁用 TLS 校验，也不从未知来源安装。macOS 受管安装不读取用户 pip 配置文件，应使用该环境变量并保留失败日志。平台参数、Skill 交付和客户端配置边界仍以下文及所选源码版本的指南为准。

## 安装整套组件

完整安装包含 Lightroom 插件、ToneRelay MCP 和客户端启动 Skill。先选择一个 ToneRelay 专用安装目录，源码下载、软件和运行文件均放在专用目录中；已有开发仓库可以作为安装输入。

macOS 默认安装目录为 `~/Library/Application Support/ToneRelay`。将核验后的固定版本源码放在该目录的 `downloads/source/`，进入这份源码目录后运行；下例的路径应与实际下载位置一致：

```sh
python3 plugins/tonerelay-lightroom/scripts/tonerelay_bootstrap_macos.py install \
  --source-root "$HOME/Library/Application Support/ToneRelay/downloads/source" \
  --version "fixed-version-label" --manage-source
```

先把 `fixed-version-label` 替换为已核验的固定版本标签。`--manage-source` 仅用于这次安装新建的专用 `downloads/source/`，将下载源码逐文件登记为可卸载软件；**既有开发 checkout 绝不加这个参数**，它只能作为只读安装输入，不属于卸载资产。也可用 `--source-origin` 和 `--source-integrity` 记录已经核验的来源、归档哈希或固定提交；这些参数本身不执行远端核验。

Windows PowerShell 使用对应平台的入口：

```powershell
python plugins/tonerelay-lightroom/scripts/tonerelay_bootstrap.py install `
  --source-root "C:\path\to\verified-source" --version "fixed-version-label"
```

将 Windows 示例路径和版本替换为实际值。Windows 原入口不支持 macOS 的 `--manage-source` 和 `record-client`，不要把这些参数或卸载保护套用过去。

按安装结果中的实际路径继续，重点查看：

| 返回项 | 接下来做什么 |
| --- | --- |
| `lightroom_plugin` | 需要注册到 Lightroom 的插件路径；完成下面的注册步骤后，再确认已启用和实际加载版本。 |
| `mcp_server` | 将这份配置加入目标客户端的官方 MCP 配置入口，保留已有服务。每个客户端分别接入。 |
| `control` | 保存这个维护入口，用于查看状态和恢复连接。 |
| `manifest`（macOS） | 保存软件、源码下载和客户端接入的归属记录，供之后安全卸载。 |
| `verification` | 查看已经完成的检查与仍需操作的步骤。 |

### 注册 Lightroom 插件并启动 Bridge

安装 Runtime 不等于已经注册 Lightroom 插件。使用安装输出的 `control` 执行 `bridge start` 和 `lightroom-plugin install`。macOS 默认路径示例如下；自定义安装目录必须把变量改成实际返回值：

```sh
TONERELAY_CONTROL="$HOME/Library/Application Support/ToneRelay/bin/tonerelay"
"$TONERELAY_CONTROL" bridge start
"$TONERELAY_CONTROL" lightroom-plugin install
```

Windows PowerShell 同样使用安装输出的实际维护入口；下列路径是待替换的占位：

```powershell
$ToneRelayControl = "C:\path\from-install-output\tonerelay.cmd"
& $ToneRelayControl bridge start
& $ToneRelayControl lightroom-plugin install
```

注册成功后，先保存工作，再正常退出并重新打开 Lightroom Classic；只重载插件不足以验证新版本已加载。随后运行 `lightroom-plugin status`，核对注册路径和真实插件响应。如果注册命令要求手动处理，在 Lightroom 的 **File → Plug-in Manager → Add** 选择安装结果的 `lightroom_plugin`，不要添加重复或指向旧版本的条目。已有其它来源的注册先核对归属，不直接覆盖。

### 接入 MCP 与启动 Skill

安装完整客户端 Plugin 时应包含 `skills/`。仅手动添加 MCP 时，还需要按 [客户端 Skill 安装步骤](plugins/tonerelay-lightroom/skills/tonerelay-lightroom/references/installation.md#deliver-the-startup-skill-to-each-client) 安装同一份启动 Skill。仓库附带的 MCP Plugin 入口面向 macOS；Windows 使用安装器返回的配置。

将 `mcp_server` 按目标客户端的官方入口合并，保留其它服务；bootstrap 不会自动把配置和 Skill 安装进每个客户端。MCP 由客户端启动，不要另起一个脱离客户端的 stdio 常驻进程。

### 登记每个客户端（macOS）

完成接入后，使用同一个 `control` 执行 `record-client`，把实际客户端记录到 `install-manifest.json`。每个客户端分别登记，名称也应能区分它们。以下路径和客户端名称均为占位，必须先替换：

```sh
"$TONERELAY_CONTROL" record-client --client "my-client" \
  --config-path "/absolute/path/to/client-config.json" \
  --server-key "tonerelay-lightroom"
```

默认 JSON adapter **只支持 JSON 顶层 `mcpServers`**，且指定条目的 `command` 必须是本安装根 `bin/tonerelay-mcp` 的精确路径；键名不同就修改 `--server-key`。它不支持 Codex TOML，也不支持以 `/bin/sh` 等包装 launcher 的条目。符合条件的记录让卸载器核对条目未被修改后，只移除 ToneRelay 对应键并备份原配置，保留其它服务。

Codex TOML、官方 Plugin 或 UI 管理的接入，改用官方模式登记实际配置位置：

```sh
"$TONERELAY_CONTROL" record-client --client "my-official-client" \
  --config-path "/absolute/path/to/actual-client-config" \
  --client-kind official
```

官方模式只记录位置和操作员证据，**不会自动解析、修改或卸载这些客户端配置**。以后须通过该客户端的官方入口移除，再按 [卸载指南](plugins/tonerelay-lightroom/skills/tonerelay-lightroom/references/uninstallation.md) 用 `--removal-evidence` 记录实际观察到的移除结果；安装时不填写移除证据。若无法确定真实配置位置，先保留待确认的客户端记录，不虚构路径或声称已自动登记。Windows 原入口没有此命令，应保留实际接入位置和卸载步骤，不能宣称具有相同清单能力。

## 验证能否开始使用

在 Lightroom 打开目标 Catalog 并选中一张照片，在目标客户端说“开始 Lightroom 修图”。按提示确认首次 Catalog 绑定，查看实际连接状态和当前照片名称。能够读到本次选片，才说明这份客户端连接已经可用。

需要查看配置或排查时，可以说：

> 查看 ToneRelay 当前配置和连接状态。

首次 Catalog 绑定需要明确确认；绑定后的连通和选片验收保持只读。修改照片由后续明确的修图任务发起。

## 连接失败与恢复

先用已安装版本给出的 `control` 查询状态；macOS 可按需要使用 `bridge status`、`bridge start`、`lightroom-plugin status` 和 `mcp status`。在真实客户端中刷新或重连后，再验证当前选片。

| 现象 | 排查入口 |
| --- | --- |
| 客户端没有 ToneRelay 工具 | 核对 MCP 与 Skill 是否分别启用，以及启动命令是否仍指向实际安装位置。 |
| Bridge 不可达 | 使用安装器返回的维护入口查看 Bridge 状态。 |
| Lightroom 插件没有响应 | 确认 Lightroom 界面可用和插件启用状态；升级后按需要正常退出并重新打开。 |
| Catalog 授权未完成 | 按实际 Catalog 名称完成首次确认，或处理已有绑定冲突。 |
| Python 依赖下载失败 | 检查安装输出与网络；使用[本仓 FAQ](FAQ.md) 的依赖排障入口。 |

客户端配置文件可由源码中的 `tools/check_client_mcp.py` 检查（JSON MCP 清单；TOML 配置需要 Python 3.11+）。它用于检查启动路径，不会代替客户端的真实连接验证。

## 升级与卸载

使用已经核验的新固定版本执行相应平台的升级或修复步骤，并保留原有配置与编辑数据。完成后重新验证 Lightroom 实际加载版本与客户端连接。

卸载的完整步骤见 [卸载指南](plugins/tonerelay-lightroom/skills/tonerelay-lightroom/references/uninstallation.md)。macOS 维护入口的 `uninstall` 默认先给出计划；核对客户端接入、进程、Lightroom 注册和软件清单，再按指南执行删除。Windows 使用其原安装入口，整根卸载会删除根内数据，应先确认备份和实际文件归属。

[返回介绍页](README.md) · [开始修图](USAGE.md) · [ToneRelay Portal](https://reefruan.github.io/tonerelay-portal/)
