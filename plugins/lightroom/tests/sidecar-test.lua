local plugin = assert(arg[1], 'plugin directory required')
package.path = plugin .. '/?.lua;' .. package.path
local photo = { virtual = false }
function photo:getRawMetadata(key)
    if key == 'uuid' then return 'master' end
    if key == 'path' then return '/fixtures/image.ARW' end
    if key == 'isVirtualCopy' then return self.virtual end
end
function photo:getDevelopSettings() return { Exposure2012 = .2 } end
local catalog = {}
function catalog:findPhotoByUuid(uuid) if uuid == 'master' then return photo end end
function catalog:withReadAccessDo(callback) callback() end
import = function(name)
    if name == 'LrApplication' then return { activeCatalog = function() return catalog end } end
    error('Sidecar resolver must not request filesystem/UI modules')
end
local Sidecar = require 'Sidecar'
assert(not pcall(Sidecar.prepare, {photo_uuid='master'}), 'old service must not receive private path context')
local params = { photo_uuid = 'master', _sidecar_protocol = 1 }
local result = Sidecar.prepare(params)
assert(result._sidecar_context.photo_uuid == 'master' and result._sidecar_context.source_path == '/fixtures/image.ARW')
params.action = 'update'
assert(not pcall(Sidecar.prepare, params), 'master sidecar writes require explicit XMP confirmation')
params.confirm_xmp_mode = true
assert(Sidecar.prepare(params)._sidecar_context.develop_settings.Exposure2012 == .2)
photo.virtual = true
assert(not pcall(Sidecar.prepare, params), 'virtual copies must not address the shared sidecar')
print('sidecar-test: passed')
