-- Model Lightroom's omission reset so this fails with a patch-only preset.
local plugin = assert(arg[1], 'plugin directory required')
package.path = plugin .. '/?.lua;' .. package.path
local function clone(value)
    local result = {}; for key, item in pairs(value) do result[key] = item end; return result
end
local initial = { Exposure2012 = 0, Temperature = 6150, Tint = 17,
    WhiteBalance = 'As Shot', GrainAmount = 28, CameraProfile = 'Camera NT',
    ToneCurveName2012 = 'Custom', ToneCurvePV2012 = {0, 0, 255, 255} }
local photo = { settings = clone(initial), virtual = true }
function photo:getDevelopSettings() return self.settings end
function photo:getRawMetadata(key)
    if key == 'isVirtualCopy' then return self.virtual end
    if key == 'uuid' then return 'copy' end
    if key == 'dimensions' then return {} end
end
function photo:getFormattedMetadata() return '' end
function photo:applyDevelopPreset(preset)
    self.settings = clone(preset)
    self.settings.Temperature = preset.Temperature or 2000
    self.settings.Tint = preset.Tint or -150
    self.settings.WhiteBalance = preset.WhiteBalance or 'Custom'
end
local catalog = {}
function catalog:findPhotoByUuid() return photo end
function catalog:withWriteAccessDo(_, callback) callback(); return 'executed' end
import = function(name)
    if name == 'LrApplication' then return {
        activeCatalog = function() return catalog end,
        addDevelopPresetForPlugin = function(_, _, settings) return settings end,
    } end
    if name == 'LrTasks' then return { pcall = pcall, sleep = function() end } end
    return {}
end
local Commands = assert(loadfile(plugin .. '/Commands.lua'))()
Commands.apply_candidate { photo_uuid = 'copy', settings = { Exposure2012 = 0.35 } }
assert(photo.settings.Temperature == 6150 and photo.settings.Tint == 17 and photo.settings.WhiteBalance == 'As Shot',
    'exposure-only patches must preserve RAW white balance')
assert(photo.settings.GrainAmount == 28 and photo.settings.CameraProfile == 'Camera NT'
    and photo.settings.ToneCurveName2012 == 'Custom', 'unrequested develop values must survive')
photo.settings = clone(initial)
Commands.apply_candidate { photo_uuid = 'copy', settings = { Tint = 23 } }
assert(photo.settings.Temperature == 6150 and photo.settings.Tint == 23 and photo.settings.WhiteBalance == 'Custom',
    'a single WB-axis patch must retain the other axis')
photo.settings = { Exposure2012 = 0, IncrementalTemperature = 12, IncrementalTint = -9, WhiteBalance = 'Custom' }
Commands.apply_candidate { photo_uuid = 'copy', settings = { Exposure2012 = 0.5 } }
assert(photo.settings.IncrementalTemperature == 12 and photo.settings.IncrementalTint == -9,
    'rendered-file relative WB must also survive')
photo.virtual = false
assert(not pcall(Commands.apply_candidate, { photo_uuid = 'copy', settings = { Exposure2012 = 1 } }),
    'the existing master protection must remain enforced')
print('white-balance-patch-test: passed')
