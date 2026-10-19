"""Configuration entry points and factual tutorials delivered with live state.

This content is available without repository files or a network documentation
site. It describes existing configuration mechanisms; it grants no write
permission and never interpolates host paths, URLs or credentials.
"""

from __future__ import annotations

from copy import deepcopy


_GUIDES = {
    "versions": {
        "title": "当前组件版本",
        "example": "查看 ToneRelay 版本",
        "read_fields": ["versions"],
        "managed_by": "MCP Server and live Lightroom plugin probe",
        "editable_in_conversation": False,
        "description": "MCP Server、Lightroom 与 Lightroom 插件各自有独立版本；插件 build 用于区分相同版本号的构建。Lightroom 的值来自本次插件响应，断连或字段缺失时为 null，不代表已加载安装目录中的副本。",
        "steps": [
            "读取 versions.mcp_server；它与 MCP initialize 的 serverInfo.version 一致。",
            "读取 versions.lightroom、versions.lightroom_plugin 和 versions.lightroom_plugin_build；这些值只来自本次只读 probe。",
            "缺失值报告未知；不能用 MCP 版本代替 Lightroom 插件版本，也不能由版本号猜固定源码 commit。",
            "安装或升级维护时，另通过该版本支持的 runtime status 核对固定安装来源标签；普通版本查询不执行安装或搜索本机目录。",
        ],
        "takes_effect": "MCP 更新需重启相应进程并重连；Lightroom 插件更新需正常退出并重开 Lightroom 后再读取实际 build。",
        "verification_fields": ["versions"],
    },
    "access_mode": {
        "title": "safe / full 访问模式",
        "example": "查看 safe 模式，并告诉我怎么设置",
        "read_fields": ["catalog.access_mode", "catalog.authorized", "catalog.reason"],
        "managed_by": "Lightroom Plugin Manager",
        "editable_in_conversation": False,
        "description": "safe 只允许已登记的 Sandbox Catalog；full 允许当前 Catalog。两种模式都可能写入 Catalog，并保留身份绑定、虚拟副本和回读检查。expected 是当前有效期望模式；expected_source 与 expected_persisted 分别说明来源和是否已持久保存。只读查询不代表本次执行了保存。",
        "steps": [
            "在 Lightroom Classic 打开 File（文件）→ Plug-in Manager（增效工具管理器），选择 ToneRelay 插件。",
            "找到 ToneRelay · Runtime access → Access mode，选择 Safe (registered sandbox only) 或 Full access。",
            "点击 Save mode 保存。若使用 safe，在目标 Catalog 已打开时点击 Register active Catalog as sandbox。",
            "回到对话查看访问模式，核对插件实际模式、Runtime 期望模式和 Sandbox 状态。",
            "若实际与期望模式不一致，明确告诉 Agent 要保留的模式和当前 Catalog；经用户确认后，用 Catalog 确认流程同步 Runtime 绑定。",
        ],
        "takes_effect": "Plugin Manager 保存后在下一次配置查询中读取；Runtime 绑定的期望模式可能还需要用户明确确认后同步。无需因保存模式而重启 Lightroom。",
        "verification_fields": ["catalog.access_mode.actual", "catalog.access_mode.expected", "catalog.authorized"],
    },
    "sandbox": {
        "title": "Sandbox 登记",
        "example": "查看 Sandbox，给我登记教程",
        "read_fields": ["catalog.active_catalog", "catalog.sandbox_registered", "catalog.sandbox_matches_active"],
        "managed_by": "Lightroom Plugin Manager",
        "editable_in_conversation": False,
        "description": "Sandbox 是 safe 模式中显式登记的一个 Catalog。登记不会复制 Catalog，也不会将其变成只读。",
        "steps": [
            "在 Lightroom 打开要作为 Sandbox 的 Catalog。",
            "打开 File（文件）→ Plug-in Manager（增效工具管理器），选择 ToneRelay → Runtime access。",
            "核对 Active Catalog，然后点击 Register active Catalog as sandbox。",
            "若需要取消登记，使用同一区域的 Clear sandbox registration。",
            "回到对话查看 Sandbox，核对已登记且匹配当前 Catalog；首次使用的 Runtime 绑定按 Catalog 教程完成。",
        ],
        "takes_effect": "登记或清除后在下一次配置查询中读取；safe 下清除登记会阻止后续 Lightroom 操作。",
        "verification_fields": ["catalog.sandbox_registered", "catalog.sandbox_matches_active", "catalog.authorized"],
    },
    "catalog": {
        "title": "Catalog 绑定与切换",
        "example": "查看当前 Catalog，告诉我怎么绑定或切换",
        "read_fields": ["catalog.binding_recorded", "catalog.active_identity_status", "catalog.bound_catalog", "catalog.active_catalog", "catalog.binding_status", "catalog.can_switch_catalog"],
        "managed_by": "Runtime Catalog tools",
        "editable_in_conversation": True,
        "description": "binding_recorded 表示当前 Target 是否记住了 Catalog 身份；bound_catalog 只是显示名称，名称为空不代表未绑定。active_identity_status 表示是否取得活动身份，unknown 不表示绑定被清空。绑定保存 Runtime 要操作的 Catalog 身份；切换会改变 Lightroom 当前工作环境。本机 macOS/Windows 支持切换发现的 Catalog。",
        "steps": [
            "当前 Catalog 未绑定时，Agent 在客户端显示实际名称和模式并询问是否绑定；回复‘绑定’即可确认刚展示的目标和模式。只查看本教程不会写入绑定。",
            "收到确认后，Agent 重新探测核对目标未变，再使用 trlr_confirm_catalog，读回授权并继续。已有同一目标和模式的明确授权不重复询问；要选择其他 Catalog 时再列出清单。",
            "切换另一 Catalog 时，先展示准确名称和位置提示、说明可能中断当前操作，取得用户明确同意后才使用 trlr_switch_catalog。",
            "Runtime 等待 Lightroom 插件确认活动身份匹配后保存绑定；远端 Target 需要在 Lightroom 主机切换。",
            "切换后查看配置，核对活动与绑定 Catalog 一致且已授权；safe 下仍须登记对应 Sandbox。",
        ],
        "takes_effect": "确认绑定持久化到 config.json 的 targets 节；切换请求只有在插件确认新活动身份后才更新绑定。旧 Episode 不自动迁移。",
        "verification_fields": ["catalog.binding_recorded", "catalog.active_identity_status", "catalog.bound_catalog", "catalog.active_catalog", "catalog.binding_status", "catalog.authorized"],
    },
    "image_presentation": {
        "title": "图片回传与工作渲染大小",
        "example": "查看图片大小配置，解释两个尺寸的用途",
        "read_fields": ["image_presentation"],
        "managed_by": "Runtime presentation tools",
        "editable_in_conversation": True,
        "description": "codex_preview_max_edge 控制发给 Codex 的图片最长边；edit_render_max_edge 控制后续编辑工作的渲染上限。",
        "steps": [
            "说明要修改发给 Codex 的图片上限，还是新编辑工作的渲染尺寸，并给出像素数。",
            "codex_preview_max_edge 范围为 64–2048；edit_render_max_edge 范围为 256–2048。",
            "Agent 通过 trlr_get_presentation_config 读取，再通过 trlr_set_presentation_config 保存用户指定值。",
            "保存后读回核对数值和影响范围。",
        ],
        "takes_effect": "无需重启；影响后续图片回传、后续 Session 工作渲染和新 Episode 的初始渲染上限。已有产物、已有 Episode 上限和最终导出不变。",
        "verification_fields": ["image_presentation.codex_preview_max_edge", "image_presentation.edit_render_max_edge", "image_presentation.config_status"],
    },
    "folder_access": {
        "title": "本机目录访问范围",
        "example": "查看目录权限，告诉我怎么限制范围",
        "read_fields": ["folder_access"],
        "managed_by": "config.json/folders",
        "editable_in_conversation": False,
        "description": "folders.mode=full 时访问 Runtime OS 账号可访问的路径；restricted 时限制为 roots 中的目录。该范围与 Lightroom 的 safe/full 独立。",
        "steps": [
            "先查看当前访问模式、配置是否有效、根显示名与支持的图片类型。",
            "需要限制范围时，由部署管理员修改安装根 config.json 的 folders 节，将 mode 设为 restricted 并填写 roots。",
            "restricted 模式的 roots 列出允许目录；roots 为空会禁止目录访问。扫描仍遵守 OS 权限和隐藏/系统目录过滤。",
            "修改后重新查看目录范围；不再读取旧的 folder-library 文件或覆盖变量。配置错误须修复后再使用目录工具。",
        ],
        "takes_effect": "folders 节改动在后续读取时生效。扫描不访问 Lightroom，RAW 预览的 Catalog 写入仍须显式允许。",
        "verification_fields": ["folder_access.mode", "folder_access.roots", "folder_access.config_valid"],
    },
    "connection": {
        "title": "Lightroom / Bridge 连接",
        "example": "查看连接配置，给我连接排查教程",
        "read_fields": ["target", "catalog.reason"],
        "managed_by": "config.json",
        "editable_in_conversation": False,
        "description": "配置查询显示本机/远端类型与 Bridge 连通状态。Bridge 可达与 Lightroom 插件可响应是两个状态。",
        "steps": [
            "确认 Lightroom Classic 已打开且 ToneRelay Lua 插件已启用；插件启动的是 Lua 轮询。Python Bridge 核心随 MCP 自带，但默认 external 需要已知安装维护入口先执行 bridge start。",
            "查看配置：target.reachable 表示 Bridge 可达；catalog.reason 为 probe_failed 时，Bridge 可达但未取得有效的 Lightroom 插件响应。",
            "活动 Catalog 未知时，binding_status 为 active_catalog_unknown；binding_recorded 独立表示是否记住身份，名称为空不代表未绑定，断线不会清空已有绑定。先恢复有效的只读 probe，再判断活动身份，保留原绑定。",
            "连接地址保存在 config.json 的 targets 节，本机 Bridge 的监听设置保存在 bridge 节；受管安装不会用继承环境覆盖这些值。",
            "bridge.mode 默认为 external：MCP 不启动或停止独立服务。显式 builtin 在后台预检和启动本机 Bridge，不阻塞工具发现；实际请求仍核对身份和凭据。",
            "先开客户端、Lightroom 或 Bridge 均可分别就绪。Bridge 不可达与插件 probe 未响应分开处理；服务和插件晚就绪后，已挂载的工具只需再次只读检查，工具挂载失败才需要客户端官方重连。",
            "同一 TONERELAY_CONFIG 受管配置决定客户端模式；不同客户端的 TONERELAY_BRIDGE_MODE 环境值不会覆盖该配置。多个 external 客户端不能互相启动 Bridge。",
            "当前 Lightroom 插件固定连接本机 loopback 的 17777 端口；不要单独改变 Bridge 端口后假定插件会跟随。",
            "由部署管理员修改连接配置后重启受影响的 MCP/Bridge；若更新了 Lua 插件，正常退出并重新打开 Lightroom，再查看配置。",
        ],
        "takes_effect": "Target 连接设置在下一次请求读取；bridge 节变更需要重启 Bridge，随后核对插件与客户端。",
        "verification_fields": ["target.reachable", "catalog.reason", "catalog.authorized"],
    },
    "credentials": {
        "title": "Bridge 凭据",
        "example": "查看是否配置了 Token，给我配置教程",
        "read_fields": ["target.credential_configured"],
        "managed_by": "config.json and Lightroom plugin",
        "editable_in_conversation": False,
        "description": "默认本机回环 Bridge 不要求手动配置令牌；credential_configured=false 不表示缺少组件或未连接。已有令牌继续保留。该字段只显示 Target 是否配置凭据，不能单独证明连接与插件认证；凭据值不进入对话。",
        "steps": [
            "默认回环配置的 token 为 null；external 与 builtin 均可使用无令牌连接，不需要为了连接生成令牌。已有凭据必须保持配套。",
            "通过本机受控安装/配置渠道管理共享令牌，不将令牌粘贴到对话。",
            "部署管理员使 config.json 中对应 Target token、bridge.token 与安装插件副本的 local-token.txt 使用相同令牌。",
            "config.json 是 Runtime 的配置来源；插件的 local-token.txt 是当前 Lua 接口要求的副本，升级插件副本时也须核对。",
            "更新后重启 MCP 与 Bridge，并正常退出、重新打开 Lightroom，然后重新查看连接和授权状态。",
        ],
        "takes_effect": "相关进程重新启动后读取；只报告是否配置，不返回值或本机凭据文件路径。",
        "verification_fields": ["target.credential_configured", "target.reachable", "catalog.authorized"],
    },
    "data_root": {
        "title": "Runtime 数据存放位置",
        "example": "告诉我配置、数据和缓存保存在哪里",
        "read_fields": ["image_presentation.source", "admin_managed"],
        "managed_by": "config.json",
        "editable_in_conversation": False,
        "description": "安装根的 config.json 保存配置与 Catalog 绑定；data 保存任务与产物，cache 和 tmp 保存缓存与临时文件。配置查询不返回本机绝对路径。",
        "steps": [
            "受管安装由 TONERELAY_CONFIG 定位唯一 config.json；paths 节的目录均相对于该文件的父目录，不允许指向根外。",
            "新 macOS 安装根为 ~/Library/Application Support/ToneRelay。授权安装维护渠道可运行 control 的 paths 命令查看实际目录；Windows 需核对对应入口，不能假设同等支持。",
            "macOS 启动器每次读取 config.json，统一设置 pip、Python、XDG 缓存与 TMPDIR/TEMP/TMP，并使用根内工作目录。安装、Bridge、MCP 及子进程沿用这些设置，实际写入仍须在新会话验收。",
            "按全新安装使用统一配置，不读取或迁移旧分散文件。修改 paths 需要停止 MCP 和 Bridge；仅修改路径不会搬动原数据，开发仓库不作为安装或数据目录。",
            "重新启动对应进程，并在真实新客户端会话核对路径与文件归属，再查看 Catalog 绑定与图片预算是否保留。配置查询本身不返回实际绝对路径，也不执行迁移。",
        ],
        "takes_effect": "重启后读取；仅切换到空数据根会呈现首次使用状态。配置查询只说明管理入口与相对文件名。",
        "verification_fields": ["catalog.bound_catalog", "image_presentation.codex_preview_max_edge", "image_presentation.edit_render_max_edge"],
    },
    "execution": {
        "title": "显影参数执行开关",
        "example": "查看参数透传开关，解释怎么配置",
        "read_fields": ["admin_managed"],
        "managed_by": "config.json/execution",
        "editable_in_conversation": False,
        "description": "未知参数透传与结构化参数开关决定参数校验入口，不保证 Lightroom 实际支持这些参数。",
        "steps": [
            "查看 Unknown and structured Develop parameter passthrough 中的 enabled 与 structured_enabled；null 表示未取得状态。",
            "部署管理员通过 config.json 的 execution.allow_passthrough 与 execution.allow_structured 配置布尔开关；两者默认 true。",
            "更新配置后重启 MCP，再查看配置。",
            "具体参数支持条件查看 trlr_parameter_reference；实际执行仍须遵守 Catalog 授权、参数校验与逐键回读。",
        ],
        "takes_effect": "配置更新并重启 MCP 后生效；开启透传不绕过 Lightroom 授权或回读验证。",
        "verification_fields": ["admin_managed"],
    },
}

CONFIG_TOPICS = ("overview", *_GUIDES)


def validate_config_topic(topic: str) -> None:
    if not isinstance(topic, str) or topic not in CONFIG_TOPICS:
        raise ValueError("未知配置主题；可选值：" + ", ".join(CONFIG_TOPICS))


def _editable(snapshot: dict, topic: str) -> bool:
    if topic == "image_presentation":
        return snapshot["image_presentation"]["editable_in_conversation"]
    return _GUIDES[topic]["editable_in_conversation"]


def configuration_help(snapshot: dict, topic: str = "overview") -> dict:
    """Return a help index or one self-contained guide with a readback entry."""
    validate_config_topic(topic)
    result = {
        "schema_version": "runtime-config-help-v1",
        "topic": topic,
        "topics": [
            {"id": key, "title": guide["title"], "example": guide["example"],
             "managed_by": guide["managed_by"],
             "editable_in_conversation": _editable(snapshot, key),
             "arguments": {"topic": key}}
            for key, guide in _GUIDES.items()
        ] if topic == "overview" else [],
        "guide": None,
        "next_step": _next_step(snapshot),
    }
    if topic != "overview":
        guide = deepcopy(_GUIDES[topic])
        guide.pop("example")
        guide["editable_in_conversation"] = _editable(snapshot, topic)
        guide["verification"] = {
            "tool": "trlr_get_runtime_config", "arguments": {"topic": topic},
            "fields": guide.pop("verification_fields"),
        }
        result["guide"] = guide
    return result


def _next_step(snapshot: dict) -> dict | None:
    catalog = snapshot["catalog"]
    reason = catalog["reason"]
    issue = {
        "lightroom_unavailable": ("connection", "Bridge 不可达或状态未知；本地配置仍可查看。"),
        "probe_failed": ("connection", "Bridge 可达，但未取得有效 Lightroom 插件响应。请确认 Lightroom Classic 已打开且界面可用，回复‘已打开’后再检查；不搜索应用位置，超时不能证明应用未运行。"),
        "unsupported_plugin_contract": ("connection", "插件协议不兼容；通过安装/恢复渠道更新并重新连接。"),
        "catalog_not_open": ("catalog", "Lightroom 未打开 Catalog；先打开目标，再确认绑定。"),
        "catalog_confirmation_required": ("catalog", "当前 Catalog 尚未绑定；确认名称与实际模式后回复‘绑定’，Agent 将保存绑定并继续。只读查询与验收不执行绑定。"),
        "access_mode_mismatch": ("access_mode", "插件实际模式与 Runtime 期望模式不一致；核对用户要保留的模式和 Catalog 后明确确认绑定。"),
        "catalog_identity_mismatch": ("catalog", "活动 Catalog 与绑定身份不一致；由用户选择恢复原 Catalog 或明确重绑。"),
        "safe_sandbox_not_registered": ("sandbox", "safe 模式尚未登记 Sandbox；在 Plugin Manager 登记目标 Catalog。"),
        "safe_catalog_mismatch": ("sandbox", "当前 Catalog 与登记的 Sandbox 不匹配；核对目标后在 Plugin Manager 登记。"),
        "catalog_not_authorized": ("access_mode", "Lightroom 插件尚未授权当前 Catalog；查看模式和 Sandbox。"),
    }.get(reason)
    if issue is None and catalog.get("binding_status") == "active_catalog_unknown":
        issue = ("connection", "Bridge 可达，但本次未取得活动 Catalog 身份；恢复有效的只读 probe，保留已有绑定。")
    if issue is None and snapshot["image_presentation"]["config_status"] != "ok":
        issue = ("image_presentation", "图片预算配置不可读；由部署管理员修复配置后重新查看。")
    if issue is None and not snapshot["folder_access"]["config_valid"]:
        issue = ("folder_access", "目录范围配置无效；修复显式配置后再使用目录工具。")
    if issue is None:
        return None
    result = {"topic": issue[0], "message": issue[1],
              "tool": "trlr_get_runtime_config", "arguments": {"topic": issue[0]}}
    name = catalog.get("active_catalog")
    mode = catalog.get("access_mode", {}).get("actual")
    if reason in ("catalog_confirmation_required", "access_mode_mismatch", "catalog_identity_mismatch") and name and mode in ("safe", "full"):
        action = "绑定" if reason == "catalog_confirmation_required" else "重新绑定"
        result["question"] = f"当前 Catalog 是‘{name}’，实际模式为 {mode}。是否{action}并允许 ToneRelay 操作这份 Catalog？回复‘绑定’继续。"
        result["user_reply"] = "绑定"
    return result
