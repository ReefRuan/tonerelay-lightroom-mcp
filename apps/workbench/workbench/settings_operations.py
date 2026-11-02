"""Durable, guarded edits on one existing virtual copy; no Session store."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .artifacts import build_artifact_ref, MAX_BYTES
from .studio.engine import Studio, Conflict, fingerprint, same_value, valid_id
from .studio.settings import validate, WB_KEYS


def settings_scope(photo_uuid, probe, client):
    return {'photo_uuid': photo_uuid, 'catalog_identity': probe['catalog_identity'],
            'access_mode': probe['access_mode'],
            'target_id': getattr(client, 'target_id', None),
            'endpoint_fingerprint': fingerprint(str(getattr(client, 'base', '')))}


def settings_token(photo_uuid, settings, probe, client):
    return fingerprint({'scope': settings_scope(photo_uuid, probe, client),
                        'settings': settings})


class SettingsOperations:
    """A database claim serializes writers, including across Runtime processes.

    Commit the claim before dispatch. An interrupted claim stays blocked until
    its original operation is explicitly reconciled; a new request cannot
    silently retry it. SQLite never holds a transaction across Lightroom I/O.
    """
    def __init__(self, wb):
        self.wb = wb
        self.root = Path(wb.store.root) / 'settings-operations'
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / 'operations.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS operations '
                       '(id TEXT PRIMARY KEY, payload TEXT NOT NULL, '
                       'scope TEXT NOT NULL, record TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS claims '
                       '(scope TEXT PRIMARY KEY, operation_id TEXT NOT NULL)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def encode(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)

    def _raw(self, operation_id):
        valid_id(operation_id)
        with self.connect() as db:
            row = db.execute('SELECT record FROM operations WHERE id=?',
                             (operation_id,)).fetchone()
        if not row:
            raise ValueError('Settings operation not found')
        return json.loads(row[0])

    def get(self, operation_id, details=False):
        if type(details) is not bool:
            raise ValueError('details must be boolean')
        record = self._raw(operation_id)
        if details:
            return record
        return {key: value for key, value in record.items()
                if key not in ('before', 'after', 'input')}

    def _existing(self, request_id, payload):
        with self.connect() as db:
            row = db.execute('SELECT payload, record FROM operations WHERE id=?',
                             (request_id,)).fetchone()
        if row:
            if row[0] != self.encode(payload):
                raise Conflict('request_id was already used with different arguments')
            return self.get(request_id)

    def _claim(self, request_id, payload, scope):
        scope_key = fingerprint({key: value for key, value in scope.items()
                                 if key != 'access_mode'})
        record = {'operation_id': request_id, 'status': 'prepared',
                  'scope': scope, 'photo_uuid': scope['photo_uuid'],
                  'input': payload, 'created_at': time.time(),
                  'new_virtual_copies': 0}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM operations WHERE id=?',
                             (request_id,)).fetchone()
            if row:
                if row[0] != self.encode(payload):
                    raise Conflict('request_id was already used with different arguments')
                return False
            busy = db.execute('SELECT operation_id FROM claims WHERE scope=?',
                              (scope_key,)).fetchone()
            if busy:
                raise Conflict('Previous edit is running or unknown; query operation ' + busy[0])
            db.execute('INSERT INTO operations VALUES (?,?,?,?)',
                       (request_id, self.encode(payload), scope_key, self.encode(record)))
            db.execute('INSERT INTO claims VALUES (?,?)', (scope_key, request_id))
        return True

    def _save(self, record, release=False):
        record['updated_at'] = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE operations SET record=? WHERE id=?',
                       (self.encode(record), record['operation_id']))
            if release:
                db.execute('DELETE FROM claims WHERE operation_id=?',
                           (record['operation_id'],))

    @staticmethod
    def _snapshot(reader, photo_uuid, identity):
        snapshot = reader.snapshot(photo_uuid, identity)
        if (not isinstance(snapshot, dict) or not isinstance(snapshot.get('photo'), dict)
                or snapshot['photo'].get('uuid') != photo_uuid
                or snapshot['photo'].get('is_virtual_copy') is not True
                or not isinstance(snapshot.get('settings'), dict)):
            raise ValueError('In-place edits require the exact existing virtual copy; masters refused')
        return snapshot['settings']

    def apply(self, photo_uuid, settings, expected_settings_token, request_id,
              render=True, *, reset_operation_id=None):
        valid_id(request_id)
        if not isinstance(photo_uuid, str) or not photo_uuid.strip() or len(photo_uuid) > 256:
            raise ValueError('photo_uuid must be a nonempty string of at most 256 characters')
        if (not isinstance(expected_settings_token, str)
                or len(expected_settings_token) != 64
                or any(c not in '0123456789abcdef' for c in expected_settings_token)):
            raise ValueError('A fresh settings_token from trlr_get_photo_settings is required')
        if type(render) is not bool:
            raise ValueError('render must be boolean')
        settings = validate(settings)
        payload = {'photo_uuid': photo_uuid, 'settings': settings,
                   'expected_settings_token': expected_settings_token, 'render': render,
                   'reset_operation_id': reset_operation_id}
        previous = self._existing(request_id, payload)
        if previous is not None:
            return previous
        client = self.wb.lrops.pin() if hasattr(self.wb.lrops, 'pin') else self.wb.lrops
        reader = Studio(self.wb.store.root, client)
        probe = reader.probe()
        if probe.get('capabilities', {}).get('in_place_settings_v1') is not True:
            raise ValueError('Load the Lightroom plugin with in_place_settings_v1 before editing')
        scope = settings_scope(photo_uuid, probe, client)
        identity = scope['catalog_identity']
        before = self._snapshot(reader, photo_uuid, identity)
        if settings_token(photo_uuid, before, probe, client) != expected_settings_token:
            raise Conflict('Photo settings or Target/Catalog changed; read fresh settings')
        if reset_operation_id:
            original = self._raw(reset_operation_id)
            if original['scope'] != scope or original.get('after') != before:
                raise Conflict('Photo changed since that edit; reset refused to preserve later edits')
        for key in settings:
            if key in WB_KEYS and key not in before:
                raise ValueError(key + ' is not supported by this source')
        if not self._claim(request_id, payload, scope):
            return self.get(request_id)
        record = self._raw(request_id)
        dispatched = False
        try:
            fresh = self._snapshot(reader, photo_uuid, identity)
            if fingerprint(fresh) != fingerprint(before):
                raise Conflict('Photo changed before dispatch; read fresh settings')
            record.update(before=before, status='dispatching')
            self._save(record)
            # The plugin checks the entire captured state again inside its
            # Catalog write gate. Python's preflight alone is not a write CAS.
            dispatched = True
            applied = reader.run('apply_candidate', {
                'photo_uuid': photo_uuid, 'settings': settings,
                'require_in_place_guard': True, 'expected_settings_snapshot': before,
                'request_id': request_id,
            }, catalog_identity=identity)
            if applied.get('status') == 'conflict' and applied.get('applied') is False:
                record.update(status='rejected', error='Photo changed inside the write gate; read fresh settings')
                self._save(record, release=True)
                return self.get(request_id)
            after = self._snapshot(reader, photo_uuid, identity)
            second = self._snapshot(reader, photo_uuid, identity)
            if fingerprint(after) != fingerprint(second):
                raise Conflict('Photo settings changed during readback')
            record['after'] = after
            mismatches = [{'key': key, 'requested': value, 'applied': after.get(key)}
                          for key, value in settings.items()
                          if not same_value(after.get(key), value)]
            if applied.get('after_pending'):
                record['readback_pending_reported'] = True
            record.update(settings_token=settings_token(photo_uuid, after, probe, client),
                          before_settings_sha256=fingerprint(before),
                          after_settings_sha256=fingerprint(after),
                          readback_mismatches=mismatches,
                          applied={key: after.get(key) for key in settings})
            derived = {'WhiteBalance', 'ToneCurveName2012', 'ToneCurveName'}
            unexpected = [key for key in sorted(before.keys() | after.keys())
                          if key not in settings and key not in derived
                          and not same_value(before.get(key), after.get(key))]
            record['unrequested_changes'] = unexpected
            self._save(record)
            if render:
                try:
                    directory = self.root / request_id
                    directory.mkdir(exist_ok=True)
                    exported = reader.run('export_preview', {
                        'photo_uuid': photo_uuid,
                        'long_edge': self.wb.presentation.get()['edit_render_max_edge'],
                    }, catalog_identity=identity)
                    if exported.get('color_space') != 'sRGB' or not exported.get('output_ref'):
                        raise ValueError('Invalid controlled export response')
                    path = directory / 'preview.jpg'
                    client.fetch_output(exported['output_ref'], path)
                    if fingerprint(self._snapshot(reader, photo_uuid, identity)) != fingerprint(after):
                        raise Conflict('Photo changed during render')
                    record['preview'] = build_artifact_ref(
                        path, artifact_id=f'settings-operation:{request_id}',
                        source={'kind': 'settings_operation', 'operation_id': request_id,
                                **scope}, purpose='verified', method='LrExportSession',
                        freshness='verified', evidence_status='verified_render', color_space='sRGB')
                except Exception as exc:
                    # The edit already has durable, verified value readback.
                    # A failed preview must not turn that known write into an
                    # unknown operation or permanently lock further edits.
                    record['preview_error'] = str(exc)
            record['status'] = 'completed_with_mismatches' if mismatches or unexpected else 'completed'
            self._save(record, release=True)
        except Exception as exc:
            record.update(status='unknown' if dispatched else 'rejected', error=str(exc))
            self._save(record, release=not dispatched)
        return self.get(request_id)

    def reset(self, operation_id, expected_settings_token, request_id, render=True):
        original = self._raw(operation_id)
        if original['status'] not in ('completed', 'completed_with_mismatches'):
            raise Conflict('Only a completed edit can be reset')
        keys = original['input']['settings']
        before = original['before']
        if any(key not in before for key in keys):
            raise Conflict('A changed field had no baseline value; exact field reset unavailable')
        return self.apply(original['photo_uuid'], {key: before[key] for key in keys},
                          expected_settings_token, request_id, render,
                          reset_operation_id=operation_id)

    def preview_bytes(self, operation_id):
        record = self._raw(operation_id)
        artifact = record.get('preview')
        if not artifact:
            raise ValueError('This operation has no verified preview')
        path = self.root / operation_id / 'preview.jpg'
        if path.stat().st_size > MAX_BYTES:
            raise ValueError('Saved preview exceeds artifact limit')
        content = path.read_bytes()
        import hashlib
        if hashlib.sha256(content).hexdigest() != artifact['sha256']:
            raise ValueError('Saved preview hash mismatch')
        return content
