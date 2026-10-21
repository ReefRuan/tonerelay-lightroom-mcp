# ToneRelay 常见问题

## 如何描述目标？

可以使用日常语言，如“保留暖色，让人物肤色自然”。先查看结果，再决定下一步。

## 工具会自动决定哪张最好吗？

ToneRelay 提供图像测量与对比；哪张更好，还是由你和 Agent 判断。

## 默认需要复制 Catalog 才能使用吗？

不需要复制 Catalog。修图使用 Lightroom 虚拟副本，原片不会被覆盖。

新安装默认使用 `full`，核对并绑定你确认的当前 Catalog 后即可使用。`safe` 是可选的 Sandbox 限制，不是只读模式。已有安装保留你选择的模式，安装结束会提示可协助备份 Catalog。

## Agent 找不到 Lightroom 安装位置怎么办？

直接手动打开 Lightroom Classic 和要用的 Catalog，再回复“已打开”。安装提示词要求 Agent 在客户端明确询问，不遍历磁盘寻找应用；下载和依赖准备可以先继续，连接验证等 Lightroom 打开后再做。探针超时不等于应用未打开。

## 提示 Catalog 未绑定或未授权，怎么继续？

先核对 Agent 显示的 Catalog 名称；确认是你要用的目录后，回复“绑定”。Agent 会核对目标、保存绑定并回读授权。模式不一致或目标改变时也要说明具体选择；不能只输出错误让你自己猜下一步。明确要求只读验收时不会写入绑定。

## 怎么查看版本和升级？

可以问 Agent“查看 ToneRelay 版本”，请它报告实际 MCP 版本、Lightroom 插件版本和 build；固定安装来源版本由该版本支持的安装状态入口核对。各组件版本不同，不能混用；没有取得运行信息时标为未知。

升级时使用已核对的新版并更新 ToneRelay 入口，保留个人数据和当前有效配置；全新安装不合并旧分散配置。

## 插件是 full，Runtime 却期望 safe，怎么办？

这是访问模式与保存的绑定不一致。若当前 Catalog 身份就是你已确认的目标，且你明确选择保留 `full`，Agent 可通过 Catalog 确认工具保存实际模式并重新检查授权，无需来回切 Plugin Manager。没有相应授权时先明确选择；Catalog 身份改变时须重新确认目标。不能仅因桥已连接就继续读写照片。

## Catalog 备份会备份原照片吗？

不会。Catalog 备份保存目录记录，原照片需要单独备份。可以让 Agent 协助使用 Lightroom 自带的目录备份；手工复制前先正常退出 Lightroom，同时保留 `.lrcat` 和存在的 `.lrcat-data`。见 [Adobe 目录备份说明](https://helpx.adobe.com/cn/lightroom-classic/desktop/manage-catalogs-and-files/back-catalog.html)。

## 可以通过对话给照片打星、旗标和颜色标签吗？

如果当前连接的版本提供标记工具，可以批量设置星级、旗标和颜色标签，并按操作撤销。默认跳过已有任何标记的照片，也可选择只填写空字段；已有标记受到保护。使用前请 Agent 核对当前会话是否实际发现相关工具；开发实现不代表公开固定发行物已经就绪。“排除”旗标表示筛选意见，不等于删除照片。

## 现在能直接安装吗？

可以按公开源码的固定版本安装。[GitHub MCP 主仓库](https://github.com/ReefRuan/tonerelay-lightroom-mcp)已经公开，[Gitee MCP 备用仓库](https://gitee.com/ReefRuan/tonerelay-lightroom-mcp)提供官方源码备用；先核验仓库身份、访问权限和完整 commit，使用标签时也核验它指向的完整 commit，再阅读该版本的 INSTALL.md。当前是源码预览，不代表已有稳定安装包或完成全平台验收；安装后仍需真实客户端验证。没有可核验来源或完整性依据时，停止安装下载分支并报告，仍可阅读文档或核验复用健康本机安装，不退回内部仓。本机开发源码仍需要你明确指定并授权。若只为新 Agent 客户端接入，可以复用来源可核验、组件健康的已有安装，不必重新下载整套源码。

## 已在一个 Agent 客户端使用 ToneRelay，换客户端还要重装吗？

通常不用。先核对已有安装的来源、版本，以及 Runtime、Bridge、Lightroom 插件和 Catalog 授权是否健康；只装了某个客户端的 Plugin，不能直接证明共享组件都已就绪。确认可复用后，新客户端只需按自己的官方方式接入同一套本机 MCP；如果支持 Skill，也从同版本来源安装启动 Skill，并在新会话做只读连接检查。不要为了增加客户端重新下载、升级或注册一份 Lightroom 插件；某个共享组件确实损坏或不兼容时，才针对该层修复并说明对其他客户端的影响。

## Windows 和 macOS 的卸载能力一样吗？

新增的安装清单、进程身份核对和断点卸载目前仅在 macOS 开发版实现，尚未完成 Windows 实机验收。Windows 请使用对应版本支持的入口；旧版整目录卸载会删除其中的 `data`，保留个人数据时应先备份，再逐项移除软件。

## ToneRelay 安装和运行的数据放在哪里？

新安装要求 ToneRelay 自有文件集中在一个专用根目录：macOS 为 `~/Library/Application Support/ToneRelay`，Windows 以固定版本支持的安装根为准。源码副本、Runtime、依赖、插件副本、运行数据、日志、缓存、临时文件和备份都在其中；MCP、Bridge 及其子进程也须持续使用根内的数据与缓存/临时目录。

客户端配置、Lightroom 注册与偏好设置等宿主条目留在原位置并登记。照片、主 Catalog、用户预设和共享解释器保留原归属。已有部署可能沿用外部数据；默认目录与提示词要求不能证明当前实际位置，安装须读回核对，不自动迁移。

## 配置是不是只有一个文件？

macOS 新安装设计使用根目录下的 `config.json`，集中保存目录、连接、Catalog 绑定、图片尺寸、目录访问与执行开关；不迁移旧分散配置。`install-manifest.json` 保存文件归属和外部登记，任务数据、日志和上一份配置备份各有目录。Lightroom 管理的权限和宿主客户端配置仍留在宿主位置。开发源码的 `paths` 命令可查看实际目录，不读取照片或提交插件作业；使用时先核对固定版本是否支持。

## 为什么只设置安装目录还不够？

依赖、缓存和临时文件也会写到磁盘，光指定软件位置并不能约束它们。pip、Python 和客户端新启动的 MCP 可能使用不同的默认目录，或没有继承安装终端的环境。安装提示词要求同时配置并验证安装、修复和后续 MCP/Bridge 的数据、缓存与临时路径。macOS 开发源码已加入统一 config.json 和每次启动重新设置环境的 launcher，允许使用根内 downloads 作为安装源。新固定发行版本和真实客户端验收仍须单独核对，不能仅凭源码修改宣布已安装通过。版本无法满足时应报告未就绪，不改用开发仓库。

## 画廊中的模板可以下载吗？

可以。打开画廊详情，下载这个包或复制链接。六个正式风格包各有 ZIP 下载链接，包内包含 `style.md`、封面、参考图和逐图许可记录；公开文件位于 [Portal 的风格包目录](https://github.com/ReefRuan/tonerelay-portal/tree/main/style-packs/packages)。下载风格包不等于 Runtime 已安装或支持按编号自动安装，使用前仍应核对当前版本的安装说明。

[E01 自适应肤色保护](https://reefruan.github.io/tonerelay-portal/?preset=skin-tone-adaptive-fidelity#gallery) 的画廊详情可直接阅读提示词与来源索引并下载完整候选效果包，包含两张已核对许可的实体色卡照片；照片不是标准色值，Lightroom 成对效果仍待验证。[E02 朦胧发光人像](https://reefruan.github.io/tonerelay-portal/?preset=luminous-mist-portrait#gallery) 的详情提供文字链接版候选包，六张教程参考照片只以原站链接出现，不在 ZIP 或网页中托管。两个效果包均不按风格包编号自动安装。

## 国内网络访问 GitHub 慢、打不开时怎么下载？

安装源码和文档／风格包有两组不同的官方双源：

- MCP 完整安装源码及随仓库提供的安装提示词、FAQ：[GitHub 主源](https://github.com/ReefRuan/tonerelay-lightroom-mcp)／[Gitee 源码备用](https://gitee.com/ReefRuan/tonerelay-lightroom-mcp)。取得包含安装文档的固定提交后，完整阅读根目录的 `INSTALL-PROMPT.md`、`FAQ.md` 与 `INSTALL.md`，不必再访问 Portal 才能安装。GitHub 不可达时可从 Gitee 获取同一固定完整 commit 的源码；旧标签或滞后镜像可能没有新增文档，应如实报告，不移动旧标签或混装组件。Portal 仓库不能代替 MCP 源码。
- 公开安装文档的维护源、网页与风格资源：[GitHub Portal](https://github.com/ReefRuan/tonerelay-portal)／[Gitee Portal 备用](https://gitee.com/ReefRuan/tonerelay-portal)。旧 MCP 版本未附带文档时，可另外读取 [Gitee 安装提示词](https://gitee.com/ReefRuan/tonerelay-portal/blob/main/docs/install-prompt.md)，记录文档的固定提交，仍按目标源码版本的能力执行。六个正式风格包的 ZIP 备用目录是 https://gitee.com/ReefRuan/tonerelay-portal/raw/main/style-packs/downloads/ ，画廊详情也提供国内备用地址；E01、E02 先核对镜像版本和文件是否存在，不能假定已经同步。

镜像可能滞后，`main` 和“备用”都不代表最新版。先核验官方所有者、公开访问、固定完整 commit 和文件完整性，不混装不同 commit 的组件。最初仅镜像可达时，报告选用的镜像、固定 commit 与核验依据，不必先恢复 GitHub 才能使用官方备用；没有足够依据则停止安装下载分支并报告，仍可阅读文档或核验复用健康本机安装。

源码备用不会自动解决 pip 依赖下载。需要 PIP_INDEX_URL 时仅使用核验过的可信 HTTPS 索引，限本次进程或专用环境并记录，不静默改全局配置或泄露凭据；保留 TLS 校验，不用 --trusted-host、关闭证书校验或不明包绕过失败。

## 安装为什么还需要真人授权？

遇到登录、系统密码或必须由你确认的步骤，Agent 会说明要你做什么，然后从断点继续。其他已授权的普通安装步骤可连续完成。

## 怎样验证安装和卸载？

安装时逐层核对组件，再在新的客户端会话检查工具发现和只读连接。

卸载时按清单核对客户端实际接入、进程、Lightroom 注册及文件；不要再提交插件探测作业。只有无法确认客户端刷新时才重连或创建新会话，不能观察的层标记待验证。

## 卸载后会重装吗？

只要求卸载时不重装。明确要求干净重装后，在同一任务完成卸载、卸载基线核对、安装和新会话验证。

## Windows：装完 Lightroom 插件却没反应？

先检查本机 Bridge 是否运行：`%SystemDrive%\ToneRelay\bin\tonerelay.cmd bridge status` 应显示 `listening`。插件靠轮询本机 Bridge 工作，Bridge 不在时它不会响应。Bridge 需要在普通终端里启动，安装器不注册开机自启，重启电脑后也要再启动一次。

## Windows：插件检查超时，是不是没装好？

不一定。那是短超时探测，刚重开 Lightroom 时插件可能还没进入轮询。正常退出并重开 Lightroom 后再看，不要靠反复重装解决；连续多次都超时才按连接问题排查。

## Windows：需要手动把插件加进 Plug-in Manager 吗？

安装器会在 `%APPDATA%\Adobe\Lightroom\Modules` 写入条目，Lightroom 启动时自动加载，一般不需要手动添加。这类自动发现的条目在 Plug-in Manager 里“移去”是灰的，属正常；不要为了“更像正式安装”去给它改名或换位置。

## Windows：安装后提示需要修复或要求注册插件？

先检查 Bridge 是否在监听。Bridge 没运行也可能显示插件未加载；先启动，再判断是否需要修复。

## Windows：退出不了 Lightroom？

有对话框（例如 Plug-in Manager）开着时，关闭请求不会生效。先关掉对话框再正常退出；不要强制结束正在写 Catalog 的 Lightroom。

## Windows：快速缩略图报错怎么办？

先确认是工具没拿到图片，还是聊天里没显示图片。后者要检查客户端图片交付引用与实际正文渲染；工具卡片里有缩略图不代表正文显示成功。记录版本、工具结果和错误交给维护者定位，不要据此反复重装。

## Windows：客户端应该用哪份 MCP 配置？

用安装器打印的 `mcp_server` 条目。为 macOS 写的 `.mcp.json` 不能照搬到 Windows，否则连接一定失败。

## 新会话里看不到 ToneRelay 工具？

有些客户端需要新会话或重启后才会显示新工具。安装会话里的成功结果不能代替新会话验证。
