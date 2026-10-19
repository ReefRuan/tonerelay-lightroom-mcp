# ToneRelay — Adobe Lightroom Classic MCP

[中文](README.md) · [English](README.en.md)

### 把想要的效果说出来，和 Agent 一起把照片修到满意。

> 源码预览：当前提供源码与安装说明，尚未发布稳定安装包或声明完成全平台验收。安装时请固定提交，并按 [INSTALL.md](INSTALL.md) 核验实际环境。

面向摄影与 Adobe Lightroom Classic 的本地 MCP 修图工具。在对话里查看当前照片和目录预览、按参考图调色、细调人物与光影，也能整理一整组照片。看过结果后继续说哪里要改，编辑结果留在 Lightroom Classic 中。

A local Model Context Protocol (MCP) server for Adobe Lightroom Classic: conversational AI photo editing, reference-based color matching and color grading, subject masks, photo previews, and batch photo culling through an AI agent.

**[看实际效果](https://reefruan.github.io/tonerelay-portal/#experience) · [风格与效果画廊](https://reefruan.github.io/tonerelay-portal/#gallery) · [安装与使用](https://reefruan.github.io/tonerelay-portal/#experience) · [联系作者](https://reefruan.github.io/tonerelay-portal/contact.html)**

[安装指南](INSTALL.md) · [使用指南](USAGE.md) · [复制安装提示词](#复制给-agent开始安装) · [Portal 仓库](https://github.com/ReefRuan/tonerelay-portal) · [Bilibili 主页](https://space.bilibili.com/2869626) · [Star MCP](https://github.com/ReefRuan/tonerelay-lightroom-mcp)

## 推荐先逛逛风格画廊

<a href="https://reefruan.github.io/tonerelay-portal/#gallery"><img src="assets/readme/gallery-preview.png" alt="ToneRelay 风格与效果画廊预览，点击浏览不同风格的参考照片" width="360"></a>

**[打开画廊，看图挑方向 →](https://reefruan.github.io/tonerelay-portal/#gallery)**

还没想好怎么修？强烈推荐先点开画廊，比较不同风格的参考照片，再把喜欢的方向带回对话讨论。上方小图也可以直接点击进入；画廊照片是风格参考，不是套用预设后的效果承诺。

## 看过一版，再接着修

> “感觉暗部太黑了，再调整一下。”

先讨论照片想达到的效果，或发一张喜欢的参考图。Agent 给出一版后，可以继续调整阴影、高光、冷暖和层次。下面是同一次对话中，根据反馈继续精修的实际过程。

[![Adobe Lightroom Classic 与 ToneRelay MCP 的对话精修：参考图、第一版结果与提亮暗部后的第二版](assets/readme/dialogue-workflow.png)](assets/readme/dialogue-workflow.png)

左侧是 Lightroom 实际画面，右侧是参考图、两轮对话与修改结果。点击图片可看大图；[在 Portal 中继续浏览](https://reefruan.github.io/tonerelay-portal/#experience)。

## 从一张照片，到一整组照片

| 你想做的事 | 可以这样说 |
| --- | --- |
| 查看当前照片与同目录图片 | “看看我当前选中的照片，再给我看同目录的 10 张预览。” |
| 按参考图调整颜色与影调 | “参考这张图的冷暖和层次，先给当前照片试一版。” |
| 细调人物与背景 | “背景保留现在的感觉，把人物曝光和暗部再修一下。” |
| 选片与整理 | “看看这组照片，先提出星级和旗标建议，我确认后再写回。” |
| 探索风格与效果 | “看看画廊里的风格，讨论哪一种适合这张照片。” |

### 人物曝光与整体光影一起调整

> “蒙版恢复人物曝光和整体曝光。”

全局影调与主体局部调整可以配合进行。看过实际图片后继续修正亮度、边缘与画面关系。

[![Lightroom 主体蒙版与全局曝光配合：人物提亮、原图和修改后照片的实际对比](assets/readme/subject-mask.png)](assets/readme/subject-mask.png)

### 把参考方向用到多张照片

同一组照片的光线和主体各不相同。Agent 可以结合参考图逐张判断，再把调整结果带回 Lightroom 继续查看。

[![Lightroom 参考图仿色工作流：多张参考图、按照片光线选择方向并回读调色结果](assets/readme/reference-color.png)](assets/readme/reference-color.png)

### 在 Lightroom 里保留选片结果

浏览、比较照片后，可以按当前任务写入星级、旗标或颜色标签，并保护已有标记。

[![Lightroom 批量选片与照片整理：目录网格、双图比较以及 Agent 星级和旗标写回记录](assets/readme/batch-workflow.png)](assets/readme/batch-workflow.png)

以上展示图取自 Portal，裁边与圆角仅用于页面排版；其中的 Lightroom 与 Agent 对话来自作者此前的实际工作流，并非本轮重新执行修图。没有重绘照片、修改对话或虚构结果，也不作为速度或 Token 对照测试。[查看实际素材来源](https://github.com/ReefRuan/tonerelay-portal/blob/main/public/experience/SOURCES.md)。具体效果取决于照片、参考目标和所用模型。

## 开始使用

**[在 Portal 安装与使用 →](https://reefruan.github.io/tonerelay-portal/#experience)**

[完整安装与恢复指南](INSTALL.md) · [日常使用指南](USAGE.md) · [直接复制下方安装提示词](#复制给-agent开始安装)

准备好 Lightroom Classic 和一个支持本地 MCP 的 Agent 客户端。在 Lightroom 中打开要使用的 Catalog 并选中照片；安装完成后，在对话中说：

> 开始 Lightroom 修图。

完整安装包含 Lightroom 插件、ToneRelay MCP 和客户端启动 Skill。支持的安装入口、客户端接入和平台说明以本仓 **[INSTALL.md](INSTALL.md)** 为准；也可以把 Portal 保留的 **[安装提示词](https://github.com/ReefRuan/tonerelay-portal/blob/main/docs/install-prompt.md)** 交给 Agent 执行。

MCP 源码：[GitHub 主源](https://github.com/ReefRuan/tonerelay-lightroom-mcp) · [Gitee 备用源](https://gitee.com/ReefRuan/tonerelay-lightroom-mcp)。完整安装提示词保留在 Portal：[GitHub 文档](https://github.com/ReefRuan/tonerelay-portal/blob/main/docs/install-prompt.md) · [Gitee 备用文档](https://gitee.com/ReefRuan/tonerelay-portal/blob/main/docs/install-prompt.md)。Portal 是说明与展示仓库，不是 MCP 源码。

安装时先核验仓库身份和完整 commit SHA，再固定该源码版本；切换下载源也保持同一 commit。备用源可能滞后，不把镜像旧版本当成最新版。具体核验、平台和客户端接入边界见 [安装指南](INSTALL.md#安装前)；源码备用源不替代 Python 依赖下载源。

## Source installation

已取得并核验本仓某个固定 commit 的源码时，需要 Python 3.10 或以上。先按 [安装指南](INSTALL.md) 将源码放入专用安装目录，再从该源码根目录运行对应平台的入口，并使用当前固定版本标识。

macOS：

```sh
python3 plugins/tonerelay-lightroom/scripts/tonerelay_bootstrap_macos.py install \
  --source-root "$HOME/Library/Application Support/ToneRelay/downloads/source" \
  --version "fixed-version-label" --manage-source
```

`--manage-source` 仅登记为这次安装新建的专用下载源码，以便卸载时按清单清理。已有开发 checkout 不加这个参数，源码只作为只读安装输入；自定义目录应与实际配置的下载位置一致。

Windows PowerShell：

```powershell
python plugins/tonerelay-lightroom/scripts/tonerelay_bootstrap.py install `
  --source-root "C:\path\to\verified-source" --version "fixed-version-label"
```

安装结果会给出 Lightroom 插件位置、维护入口、MCP 客户端配置和 `next` 步骤。两个平台都需按返回的维护入口执行 `lightroom-plugin install`，完成 Lightroom 插件注册；不能只安装 Runtime 就认为全部接入完成。按所用客户端分别完成 MCP 与 Skill 接入，再从真实对话核对连接和当前选片。macOS 与 Windows 使用各自的安装入口；原生 Windows 运行情况需在实际环境确认。[安装常见问题](https://github.com/ReefRuan/tonerelay-portal/blob/main/docs/faq.md)

macOS 客户端接入后还需用 `record-client` 登记安装清单；JSON `mcpServers` 与官方管理/TOML 配置的登记方式不同，详见[登记每个客户端](INSTALL.md#登记每个客户端macos)。Windows 原入口不支持 `--manage-source` 或 `record-client`，不要混用平台参数。

## Dependency download help

Python 依赖下载失败时，先保留安装错误与日志，再查看[公开 FAQ 的安装排障说明](https://github.com/ReefRuan/tonerelay-portal/blob/main/docs/faq.md)。如需切换 `PIP_INDEX_URL`，使用经过核验的下载源；不要禁用 TLS 校验，或从不明来源安装依赖。恢复后继续当前安装，不要以清空照片、Catalog 或编辑数据作为重装步骤。

## 关于照片与编辑

常规显影流程通过独立虚拟副本保留原片，可以查看已有版本并继续调整。星级、旗标和颜色标签会按已确认的任务写回 Catalog。RAW sidecar 等直接文件操作需要明确授权并指定目标。执行情况以 Lightroom 实际回读和返回的图像为准。

ToneRelay 是独立项目，非 Adobe 官方产品，与 Adobe 无隶属或背书关系。该集成面向 **Lightroom Classic**，不等于支持云版 Lightroom 或 Photoshop。

## 许可证

Copyright (C) 2026 Reef Ruan。

本仓库当前版本中作者原创的代码、运行 Prompt、Skill 与说明采用 [GNU AGPL-3.0-only](LICENSE)，与 [Portal](https://github.com/ReefRuan/tonerelay-portal) 一致。第三方依赖继续适用各自的许可证；Adobe 软件、产品及商标不由本项目授权。照片、参考图及截图素材不纳入代码的 AGPL 授权，按其[来源与原许可](https://github.com/ReefRuan/tonerelay-portal/blob/main/public/experience/SOURCES.md)处理。历史提交保留当时附带的许可证，不改写 Git 历史。

作者主页：[GitHub](https://github.com/ReefRuan) · [Bilibili](https://space.bilibili.com/2869626) · [联系作者](https://reefruan.github.io/tonerelay-portal/contact.html)。

## 复制给 Agent，开始安装

点击下方代码块右上角的复制按钮，粘贴给当前 Agent 即可。这是简短引导，不替代完整流程；Agent 应先读取链接中的完整安装提示词与本仓安装指南。完整提示词继续保留在 Portal 原路径。

```text
请协助在当前 Agent 客户端安装或接入 ToneRelay for Lightroom Classic。
先读取完整安装提示词：https://github.com/ReefRuan/tonerelay-portal/blob/main/docs/install-prompt.md
提示词备用文档：https://gitee.com/ReefRuan/tonerelay-portal/blob/main/docs/install-prompt.md
安装指南：https://github.com/ReefRuan/tonerelay-lightroom-mcp/blob/main/INSTALL.md
安装指南备用：https://gitee.com/ReefRuan/tonerelay-lightroom-mcp/blob/main/INSTALL.md
MCP 源码主源：https://github.com/ReefRuan/tonerelay-lightroom-mcp
MCP 源码备用：https://gitee.com/ReefRuan/tonerelay-lightroom-mcp

先只读检查系统、客户端、Lightroom 和已有 ToneRelay 安装，核对来源、固定版本及健康状态。健康且兼容时复用已有安装；只接入当前客户端，不顺便重装或升级共享组件。需下载时，核验上述官方公开 MCP 仓的真实身份、公开访问权限与完整 commit SHA，并核对本地检出版本。首次安装且用户或清单没有既定版本时，若仅 Gitee 官方 MCP 镜像可达，可根据可核验的来源、完整 commit 与完整性证据选定并固定镜像版本，报告来源和版本，不冒称最新，不要求 GitHub 在线。若已有固定版本，切换下载源必须取得同一已核验 commit 的完整源码。Portal 仅提供文档，不是 MCP 源码。镜像缺少选定 commit 或来源无法核验时先报告，不静默改装旧版，不使用内部仓或未知第三方来源。Python 依赖需另有可用的可信下载源，不禁用 TLS 校验。

完成必要的 MCP、Lightroom 插件及客户端接入；客户端支持 Skill 时接入同版本启动 Skill。使用专用安装目录并记录归属，保留照片、Catalog、预设和其他 MCP 配置。涉及 Catalog 绑定、模式或共享组件变更，以及系统要求本人确认时，先说明影响并取得对应授权。

完成后在当前客户端的真实新会话中只读验证工具可见、健康状态和当前选片，并核对实际加载版本与路径；不以进程启动代替验收，不修改照片或 Catalog 内容。无法观察的结果标记待验证，不宣布成功。
```

<a href="https://reefruan.github.io/tonerelay-portal/#experience"><kbd>打开安装与使用 →</kbd></a> · **[查看完整安装提示词 →](https://github.com/ReefRuan/tonerelay-portal/blob/main/docs/install-prompt.md)** · [Gitee 备用提示词](https://gitee.com/ReefRuan/tonerelay-portal/blob/main/docs/install-prompt.md)
