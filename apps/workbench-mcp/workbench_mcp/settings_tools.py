"""Photo-addressed working-copy tools, independent of legacy Sessions."""

UUID = {'type': 'string', 'pattern': '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'}
TOKEN = {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}


def tool(name, description, properties, required):
    return {'name': name, 'description': description, 'inputSchema': {
        'type': 'object', 'additionalProperties': False,
        'properties': properties, 'required': required}}


TOOLS = [
    tool('trlr_apply_settings',
         'Apply an explicit develop patch directly to one existing virtual copy by photo_uuid. '
         'Creates NO virtual copy and NO Session. Repeated edits keep the same UUID. '
         'Requires a fresh settings_token from trlr_get_photo_settings and a unique request_id. '
         'The token binds full settings, photo, Target, Catalog and access mode. '
         'The plugin checks the captured state inside its Catalog write gate; masters are refused. '
         'Returns actual-value readback, mismatches, durable operation_id and optionally a '
         'controlled preview image. preview_error means the edit has verified value readback '
         'but no verified image; it does not make the write unknown. '
         'A duplicate request returns its stored result without rewriting. '
         'Unknown writes block further edits on this photo; query trlr_get_settings_operation, '
         'never resubmit under another ID. Explicitly editing an Episode copy changes its live '
         'state; old Episode snapshots/renders remain historical and are not updated.',
         {'photo_uuid': {'type': 'string', 'minLength': 1, 'maxLength': 256},
          'settings': {'type': 'object', 'minProperties': 1, 'additionalProperties': True},
          'expected_settings_token': TOKEN, 'request_id': UUID,
          'render': {'type': 'boolean', 'default': True}},
         ['photo_uuid', 'settings', 'expected_settings_token', 'request_id']),
    tool('trlr_get_settings_operation',
         'Read a durable in-place edit record without contacting Lightroom or replaying a write. '
         'details=true includes captured before/after settings. include_preview=true returns '
         'the saved hash-verified render if available. prepared/dispatching/unknown is not '
         'permission to retry. Records survive Runtime restart.',
         {'operation_id': UUID, 'details': {'type': 'boolean', 'default': False},
          'include_preview': {'type': 'boolean', 'default': False}}, ['operation_id']),
    tool('trlr_reset',
         'Restore the changed parameter VALUES of one completed in-place edit on the same '
         'virtual copy, using its captured before values. Does not create a copy or revive '
         'Session. Requires a fresh settings_token and unique request_id; refuses if the '
         'photo has changed since the original operation. Fields absent from the original '
         'snapshot cannot be restored. This is parameter-value undo, not a full snapshot '
         'restore: derived LR metadata such as WhiteBalance mode may remain Custom.',
         {'operation_id': UUID, 'expected_settings_token': TOKEN, 'request_id': UUID,
          'render': {'type': 'boolean', 'default': True}},
         ['operation_id', 'expected_settings_token', 'request_id']),
]
