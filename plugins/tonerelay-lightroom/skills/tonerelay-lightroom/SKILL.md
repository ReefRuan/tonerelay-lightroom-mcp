---
name: tonerelay-lightroom
description: 在用户要用 Lightroom Classic（LR）看图、导入照片、读取当前显影参数、选片打星或旗标色标、修图调色、命名编辑上下文或比较版本，或查询、安装、修复、卸载 ToneRelay 时使用；也适用于 ToneRelay 工具未出现或连接失败。普通图片生成及其它软件修图不使用。
---

# ToneRelay Lightroom

这是发现与启动入口；修图流程由当前 MCP 的 Runtime Operator 指令和实际工具 schema 提供。

- **工具可用**：通过客户端的工具发现入口找到 ToneRelay，直接按 Runtime 指令完成看图、导入、标记、修图、Episode 标签与版本对比或配置查询；导入确认流程见 Runtime Operator 的“导入”节，局部字段以工具 schema 为准；普通使用不加载安装资料。
- **工具缺席**：先核对当前客户端的插件及 MCP 是否启用、是否需要官方刷新或重连。已知维护入口时可执行一次有界 `status`，报告首个阻塞及下一步；区分客户端接入、Bridge、Lightroom 和 Catalog 授权。独立握手不等于当前会话已连接；普通修图不翻源码或临时编写 stdio 客户端。工具缺席**禁止按“未安装”静默略过**：在已验证安装源码里运行 `python tools/check_client_mcp.py <客户端清单...>`（支持 Kimi `kimi.plugin.json` 与 Codex `config.toml`），把首个 FAIL 作为阻塞 loudly 报告给用户——清单里的 launcher 路径写死过期是最常见原因。
- **安装、升级、换机或修复**：按需读 [installation.md](references/installation.md)。每个客户端分别交付并验证 MCP 与本 skill，保留已完成步骤。
- **卸载**：读 [uninstallation.md](references/uninstallation.md)，依据安装清单和离线检查处理。
- **明确要求验收**：再读 [acceptance.md](references/acceptance.md)；普通安装不包含照片写入试验或清空重装。

普通查询直接回复结果，不改配置、技能或记忆。发现说明与实际接口冲突时报告差异；未知写入不重放。
