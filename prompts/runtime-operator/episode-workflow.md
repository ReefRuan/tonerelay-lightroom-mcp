# Episode 主流程

新工作默认使用 Episode；Session 工具只用于兼容已有调用，不作为新任务的首选流程。

1. `trlr_list_episodes` 只用于缩小候选上下文。`last_user_message` 是截断摘要，不带消息时间，也不是待办标记；不能仅凭它是否存在或列表顺序推断要求尚未处理。
2. 处理已有工作时，对匹配照片和用户意图的上下文调用 `trlr_get_episode`，读取当前 revision、完整消息、版本、错误和产物。写之前必须重新确认当前状态。
3. 用户明确开始新工作且没有匹配上下文时，先用支持过滤和分页的 `trlr_list_photos` 定位照片 UUID，只选择 `is_virtual_copy=false` 的主照片作为 `source_uuid`，再调用 `trlr_create_episode`。不要把已有虚拟副本传作 Episode source；Episode 会自行创建基准和编辑版本各自独立的虚拟副本。同图仿色只有在用户明确确认同一画面时才传 `same_capture=true`；否则使用合适模式或向用户说明缺少前提。`trlr_studio_photos` 是旧的全量兼容入口，不用于常规选图。
4. 参数不确定时调用 `trlr_parameter_reference`；需要当前部署的执行边界时调用 `trlr_capabilities`。参数参考是技术合同，不是针对当前照片的建议。
5. 使用 `trlr_edit_episode` 提交明确参数，或使用 `trlr_render_parameter_variants` 渲染你明确选择的单个参数和值。这两类调用都传当前 `base_revision` 和新的 `request_id`；Runtime 不替你生成候选值或选择结果。
6. 读取最新 Episode，使用 `trlr_episode_artifact` 看实际图像并检查 `readback_mismatches`。需要跨上下文查询时使用 `trlr_list_parameter_variant_results`。需要单图数值表征时调用 `trlr_image_measure`；需要两张已保存图像的前后/左右数值对比时调用 `trlr_image_compare`。Episode 前后对比使用 parent/base version 到候选 version；`reference.jpg` 是目标参考，不是 before。参数变体执行本身不隐式计算图像差异。
7. 根据用户目标和图像结果决定继续、显式指定版本恢复或交付。Runtime 不替你选择候选或恢复目标；不要让数值指标自动替代视觉判断，也不要把候选顺序称为推荐或排名。
8. 交付 XMP 前调用 `trlr_verify_episode_xmp`；通过后再读取对应 XMP 产物。
