# Episode 主流程

新工作使用 Episode；已有 Session 只按兼容边界继续。

1. **定位上下文**：用户给出 Episode 时直接 `trlr_get_episode`；否则用 `trlr_list_episodes` 缩小候选，再读取与照片及意图匹配的完整状态。列表中的 `last_user_message` 是无时间的截断摘要，不是待办标记，不能凭它或排序推断未完成任务。
2. **新建**：用户已授权开始编辑、且没有应继续的上下文时，用支持过滤和分页的 `trlr_list_photos` 核对来源 UUID，再 `trlr_create_episode`。来源可以是主照片或用户指定的虚拟副本，使用该来源当前显影状态作基准；Episode 为每个版本另建独立虚拟副本，不修改来源。不只凭同名猜 UUID。普通编辑显式用 `mode="source_edit"`；同图仿色须用户确认同一画面，并提供参考 UUID 与 `same_capture=true`，尺寸相同不证明此前提。不使用旧的全量 `trlr_studio_photos` 常规选图。
3. **执行**：读取当前 revision、完整消息、版本、错误和产物；按用户目标设计明确参数。用 `trlr_edit_episode` 编辑，或 `trlr_render_parameter_variants` 渲染明确选择的单个参数和值，遵守 revision/request-id 规则。异步操作读取 `trlr_get_episode` 的实际进度，未完成就不提交下一次冲突写入或提前宣称完成。
4. **观察**：读最新 Episode，检查 `readback_mismatches`，用 `trlr_episode_artifact` 查看实际版本图像。按任务需要选择 `trlr_image_measure`、`trlr_image_compare` 或局部测量；跨上下文查询已有变体记录用 `trlr_list_parameter_variant_results`。不为每次编辑固定调用所有测量工具。
5. **决定与交付**：依据用户目标和图像决定继续、用 `trlr_restore_episode` 恢复明确版本或交付。候选顺序及数值不代表 Runtime 推荐。回复说明实际版本、观察到的变化与尚未验证项；用户要求看图时遵守正文交付规则。交付 XMP 前调用 `trlr_verify_episode_xmp`，通过后读取对应产物。
