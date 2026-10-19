# 证据与表述边界

- `trlr_get_photo_preview` 返回的快速缩略图是 `unverified`，不能证明最新编辑状态。严格验收使用受控 Episode 渲染。
- Lightroom 写入后的逐键回读是事实边界。`readback_mismatches` 中被夹取、忽略或未稳定的键必须如实报告，不能用“等效参数”掩盖。
- `trlr_image_measure` 是单图下采样后的确定性、手工数值表征，不是 ML embedding，也不负责前后关系。`trlr_image_compare` 在两张服务器已保存图像的解码后原生尺寸上返回左右测量、右减左数值差和可选对齐误差；两者不共用顶层 schema，数字不必相同。
- `trlr_color_patch_measure` 对调用方标出的同一批 ROI 逐块取 sRGB8 中位数；有可追溯且条件匹配的参考值时才解释其 `ΔE00`。`trlr_digital_color_references` 只把调用方给的 sRGB/Lab 值转成数字色块，不证明实体卡、现场照明或真人肤色。快速预览的颜色与新鲜度仍未验证；严格比较优先使用已保存的 Episode 渲染。
- 对同一张照片的两个已保存版本，`trlr_color_region_compare` 可把少量粗框变成基线数字色卡，并在候选版复用相同采样坐标；检查返回的选中小框是否仍落在目标材质和相同画面位置。这里的 `ΔE00` 是变化量，不是准确率，也不自动说明应向哪个方向调色。
- Episode 版本的来源会附带 Lightroom 的白平衡回读（若快照有值）。RAW `Temperature` 的 K 数是处理设置，不等于现场实测光源色温；已渲染文件 `IncrementalTemperature` 不是 K。按同光区中性格、肤色格和风格锚点解释前后变化，勿按一个 K 值把所有人的肤色推向固定 RGB。
- 前后关系必须由你按状态显式选择：Episode 使用 parent/base version → candidate version，Session 通常使用 source → round。Episode/Session reference 是目标参考，不是编辑前的 before。Session 可分别用 `trlr_get_preview` 看 source 和 round；看图后的总结和效果判断由你完成，不要声称 compare 已经作出了判断。
- MAE、p95、直方图和通道统计描述特定图像与特定条件下的数值差异，不是审美评分，也不能自动证明肤色、蒙版或跨图适用性。
- `trlr_render_parameter_variants` 只证明指定参数值在固定基准上被实际执行并回读；它不自动计算图像响应，也不表示哪个候选更好。需要数值差异时显式调用 `trlr_image_compare`。
- `trlr_verify_episode_xmp` 只验证相同继承基准下，经 SDK 回放参数后得到的真实渲染；它不验证 Lightroom 原生预设文件导入，也不证明其他照片有效。
- 一次真实渲染只证明该次执行，不自动升级为通用知识、建议或审美结论。
