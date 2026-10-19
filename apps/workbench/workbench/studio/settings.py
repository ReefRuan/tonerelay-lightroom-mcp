"""Validated numeric develop patches and lossless XMP serialization.

Two classes of setting travel through here:

**structured** -- the fields in :data:`BOUNDS`/:data:`CURVES`. These have a known
range, and the engine checks it. This is the class the engine can *explain*.

**passthrough** -- every other ``crs:`` scalar or numeric array. The engine only
checks that the value is a finite number or an array of them; **the range is
Lightroom's business, not ours**. Refusing a field just because nobody wrote its
bounds down turns "the engine doesn't know this one" into "the user can't edit
this" -- which is a limit on their photos, not a safety property.

Anything nested (masks, retouch) needs ``allow_structured=True``: it is written
and then checked by reading back whether the key survived, but the engine does
not claim to understand its shape.
"""
import math
from lr_ops.configuration import active_store
import os
import re
import uuid
import xml.etree.ElementTree as ET

# 参数契约的字段清单、范围与分组只有一份真源：``parameter_specs``。
# 这里只做导出与校验，不再另行维护第二份有效范围或字段列表。
from .parameter_specs import (  # noqa: F401  (BOUNDS/CURVES/FAMILIES 为兼容导出)
    BOUNDS,
    CURVES,
    CURVE_COORD_MAX,
    CURVE_COORD_MIN,
    CURVE_MAX_POINTS,
    CURVE_MIN_POINTS,
    FAMILIES,
    WB_KEYS,
)

# 在 Lightroom Classic 15.4.1 的本地校验中，分离色调 5 键、三轮调色 9 键和
# 裁剪后暗角 6 键写入后均被原样回读。注意
# ColorGradeShadowHue/Sat、ColorGradeHighlightHue/Sat 四个旧版键 LR 15.4.1 不回读
# （被忽略），**不**收进 BOUNDS —— 它们仍是透传字段，由 readback_mismatches 如实报告。
# 这些范围现在写在 ``parameter_specs.PARAMETERS`` 里，与配套说明同源。

# 不是显影参数：LR 自己维护的元数据，写它只会制造不一致。
DENY_SETTINGS = frozenset({
    'PresetType', 'Cluster', 'UUID', 'HasSettings', 'CameraProfileDigest',
    'History', 'Snapshots', 'ToneCurveName', 'ToneCurveName2012',
})
# 字段名的形状：crs: 里的键都是这种 camelCase。挡掉奇怪的键名，也就挡住了
# 把路径/表达式塞进来的可能。
NAME_RE = re.compile(r'\A[A-Za-z][A-Za-z0-9]{0,60}\Z')
_STRUCTURED_MAX_DEPTH = 6
_STRUCTURED_MAX_ITEMS = 256

_CONFIG = active_store()
_EXECUTION = _CONFIG.read()["execution"] if _CONFIG else None


def _flag(name, default=True):
    if _EXECUTION is not None:
        key = {"LR_XMP_MIMIC_ALLOW_PASSTHROUGH": "allow_passthrough",
               "LR_XMP_MIMIC_ALLOW_STRUCTURED": "allow_structured"}[name]
        return _EXECUTION[key]
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ('0', 'false', 'no', 'off', '')

# 放开到什么程度是**部署配置**，不是写死在代码里的产品判断。
# 关掉 passthrough 就回到「只接受 BOUNDS/CURVES」的老行为——给需要收紧的部署留一个开关，
# 而不是由底座替用户决定他能不能调某个滑块。
ALLOW_PASSTHROUGH = _flag('LR_XMP_MIMIC_ALLOW_PASSTHROUGH', True)
# 嵌套结构（蒙版/修饰）单独一个开关：它没经过真机验证，可以单独关掉而不影响标量透传。
ALLOW_STRUCTURED = _flag('LR_XMP_MIMIC_ALLOW_STRUCTURED', True)

CRS = 'http://ns.adobe.com/camera-raw-settings/1.0/'
RDF = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#'
XMP_METADATA_ATTRIBUTES = frozenset({
    'PresetType', 'UUID', 'SupportsAmount', 'WhiteBalance',
})
ET.register_namespace('x', 'adobe:ns:meta/')
ET.register_namespace('rdf', RDF)
ET.register_namespace('crs', CRS)


def is_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def is_numeric_array(value, max_len=512):
    return (isinstance(value, list) and 2 <= len(value) <= max_len
            and all(is_number(item) for item in value))


def is_structured(value, depth=0):
    """Depth- and size-limited tables of primitives -- masks and retouch live here."""
    if depth > _STRUCTURED_MAX_DEPTH:
        return False
    if isinstance(value, dict):
        if len(value) > _STRUCTURED_MAX_ITEMS:
            return False
        return all(isinstance(k, str) and is_structured(v, depth + 1)
                   for k, v in value.items())
    if isinstance(value, list):
        if len(value) > _STRUCTURED_MAX_ITEMS:
            return False
        return all(is_structured(item, depth + 1) for item in value)
    return value is None or isinstance(value, (str, bool)) or is_number(value)


def validate(settings, *, allow_structured=None, allow_passthrough=None):
    if allow_passthrough is None:
        allow_passthrough = ALLOW_PASSTHROUGH
    if allow_structured is None:
        allow_structured = ALLOW_STRUCTURED
    if not isinstance(settings, dict) or not settings:
        raise ValueError('settings must be a nonempty object')
    for key, value in settings.items():
        if not isinstance(key, str) or not NAME_RE.match(key):
            raise ValueError(f'Invalid setting name: {key!r}')
        if key in DENY_SETTINGS:
            raise ValueError(f'{key} 不是显影参数，底座不写它')
        if key in CURVES:
            # 点数与坐标上下限来自 parameter_specs，说明文字读的是同一组数字。
            if (not isinstance(value, list)
                    or not 2 * CURVE_MIN_POINTS <= len(value) <= 2 * CURVE_MAX_POINTS
                    or len(value) % 2
                    or any(type(v) not in (int, float) or not math.isfinite(v)
                           or not CURVE_COORD_MIN <= v <= CURVE_COORD_MAX or v != int(v)
                           for v in value)):
                raise ValueError(f'Curve needs {CURVE_MIN_POINTS}..{CURVE_MAX_POINTS} x,y points '
                                 f'in {CURVE_COORD_MIN}..{CURVE_COORD_MAX}')
            if (value[0] != CURVE_COORD_MIN or value[-2] != CURVE_COORD_MAX
                    or any(a >= b for a, b in zip(value[::2], value[2::2]))):
                raise ValueError('Curve x coordinates must strictly increase from 0 to 255')
            continue
        if key in BOUNDS:
            lo, hi = BOUNDS[key]
            if not is_number(value) or not lo <= value <= hi:
                raise ValueError(f'{key} must be finite and in {lo}..{hi}')
            continue
        # 透传：只要类型是对的，范围由 Lightroom 决定。
        # 布尔也要放行 —— ConvertToGrayscale / AutoLateralCA 这类开关就是布尔，
        # 把黑白转换挡在外面等于把"黑白混色"整个功能挡在外面。
        scalar = is_number(value) or isinstance(value, bool) or is_numeric_array(value)
        if scalar:
            if allow_passthrough:
                continue
            raise ValueError(f'Unsupported setting: {key}（透传已由配置关闭）')
        # 走到这里只可能是嵌套结构（蒙版/修饰）。注意不能拿 is_structured 当第一道门：
        # 它对数字也返回真，会把上面那条「关闭透传」的分支整个吞掉。
        # 顶层数组的形状也要管：LR 里只有两种 —— 数值数组（曲线/混色，上面已处理）
        # 和「表数组」（蒙版）。`[1, "a"]` 这种混合标量不是任何东西，别放它过去。
        if isinstance(value, list) and not all(isinstance(item, dict) for item in value):
            raise ValueError(f'{key} 是数组，但既不是数值数组，也不是蒙版那样的表数组')
        if isinstance(value, (list, dict)) and is_structured(value):
            if allow_structured:
                continue
            raise ValueError(f'{key} 是嵌套结构，已由配置关闭（LR_XMP_MIMIC_ALLOW_STRUCTURED）')
        raise ValueError(
            f'{key} 的值类型不受支持：只接受有限数字、布尔、数值数组、嵌套结构'
            + f'，收到 {type(value).__name__}')
    if set(settings) & {'Temperature','Tint'} and set(settings) & {'IncrementalTemperature','IncrementalTint'}:
        raise ValueError('Do not mix RAW and rendered-file white balance controls')
    return dict(settings)


def to_xmp(settings, name='ToneRelay'):
    validate(settings)
    # SDK passthrough accepts nested masks, but a Python repr is not an Adobe
    # RDF mask/array encoding. Keep those edits usable while refusing a lossy
    # downloadable preset until their native serialization is implemented.
    unsupported = [key for key, value in settings.items()
                   if key not in CURVES and not (is_number(value) or type(value) is bool)]
    if unsupported:
        raise ValueError('XMP serialization unsupported for structured settings: '
                         + ', '.join(sorted(unsupported)))
    root = ET.Element('{adobe:ns:meta/}xmpmeta')
    rdf = ET.SubElement(root, '{'+RDF+'}RDF')
    desc = ET.SubElement(rdf, '{'+RDF+'}Description', {
        '{'+RDF+'}about': '', '{'+CRS+'}PresetType': 'Normal',
        '{'+CRS+'}UUID': uuid.uuid5(uuid.NAMESPACE_URL, name).hex.upper(),
        '{'+CRS+'}SupportsAmount': 'False'})
    if any(k in settings for k in WB_KEYS):
        desc.set('{'+CRS+'}WhiteBalance', 'Custom')
    if any(k in settings for k in CURVES):
        desc.set('{'+CRS+'}ToneCurveName2012', 'Custom')
    for key, value in sorted(settings.items()):
        if key in CURVES:
            sequence = ET.SubElement(ET.SubElement(desc, '{'+CRS+'}'+key), '{'+RDF+'}Seq')
            for x, y in zip(value[::2], value[1::2]):
                ET.SubElement(sequence, '{'+RDF+'}li').text = f'{x}, {y}'
        else:
            desc.set('{'+CRS+'}'+key, repr(value))
    names = ET.SubElement(desc, '{'+CRS+'}Name')
    alt = ET.SubElement(names, '{'+RDF+'}Alt')
    ET.SubElement(alt, '{'+RDF+'}li', {'{http://www.w3.org/XML/1998/namespace}lang': 'x-default'}).text = name
    return ET.tostring(root, encoding='unicode')


def from_xmp(text):
    root = ET.fromstring(text)
    desc = root.find('.//{'+RDF+'}Description')
    if desc is None:
        raise ValueError('Missing XMP description')
    result = {}
    # Numeric/bool passthrough is lossless. Do not parse Python container reprs:
    # accepting those would make an invalid native preset pass our SDK replay.
    for namespaced_key, text_value in desc.attrib.items():
        if not namespaced_key.startswith('{'+CRS+'}'):
            continue
        key = namespaced_key[len(CRS) + 2:]
        if key in XMP_METADATA_ATTRIBUTES:
            continue
        if text_value in ('True', 'False'):
            value = text_value == 'True'
        else:
            try:
                value = float(text_value)
            except ValueError:
                if text_value.lstrip().startswith(('[', '{', '(')):
                    raise ValueError(f'Unsupported structured XMP attribute: {key}') from None
                continue
        result[key] = value
    for key in CURVES:
        curve = desc.find('{'+CRS+'}'+key)
        if curve is not None:
            result[key] = [float(v.strip()) for item in curve.findall('./{'+RDF+'}Seq/{'+RDF+'}li') for v in (item.text or '').split(',')]
    return validate(result)


# 明确不具备的能力。不是"还没做"，是链路上不存在 —— 与所有适配器输出必须一致。
# 只有**真的做不到**的才列在这里，并且必须给出去 Lightroom 界面里的进入路径。
# 「我们的白名单里没有」不是做不到的理由——那是自我设限，不是 LR 的能力边界。
# 2026-09-16 更正：蒙版/Profile/清晰度类此前被列为「做不到」，理由写的是
# 「不在可写参数白名单内」。白名单已经改成类型检查，那些字段现在都能透传，
# 所以它们从这里移走了。剩下的这两条是 SDK 层面真的没有入口。
UNSUPPORTED_CAPABILITIES = (
    ('geometry_auto', '几何校正的自动分析（Upright / 自动镜头校正）',
     'PerspectiveUpright 这类开关可以透传，但"自动分析"跑在 Lightroom 内部，SDK 不暴露',
     '修改照片 → 几何 → Upright，或 镜头校正 → 启用配置文件校正'),
    ('ai_features', 'AI 去噪 / 超分 / 生成式移除',
     '依赖 Lightroom 内置模型，SDK 没有任何调用入口',
     '修改照片 → 细节 → 降噪（AI），或 移除工具'),
)
