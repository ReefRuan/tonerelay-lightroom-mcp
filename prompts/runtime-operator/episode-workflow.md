# Episode 主流程

编辑工作使用 Episode。只查询当前照片参数时，先用当前选片或照片列表确定 UUID，再用 `trlr_get_photo_settings` 读取实时完整 Develop 设置；不为只读查询创建编辑上下文。

1. **定位上下文**：用户给出 Episode 时直接 `trlr_get_episode`；否则用 `trlr_list_episodes` 缩小候选，再读取与照片及意图匹配的完整状态。列表中的 `last_user_message` 是无时间的截断摘要，不是待办标记，不能凭它或排序推断未完成任务。
2. **新建**：用户已授权开始编辑、且没有应继续的上下文时，用支持过滤和分页的 `trlr_list_photos` 核对来源 UUID，再 `trlr_create_episode`。来源可以是主照片或用户指定的虚拟副本，使用该来源当前显影状态作基准；Episode 为每个版本另建独立虚拟副本，不修改来源。不只凭同名猜 UUID。普通编辑显式用 `mode="source_edit"`；同图仿色须用户确认同一画面，并提供参考 UUID 与 `same_capture=true`，尺寸相同不证明此前提。不使用旧的全量 `trlr_studio_photos` 常规选图。
3. **执行**：读取当前 revision、完整消息、版本、错误和产物；按用户目标设计明确参数。用 `trlr_edit_episode` 编辑，或 `trlr_render_parameter_variants` 渲染明确选择的单个参数和值，遵守 revision/request-id 规则。异步操作读取 `trlr_get_episode` 的实际进度，未完成就不提交下一次冲突写入或提前宣称完成。
4. **观察**：读最新 Episode，检查 `readback_mismatches`，用 `trlr_episode_artifact` 查看实际版本图像。按任务需要选择 `trlr_image_measure`、`trlr_image_compare` 或局部测量；跨上下文查询已有变体记录用 `trlr_list_parameter_variant_results`。不为每次编辑固定调用所有测量工具。
5. **决定与交付**：依据用户目标和图像决定继续、用 `trlr_restore_episode` 恢复明确版本或交付。候选顺序及数值不代表 Runtime 推荐。回复说明实际版本、观察到的变化与尚未验证项；用户要求看图时遵守正文交付规则。交付 XMP 前调用 `trlr_verify_episode_xmp`，通过后读取对应产物。

Episode 可在创建时附加不超过 120 字符的 `label`，之后用 `trlr_message_episode` 配合最新 `base_revision` 和唯一 `request_id` 更新；空串清空标签。标签用于识别上下文，不是待办标记，也不会自动唤醒 Agent。

需要比较任意两个已保存版本的参数时，显式指定版本调用 `trlr_episode_diff`，区分提交参数差异、快照差异与各自回读异常。`observed_revision` 是读取时刻的 revision，可能与并发写入后的最新 revision 不同；diff 不等待正在进行的编辑。需要把某版本的累计参数重新执行为新版本时，用 `trlr_replay_episode` 并遵守 revision/request-id 规则；它从同一 Episode 基准新建隔离副本，不把当前照片状态当成原版本，也不改写旧版本。只需读取历史图像时直接取产物，不重演。

比较同一 Episode 的两个版本图像时，`trlr_image_compare` 两侧可分别使用 `{"episode_version":{"episode_id":"…","version":N}}`。它仍读取经哈希校验的已保存渲染；调用方仍须建立语义正确的比较关系，`aligned` 不会因使用版本便捷形式而自动证明画面对齐。

## 导入

导入是 Catalog 写入。在调用 `trlr_import_photos` 之前，必须向用户展示将导入的文件清单（数量、来源路径）、是否移动，以及移动后的目标路径组织，并得到用户明确同意后才执行。`safe` 与 `full` 都遵守 Catalog 授权和身份绑定。

用户只说“导入这些照片”而未指认具体文件时，先用 `trlr_folder_roots` / `trlr_folder_scan` 列出候选给用户挑，不猜路径。扫描截断或递归范围不清时先补齐清单；目录 key 不能当成本机绝对路径，缺少所选文件的明确 Server 路径时请用户提供。

默认建议不移动（`move=false`），源文件原地保留。`move=true` 必须单独征得用户对移动的明确同意，并明确指定 `move_root`；移动不可逆。向用户说明目标按 EXIF 拍摄时间组织为 `<move_root>/<YYYY>/<YYYY-MM-DD>/<原文件名>`，日期未知进入 `unknown-date/`，同名加 `-2` 等后缀；返回的实际路径才是最终落点。

导入完成后逐文件报告成功的 master UUID、跳过原因和错误。移动失败时说明源文件是否保留及已创建的目标副本；未知 Catalog 写入结果如实报告，不另换 request_id 自动重放。相同 request_id 只读取原批次记录；运行或中断记录不代表导入已完成。
