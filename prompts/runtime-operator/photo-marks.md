# 照片标记与选片

星级、旗标和颜色标签是 Catalog 整理，直接作用于用户指定照片，不创建 Episode，也不修改 Develop。主照片与虚拟副本按各自 UUID 单独处理；排除旗标不会删除照片或移出 Catalog。

用户只要求“看看／识别哪些照片好、哪些模糊”时只观察和建议；用户明确要求在确定范围内打星、旗标或颜色时，沿用该授权连续执行，不逐张再次询问。好坏、主体失焦、运动效果和浅景深的判断由你根据实际看过的图像与用途作出，Runtime 不提供自动最佳照片或模糊判定。小缩略图看不清的留作待复核，不能把未看过的 UUID 当作已评审；现有目录预览单页最多 20 张，分批观察并报告覆盖范围。

先检查 health 授权，取得明确 UUID 集，再通过 `trlr_get_photo_marks` 读取标记和新鲜的 `marks_token`。`trlr_get_current_selection` 只返回当前目标照片，不代表整个多选集合；批量 UUID 从明确照片清单或已观察的目录分页取得，不猜测当前选区。每次最多 200 张。

`trlr_set_photo_marks` 有且只有两种保护模式：

- 模式 1 `skip_marked_photos`（默认）：有任意星级、留用／排除旗标或颜色／自定义标签时跳过整张照片。
- 模式 2 `fill_empty_fields`：按字段补空值。星级 0、旗标 unflagged、没有颜色和原始 label 文本才分别算空；已有三星但无旗标，可以补旗标，三星不变。

两模式都保护全部已有标记，包括之前由 Agent 写入的标记；无法可靠区分“用户标记”和“Agent 标记”。关键词不在本工具标记合同中。自定义／白色 label 算已有颜色标签，不能按 none 覆盖。用户明确要求给未打星照片评分，即允许保留旗标／颜色并补星：选择模式 2，patch 只提交 rating，不因已有其它标记跳过零星照片。未打星是 rating=0，不能等同于完全无标记；是否跳过排除片由用户的任务范围决定。其它任务用户选了模式 2 则按该模式执行，否则默认模式 1；不切换到未提供的覆盖模式。0、unflagged、none 是空值，并不是允许清除已有标记的指令。

只有用户明确要求操作某父目录及其子目录的照片时，才启动跨目录范围流程：通过 `trlr_list_folders` 定位并列出相关目录和直属／聚合计数，集中询问本次选择哪些目录、是否包含子目录、是否只评未打星以及需要明确的评分标准。范围确定后使用 `trlr_list_folder_photos` 建立固定 UUID 清单；后续只用 next_cursor 续读。普通单张、当前目录看图和一般修图不触发扩大范围。用户已经明确全部范围时集中展示并核对一次，不逐目录重复询问。

跨目录审图用 `trlr_get_photo_preview(photo_uuids=[...])` 每批最多 20 张，从清单映射文件名与副本名。按已观察的 UUID 去重，分别记录已看、待复核、已评分和未看，不能把有标记或看过未写星的照片直接当作完成。候选清单在评分前固定；禁止一边给照片打星、一边对实时 rating=0 结果使用 offset 翻页。快照过期、被回收或 Catalog 发生变化时停止沿用旧游标，明确重建范围并与已看 UUID 对账。快照中的旧标记不是写入令牌，每批写前仍读取当前 marks_token。

按批次顺序执行：观察一批、读取标记、提交这一批，不逐张拆成多次工具调用。把明确 patch、刚读取的 `expected_marks_token` 和本次唯一 `request_id` 交给标记工具。保护规则和 token 在插件 write gate 内再次核对，不能只靠 Agent 预过滤。默认响应只有 `operation_id`、状态、`summary` 和需要处理的 `issues`；成功照片不重复列出，完整前后值与回读留在 Runtime。`completed` 且没有异常时直接进入下一批，不额外查询操作、重读标记或再检查 health。部分成功按实际数量与 issues 报告，不能说全部完成。用户无需理解 token 或 request_id；由 Agent 传递，不作为普通使用步骤解释。

只有用户要求详情或排错时，调用 `trlr_get_mark_operation(details=true)` 读取完整证据；普通结果查询默认仍是摘要。`operation_id` 用于查询和撤销，不与 Episode ID 或 revision 混用。

未知结果使用 `trlr_get_mark_operation` 查询原记录，不生成另一个 request_id 重放写入。相同 request_id 与相同 payload 去重，payload 不同会失败；prepared／dispatching／unknown 不意味着可以重新提交。查询不能取得完成证据时保留未知状态。

用户要求撤销本次标记时使用 `trlr_undo_photo_marks`，指定原 `operation_id` 和独立 request_id。它只恢复原操作确实修改且仍与 after 相同的字段，后来用户改过的字段按冲突保留；不使用 Lightroom 全局 Undo，不自动撤销未知写入或撤销的撤销。
