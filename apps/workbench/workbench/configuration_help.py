"""Configuration entry points and factual tutorials delivered with live state.

This content is available without repository files or a network documentation
site. It describes existing configuration mechanisms; it grants no write
permission and never interpolates host paths, URLs or credentials.
"""

from __future__ import annotations

from copy import deepcopy


_GUIDES = {
    "access_mode": {
        "title": "safe / full 访问模式",
        "example": "查看 safe 模式，并告诉我怎么设置",
        "read_fields": ["catalog.access_mode", "catalog.authorized", "catalog.reason"],
        "managed_by": "Lightroom Plugin Manager",
        "editable_in_conversation": False,
        "description": "safe 只允许已登记的 Sandbox Catalog；full 允许当前 Catalog。两种模式都可能写入 Catalog，并保留身份绑定、虚拟副本和回读检查。期望值不等于已保存：expected 是当前有效期望模式，可能来自持久 Target、环境或默认值；当前接口未报告该值的确切来源与持久保存状态，仅凭该值不能证明已执行保存。",
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
            "回到对话查看 Sandbox，核对已登记且匹配当前 Catalog；首次使用仍须确认 Runtime 的 Catalog 绑定。",
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
            "让 Agent 列出 Catalog，查看当前活动项与可切换项。",
            "首次绑定已打开的 Catalog 时，确认具体 Catalog 后使用 trlr_confirm_catalog。",
            "切换另一 Catalog 时，先展示准确名称和位置提示、说明可能中断当前操作，取得用户明确同意后才使用 trlr_switch_catalog。",
            "Runtime 等待 Lightroom 插件确认活动身份匹配后保存绑定；远端 Target 需要在 Lightroom 主机切换。",
            "切换后查看配置，核对活动与绑定 Catalog 一致且已授权；safe 下仍须登记对应 Sandbox。",
        ],
        "takes_effect": "确认绑定持久化到 targets.json；切换请求只有在插件确认新活动身份后才更新绑定。旧 Episode 不自动迁移。",
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
        "managed_by": "Runtime folder-library configuration",
        "editable_in_conversation": False,
        "description": "没有 folder-library 配置时，默认访问 Runtime OS 账号可访问的路径；显式配置会限制为 roots 中的目录。该范围与 Lightroom 的 safe/full 独立。",
        "steps": [
            "先查看当前访问模式、配置是否有效、根显示名与支持的图片类型。",
            "需要限制范围时，由部署管理员维护 Runtime 数据区的 workbench/folder-library.yaml，或用 LR_XMP_MIMIC_FOLDER_CONFIG 指定限制文件。",
            "配置 roots 列出允许目录；roots 为空会禁止目录访问。扫描仍遵守 OS 权限和隐藏/系统目录过滤。",
            "修改默认配置文件后重新查看目录范围；修改环境变量指定的文件位置后重启 MCP。配置错误须修复后再使用目录工具。",
        ],
        "takes_effect": "默认配置文件改动在后续读取时检测；环境变量变更需要重启 MCP。扫描不访问 Lightroom，RAW 预览的 Catalog 写入仍须显式允许。",
        "verification_fields": ["folder_access.mode", "folder_access.roots", "folder_access.config_valid"],
    },
    "connection": {
        "title": "Lightroom / Bridge 连接",
        "example": "查看连接配置，给我连接排查教程",
        "read_fields": ["target", "catalog.reason"],
        "managed_by": "Runtime installation/environment",
        "editable_in_conversation": False,
        "description": "配置查询显示本机/远端类型与 Bridge 连通状态。Bridge 可达与 Lightroom 插件可响应是两个状态。",
        "steps": [
            "确认 Lightroom Classic 已打开，ToneRelay 插件在 Plug-in Manager 中启用，lr-ops Bridge 已启动。",
            "查看配置：target.reachable 表示 Bridge 可达；catalog.reason 为 probe_failed 时，Bridge 可达但未取得有效的 Lightroom 插件响应。",
            "活动 Catalog 未知时，binding_status 为 active_catalog_unknown；binding_recorded 独立表示是否记住身份，名称为空不代表未绑定，断线不会清空已有绑定。先恢复有效的只读 probe，再判断活动身份，保留原绑定。",
            "连接地址由安装环境或 targets.json 管理；LROPS_BASE 仅初始化尚无持久 Target 的默认连接。",
            "当前 Lightroom 插件固定连接本机 loopback 的 17777 端口；不要单独改变 Bridge 端口后假定插件会跟随。",
            "由部署管理员修改连接配置后重启受影响的 MCP/Bridge；若更新了 Lua 插件，正常退出并重新打开 Lightroom，再查看配置。",
        ],
        "takes_effect": "安装环境或 targets.json 的手工变更需要重启 MCP；Bridge 启动环境变更需要重启 Bridge。",
        "verification_fields": ["target.reachable", "catalog.reason", "catalog.authorized"],
    },
    "credentials": {
        "title": "Bridge 凭据",
        "example": "查看是否配置了 Token，给我配置教程",
        "read_fields": ["target.credential_configured"],
        "managed_by": "Runtime installation/environment and Lightroom plugin",
        "editable_in_conversation": False,
        "description": "只显示当前 Runtime Target 是否配置凭据；该状态不证明 Bridge 和插件的凭据已匹配。凭据值不进入对话。",
        "steps": [
            "通过本机受控安装/配置渠道管理共享令牌，不将令牌粘贴到对话。",
            "部署管理员使 Runtime Target、lr-ops 的 LR_XMP_MIMIC_LOCAL_TOKEN 与插件 local-token.txt 使用相同令牌。",
            "已有 targets.json 时还须同步其中的 Target token；仅更改环境变量不会覆盖持久 Target。",
            "更新后重启 MCP 与 Bridge，并正常退出、重新打开 Lightroom，然后重新查看连接和授权状态。",
        ],
        "takes_effect": "相关进程重新启动后读取；只报告是否配置，不返回值或本机凭据文件路径。",
        "verification_fields": ["target.credential_configured", "target.reachable", "catalog.authorized"],
    },
    "data_root": {
        "title": "Runtime 数据存放位置",
        "example": "告诉我配置保存在哪里，以及如何迁移",
        "read_fields": ["image_presentation.source", "admin_managed"],
        "managed_by": "Runtime installation/environment",
        "editable_in_conversation": False,
        "description": "Runtime 数据根保存 Catalog 绑定、图片预算、任务状态和产物；配置查询不返回本机绝对路径。",
        "steps": [
            "数据根由 LR_XMP_MIMIC_RUNTIME_DATA_ROOT 配置；配置文件位于其 workbench 子目录，如 targets.json、presentation-settings.json。",
            "迁移前停止 MCP 和 lr-ops，并备份运行数据。",
            "由部署管理员迁移现有数据，在 MCP 与 lr-ops 启动环境中设置一致的数据根，并确认 OS 账号权限。",
            "重新启动对应进程，再查看 Catalog 绑定与图片预算是否保留。",
        ],
        "takes_effect": "重启后读取；仅切换到空数据根会呈现首次使用状态。配置查询只说明管理入口与相对文件名。",
        "verification_fields": ["catalog.bound_catalog", "image_presentation.codex_preview_max_edge", "image_presentation.edit_render_max_edge"],
    },
    "execution": {
        "title": "显影参数执行开关",
        "example": "查看参数透传开关，解释怎么配置",
        "read_fields": ["admin_managed"],
        "managed_by": "Runtime environment",
        "editable_in_conversation": False,
        "description": "未知参数透传与结构化参数开关决定参数校验入口，不保证 Lightroom 实际支持这些参数。",
        "steps": [
            "查看 Unknown and structured Develop parameter passthrough 中的 enabled 与 structured_enabled；null 表示未取得状态。",
            "部署管理员通过 LR_XMP_MIMIC_ALLOW_PASSTHROUGH 与 LR_XMP_MIMIC_ALLOW_STRUCTURED 配置开关；两者默认开启，设为 0 或 false 可关闭，设为 1 可开启。",
            "更新 MCP 启动环境并重启 MCP，再查看配置。",
            "具体参数支持条件查看 trlr_parameter_reference；实际执行仍须遵守 Catalog 授权、参数校验与逐键回读。",
        ],
        "takes_effect": "启动环境更新并重启 MCP 后生效；开启透传不绕过 Lightroom 授权或回读验证。",
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
        "probe_failed": ("connection", "Bridge 可达，但未取得有效 Lightroom 插件响应。"),
        "unsupported_plugin_contract": ("connection", "插件协议不兼容；通过安装/恢复渠道更新并重新连接。"),
        "catalog_not_open": ("catalog", "Lightroom 未打开 Catalog；先打开目标，再确认绑定。"),
        "catalog_confirmation_required": ("catalog", "当前 Catalog 尚未绑定；查看清单并由用户确认。"),
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
    return {"topic": issue[0], "message": issue[1],
            "tool": "trlr_get_runtime_config", "arguments": {"topic": issue[0]}}
