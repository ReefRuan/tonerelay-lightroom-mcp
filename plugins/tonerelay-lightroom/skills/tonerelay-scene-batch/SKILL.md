---
name: tonerelay-scene-batch
description: 当用户需要对大批量（20+ 张）固定色温多场景拍摄的照片在 Lightroom Classic 中批量修图、按光源场景分段并只精修每组锚点图、0-Token 批量同步参数、按体积上限串行导出或拼图质检与裁剪微调时使用。普通单张/少量修图、导入或安装诊断请用 tonerelay-lightroom。
---

# ToneRelay Scene Batch（场景分块与 0-Token 批修）

把机械循环的**场景分块、批量写参、串行导出、拼图算子质检**交给本 skill 自带的本地 Python 工具（0 LLM Token）；大模型只聚焦两件事：**每个光源场景精修 1 张锚点虚拟副本** + **看拼图挑选异常片输出 `overrides.json`**。

## 前置条件

1. Lightroom Classic 已打开目标 Catalog，且 ToneRelay `lr-ops` Bridge 处于运行状态。
2. Python 3.10+ 环境已安装 `numpy` 与 `Pillow`（可优先复用 ToneRelay 运行时的 `.venv`，或在当前 Python 环境执行 `pip install numpy pillow`）。
3. 将当前 skill 目录下的 `scripts/scene_batch/cli.py` 记作 `$CLI`（支持直接传脚本路径运行，无需额外设置 `PYTHONPATH`）。
4. 连接配置按 `--config <config.json>` -> `TONERELAY_CONFIG` -> `TONERELAY_DATA_ROOT` -> 系统默认应用数据目录（Windows `%LOCALAPPDATA%\ToneRelay`，macOS `~/Library/Application Support/ToneRelay`，Linux `~/.local/share/ToneRelay`）-> `127.0.0.1:17777` 顺序自动发现。

## 5 步低 Token 标准流程

完整参数说明、EXIF 曝光/降噪补偿与 `overrides.json` / 语义裁剪 Schema 见 [references/workflow.md](references/workflow.md)。

1. **固定色温场景分段（0 Token，不连 Lightroom）**
   ```bash
   python "$CLI" segment --input "<照片目录>" --output "<工作目录>/seg"
   ```
   只读取生成的 `color_barcode.jpg` 与 `scene_overview.jpg`（2 张图）检查分块和每组 `anchor_stem`；调参时加 `--resegment "<工作目录>/seg/scenes.json"` 复用缓存特征免重复解码。
2. **仅精修每场景 1 张锚点图（ToneRelay MCP）**
   - 通过 ToneRelay MCP 在每组的 `anchor_stem`（通常 4～8 张）工作虚拟副本上调色至满意。
   - **白平衡提示**：ToneRelay MCP `trlr_apply_settings` 在未改动过的 `"As Shot"` RAW 上若只传 `Temperature` 或只传 `Tint` 会被拦截；通过本工具批量同步或同时成对传入 `Temperature + Tint` 时，`lr-ops` `apply_candidate` 会自动切换 `Custom` 白平衡并双双生效。
3. **回读锚点配方并生成同步计划（0 Token）**
   ```bash
   python "$CLI" read-anchors --scenes "<工作目录>/seg/scenes.json" --folder-name "<LR文件夹名>" --output "<工作目录>/scene_recipes.json"
   python "$CLI" plan --scenes "<工作目录>/seg/scenes.json" --recipes "<工作目录>/scene_recipes.json" --folder-name "<LR文件夹名>" --flag pick --auto-exposure --auto-nr --output "<工作目录>/sync_plan.json"
   ```
4. **0-Token 批量同步写入 + 串行导出**
   先 `--dry-run` 核对数量，再正式执行（自动复用非保护虚拟副本、跳过保护副本、支持断点续传；可加 `--max-file-kb 2000` 匹配 2 MB 体积限制导出，`--export-only` 仅重导）：
   ```bash
   python "$CLI" sync --plan "<工作目录>/sync_plan.json" --state "<工作目录>/sync_state.json" --output-dir "<导出目录>" --max-file-kb 2000
   ```
5. **拼图质检与异常片局部返修（少 Token 闭环）**
   ```bash
   python "$CLI" audit --input "<导出目录>" --scenes "<工作目录>/seg/scenes.json" --grid --output "<工作目录>/audit"
   ```
   查看 `outliers_sheet.jpg` 与分页 `audit_sheet_*.jpg`（`--grid` 叠加三分线、水平中线与 `0.2/0.5/0.8` 刻度），将微调与语义裁剪 `"crop": {"angle", "zoom", "center_x", "center_y", "box", "aspect"}` 写入 `overrides.json`，再重跑 `plan --overrides ...` 与 `sync --stems ...` 仅重导改动照片。

## 硬性纪律

- **严禁多线程抢占 Lightroom Bridge**：`lr-ops` 是严格单线程串行队列，不要并行启动多个子代理调用 ToneRelay MCP 或并发执行 `sync`。
- **20 张以上禁止大模型循环调 `trlr_apply_settings` / `trlr_export_image`**：全部收敛为 `cli.py sync`。
- **避免全量原图看图**：先看分块概览图与拼图定位异常片，再按需单张微调。
- **默认并发与慢速盘降级**：`segment` / `audit` 默认使用轻量并发 `min(4, CPU 核心数)`（适合本地 SSD）；若读取外接慢速盘或网络共享盘，加 `--workers 1` 切回顺序单线程扫描。
