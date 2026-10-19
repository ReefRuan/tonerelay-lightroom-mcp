"""Runtime 参数操作文档：参数契约 + 契约的配套解释。

两类内容都归 Runtime，真源只有这一份：

* **参数契约**——字段名、类型、单位、允许范围、输入条件、校验规则与支持限制。
  :data:`PARAMETERS` 定义它，:func:`parameter_records` 生成查询用的记录。
  执行校验（``settings.validate``）、能力元数据（``engine.capabilities``）、
  MCP 参数参考入口也读取这里，不再维护第二份字段清单或范围。
* **契约的配套解释**——字段技术含义、怎样填写、特殊情况与兼容限制。以核对过的
  中文短说明集中写在这里，**不从数值范围反推**，也不生成审美或摄影结论。

``settings.BOUNDS``/``CURVES``/``FAMILIES`` 由本模块派生，避免两份分别维护的有效
范围与字段清单。本模块不 import ``settings``，依赖方向单向。

结构化字段是 56 个数值 + 4 条曲线，但那是**当前快照，不是 Lightroom 的能力上限**：
其余 ``crs:`` 字段走透传，没有说明不等于不允许写。
"""

from __future__ import annotations

import hashlib
import json

# --- 曲线约束：执行校验与配套说明共用这一组数字，不再各写一遍 ---
CURVE_MIN_POINTS = 2
CURVE_MAX_POINTS = 16
CURVE_COORD_MIN = 0
CURVE_COORD_MAX = 255

# --- 公共说明：重复的通用警示放这里，减少每条机械重复 ---
RANGE_IS_CONSTRAINT = "数值范围是执行校验约束，不是适合所有照片的推荐幅度。"
INPUT_FROM_SNAPSHOT = "可用字段取决于解析后的基准快照；写入后需回读确认，才知 Lightroom 是否真正应用。"
NATIVE_IMPORT_UNVERIFIED = "原生 XMP 预设导入未经本项目验证，这里的支持说明只覆盖本项目执行链路。"

# 分组：只声明顺序与名称，成员由每条参数的 group 决定（单一真源）。
GROUPS = (
    ("tone", "基础影调"),
    ("color", "整体色彩"),
    ("wb", "白平衡"),
    ("hsl", "HSL 分区"),
    ("curve", "曲线"),
    ("split_toning", "分离色调"),
    ("color_grade", "三轮调色"),
    ("vignette", "裁剪后暗角"),
)

# HSL 的命名规则稳定（<色>色 · <轴>），按规则组合，不手写 24 条。
HSL_AXES = (("Hue", "色相"), ("Saturation", "饱和度"), ("Luminance", "明亮度"))
HSL_COLORS = (("Red", "红"), ("Orange", "橙"), ("Yellow", "黄"), ("Green", "绿"),
              ("Aqua", "青"), ("Blue", "蓝"), ("Purple", "紫"), ("Magenta", "洋红"))

CURVE_KEYS = ("ToneCurvePV2012", "ToneCurvePV2012Red",
              "ToneCurvePV2012Green", "ToneCurvePV2012Blue")
CURVE_TITLE_PARTS = {
    "ToneCurvePV2012": ("主曲线", "RGB 合成通道，同时影响明度与色彩关系。"),
    "ToneCurvePV2012Red": ("红通道曲线", "只影响红通道，用于分通道校正偏色。"),
    "ToneCurvePV2012Green": ("绿通道曲线", "只影响绿通道，用于分通道校正偏色。"),
    "ToneCurvePV2012Blue": ("蓝通道曲线", "只影响蓝通道，用于分通道校正偏色。"),
}

WB_RAW_KEYS = ("Temperature", "Tint")
WB_RENDERED_KEYS = ("IncrementalTemperature", "IncrementalTint")

# 曲线族的配套解释：点数是 C2..C16，坐标 0..255，x 严格递增且首 0 尾 255。
CURVE_DETAILS = (
    f"点曲线输入 {CURVE_MIN_POINTS}..{CURVE_MAX_POINTS} 组整数 (x,y) 坐标点；"
    f"x 与 y 均在 {CURVE_COORD_MIN}..{CURVE_COORD_MAX}，x 严格递增，且首点 x 必须是 "
    f"{CURVE_COORD_MIN}、末点 x 必须是 {CURVE_COORD_MAX}。扁平数组按 [x0,y0,x1,y1,...] 排列。"
)
CURVE_LIMITATIONS = (
    "点数是项目校验约束，不是 Lightroom 的曲线点数上限。",
    "曲线经项目执行链路写入并经回读确认；原生 XMP 预设导入未验证。",
)

# 暗角样式：本地校验只证明数值范围与回读（写入 2，回读 2，
# XMP roundtrip 解析为 2.0），
# 没有证据说明各档位对应的 Lightroom 语义。因此保留数值校验，语义单列为未核实。
VIGNETTE_STYLE_UNVERIFIED_ENUM = (
    "该项目档位在 Lightroom 界面里对应哪种暗角样式，本项目尚无可靠证据，未核实；"
    "此处只承诺数值范围与回读行为，不承诺档位含义。",
)


def _spec(key, group, title, *, unit="LR scale", bounds=None, value_type="number",
          input_type="resolved_snapshot", summary, details=None, limitations=()):
    """一条参数操作文档记录。只填实际有的字段，不建空泛元数据框架。"""
    return {
        "id": key, "parameter": key, "group": group, "title": title, "unit": unit,
        "value_type": value_type, "bounds": None if bounds is None else tuple(bounds),
        "bounds_kind": "curve_shape" if value_type == "curve" else "numeric_constraint",
        "input_type": input_type,
        "summary": summary, "details": details or summary,
        "limitations": tuple(limitations),
    }


def _hsl_specs():
    # 键按字段名排序生成，保持返回顺序稳定。
    labels = {f"{axis}Adjustment{color}": (axis_label, color_label)
              for axis, axis_label in HSL_AXES for color, color_label in HSL_COLORS}
    for key in sorted(labels):
        axis_label, color_label = labels[key]
        yield _spec(
            key, "hsl", f"{color_label}色 · {axis_label}", bounds=(-100, 100),
            summary=f"{color_label}色范围的{axis_label}。只作用在该色相范围内的像素。",
            details=(f"HSL 单一颜色通道的{axis_label}，按 Lightroom 的颜色范围划分，"
                     f"与其它 {len(HSL_COLORS)} 个颜色通道相互独立。"),)


def _curve_specs():
    for key in CURVE_KEYS:
        title, note = CURVE_TITLE_PARTS[key]
        yield _spec(key, "curve", title, unit="0..255 coordinate", value_type="curve",
                    bounds=(CURVE_COORD_MIN, CURVE_COORD_MAX), summary=note,
                    details=CURVE_DETAILS, limitations=CURVE_LIMITATIONS)


# 逐条维护的参数操作文档。数值范围是项目输入校验约束；LR 是否支持、
# 原生 XMP 导入是否成立，分别由 support 与 limitations 说明，不能互相替代。
PARAMETERS = (
    _spec("Exposure2012", "tone", "曝光", unit="EV", bounds=(-5, 5),
          summary="整体明暗，单位 EV。",
          details="整体提亮或压暗，1 EV 约等于一档光圈。会同时影响全图，不是局部提亮。"),

    _spec("Contrast2012", "tone", "对比度", bounds=(-100, 100),
          summary="同时拉开或压缩明暗两端。"),

    _spec("Highlights2012", "tone", "高光", bounds=(-100, 100),
          summary="只回收或加强较亮区域。",
          details="向负方向主要是回收高光细节，正方向加强明亮区域。"),

    _spec("Shadows2012", "tone", "阴影", bounds=(-100, 100),
          summary="只提亮或压暗较暗区域。"),

    _spec("Whites2012", "tone", "白色", bounds=(-100, 100),
          summary="调整接近纯白的端点。",
          details="与高光的作用区间不同：白色偏端点，容易直接推向裁切。"),

    _spec("Blacks2012", "tone", "黑色", bounds=(-100, 100),
          summary="调整接近纯黑的端点。",
          details="与阴影的作用区间不同：黑色偏端点，负值压出纯黑。"),

    _spec("Vibrance", "color", "自然饱和度", bounds=(-100, 100),
          summary="提饱和时对已经高饱和的颜色更保守。"),

    _spec("Saturation", "color", "饱和度", bounds=(-100, 100),
          summary="所有颜色等比例增减饱和度。"),

    # --- 白平衡：RAW 与已渲染文件字段互斥，由执行校验强制 ---
    _spec("Temperature", "wb", "RAW 色温", unit="kelvin", bounds=(2000, 50000),
          input_type="raw",
          summary="RAW 文件的白平衡色温设置，单位开尔文；不是人物所在光区的实测光源色温。",
          details="只适用于解析后仍带 RAW 白平衡字段的源；与 IncrementalTemperature 不能同时出现。混合光、反射光和有色灯光不能由一个 Temperature 值完整描述。",
          limitations=("RAW 与已渲染文件的色温字段不能混用，执行校验会直接拒绝混用。",)),

    _spec("Tint", "wb", "RAW 色调", unit="LR tint", bounds=(-150, 150), input_type="raw",
          summary="RAW 文件的绿-洋红补偿。",
          limitations=("RAW 与已渲染文件的色调字段不能混用，执行校验会直接拒绝混用。",)),

    _spec("IncrementalTemperature", "wb", "JPEG 色温", unit="LR incremental",
          bounds=(-100, 100), input_type="rendered",
          summary="已渲染文件的相对色温偏移。",
          details="用于 JPEG/TIFF 等已渲染源，表示相对当前值的偏移而不是绝对色温。",
          limitations=("RAW 与已渲染文件的色温字段不能混用，执行校验会直接拒绝混用。",)),

    _spec("IncrementalTint", "wb", "JPEG 色调", unit="LR incremental",
          bounds=(-100, 100), input_type="rendered",
          summary="已渲染文件的相对色调偏移。",
          limitations=("RAW 与已渲染文件的色调字段不能混用，执行校验会直接拒绝混用。",)),

    # ---  HSL 24 键按命名规则生成 ---
    *_hsl_specs(),

    # --- 曲线 4 条 ---
    *_curve_specs(),

    # --- 分离色调 5 键 ---
    _spec("SplitToningShadowHue", "split_toning", "分离色调阴影色相", unit="degrees",
          bounds=(0, 360), summary="给阴影区染色的色相。"),
    _spec("SplitToningShadowSaturation", "split_toning", "分离色调阴影饱和度",
          bounds=(0, 100), summary="阴影染色的强度。"),
    _spec("SplitToningHighlightHue", "split_toning", "分离色调高光色相",
          unit="degrees", bounds=(0, 360), summary="给高光区染色的色相。"),
    _spec("SplitToningHighlightSaturation", "split_toning", "分离色调高光饱和度",
          bounds=(0, 100), summary="高光染色的强度。"),
    _spec("SplitToningBalance", "split_toning", "分离色调平衡", bounds=(-100, 100),
          summary="阴影与高光染色的分界倾向。"),

    # --- 三轮调色 9 键 ---
    _spec("ColorGradeMidtoneHue", "color_grade", "三轮调色中间调色相", unit="degrees",
          bounds=(0, 360), summary="中间调染色的色相。"),
    _spec("ColorGradeMidtoneSat", "color_grade", "三轮调色中间调饱和度",
          bounds=(0, 100), summary="中间调染色强度。"),
    _spec("ColorGradeMidtoneLum", "color_grade", "三轮调色中间调亮度",
          bounds=(-100, 100), summary="中间调亮度。"),
    _spec("ColorGradeShadowLum", "color_grade", "三轮调色阴影亮度",
          bounds=(-100, 100), summary="阴影区亮度。"),
    _spec("ColorGradeHighlightLum", "color_grade", "三轮调色高光亮度",
          bounds=(-100, 100), summary="高光区亮度。"),
    _spec("ColorGradeGlobalHue", "color_grade", "三轮调色全局色相", unit="degrees",
          bounds=(0, 360), summary="全图染色的色相。"),
    _spec("ColorGradeGlobalSat", "color_grade", "三轮调色全局饱和度",
          bounds=(0, 100), summary="全图染色强度。"),
    _spec("ColorGradeGlobalLum", "color_grade", "三轮调色全局亮度",
          bounds=(-100, 100), summary="全图亮度。"),
    _spec("ColorGradeBlending", "color_grade", "三轮调色混合", bounds=(0, 100),
          summary="三个亮度区间之间的过渡程度。"),

    # --- 裁剪后暗角 6 键 ---
    _spec("PostCropVignetteAmount", "vignette", "裁剪后暗角强度", bounds=(-100, 100),
          summary="暗角强度，负值压暗四周、正值提亮四周。"),
    _spec("PostCropVignetteMidpoint", "vignette", "裁剪后暗角中点", bounds=(0, 100),
          summary="暗角起始位置离中心的远近。"),
    _spec("PostCropVignetteFeather", "vignette", "裁剪后暗角羽化", bounds=(0, 100),
          summary="暗角边缘的过渡柔和程度。"),
    _spec("PostCropVignetteRoundness", "vignette", "裁剪后暗角圆度", bounds=(-100, 100),
          summary="暗角形状从方形到圆形的倾向。"),
    _spec("PostCropVignetteStyle", "vignette", "裁剪后暗角样式", bounds=(1, 3),
          summary="暗角样式的档位编号；本项目只约束数值范围，档位含义未核实。",
          details=("执行校验只检查取值范围 1..3，不限制为整数枚举 —— "
                   "本地证据（XMP roundtrip 解析为 2.0）显示该字段以数值形式往返，"
                   "收紧为整数会改变已有执行行为且没有证据支持。"),
          limitations=VIGNETTE_STYLE_UNVERIFIED_ENUM),
    _spec("PostCropVignetteHighlightContrast", "vignette", "裁剪后暗角高光对比度",
          bounds=(0, 100), summary="暗角区域内高光的对比表现。"),
)

PARAMETER_IDS = tuple(spec["id"] for spec in PARAMETERS)
PARAMETER_INDEX = {spec["id"]: spec for spec in PARAMETERS}
GROUP_LABELS = dict(GROUPS)
GROUP_IDS = tuple(group_id for group_id, _ in GROUPS)

# 只有真正受结构化校验的字段，而且数值组与曲线组分开 —— 曲线有自己的形状约束，
# 不享受通用上下限语义。透传字段不在这里，也不因此被禁止。
BOUNDS = {spec["id"]: spec["bounds"] for spec in PARAMETERS
          if spec["bounds"] is not None and spec["value_type"] != "curve"}
CURVES = tuple(spec["id"] for spec in PARAMETERS if spec["value_type"] == "curve")
WB_KEYS = WB_RAW_KEYS + WB_RENDERED_KEYS
FAMILIES = tuple(
    (group_id, label, tuple(spec["id"] for spec in PARAMETERS if spec["group"] == group_id))
    for group_id, label in GROUPS
)

# 输入类型由源快照决定，不是参数自身的固定属性：这里只标出必须区分的那一组。
RAW_ONLY_KEYS = WB_RAW_KEYS
RENDERED_ONLY_KEYS = WB_RENDERED_KEYS

_SCOPE_BY_GROUP = {"vignette": "post_crop_spatial"}
PARAMETER_SOURCES = ["project-execution-contract"]


def parameter_records():
    """生成查询用的参数操作文档。数值范围与分组都来自本模块的定义。"""
    records = []
    for spec in PARAMETERS:
        limitations = list(spec["limitations"])
        if spec["value_type"] != "curve":
            limitations.append(RANGE_IS_CONSTRAINT)
        limitations.append(INPUT_FROM_SNAPSHOT)
        limitations.append(NATIVE_IMPORT_UNVERIFIED)
        records.append({
            "id": spec["id"], "kind": "parameter", "parameter": spec["id"],
            "title": spec["title"], "summary": spec["summary"], "details": spec["details"],
            "family": spec["group"], "family_title": GROUP_LABELS[spec["group"]],
            "unit": spec["unit"], "input_type": spec["input_type"],
            "scope": _SCOPE_BY_GROUP.get(spec["group"], "global"),
            "bounds": None if spec["bounds"] is None else list(spec["bounds"]),
            "bounds_kind": spec["bounds_kind"], "value_type": spec["value_type"],
            "limitations": limitations,
            # 四种状态分开表达，不能互相替代：
            # 项目输入校验在下一层 settings.validate；当前照片是否可用看基准快照；
            # 是否真正生效看回读；原生 XMP 导入未经本项目验证。
            "support": {"execution": "enabled", "source_availability": "must_check_snapshot",
                        "native_xmp_import": "unverified"},
            "source_refs": list(PARAMETER_SOURCES), "evidence_status": "project_contract",
        })
    return records


def parameter_record(identifier):
    """按字段名取一条；未知字段报错，不做模糊匹配。"""
    for row in parameter_records():
        if row["id"] == identifier:
            return row
    raise ValueError(f"Unknown parameter: {identifier}")


def is_parameter(identifier):
    return identifier in PARAMETER_INDEX


def curve_point_rule():
    """曲线点数与坐标约束，供执行校验和界面提示共用。"""
    return {"min_points": CURVE_MIN_POINTS, "max_points": CURVE_MAX_POINTS,
            "coord_min": CURVE_COORD_MIN, "coord_max": CURVE_COORD_MAX}


def fingerprint():
    """参数规格的内容哈希。

    用于执行记录核验「当时用的是哪一份契约」，以及查询缓存失效判断。
    这个哈希不构成版本链，只是同一份规格的内容指纹。
    """
    payload = json.dumps(parameter_records(), sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def search(query="", identifier=None):
    """按字段名或名称检索参数操作文档。

    支持两种情况：精确取一个字段名，或按关键词在标题/说明/单位里匹配。
    当前规模不需要语义检索。
    """
    if identifier is not None:
        return [parameter_record(identifier)]
    tokens = [token for token in (query or "").casefold().split() if token]
    if not tokens:
        return parameter_records()
    rows = []
    for row in parameter_records():
        haystack = " ".join(str(row.get(field) or "") for field in
                            ("id", "title", "summary", "details", "unit",
                             "family_title", "value_type", "input_type")).casefold()
        if all(token in haystack for token in tokens):
            rows.append(row)
    return rows
