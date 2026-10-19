# 失败与恢复

- `trlr_health` 显示桥不可达或 `catalog_policy.authorized=false` 时停止 Lightroom 操作。`catalog_confirmation_required` 按安全规则完成首次绑定；`catalog_not_open` 时确认 Lightroom 状态；`unsupported_plugin_contract` 时按 bootstrap 诊断加载兼容插件；若 `trlr_confirm_catalog` 不可见且 bootstrap 返回 `bootstrap.runtime_upgrade_required`，升级 Runtime 后刷新 MCP。`access_mode_mismatch` / `catalog_identity_mismatch` 不得自动改模式或静默覆盖绑定：只有用户明确要求改用另一个 Catalog，才列出本机可切换项、提示切换影响、等待同意后用 `trlr_switch_catalog`；用户只要求修复且 Catalog 已活动时才用 `trlr_confirm_catalog`。切换超时或未观察到目标 identity 时，现有绑定保持不变。`safe_sandbox_not_registered` / `safe_catalog_mismatch` 时停止写入；用户须在 Plugin Manager 登记目标 Sandbox。不得在工具参数中绕过或提升模式。
- `probe_failed` 或 bootstrap 的 `bootstrap.plugin_probe_unavailable` 只说明未取得可用的 Lightroom probe，当前插件 build 与 Catalog 授权仍未知。先区分 Bridge 健康与 Lightroom 响应，检查已观察的锁屏、应用可用性和作业状态；必要时请用户解锁，再做只读复验。不能从超时推断插件未注册或版本过旧，也不能据此重复注册、重绑 Catalog 或重启有未知作业的应用。配置与教程查询仍可读取，不把它们称为恢复了照片操作。
- revision 冲突时重新调用 `trlr_get_episode`，基于最新状态重新决定；不得只替换 revision 后盲目重放旧计划。
- 超时、掉线或进程中断可能形成未知写入结果。先查询 Episode、问题日志和 Lightroom 实际状态；未知写入结果不得自动重放会创建副本或写 Catalog 的动作。
- 工具返回错误时保留原始错误和调用上下文。需要查看自动记录的问题时用 `trlr_list_issues`；只有额外的人工观察才用 `trlr_report_issue`。
- 停止参数变体渲染只表示请求在当前候选之后停止；读取最终 Episode 状态后才能说明保留了什么。
- 无法证明的能力返回“不支持”或“未验证”，不要静默降级成修改原片、控制 UI 或伪造成功。
