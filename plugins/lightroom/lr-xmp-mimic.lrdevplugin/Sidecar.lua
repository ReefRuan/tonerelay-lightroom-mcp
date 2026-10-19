-- Resolve UUIDs in the guarded active Catalog. XML/file editing runs in lr-ops
-- on this same host, after the result enters its durable finalizing state.
-- No guessed readMetadata/saveMetadata SDK calls or UI automation live here.
local LrApplication = import 'LrApplication'
local Sidecar = {}

function Sidecar.prepare(params)
    if type(params) ~= 'table' or type(params.photo_uuid) ~= 'string' then
        error('Sidecar operations require photo_uuid.')
    end
    if params._sidecar_protocol ~= 1 then
        error('Sidecar operations require the current lr-ops finalization protocol.')
    end
    local catalog = LrApplication.activeCatalog()
    local photo = catalog:findPhotoByUuid(params.photo_uuid)
    if not photo then error('Sidecar photo was not found in the active Catalog.') end
    if photo:getRawMetadata('isVirtualCopy') then
        error('Virtual copies share the master XMP; explicitly name the master UUID.')
    end
    local action = params.action or 'inspect'
    if action ~= 'inspect' and action ~= 'verify' and params.confirm_xmp_mode ~= true then
        error('Writing a master sidecar requires explicit confirm_xmp_mode=true.')
    end
    local context
    catalog:withReadAccessDo(function()
        context = {
            photo_uuid = photo:getRawMetadata('uuid'),
            source_path = photo:getRawMetadata('path'),
            is_virtual_copy = false,
            develop_settings = photo:getDevelopSettings() or {},
        }
    end)
    return { _sidecar_context = context }
end

return Sidecar
