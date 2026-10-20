"""全量 develop 快照 → 标准 XMP 文本的服务端组装。

把 `get_develop_snapshot` 的全量回读（含曲线/HSL/颗粒/校准等）
序列化成 XMP 文本。组装文本不证明 SDK 回放或原生预设导入通过。

取舍（如实记录在返回值里）：
- 数字 → crs 属性；布尔 → True/False；短字符串 → XML 转义后的属性；
- 数值数组 → 偶数长度时按 LR 曲线约定写成 rdf:Seq 的 "x, y" 点对，
  奇数长度无法配对的跳过；
- 嵌套表（蒙版/修饰）没有可靠的 XMP 属性表示，整体跳过并列入
  skipped_structured_keys —— 归档方能看见丢了什么，而不是静默丢。
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET

# 复用 app 层的常量（只读）：不是显影参数的键不序列化。
from workbench.studio.settings import CURVES, DENY_SETTINGS, WB_KEYS  # noqa: E402

CRS = "http://ns.adobe.com/camera-raw-settings/1.0/"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
ET.register_namespace("x", "adobe:ns:meta/")
ET.register_namespace("rdf", RDF)
ET.register_namespace("crs", CRS)

NAME_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9]{0,60}\Z")
MAX_STRING_LEN = 128
MAX_ARRAY_LEN = 512


def _format_number(value) -> str:
    if type(value) is int:
        return str(value)
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def _is_number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def serialize_full_settings(settings: dict, name: str = "ToneRelay full") -> dict:
    """全量快照 → XMP 文本。返回 {xmp, written_keys, skipped_*}。"""
    if not isinstance(settings, dict):
        raise ValueError("settings must be an object")
    scalars, arrays = {}, {}
    skipped_meta, skipped_structured, skipped_shape = [], [], []
    for key, value in sorted(settings.items()):
        if not isinstance(key, str) or not NAME_RE.match(key):
            skipped_shape.append(key if isinstance(key, str) else repr(key))
            continue
        if key in DENY_SETTINGS:
            skipped_meta.append(key)
            continue
        if _is_number(value):
            scalars[key] = _format_number(value)
        elif type(value) is bool:
            scalars[key] = "True" if value else "False"
        elif isinstance(value, str):
            if len(value) <= MAX_STRING_LEN and "\n" not in value and "\r" not in value:
                scalars[key] = value
            else:
                skipped_shape.append(key)
        elif (isinstance(value, list) and 2 <= len(value) <= MAX_ARRAY_LEN
              and all(_is_number(item) for item in value)):
            if len(value) % 2 == 0:
                arrays[key] = value
            else:
                skipped_shape.append(key)
        else:
            # 蒙版/渐变等结构化值当前不序列化，只登记进
            # skipped_structured_keys；若以后支持结构化 XMP，需另行定义 rdf 合同。
            skipped_structured.append(key)

    root = ET.Element("{adobe:ns:meta/}xmpmeta")
    rdf = ET.SubElement(root, "{" + RDF + "}RDF")
    desc = ET.SubElement(rdf, "{" + RDF + "}Description", {
        "{" + RDF + "}about": "", "{" + CRS + "}PresetType": "Normal",
        "{" + CRS + "}SupportsAmount": "False"})
    if any(k in settings for k in WB_KEYS):
        desc.set("{" + CRS + "}WhiteBalance", "Custom")
    if any(k in arrays for k in CURVES):
        desc.set("{" + CRS + "}ToneCurveName2012", "Custom")
    for key, text in scalars.items():
        desc.set("{" + CRS + "}" + key, text)
    for key, values in arrays.items():
        seq = ET.SubElement(ET.SubElement(desc, "{" + CRS + "}" + key), "{" + RDF + "}Seq")
        for x, y in zip(values[::2], values[1::2]):
            ET.SubElement(seq, "{" + RDF + "}li").text = f"{_format_number(x)}, {_format_number(y)}"
    names = ET.SubElement(desc, "{" + CRS + "}Name")
    alt = ET.SubElement(names, "{" + RDF + "}Alt")
    ET.SubElement(alt, "{" + RDF + "}li",
                  {"{http://www.w3.org/XML/1998/namespace}lang": "x-default"}).text = name

    return {
        "xmp": ET.tostring(root, encoding="unicode"),
        "written_keys": sorted([*scalars, *arrays]),
        "skipped_metadata_keys": skipped_meta,
        "skipped_structured_keys": skipped_structured,
        "skipped_shape_keys": skipped_shape,
    }
