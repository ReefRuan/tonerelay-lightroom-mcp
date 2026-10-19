# ToneRelay — Adobe Lightroom Classic MCP

[中文](README.md) · [English](README.en.md)

### 把想要的效果说出来，和 Agent 一起把照片修到满意。

> 源码预览：当前提供源码与安装说明，尚未发布稳定安装包或声明完成全平台验收。安装时请固定提交，并按所选版本的安装说明核验实际环境。

面向摄影与 Adobe Lightroom Classic 的本地 MCP 修图工具。在对话里查看当前照片和目录预览、按参考图调色、细调人物与光影，也能整理一整组照片。看过结果后继续说哪里要改，编辑结果留在 Lightroom Classic 中。

## [打开 ToneRelay Portal · 看功能与真实修图效果 →](https://reefruan.github.io/tonerelay-portal/)

<a href="https://reefruan.github.io/tonerelay-portal/"><img src="assets/readme/gallery-preview.png" alt="ToneRelay Portal 中的风格参考预览，点击查看功能介绍、真实修图过程与风格参考" width="960"></a>

**[Portal 源码](https://github.com/ReefRuan/tonerelay-portal) · [一键安装](#一键安装)**

Portal 集中展示对话修图、参考图仿色、人物与光影调整，以及风格参考。点击标题或截图即可进入。

## 看过一版，再接着修

> “感觉暗部太黑了，再调整一下。”

先讨论照片想达到的效果，或发一张喜欢的参考图。Agent 给出一版后，可以继续调整阴影、高光、冷暖和层次。下面是同一次对话中，根据反馈继续精修的实际过程。

[![Adobe Lightroom Classic 与 ToneRelay MCP 的对话精修：参考图、第一版结果与提亮暗部后的第二版](assets/readme/dialogue-workflow.png)](assets/readme/dialogue-workflow.png)

左侧是 Lightroom 实际画面，右侧是参考图、两轮对话与修改结果。点击图片可看大图。

## 从一张照片，到一整组照片

| 你想做的事 | 可以这样说 |
| --- | --- |
| 查看当前照片与同目录图片 | “看看我当前选中的照片，再给我看同目录的 10 张预览。” |
| 按参考图调整颜色与影调 | “参考这张图的冷暖和层次，先给当前照片试一版。” |
| 细调人物与背景 | “背景保留现在的感觉，把人物曝光和暗部再修一下。” |
| 选片与整理 | “看看这组照片，先提出星级和旗标建议，我确认后再写回。” |
| 探索风格与效果 | “比较不同风格参考，讨论哪一种适合这张照片。” |

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

## 一键安装

复制下面整段提示词，发给支持本地 MCP 的 Agent，由它按完整指南完成安装。这里的“一键”是复制入口，不是点击后免确认安装。

```text
请为当前 Agent 客户端安装或接入 ToneRelay for Lightroom Classic。
先读取官方完整安装提示词，GitHub 不可达时使用 Gitee 文档备用：
https://github.com/ReefRuan/tonerelay-portal/blob/main/docs/install-prompt.md
https://gitee.com/ReefRuan/tonerelay-portal/blob/main/docs/install-prompt.md

按完整提示词及所选版本的 INSTALL.md 执行。先只读核验已有安装，健康兼容时只接入当前客户端，不顺便升级共享组件。需要下载时核验官方来源和完整 commit；可使用提示词中的官方 Gitee MCP 源码备用，固定版本，不混用组件，不把滞后镜像称为最新版。
完成当前平台所需的 Lightroom 插件、MCP 和客户端支持的启动 Skill 接入，记录安装归属。保留照片、Catalog、预设和其他客户端配置；涉及 Catalog 绑定、模式或共享组件变更时先取得对应授权。保留 TLS 校验，不从未知来源下载依赖。
完成后在当前客户端的真实新会话中只读验证工具发现、健康和当前选片，核对实际版本与路径。不能观察的结果标记待验证，不以进程启动代替验收，不修改照片或 Catalog 内容。
```

完整安装说明保留在本仓 `INSTALL.md`；使用说明在 `USAGE.md`，双源与排障流程保留在 Portal 原位文档。安装并验收完成后，打开 Lightroom Classic 与目标 Catalog、选中照片，在对话中说：

> 开始 Lightroom 修图。

## 关于照片与编辑

常规显影流程通过独立虚拟副本保留原片，可以查看已有版本并继续调整。星级、旗标和颜色标签会按已确认的任务写回 Catalog。RAW sidecar 等直接文件操作需要明确授权并指定目标。执行情况以 Lightroom 实际回读和返回的图像为准。

ToneRelay 是独立项目，非 Adobe 官方产品，与 Adobe 无隶属或背书关系。该集成面向 **Lightroom Classic**，不等于支持云版 Lightroom 或 Photoshop。

## 许可证

Copyright (C) 2026 Reef Ruan。

本仓库当前版本中作者原创的代码、运行 Prompt、Skill 与说明采用 [GNU AGPL-3.0-only](LICENSE)，与 Portal 一致。第三方依赖继续适用各自的许可证；Adobe 软件、产品及商标不由本项目授权。照片、参考图及截图素材不纳入代码的 AGPL 授权，按其[来源与原许可](https://github.com/ReefRuan/tonerelay-portal/blob/main/public/experience/SOURCES.md)处理。历史提交保留当时附带的许可证，不改写 Git 历史。

## 联系方式

QQ：1843727310 · 邮箱：[leningruan1@gmail.com](mailto:leningruan1@gmail.com)
