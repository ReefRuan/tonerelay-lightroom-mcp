# ToneRelay 场景分块与 0-Token 批同步工作流指南 (`scene_batch`)

> 本工具包专为**固定色温拍摄的多场景批量修图**设计：把机械循环的「场景分块 / 参数同步 / 批量导出 / 拼图筛错」交给本地 Python（0 LLM Token），大模型只把推理和视觉能力花在**每场景 1 张锚点定调**和**拼图异常片微调**上。

## 运行环境

- Python >= 3.10，依赖仅包含标准库 + `numpy` + `Pillow`（可直接使用 ToneRelay active `.venv` 的 Python，或在工作环境中 `pip install numpy pillow`）。
- 统一入口：当前 skill 目录下的 `scripts/scene_batch/cli.py`（直接传脚本路径即可运行，无需额外设置 `PYTHONPATH`）。
- 若 ToneRelay 使用自定义安装目录而非默认 `%LOCALAPPDATA%\ToneRelay`（macOS 为 `~/Library/Application Support/ToneRelay`），在 `read-anchors` / `plan` / `sync` 命令中添加 `--config "<workbench/config.json>"` 或设置环境变量 `TONERELAY_CONFIG` / `TONERELAY_DATA_ROOT`。

示例（PowerShell；bash/zsh 将变量替换即可）：

```powershell
$PY = "python"
$CLI = "<path-to-skill>/scripts/scene_batch/cli.py"
```

---

## 标准 5 步低 Token 工作流

### 第 1 步：固定色温场景自动分块（0 Token，默认不连 Lightroom）
扫描本地 RAW/JPEG 目录，在 CIE Lab 中性低饱和区域检测环境色温跳变，合并同光源串场，自动挑选每个场景最清晰居中的 `anchor_stem`（默认启用轻量并发 `min(4, CPU 核心数)`，适合本地 SSD；读取慢速存储可加 `--workers 1`）：

```powershell
& $PY $CLI segment --input "<照片目录>" --output "<工作目录>/seg"
```

- **可选从已选片中挑锚点**：若已在 Lightroom 挑完片，可加 `--anchor-flag pick --folder-name "<LR文件夹名>"`（或 `--prefer-stems ...`），在保持全量时间轴分块精度的同时确保每组 `anchor_stem` 落在精选片中。
- 产物：`scenes.json`、`color_barcode.jpg`、`scene_overview.jpg`
- **零原图重解码调参**：如果已生成 `scenes.json` 且想微调分块参数（如 `--wb-jump`、`--cluster-threshold`、`--sensitivity`），用 `--resegment` 直接读缓存特征调优（可加 `--render-overview` 刷新概览图）：
  ```powershell
  & $PY $CLI segment --resegment "<工作目录>/seg/scenes.json" --wb-jump 7.0 --render-overview --output "<工作目录>/seg_v2"
  ```
- **Agent 操作**：只查看 `color_barcode.jpg` 和 `scene_overview.jpg`（2 张图）确认分块合理与每个场景的 `anchor_stem`。

### 第 2 步：大模型只精修每个场景的 1 张锚点图（`apply-anchor` / ToneRelay MCP）
- 对每个场景的 `anchor_stem`（例如 `S01`..`S0N`，通常只需 4～8 张）在工作虚拟副本上调色至满意。
- **重要（未改过 `"As Shot"` RAW 白平衡直通）**：在未改动过的 `"As Shot"` RAW 上，ToneRelay MCP 的 `trlr_apply_settings` 会拦截 `Temperature`/`Tint` 写入；**直接使用 `cli.py apply-anchor` 成对传入 `Temperature` 与 `Tint`**，一条命令完成 `WB=Custom` 写入、`0 mismatch` 回读校验与可选预览图导出：
  ```powershell
  & $PY $CLI apply-anchor `
    --folder-name "<LR文件夹名>" --stem "<锚点文件名不含后缀>" `
    --settings '{"Temperature": 4500, "Tint": 12, "Exposure2012": 0.25, "Highlights2012": -55, "Shadows2012": 35}' `
    --render "<工作目录>/anchor_S01.jpg"
  ```
- 已有非保护虚拟副本时，直接沿用该 `vc_uuid` 修改，不要重复新建副本。

### 第 3 步：从 Lightroom 回读锚点配方并生成同步计划（0 Token）
```powershell
& $PY $CLI read-anchors `
  --scenes "<工作目录>/seg/scenes.json" `
  --folder-name "<LR文件夹名>" `
  --output "<工作目录>/scene_recipes.json"

& $PY $CLI plan `
  --scenes "<工作目录>/seg/scenes.json" `
  --recipes "<工作目录>/scene_recipes.json" `
  --folder-name "<LR文件夹名>" `
  --flag pick `
  --auto-exposure --auto-nr `
  --output "<工作目录>/sync_plan.json"
```

- `--flag` 支持 `pick`（默认，仅旗标精选）、`unflagged`（未标记）、`reject` 或 `all`（按需适配三层挑片法）。
- 若某些锚点是手动指定的副本 UUID，可用 `--anchor S01=<vc_uuid> --anchor S02=<vc_uuid>` 显式指定。
- `read-anchors` 默认**显式输出锚点的 `ToneCurvePV2012*`（含默认线性 `[0, 0, 255, 255]`）**，以便覆写既有工作虚拟副本时彻底清除残留旧曲线（不需要时加 `--omit-default-curves`）；默认**不带入裁剪参数 `Crop*`**（需要时加 `--include-crop`）。
- `--auto-exposure` 与 `--auto-nr` 根据同组照片相对锚点的 EXIF 亮度（BV）与 ISO 差异做有界自动曝光/降噪微调，消耗 0 Token。

### 第 4 步：0-Token 批量同步写入 + 串行导出（禁止高频轮询）
先用 `--dry-run` 核对数量（零写入）：
```powershell
& $PY $CLI sync `
  --plan "<工作目录>/sync_plan.json" `
  --state "<工作目录>/sync_state.json" `
  --output-dir "<导出目录>" `
  --dry-run
```
确认后正式执行（严格单线程串行 `command_batch`，断点续传，自动跳过保护副本，决不改原片 master；同时向 `stderr` 输出进度与 ETA，并在 `sync_state.json` 顶部写入 `progress` 摘要）：
若需要**限制导出文件大小（等效 Lightroom「文件大小限制为: N K」的 sRGB JPEG）**，加上 `--max-file-kb 2000`（可选配合 `--max-edge 4096` 控制长边并加速渲染）：
```powershell
& $PY $CLI sync `
  --plan "<工作目录>/sync_plan.json" `
  --state "<工作目录>/sync_state.json" `
  --output-dir "<导出目录>" `
  --max-file-kb 2000 `
  --batch-size 12
```
> **零轮询省 Token 原则**：Lightroom 串行导出通常为 6–10 秒/张（250 张约 30–50 分钟）。启动 `sync` 后应等待进程退出通知，**避免每 1–2 分钟唤醒模型轮询状态**；仅在超时未结束或粗粒度间隔（≥ 10–15 分钟）时查看一次 `sync_state.json` 中的 `progress` 字段即可。
> 若之前已经调完色，只需要把已调好的虚拟副本按体积上限重新导出，可直接运行 `--export-only --max-file-kb 2000`（0 次参数写入，仅串行重导）。

### 第 5 步：全量导出拼图质检 + 旋转缩放裁剪突出主体（少 Token 闭环）
```powershell
& $PY $CLI audit `
  --input "<导出目录>" `
  --scenes "<工作目录>/seg/scenes.json" `
  --grid `
  --output "<工作目录>/audit"
```

- **Agent 操作**：
  1. 查看 `outliers_sheet.jpg` 和分页 `audit_sheet_*.jpg`（`--grid` 会在每张图上叠加浅色三分线、水平中线与 `0.2 / 0.5 / 0.8` 坐标刻度，便于直接看清地平线歪斜角度、边缘杂物与主体位置）。
  2. 把需要**旋转水平 / 缩放裁剪突出主体 / 微调曝光或色温**的照片写进 `overrides.json`：
     - **建议使用语义 `"crop"` 字段**（由 `crop_math.py` 自动处理横图/竖图 `DA`/`BC` 坐标转换与旋转内接边界，避免黑边或横竖坐标颠倒）：
       - `angle`：水平校正旋转角度（度，范围 `-45..45`，例如 `-1.5` 或 `1.8`）
       - `zoom`：缩放放大倍数（`>= 1.0`，例如 `1.25` 表示裁去外围 20% 杂景突出主体）
       - `center_x` / `center_y`：画面主体中心在**正向显示画面**中的归一化坐标（`0..1`，默认 `0.5`）
       - `aspect`：裁剪比例，默认 `"original"` 保持原片比例（也可写 `"3:2"` / `"2:3"` / `"4:3"` / `"1:1"` / `"free"`）
       - 或者直接用 `box: [left, top, right, bottom]`（正向显示画面的归一化框，与 `aspect` 配合）
     ```json
     {
       "IMG_0102": {
         "Exposure2012_delta": -0.35,
         "Highlights2012": -75,
         "crop": {"angle": -1.4, "zoom": 1.25, "center_x": 0.48, "center_y": 0.45, "aspect": "original"}
       },
       "IMG_0158": {
         "crop": {"box": [0.08, 0.05, 0.88, 0.92], "angle": 1.2, "aspect": "original"}
       },
       "IMG_0210": {"Temperature": 4100, "Tint": 18}
     }
     ```
  3. 重新 `plan --overrides overrides.json` 并执行 `sync --stems IMG_0102,IMG_0158,IMG_0210 --max-file-kb 2000`：**只重做和重导这几张图**，`sync_state.json` 会自动跳过未改动照片。

---

## 严禁与推荐实践
1. **大批量任务优先在新对话启动，并严禁高频轮询后台 `sync`**：避免把长历史缓存带入每轮调用，也避免在 Lightroom 导出期间反复唤醒轮询浪费 Token。
2. **避免大目录全量高分辨率看图**：先看 `color_barcode.jpg` / `scene_overview.jpg` / `outliers_sheet.jpg` / `audit_sheet_*.jpg` 拼图定位问题，仅对少数存疑或锚点单张看图。
3. **不要开多个子代理同时抢 Lightroom Bridge**：Lightroom 队列是严格单线程的，并行子代理会导致 `probe_client` 超时重试。
4. **不要用大模型循环逐张调用 `trlr_apply_settings` / `trlr_export_image`**：20 张以上全部走 `scene_batch` `sync`。
5. **重做全片时无需新建第二批副本**：`plan` 会自动识别并复用每张图现有的非保护工作虚拟副本，`read-anchors` 默认显式写出曲线键以清零残留旧曲线。
6. **慢速存储可随时切回单线程顺序扫描**：常规在本地 SSD 上运行即可用默认并发；若临时扫描慢速外接盘或网络盘，在 `segment` / `audit` 中传 `--workers 1`。
