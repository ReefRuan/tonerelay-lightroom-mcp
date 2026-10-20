# 证据与表述边界

- **预览与执行**：快速缩略图是 `unverified`，ICC/EXIF 声明不证明颜色或最新编辑状态；严格验收用受控 Episode 渲染。此限制用于判断证据，普通看图不复述元数据说明。写入以逐键回读为准，如实报告 `readback_mismatches` 中被夹取、忽略或未稳定的键，不用“等效参数”掩盖。
- **实时参数**：`trlr_get_photo_settings` 返回绑定 Catalog 中指定 UUID 的完整 SDK 设置；稳定的两次读回只证明观察期间参数一致，不保证此后 UI 未变化。它不产生图像，色彩证据为不适用；输出的 XMP 序列化须连同遗漏字段查看，不代表 Episode 版本快照、原生导入或 XMP 回放验证。
- **前后关系**：Episode 使用 parent/base version → candidate version；`reference.jpg` 是目标参考，不是 before。由 Agent 显式选两侧并解释结果。`trlr_episode_artifact` 捕获并校验已保存渲染的源字节后回放，不请求 Lightroom；返回图像可经裁切、缩放或重编码，源渲染哈希与交付图像哈希须区分；`v0.jpg` 可作为源基准证据。图像比较与区域比较仅接受这些经过哈希校验的 Episode 渲染，不接受快速缩略图或目录文件。
- **版本参数**：`trlr_episode_diff` 只比较已保存的累计提交参数、Develop 快照与各版本回读异常，不读取实时照片、不做图像比较。重演产生的新版本仍须检查实际回读与渲染；相同提交参数不证明相同图像或原生 XMP 导入成功。
- **整体测量**：`trlr_image_measure` 是单图下采样后的确定性、手工数值表征，不是 ML embedding。`trlr_image_compare` 对两张服务器已保存图像在解码后原生尺寸测量，返回右减左差值；aligned 模式需调用方已确认同一画面与对齐。两者不共用顶层 schema，也不保证同图数字相同。MAE、p95、直方图等描述该条件下的差异，不是审美评分或肤色、蒙版、跨图效果的证明。
- **色块与区域**：`trlr_color_patch_measure` 对指定 ROI 取 sRGB8 中位数；只有可追溯且条件匹配的参考值才能支持准确性解释。`trlr_digital_color_references` 只转换调用方给定的 sRGB/Lab 数值，不证明实体卡、照明或真人肤色。`trlr_color_region_compare` 在同图两个已保存版本复用基线采样坐标，须检查小框仍落在同一材质和位置；这里的 `ΔE00` 是变化量，不是准确率或调色方向。
- **白平衡**：快照中的 RAW `Temperature` K 值是处理设置，不是实测光源色温；渲染文件的 `IncrementalTemperature` 不是 K。结合相同光区的中性、肤色与风格锚点解释，不把肤色推向固定 RGB。
- **变体与 XMP**：参数变体只证明指定值从固定基准被执行和回读，不隐式计算图像差异或选择最佳值；需要差异再显式 compare。`trlr_verify_episode_xmp` 只验证相同继承基准的新副本经 SDK 回放后的渲染，不验证原生预设文件导入或其他照片。一次渲染不自动形成通用知识。
