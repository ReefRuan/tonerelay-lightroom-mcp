"""Versioned edit episodes using only real LR rendering.

All versions are separate virtual copies. A master photo or one of its
virtual copies may serve as the episode source; copies made during an
episode inherit the source's current develop state.
File locks serialize Runtime writers; atomic JSON commits keep reads coherent.
No automatic replay after an uncertain LR write or process interruption.
"""
from contextlib import contextmanager
import hashlib
import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None

from .imaging import compare, sha
from ..artifacts import build_artifact_ref
from ..targets import TargetError, normalize_access_mode, normalize_catalog_identity
from .settings import (ALLOW_PASSTHROUGH, ALLOW_STRUCTURED, BOUNDS, CURVES, WB_KEYS,
                       FAMILIES, UNSUPPORTED_CAPABILITIES, is_number,
                       from_xmp, to_xmp, validate)
from . import parameter_specs

# ``searching``, ``exploring`` and ``probing`` are retained only so an
# unfinished Episode written by an older Runtime is detected as interrupted
# instead of looking ready. New code never writes those statuses.
ACTIVE = {'creating', 'editing', 'searching', 'rendering_parameter_variants',
          'verifying', 'restoring', 'exploring', 'probing'}

# Name prefix for every virtual copy an Episode renders; MCP-created copies
# are recognized (and can be continued) by this prefix.
RENDER_COPY_PREFIX = 'TRLR'


def render_copy_name(state, number):
    """Human-readable name for a rendered version copy, e.g.
    ``TRLR 2026-10-17 6b2bf530 v2`` — date + episode id + version."""
    day = datetime.fromtimestamp(state['created_at']).strftime('%Y-%m-%d')
    return f'{RENDER_COPY_PREFIX} {day} {state["id"][:8]} v{number}'


class Conflict(ValueError):
    pass


def ui_context_summary(value):
    """Keep restoration facts; never persist SDK objects or host paths."""
    parts = ('sources', 'selection', 'module')
    statuses = {'restored', 'partial', 'unavailable'}
    part_statuses = {'restored', 'not_restored', 'unavailable', 'skipped'}
    codes = {'snapshot_unavailable', 'catalog_changed', 'ownership_unavailable',
             'user_interference', 'restore_unavailable', 'restore_failed',
             'readback_mismatch', 'restore_dependency_failed', 'not_reported',
             'invalid_report'}
    if not isinstance(value, dict) or value.get('schema_version') != 'lr-ui-context-v1':
        return {
            'schema_version': 'lr-ui-context-v1', 'status': 'unavailable',
            'parts': {part: {'status': 'unavailable'} for part in parts},
            'warnings': [{'part': 'all', 'code': 'not_reported' if value is None else 'invalid_report'}],
        }
    raw_status = value.get('status')
    invalid = not isinstance(raw_status, str) or raw_status not in statuses
    invalid = invalid or not isinstance(value.get('warnings'), list)
    report = {'schema_version': 'lr-ui-context-v1',
              'status': raw_status if not invalid else 'unavailable',
              'parts': {}, 'warnings': []}
    raw_parts = value.get('parts') if isinstance(value.get('parts'), dict) else {}
    for part in parts:
        raw = raw_parts.get(part)
        status = raw.get('status') if isinstance(raw, dict) else None
        valid = isinstance(status, str) and status in part_statuses
        invalid = invalid or not valid
        report['parts'][part] = {'status': status if valid else 'unavailable'}
    for warning in value.get('warnings', []) if isinstance(value.get('warnings'), list) else []:
        if (isinstance(warning, dict) and isinstance(warning.get('part'), str)
                and warning.get('part') in (*parts, 'all')
                and isinstance(warning.get('code'), str) and warning.get('code') in codes):
            report['warnings'].append({'part': warning['part'], 'code': warning['code']})
        else:
            invalid = True
    if invalid:
        report['status'] = 'unavailable'
        report['warnings'].append({'part': 'all', 'code': 'invalid_report'})
    if report['status'] == 'restored' and any(report['parts'][part]['status'] != 'restored' for part in parts):
        report['status'] = 'unavailable'
        report['warnings'].append({'part': 'all', 'code': 'invalid_report'})
    return report


def atomic(path, value):
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        deadline = time.monotonic() + 1.0
        while True:
            try:
                temp.replace(path)
                break
            except PermissionError as exc:
                # Windows readers can briefly deny replacement of episode.json.
                # Retry only the same finished JSON commit, never Lightroom I/O.
                if (getattr(exc, 'winerror', None) not in (5, 32, 33)
                        or time.monotonic() >= deadline):
                    raise
                time.sleep(0.01)
    finally:
        temp.unlink(missing_ok=True)


def fingerprint(value):
    """Stable hash for immutable execution provenance stored in an Episode."""
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(payload).hexdigest()


def omitted_snapshot(value):
    """Compact stand-in for a develop snapshot omitted from a public response.

    Full snapshots stay on disk and remain available through
    ``trlr_get_episode(include_snapshots=True)``.
    """
    if (isinstance(value, dict) and value.get('omitted') is True
            and set(value) == {'omitted', 'sha256', 'keys'}):
        return value
    return {'omitted': True,
            'sha256': fingerprint(value),
            'keys': len(value) if isinstance(value, dict) else None}


def mismatch_value(value):
    """Keep small readback facts verbatim; summarize bulky structured values.

    A passthrough key like MaskGroupBasedCorrections can carry kilobytes of
    nested tables; echoing the request verbatim inside every mismatch made
    responses larger than the render they described.
    """
    # Some older Runtime versions persisted these summaries. Their original
    # values are no longer available here; preserve the existing evidence,
    # including its hash, instead of hashing the summary a second time.
    if (isinstance(value, dict) and value.get('omitted') is True
            and set(value) == {'omitted', 'type', 'sha256', 'preview'}):
        return value
    if isinstance(value, (dict, list)):
        payload = json.dumps(value, ensure_ascii=False)
        if len(payload) > 200:
            return {'omitted': True, 'type': type(value).__name__,
                    'sha256': fingerprint(value),
                    'preview': payload[:200]}
    return value


def validate_query(query):
    if not isinstance(query, str) or len(query) > 200:
        raise ValueError('query must be at most 200 characters')
    return query.strip()


def matches(query, values):
    text = ' '.join(str(value) for value in values if value is not None).casefold()
    return not query or all(token in text for token in query.casefold().split())


def reference_digest(state):
    """Return the current reference hash, with read-only legacy fallback."""
    digest = state.get('reference_sha256')
    if isinstance(digest, str) and digest:
        return digest
    versions = state.get('versions') or []
    if versions:
        legacy = (versions[0].get('metrics') or {}).get('target_sha256')
        if isinstance(legacy, str) and legacy:
            return legacy
    return None


def public_state(state, include_snapshots=True):
    """Project an Episode without retired Runtime-side decision/content fields.

    ``raw()`` deliberately preserves every historical JSON field in memory,
    while public responses expose only current state, measurements, execution
    provenance, and neutral compatibility records.

    With ``include_snapshots=False`` the bulky develop snapshots
    (``source_snapshot``, ``baseline_snapshot`` and each version's
    ``snapshot``) are replaced by ``omitted_snapshot`` stand-ins carrying a
    stable hash. Bulky mismatch values are also summarized only in this
    compact projection. Callers can retrieve the full stored values through
    ``trlr_get_episode(include_snapshots=True)``; summaries persisted by older
    Runtime versions remain summaries because their original values are lost.
    """
    result = json.loads(json.dumps(state))
    if not include_snapshots:
        for key in ('source_snapshot', 'baseline_snapshot'):
            if isinstance(result.get(key), dict):
                result[key] = omitted_snapshot(result[key])
    if 'ui_context' in result:
        result['ui_context'] = ui_context_summary(result['ui_context'])
    if result.get('mode') == 'style_explore':
        # Do not leak the retired Agent-like name from legacy Episode files.
        result['mode'] = 'source_edit'
    for key in ('best_version', 'search', 'exploration', 'probe', 'measurement_scope'):
        result.pop(key, None)
    retired_actions = {'explore', 'probe', 'search'}

    def operation_action(operation):
        if not isinstance(operation, dict):
            return None
        if operation.get('action') is not None:
            return operation['action']
        request = operation.get('input')
        return request.get('action') if isinstance(request, dict) else None

    result['operations'] = {
        request_id: operation
        for request_id, operation in (result.get('operations') or {}).items()
        if operation_action(operation) not in retired_actions
    }
    for operation in result['operations'].values():
        if not isinstance(operation, dict):
            continue
        if operation.get('mode') == 'style_explore':
            operation['mode'] = 'source_edit'
        request = operation.get('input')
        if isinstance(request, dict) and request.get('mode') == 'style_explore':
            request['mode'] = 'source_edit'
    for version in result.get('versions') or []:
        if 'ui_context' in version:
            version['ui_context'] = ui_context_summary(version['ui_context'])
        # Older Runtime versions attached an implicit image-response comparison
        # to every render. Keep it on disk for compatibility, but do not expose
        # it as part of the current execution contract.
        version.pop('metrics', None)
        version.pop('accepted_by_search', None)
        version.pop('style_candidate', None)
        version.pop('technical_review', None)
        legacy = version.pop('parameter_probe', None)
        if legacy and not version.get('parameter_variant'):
            parameter = legacy.get('parameter')
            requested = legacy.get('requested_value', legacy.get('value'))
            applied = legacy.get('applied_value')
            if applied is None and parameter:
                applied = (version.get('snapshot') or {}).get(parameter)
            version['parameter_variant'] = {
                key: value for key, value in {
                    'parameter': parameter,
                    'base_version': legacy.get('base_version'),
                    'base_value': legacy.get('base_value'),
                    'requested_value': requested,
                    'applied_value': applied,
                    'parameter_spec': legacy.get('parameter_spec'),
                    'parameter_specs_hash': legacy.get('parameter_specs_hash'),
                    'base_snapshot_hash': legacy.get('base_snapshot_hash'),
                    'base_preview_hash': legacy.get('base_preview_hash'),
                    'renderer': legacy.get('renderer'),
                    'long_edge': legacy.get('long_edge'),
                    'color_space': legacy.get('color_space'),
                }.items() if value is not None
            }
        # Compatibility projections above need the real readback before it
        # is omitted. Never substitute a requested value for missing evidence.
        if not include_snapshots:
            if isinstance(version.get('snapshot'), dict):
                version['snapshot'] = omitted_snapshot(version['snapshot'])
            for entry in version.get('readback_mismatches') or []:
                if isinstance(entry, dict):
                    for key in ('requested', 'applied'):
                        if key in entry:
                            entry[key] = mismatch_value(entry[key])
    return result


def valid_id(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('Invalid episode ID')
    return value


def same_value(actual, requested, tolerance=1e-5):
    """写入值与回读值是否是同一个东西。

    数字带容差，布尔按布尔比，数组逐项比 —— 原来的 ``abs(a - b) > 1e-5`` 在遇到
    数组时直接抛 TypeError，而透传之后数组会经常出现。
    """
    if isinstance(requested, bool):
        return actual is requested or actual == int(requested)
    if isinstance(requested, (int, float)):
        return is_number(actual) and abs(actual - requested) <= tolerance
    if isinstance(requested, list):
        if not isinstance(actual, list) or len(actual) != len(requested):
            return False
        return all(same_value(a, r, tolerance) for a, r in zip(actual, requested))
    if isinstance(requested, dict):
        if not isinstance(actual, dict) or set(actual) != set(requested):
            return False
        return all(same_value(actual[k], v, tolerance) for k, v in requested.items())
    return actual == requested


def equivalent_snapshot(a, b):
    """LR lazily materializes empty settings and As Shot WB on RAW copies.

    Preserve raw snapshots for audit. Only empty collections and unresolved
    As Shot WB compare semantically; actual profile/curve/value changes fail.
    """
    def normalize(value):
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items() if item != {} and item != []}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value
    left, right = normalize(a), normalize(b)
    if left.get('WhiteBalance') == right.get('WhiteBalance') == 'As Shot':
        for key in ('Temperature', 'Tint'):
            if key not in left or key not in right:
                left.pop(key, None)
                right.pop(key, None)
    return left == right


class Studio:
    def __init__(self, root, client):
        self.root = Path(root) / 'episodes'
        self.root.mkdir(parents=True, exist_ok=True)
        self.client = client

    def directory(self, sid):
        return self.root / valid_id(sid)

    @contextmanager
    def lock(self, sid):
        directory = self.directory(sid)
        if not directory.is_dir():
            raise ValueError('Episode not found')
        mode = 'a+b' if msvcrt is not None else 'a+'
        with (directory / '.lock').open(mode) as handle:
            if fcntl is not None:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise Conflict('此会话正在运行，请等待或请求停止当前操作') from None
            elif msvcrt is not None:
                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write(b'\0')
                    handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise Conflict('此会话正在运行，请等待或请求停止当前操作') from None
            else:  # pragma: no cover - supported Python platforms provide one.
                raise RuntimeError('No supported file-lock implementation')
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                else:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def raw(self, sid):
        path = self.directory(sid) / 'episode.json'
        if not path.is_file():
            raise ValueError('Episode not found')
        return json.loads(path.read_text(encoding='utf-8'))

    def save(self, state):
        state['revision'] += 1
        state['updated_at'] = time.time()
        atomic(self.directory(state['id']) / 'episode.json', state)

    def get(self, sid, include_snapshots=True):
        state = self.raw(sid)
        if state['status'] in ACTIVE:
            try:
                with self.lock(sid):
                    state = self.raw(sid)
                    if state['status'] in ACTIVE:
                        state['status'] = 'interrupted'
                        state['error'] = '操作中断，未自动重放；请确认 LR 旧任务已结束后重新建会话。'
                        self.save(state)
            except Conflict:
                pass
        return public_state(state, include_snapshots=include_snapshots)

    def list(self):
        rows = []
        for path in self.root.glob('*/episode.json'):
            s = self.get(path.parent.name)
            # source_uuid 是调用方判断「这张图有没有已存在的上下文」的唯一可靠依据；
            # 只按 source_name 匹配会在同名文件上接错历史。
            row = {k: s.get(k) for k in ('id', 'mode', 'status', 'revision', 'created_at',
                                         'updated_at', 'intent', 'source_uuid', 'source_name',
                                         'current_version', 'target_id', 'ui_context')}
            # 列表只返回版本摘要，避免把完整 Episode 状态带入筛选请求：
            # 每版的虚拟副本 uuid、预览名、是否通过 XMP 回放验证。
            copies = {entry.get('version'): entry.get('copy_name')
                      for entry in s.get('materialized') or []}
            row['versions'] = [{
                'version': v.get('version'),
                'photo_uuid': v.get('photo_uuid'),
                'preview': v.get('preview'),
                'note': v.get('note') or '',
                'verified': bool((v.get('verification') or {}).get('passed')),
                'xmp_sha256': (v.get('verification') or {}).get('xmp_sha256'),
                'copy_name': copies.get(v.get('version')),
            } for v in s.get('versions') or []]
            # 「最近动过」排前面，避免新建但未使用的空草稿长期压住
            # 真正在用的上下文。
            row['updated_at'] = row.get('updated_at') or row.get('created_at') or 0
            row['message_count'] = len(s.get('messages') or [])
            last_user = next((m for m in reversed(s.get('messages') or [])
                              if m.get('role') == 'user'), None)
            # 只带最后一条用户消息的摘要供候选定位；它没有时间或 pending 语义。
            # 调用方必须读取完整 episode 才能判断用户要求与版本的关系。
            row['last_user_message'] = ((last_user or {}).get('text') or '')[:200] or None
            rows.append(row)
        return sorted(rows, key=lambda x: x['updated_at'], reverse=True)

    def capabilities(self):
        """能力真源：可写参数、分组、以及链路上根本不存在的部分。

        这里只给**能不能写**的元数据；**怎么填**（名称、单位、输入条件、已知限制）
        在参数操作文档里，入口见 ``parameter_reference``。
        """
        return {
            'parameter_reference': {
                'entries': len(parameter_specs.PARAMETER_IDS),
                'entry_point': 'trlr_parameter_reference',
                'note': ('字段名称、单位、范围、输入条件与已知限制在参数操作文档里，'
                         '属于 Runtime 参数操作契约。下面只列可写性与分组。'),
            },
            'writable_parameters': sorted(BOUNDS),
            'curves': list(CURVES),
            'white_balance_keys': list(WB_KEYS),
            'families': [{'id': fid, 'label': label, 'keys': list(keys)} for fid, label, keys in FAMILIES],
            # 三档：底座知道范围的 / 只保证类型和回读的 / SDK 真的没有入口的。
            # 调用方应当按这个决定"自己调工具"还是"请用户去 Lightroom 里点"。
            'capability_tiers': {
                'structured': {
                    'keys': sorted(BOUNDS) + list(CURVES),
                    'note': '有范围校验，超出会被拒；这些是底座能解释的字段',
                },
                'passthrough': {
                    'enabled': ALLOW_PASSTHROUGH,
                    'structured_enabled': ALLOW_STRUCTURED,
                    'config': ['LR_XMP_MIMIC_ALLOW_PASSTHROUGH',
                               'LR_XMP_MIMIC_ALLOW_STRUCTURED'],
                    'note': ('其它 crs: 字段可以透传提交：Runtime 只检查类型（有限数字、布尔、数值数组，'
                             '或显式 allow_structured 的嵌套结构），范围交给 Lightroom。'
                             '写入后逐键回读，versions[].readback_mismatches 里会列出'
                             'Lightroom 夹取或忽略的键——那是它能告诉你的真实边界。'),
                    'examples': ['GrainAmount', 'GrainSize', 'GrainFrequency',
                                 'ConvertToGrayscale', 'GrayMixerRed',
                                 'SplitToningShadowHue', 'SplitToningHighlightSaturation',
                                 'Texture', 'Clarity2012', 'Dehaze',
                                 'LensProfileEnable', 'PerspectiveUpright',
                                 'MaskGroupBasedCorrections（需 allow_structured）'],
                },
                'manual': {
                    'note': 'SDK 没有入口，只能由用户在 Lightroom 界面里完成',
                    'items': [{'id': fid, 'label': label, 'reason': reason, 'manual_path': manual}
                              for fid, label, reason, manual in UNSUPPORTED_CAPABILITIES],
                },
            },
            'unsupported': [{'id': fid, 'label': label, 'reason': reason, 'manual_path': manual}
                            for fid, label, reason, manual in UNSUPPORTED_CAPABILITIES],
            'limits': {'parameter_variant_values_max': 5,
                       'long_edge_min': 256, 'long_edge_max': 2048},
            'notes': [
                'XMP 只承载参数数值，没有按功能开关的字段；"关闭某项"只能表达为该 XMP 不含哪些参数。',
                ('Episode 编辑版本写入独立虚拟副本；显式导入工具可以新增 Catalog 主照片记录，'
                 'Runtime 不修改或删除原片。'),
                ('插件在执行点核对 safe/full 模式和 Catalog 身份；safe 额外要求已登记的 Sandbox，'
                 'full 不限制 Catalog 路径，但不会绕过工具边界。'),
                ('Lightroom 的虚拟副本 SDK 依赖可见选择状态。创建副本后插件尝试恢复原来源、'
                 '选片和模块，ui_context 按读回结果报告；同来源不强制切换目录或模块，跨来源可临时激活'
                 ' All Photographs 并恢复。恢复警告不表示需要重新创建已成功的副本。'),
            ],
        }

    def parameter_reference(self, query='', identifier=None):
        """参数操作文档主入口：契约 + 配套解释，直接读执行定义。

        ``identifier`` 用于按字段名精确取一条。
        """
        rows = parameter_specs.search(query, identifier)
        return {"parameter_specs_hash": parameter_specs.fingerprint(),
                "count": len(rows), "items": rows,
                "scope": "Runtime parameter operation contract and checked explanations."}

    def list_parameter_variant_results(self, query=''):
        """List verified parameter-variant artifacts without interpreting them.

        Historical Episodes may contain ``parameter_probe`` records with
        additional retired fields. They remain readable on disk, but this
        projection deliberately exposes only neutral execution provenance
        shared with new ``parameter_variant`` records.
        """
        query = validate_query(query)
        results = []
        for row in self.list():
            state = self.get(row['id'])
            for version in state['versions']:
                record = version.get('parameter_variant') or version.get('parameter_probe')
                if not record:
                    continue
                parameter = record.get('parameter')
                spec = record.get('parameter_spec') or {}
                if not parameter or not matches(query, [parameter, spec.get('title'),
                                                         state.get('source_name')]):
                    continue
                base_version = record.get('base_version')
                if type(base_version) is not int or not 0 <= base_version < len(state['versions']):
                    continue
                base = state['versions'][base_version]
                try:
                    self.artifact(state['id'], version['preview'])
                    self.artifact(state['id'], base['preview'])
                except (OSError, ValueError):
                    continue
                requested = record.get('requested_value', record.get('value'))
                applied = record.get('applied_value')
                if applied is None:
                    applied = (version.get('snapshot') or {}).get(parameter)
                results.append({
                    'id': f'{state["id"]}-v{version["version"]}',
                    'episode_id': state['id'],
                    'version': version['version'],
                    'source_name': state.get('source_name'),
                    'parameter': parameter,
                    'base_version': base_version,
                    'base_value': record.get('base_value'),
                    'requested_value': requested,
                    'applied_value': applied,
                    'parameter_spec': spec or None,
                    'parameter_specs_hash': record.get('parameter_specs_hash'),
                    'base_snapshot_hash': record.get('base_snapshot_hash'),
                    'base_preview_hash': record.get('base_preview_hash') or base.get('sha256'),
                    'renderer': record.get('renderer') or state.get('renderer'),
                    'long_edge': record.get('long_edge') or state.get('long_edge'),
                    'color_space': record.get('color_space') or 'sRGB',
                    'base_artifact': base.get('artifact'),
                    'candidate_artifact': version.get('artifact'),
                    'base_preview': base.get('preview'),
                    'preview': version.get('preview'),
                    'preview_sha256': version.get('sha256'),
                    'created_at': version.get('created_at'),
                    'verification_passed': bool((version.get('verification') or {}).get('passed')),
                })
        return {'count': len(results), 'results': results}

    def run(self, command, params=None, *, catalog_identity=None):
        bound = dict(params or {})
        try:
            access_mode = normalize_access_mode(getattr(self.client, 'access_mode', None))
        except TargetError as exc:
            raise ValueError(str(exc)) from exc
        bound['expected_access_mode'] = access_mode

        # ``catalog_identity`` is an opaque bridge-issued value. Never derive or
        # normalize it here: an Episode must echo back exactly what probe_client
        # returned. A configured Target identity applies before an Episode has
        # been created and is also checked by probe().
        identity = (catalog_identity if catalog_identity is not None
                    else getattr(self.client, 'catalog_identity', None))
        if command != 'probe_client' and identity is None:
            raise ValueError(
                'Non-probe Lightroom calls require expected_catalog_identity; '
                'probe and bind the catalog first')
        if identity is not None:
            try:
                bound['expected_catalog_identity'] = normalize_catalog_identity(identity)
            except TargetError as exc:
                raise ValueError(str(exc)) from exc

        return self.client.run(command, bound)

    # ---- which Lightroom this episode belongs to ---------------------------
    #
    # A photo uuid only means something inside one catalog. Once the workbench
    # can drive more than one Lightroom, an episode created against target A
    # cannot be rendered against target B: at best the uuid is unknown, at worst
    # it names a *different* photo in B and we write to the wrong image. So an
    # episode records its target, and every mutating call checks it first.
    #
    # `self.client` is the router (see workbench.targets), which reports
    # `target_id`. A plain LrOpsClient has no such attribute; those deployments
    # have exactly one target, so the check is a no-op and stays out of the way.

    @property
    def target_id(self):
        return getattr(self.client, 'target_id', None)

    def require_target(self, state):
        """Refuse to operate on an episode that belongs to another Lightroom.

        Refusing beats silently proceeding: see the module note above for why
        the failure mode is a wrong-photo write rather than a clean error.
        Episodes predating target binding have no recorded target and are
        adopted by the current one on first use.
        """
        recorded = state.get('target_id')
        active = self.target_id
        if active is None:
            return  # single-target client: nothing to compare against
        if recorded is None:
            return  # legacy episode, adopted on its next save
        if recorded != active:
            raise ValueError(
                f'此会话属于另一台 Lightroom（{recorded}），当前目标是 {active}；'
                f'请切换回原目标，或新建会话。'
            )

    def bind_target(self, state):
        """Stamp the active target onto a state dict before it is saved."""
        if self.target_id is not None:
            state['target_id'] = self.target_id
        return state

    @staticmethod
    def _probe_catalog_identity(probe):
        """Read the opaque identity minted by the bridge.

        Display-only catalog names and old local paths are deliberately never
        promoted into the authorization identity.
        """
        value = probe.get('catalog_identity')
        try:
            return normalize_catalog_identity(value)
        except TargetError as exc:
            raise ValueError('LR 探测未返回有效 catalog_identity') from exc

    def bind_catalog(self, state, probe):
        """Bind a draft, or verify an already bound Episode.

        An Episode created before opaque identities existed remains readable,
        but cannot safely resume Lightroom work: a basename or an old local
        path is not proof that the current bridge opened the same Catalog.
        """
        current = self._probe_catalog_identity(probe)
        recorded = state.get('catalog_identity')
        renderer = state.get('renderer') or {}
        if recorded is None:
            recorded = renderer.get('catalog_identity')
        if recorded is None and state.get('source_uuid'):
            raise Conflict(
                '此历史会话没有可验证的 Catalog 身份，只能读取；'
                '请在当前 Catalog 新建会话')
        if recorded is not None and recorded != current:
            raise Conflict('此会话属于另一个 Lightroom Catalog；请切换回原 Catalog 或新建会话')
        state['catalog_identity'] = current
        return current

    def probe(self, catalog_identity=None):
        probe = self.run('probe_client', catalog_identity=catalog_identity)
        try:
            expected_mode = normalize_access_mode(getattr(self.client, 'access_mode', None))
            reported_mode = probe.get('access_mode')
            if not isinstance(reported_mode, str):
                raise TargetError('access_mode must be reported by the plugin')
            actual_mode = normalize_access_mode(reported_mode)
        except TargetError as exc:
            raise ValueError(str(exc)) from exc
        if actual_mode != expected_mode:
            raise ValueError(
                f'LR 返回的 access_mode 与目标配置不一致：{actual_mode} != {expected_mode}')
        actual_identity = self._probe_catalog_identity(probe)
        expected_identity = getattr(self.client, 'catalog_identity', None)
        if expected_identity is not None and actual_identity != expected_identity:
            raise ValueError('LR 返回的 catalog_identity 与目标绑定不一致')
        if catalog_identity is not None and actual_identity != catalog_identity:
            raise Conflict('此会话属于另一个 Lightroom Catalog；请切换回原 Catalog 或新建会话')
        caps = probe.get('capabilities', {})
        if (caps.get('expected_catalog_identity') is not True
                or caps.get('expected_access_mode') is not True):
            raise ValueError('请重启 Lightroom 加载支持 Catalog 身份绑定的当前插件')
        catalog_authorized = probe.get('catalog_authorized')
        if type(catalog_authorized) is not bool:
            raise ValueError('请重启 Lightroom 加载可报告 Catalog 授权状态的当前插件')
        if not catalog_authorized:
            raise ValueError('当前 Lightroom Catalog 未获 Runtime 授权；请检查访问模式和 Sandbox 登记')
        if caps.get('develop_snapshot') is not True:
            raise ValueError('请重启 Lightroom 加载当前版本插件')
        return {**probe, 'access_mode': actual_mode,
                'catalog_identity': actual_identity}

    def photos(self):
        probe = self.probe()
        data = self.run('list_photos', {'limit': 100000},
                        catalog_identity=probe['catalog_identity'])
        if data['count'] >= 100000:
            raise ValueError('Catalog listing truncated')
        return {**data, 'catalog': probe}

    def snapshot(self, photo, catalog_identity=None):
        """读回这张照片的全部显影设置。

        ``get_develop_snapshot`` 在插件侧直接返回 ``photo:getDevelopSettings()``
        的原始全量（实测 171 个键），**不受任何白名单限制** —— 包括蒙版那类嵌套结构。
        所以「用户在 Lightroom 界面里手动做的修改看不见」这个担心不成立，
        读回一直是完整的。
        """
        return self.run('get_develop_snapshot', {'photo_uuid': photo},
                        catalog_identity=catalog_identity)

    def export(self, sid, photo, relative, edge, catalog_identity=None):
        """Render one preview and land it in the episode directory.

        The bridge picks where Lightroom writes and returns an ``output_ref``;
        this side then asks for those bytes by handle. That is what lets the
        workbench run on a machine sharing no disk with Lightroom -- and it is
        why the path invariant that used to sit here now lives in
        ``Staging.verify_output``, on the side that can actually see the
        filesystem in question.
        """
        data = self.run('export_preview', {'photo_uuid': photo, 'long_edge': edge},
                        catalog_identity=catalog_identity)
        if data.get('color_space') != 'sRGB':
            raise ValueError('Unexpected LR export color space')
        ref = data.get('output_ref')
        if not ref:
            raise ValueError('导出未返回文件句柄；请重启 lr-ops 桥以加载新协议')
        final = self.directory(sid) / relative
        self.client.fetch_output(ref, final)
        return final

    def check_source(self, state):
        if state['versions']:
            expected = reference_digest(state)
            if not expected or sha(self.directory(state['id']) / 'reference.jpg') != expected:
                raise Conflict('Target render was modified; create a new episode')
        if not equivalent_snapshot(
                self.snapshot(state['source_uuid'], state.get('catalog_identity'))['settings'],
                state.get('source_snapshot', state['baseline_snapshot'])):
            raise Conflict('原片显影状态已变化；请创建新会话，不能在变化的基准上继续')

    @staticmethod
    def _require_source(state):
        if not state.get('source_uuid'):
            raise ValueError('Episode has no source photo; create a new Episode with source_uuid')

    def create(self, source_uuid, reference_uuid=None, same_capture=False, intent='', long_edge=1024, mode='same_capture_match', include_snapshots=True):
        if mode not in ('same_capture_match', 'source_edit'):
            raise ValueError('Unknown episode mode')
        if mode == 'same_capture_match' and (same_capture is not True or not reference_uuid):
            raise ValueError('参考图测量需要确认同一画面、相同方向与裁切')
        if mode == 'source_edit':
            if reference_uuid is not None:
                raise ValueError('Source-only mode uses the source baseline, not a reference photo')
            reference_uuid = source_uuid
        if type(long_edge) is not int or not 256 <= long_edge <= 2048:
            raise ValueError('long_edge must be 256..2048')
        if not isinstance(intent, str) or len(intent) > 4000:
            raise ValueError('intent must be at most 4000 characters')
        probe = self.probe()
        catalog_identity = probe['catalog_identity']
        baseline = self.snapshot(source_uuid, catalog_identity)
        sid = str(uuid.uuid4())
        self.directory(sid).mkdir()
        now = time.time()
        state = {'id': sid, 'schema_version': 2, 'mode': mode, 'revision': 0, 'status': 'creating',
                 'created_at': now, 'updated_at': now, 'source_uuid': source_uuid,
                 'reference_uuid': reference_uuid, 'source_name': baseline['photo']['file_name'],
                 'same_capture': mode == 'same_capture_match', 'intent': intent, 'renderer': probe, 'long_edge': long_edge,
                 'reference_sha256': None,
                 'source_snapshot': baseline['settings'], 'baseline_snapshot': baseline['settings'], 'versions': [],
                 'current_version': 0, 'messages': [], 'operations': {}, 'error': None,
                 'target_id': None, 'catalog_identity': catalog_identity}
        self.bind_target(state)
        with self.lock(sid):
            self.save(state)
            try:
                reference = self.export(sid, reference_uuid, 'reference.jpg', long_edge,
                                        catalog_identity)
                state['reference_sha256'] = sha(reference)
                # An immutable baseline copy is also the rollback target after LR restarts.
                version = self.render_version(state, {}, '基准原图')
                state['versions'].append(version)
                state['baseline_snapshot'] = version['snapshot']
                state['status'] = 'ready'
                if intent:
                    state['messages'].append({'role': 'user', 'text': intent, 'time': now})
                self.save(state)
            except Exception as exc:
                state.update(status='failed', error=str(exc))
                self.save(state)
                raise
        return public_state(state, include_snapshots=include_snapshots)

    def render_version(self, state, patch, note):
        """Render and audit one immutable LR virtual-copy version.

        Rendering records execution, readback and artifact integrity only.
        Callers request image measurement or comparison explicitly.
        """
        self.check_source(state)
        self.validate_patch(state, patch)
        number = len(state['versions'])
        sid = state['id']
        catalog_identity = state.get('catalog_identity')
        copied = self.run(
            'create_virtual_copy',
            {'photo_uuid': state['source_uuid'],
             'name': render_copy_name(state, number)},
            catalog_identity=catalog_identity)
        photo = copied['copy']['uuid']
        # Persist provenance before the next possible failure.
        state['pending_copy_uuid'] = photo
        ui_context = ui_context_summary(copied.get('ui_context'))
        state['ui_context'] = ui_context
        self.save(state)
        self.run('capture_baseline', {'photo_uuid': photo},
                 catalog_identity=catalog_identity)
        if not equivalent_snapshot(
                self.snapshot(photo, catalog_identity)['settings'], state['baseline_snapshot']):
            raise Conflict('Virtual copy did not inherit the captured source state')
        if patch:
            self.run('apply_candidate', {'photo_uuid': photo, 'settings': patch},
                     catalog_identity=catalog_identity)
        readback = self.snapshot(photo, catalog_identity)['settings']
        path = self.export(sid, photo, f'v{number}.jpg', state['long_edge'],
                           catalog_identity)
        after_render = self.snapshot(photo, catalog_identity)['settings']
        # LrPhoto:getDevelopSettings on rendered files can report -999999
        # for inherited incremental WB immediately after applying a preset.
        # LR resolves it during export. Accept ONLY that sentinel -> captured
        # baseline transition; an actual WB change still fails closed.
        for key in ('IncrementalTemperature', 'IncrementalTint'):
            if (readback.get(key) == -999999
                    and after_render.get(key) == patch.get(key, state['baseline_snapshot'].get(key))):
                readback[key] = after_render[key]
        if not equivalent_snapshot(after_render, readback):
            raise Conflict('Candidate changed during render')
        readback = after_render
        mismatched = []
        for key, value in patch.items():
            actual = readback.get(key)
            if key in CURVES or key in BOUNDS:
                # 已知字段：值对不上就是失败，这条是 XMP 回放验证的地基。
                mismatch = (actual != value if key in CURVES
                            else not is_number(actual) or abs(actual - value) > 1e-5)
                if mismatch:
                    raise ValueError(f'LR readback mismatch: {key}')
                continue
            # 透传字段：Lightroom 可能夹取到它自己的范围，也可能根本不认这个键。
            # **不失败** —— 失败会把"LR 的真实边界在哪"这件事藏起来，而 Agent 需要的
            # 正是这个。如实记下 requested/applied，让调用方自己判断。
            if not same_value(actual, value):
                mismatched.append({
                    'key': key,
                    'requested': value,
                    'applied': actual,
                    'reason': 'Lightroom 没有原样回读（夹取或忽略）',
                })
        self.check_source(state)
        result = {'version': number, 'photo_uuid': photo, 'patch': patch, 'snapshot': readback,
                  'ui_context': ui_context,
                  'readback_mismatches': mismatched,
                  'note': note, 'created_at': time.time(), 'preview': f'v{number}.jpg',
                  'sha256': sha(path),
                  'artifact': build_artifact_ref(
                      path,
                      artifact_id=f'episode:{sid}:v{number}',
                      source={'kind': 'episode_render', 'episode_id': sid,
                              'version': number, 'photo_uuid': photo},
                      purpose='verified', method='LrExportSession',
                      freshness='verified', evidence_status='verified_render',
                      color_space='sRGB')}
        state.pop('pending_copy_uuid', None)
        return result

    def validate_patch(self, state, patch):
        """Validate a complete cumulative patch before any LR copy is made."""
        if patch:
            # 放开到什么程度由 LR_XMP_MIMIC_ALLOW_PASSTHROUGH / _STRUCTURED 决定，
            # 不在这里写死。写入只落在一张新的虚拟副本上、原片永不修改，
            # 而写完会逐键回读并把实际值报回来。
            validate(patch)
        for key in patch:
            if key in WB_KEYS and key not in state['baseline_snapshot']:
                raise ValueError(f'{key} is not supported by this source; RAW and JPEG WB use different controls')

    def _begin(self, state, revision, request_id, operation):
        # Every mutating path (action/parameter variants) funnels
        # through here, so this is the one place the target check has to live.
        # Cheap, and it cannot be forgotten by a future caller.
        self.require_target(state)
        valid_id(request_id)
        if request_id in state['operations']:
            previous = state['operations'][request_id]
            if previous['input'] != operation:
                raise Conflict('request_id was already used with different arguments')
            return False
        if type(revision) is not int or state['revision'] != revision:
            raise Conflict('版本已更新，请重新读取会话后再操作')
        if state['status'] in ('failed', 'interrupted'):
            raise Conflict('上次操作结果不确定，请检查 LR 后重新建会话')
        state['operations'][request_id] = {'input': operation, 'status': 'running'}
        state['error'] = None
        return True

    def action(self, sid, action, base_revision, request_id, **options):
        if action not in ('edit', 'restore', 'verify', 'message'):
            raise ValueError('Unknown action')
        if action == 'edit':
            validate(options.get('settings'))
            if not isinstance(options.get('note', ''), str) or len(options.get('note', '')) > 4000:
                raise ValueError('Invalid edit note')
        if action == 'message':
            text, role = options.get('text'), options.get('role', 'user')
            if not isinstance(text, str) or not text.strip() or len(text) > 4000 or role not in ('user', 'assistant'):
                raise ValueError('Invalid message')
        with self.lock(sid):
            state = self.raw(sid)
            if action != 'message':
                self._require_source(state)
            include_snapshots = options.pop('include_snapshots', True)
            operation = {'action': action, **options}
            if not self._begin(state, base_revision, request_id, operation):
                return public_state(state, include_snapshots=include_snapshots)
            if action != 'message':
                probe = self.probe(state.get('catalog_identity'))
                self.bind_catalog(state, probe)
                if any(probe.get(k) != state['renderer'].get(k) for k in ('lr_version', 'plugin_version')):
                    raise Conflict('Renderer version changed; create a new episode')
                if action == 'edit':
                    current = self.check_version(state, state['current_version'])
                    self.validate_patch(state, {**current['patch'], **options['settings']})
                elif action in ('restore', 'verify'):
                    number = options.get('version', state['current_version'])
                    version = self.check_version(state, number)
                    if action == 'verify' and not version['patch']:
                        raise ValueError('基准版本没有可导出的修改参数')
            # draft 会话（还没绑原片）只允许 message，且留言不改变「待选原片」状态。
            state['status'] = 'draft' if not state.get('source_uuid') else {
                'edit': 'editing', 'restore': 'restoring', 'verify': 'verifying',
                'message': 'ready'}[action]
            self.save(state)
            try:
                if action == 'message':
                    text = options.get('text')
                    role = options.get('role', 'user')
                    if not isinstance(text, str) or not text.strip() or len(text) > 4000 or role not in ('user', 'assistant'):
                        raise ValueError('Invalid message')
                    state['messages'].append({'role': role, 'text': text, 'time': time.time()})
                elif action == 'edit':
                    note = options.get('note', '显式参数修改')
                    patch = {**state['versions'][state['current_version']]['patch'], **options['settings']}
                    version = self.render_version(state, patch, note)
                    version['parent_version'] = state['current_version']
                    state['versions'].append(version)
                    state['current_version'] = version['version']
                elif action == 'restore':
                    number = options.get('version', state['current_version'])
                    self.check_version(state, number)
                    self.run('select_photo', {'photo_uuid': state['versions'][number]['photo_uuid']},
                             catalog_identity=state.get('catalog_identity'))
                    state['current_version'] = number
                elif action == 'verify':
                    self.verify(state, options.get('version', state['current_version']))
                state['status'] = 'draft' if not state.get('source_uuid') else 'ready'
                state['operations'][request_id]['status'] = 'complete'
                self.save(state)
            except Exception as exc:
                state.update(status='failed', error=str(exc))
                state['operations'][request_id]['status'] = 'failed'
                self.save(state)
                raise
        return public_state(state, include_snapshots=include_snapshots)

    def check_version(self, state, number):
        if type(number) is not int or not 0 <= number < len(state['versions']):
            raise ValueError('Unknown version')
        version = state['versions'][number]
        if not equivalent_snapshot(
                self.snapshot(version['photo_uuid'], state.get('catalog_identity'))['settings'],
                version['snapshot']):
            raise Conflict('已存版本在 LR 中被修改，不能使用这个版本')
        if sha(self.directory(state['id']) / version['preview']) != version['sha256']:
            raise Conflict('Saved preview was modified')
        return version

    def verify(self, state, number):
        original = self.check_version(state, number)
        if not original['patch']:
            raise ValueError('基准版本没有可导出的修改参数')
        name = render_copy_name(state, number)
        xmp = to_xmp(original['patch'], name)
        parsed = from_xmp(xmp)
        directory = self.directory(state['id'])
        path = directory / f'v{number}.xmp'
        path.write_text(xmp, encoding='utf-8')
        replay = self.render_version(state, parsed, f'XMP 回放 v{number}')
        state['versions'].append(replay)
        comparison = compare(directory / replay['preview'], directory / original['preview'])
        passed = comparison['mae_rgb'] <= 0.1 and comparison['p95_rgb'] <= 1
        report = {'version': number, 'replay_version': replay['version'], 'passed': passed,
                  'xmp_sha256': sha(path), 'metrics': comparison,
                  'scope': 'Serialized XMP numeric patch parsed and applied by LR SDK on unchanged inherited baseline; not native preset-file import or cross-photo validation.'}
        original['verification'] = report
        atomic(directory / f'verification{number}.json', report)
        self.run('select_photo',
                 {'photo_uuid': state['versions'][state['current_version']]['photo_uuid']},
                 catalog_identity=state.get('catalog_identity'))

    def start_parameter_variants(self, sid, base_revision, request_id, parameter, values):
        """Render caller-specified values from one fixed current version.

        This is an execution primitive. It does not choose the parameter or
        values, rank variants, summarize results, or make aesthetic judgments.
        """
        spec = parameter_specs.parameter_record(parameter)
        if not isinstance(values, list) or not 1 <= len(values) <= 5:
            raise ValueError('values must contain 1..5 absolute parameter values')
        for value in values:
            validate({parameter: value})
        def variant_value_key(value):
            if isinstance(value, list):
                return tuple(variant_value_key(item) for item in value)
            if type(value) in (int, float):
                return float(value)
            return value
        if len({variant_value_key(value) for value in values}) != len(values):
            raise ValueError('Parameter variant values must be distinct')
        # Copy caller-owned arrays before the worker can observe them.
        values = json.loads(json.dumps(values))
        guard = self.lock(sid)
        guard.__enter__()
        try:
            state = self.raw(sid)
            self._require_source(state)
            operation = {'action': 'render_parameter_variants',
                         'parameter': parameter, 'values': values}
            if not self._begin(state, base_revision, request_id, operation):
                guard.__exit__(None, None, None)
                return public_state(state)
            renderer = self.probe(state.get('catalog_identity'))
            self.bind_catalog(state, renderer)
            if any(renderer.get(k) != state['renderer'].get(k) for k in ('lr_version', 'plugin_version')):
                raise Conflict('Renderer version changed; create a new episode')
            self.check_source(state)
            base = self.check_version(state, state['current_version'])
            if parameter not in base['snapshot']:
                raise ValueError('Parameter is unresolved or unavailable in this source snapshot')
            plans = [{**base['patch'], parameter: value} for value in values]
            for patch in plans:
                self.validate_patch(state, patch)
            (self.directory(sid) / 'stop').unlink(missing_ok=True)
            state.update(status='rendering_parameter_variants', parameter_variants={
                'parameter': parameter, 'values': values,
                'base_version': base['version'], 'budget': len(values), 'used': 0,
                'versions': [], 'stop_reason': None})
            self.save(state)
            response = public_state(state)
            threading.Thread(target=self._render_parameter_variants,
                             args=(state, request_id, plans, spec, guard),
                             daemon=True).start()
            return response
        except BaseException:
            guard.__exit__(None, None, None)
            raise

    def _render_parameter_variants(self, state, request_id, plans, spec, guard):
        try:
            reason = 'complete'
            parameter = spec['parameter']
            variants = state['parameter_variants']
            base = state['versions'][variants['base_version']]
            for patch in plans:
                if (self.directory(state['id']) / 'stop').exists():
                    reason = 'cancelled'
                    break
                self.check_version(state, base['version'])
                candidate = self.render_version(
                    state, patch, '参数变体 · ' + spec['title'])
                candidate['parent_version'] = base['version']
                candidate['parameter_variant'] = {
                    'parameter': parameter, 'base_version': base['version'],
                    'base_value': base['snapshot'][parameter],
                    'requested_value': patch[parameter],
                    'applied_value': candidate['snapshot'][parameter],
                    'parameter_spec': spec,
                    'parameter_specs_hash': parameter_specs.fingerprint(),
                    'base_snapshot_hash': fingerprint(base['snapshot']), 'base_preview_hash': base['sha256'],
                    'renderer': state['renderer'], 'long_edge': state['long_edge'],
                    'color_space': 'sRGB', 'scope': 'fixed_base_parameter_variant'}
                state['versions'].append(candidate)
                variants['versions'].append(candidate['version'])
                variants['used'] += 1
                self.save(state)
            self.check_version(state, base['version'])
            self.run('select_photo', {'photo_uuid': base['photo_uuid']},
                     catalog_identity=state.get('catalog_identity'))
            variants['stop_reason'] = reason
            state['status'] = 'ready'
            state['operations'][request_id]['status'] = 'complete'
            self.save(state)
        except Exception as exc:
            state.update(status='failed', error=str(exc))
            state['operations'][request_id]['status'] = 'failed'
            self.save(state)
        finally:
            guard.__exit__(None, None, None)

    def stop(self, sid):
        state = self.raw(sid)
        # The three legacy states allow an in-flight operation started before an
        # upgrade to be cancelled; new code never writes them.
        if state['status'] not in ('searching', 'rendering_parameter_variants',
                                   'exploring', 'probing'):
            raise ValueError('No cancellable render operation is running')
        (self.directory(sid) / 'stop').touch()
        return {'requested': True, 'message': '当前 LR 渲染结束后停止，保留已完成的版本'}

    def artifact(self, sid, name):
        import re
        if not re.fullmatch(r'(reference\.jpg|v\d+\.(jpg|xmp)|verification\d+\.json)', name):
            raise ValueError('Unknown artifact')
        directory = self.directory(sid).resolve()
        path = (directory / name).resolve()
        if path.parent != directory or not path.is_file():
            raise ValueError('Artifact not found')
        # Integrity checks need the full stored record: public_state deliberately
        # hides retired per-version metrics that contain the legacy reference hash.
        state = self.raw(sid)
        if not state['versions']:
            raise ValueError('No published version yet')
        if name == 'reference.jpg':
            expected = reference_digest(state)
            if not expected or sha(path) != expected:
                raise Conflict('Target render was modified')
        else:
            number = int(re.search(r'\d+', name).group())
            if number >= len(state['versions']):
                raise ValueError('Version has not been published')
            version = state['versions'][number]
            if name.endswith('.jpg') and sha(path) != version['sha256']:
                raise Conflict('Version render was modified')
        if name.endswith('.xmp'):
            version = state['versions'][int(name[1:-4])]
            report = version.get('verification') or {}
            if not report.get('passed') or sha(path) != report['xmp_sha256']:
                raise ValueError('XMP has not passed replay verification')
        return path
