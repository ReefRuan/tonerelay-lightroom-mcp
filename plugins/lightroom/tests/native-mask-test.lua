local plugin = assert(arg[1], 'plugin directory required')
package.path = plugin .. '/?.lua;' .. package.path
local function clone(v)
    if type(v) ~= 'table' then return v end
    local out = {}; for k, item in pairs(v) do out[k] = clone(item) end; return out
end
local initial = {Exposure2012=.32, Temperature=4050, Tint=6, WhiteBalance='As Shot',
    MaskGroupBasedCorrections={{CorrectionSyncID='existing', CorrectionName='Keep me', CorrectionActive=true,
        LocalExposure2012=0, CorrectionMasks={{What='Mask/Image', MaskID='old', MaskDigest='old-digest', WholeImageArea='0,0,1,1'}}}}}
local photo = {settings=clone(initial), virtual=true}
function photo:getDevelopSettings() return clone(self.settings) end
function photo:getRawMetadata(key) if key == 'uuid' then return 'copy' elseif key == 'isVirtualCopy' then return self.virtual end end
function photo:applyDevelopSettings(settings)
    for k, value in pairs(settings) do self.settings[k] = clone(value) end
end
local selected, module, calls, pending, ticks, interfere, failCreate, generate = photo, 'library', 0, false, 0, false, false, true
local cat = {}
function cat:getPath() return '/catalog.lrcat' end
function cat:findPhotoByUuid(id) if id == 'copy' then return photo end end
function cat:getTargetPhoto() return selected end
function cat:withWriteAccessDo(_, fn) fn(); return 'executed' end
local controller = {goToMasking=function() end}
function controller.createNewMask(kind, subtype)
    assert(kind == 'aiSelection' and subtype == 'subject')
    calls = calls+1
    if failCreate then error('SDK unavailable') end
    pending=true; ticks=0
    return nil
end
function photo:updateAISettings()
    for _, group in ipairs(self.settings.MaskGroupBasedCorrections) do
        for _, component in ipairs(group.CorrectionMasks) do
            component.MaskDigest='generated'; component.WholeImageArea='0,0,1,1'; component.MaskID=component.MaskID or 'native'
        end
    end
end
import = function(name)
    if name == 'LrApplication' then return {activeCatalog=function() return cat end} end
    if name == 'LrApplicationView' then return {getCurrentModuleName=function() return module end, switchToModule=function(m) module=m end} end
    if name == 'LrDevelopController' then return controller end
    if name == 'LrMD5' then return {digest=function(s) local n=0;for i=1,#s do n=(n*31+s:byte(i))%2147483647 end;return string.format('%032x',n) end} end
    if name == 'LrTasks' then return {pcall=pcall,sleep=function()
        if interfere then selected={} end
        if pending and generate then
            ticks=ticks+1
            if ticks == 2 then
                photo.settings.MaskGroupBasedCorrections[#photo.settings.MaskGroupBasedCorrections+1]={
                    CorrectionSyncID='new-id', CorrectionName='Mask 2', CorrectionActive=true, LocalExposure2012=0,
                    CorrectionMasks={{What='Mask/Image',MaskID='generated',MaskDigest='new-digest',WholeImageArea='0,0,1,1'}}}
                pending=false
            end
        end
    end} end
    return {}
end
package.loaded.UiContext={capture=function() return {target=selected,module=module} end,
    catalogMatches=function() return true end, owns=function() return selected == photo end,
    restore=function(c) selected=c.target;module=c.module;return {status='restored'} end,
    unavailable=function() return {status='unavailable'} end}
local Safety = require 'Safety'
local Masks = require 'NativeMasks'
local function args(action)
    return {photo_uuid='copy', action=action, mask_type='subject', expected_access_mode='full',
        expected_catalog_identity=Safety.catalogIdentity(cat:getPath()), request_id='test',
        expected_token=Masks.run({photo_uuid='copy'}, function() end).expected_token}
end
local p=args('create');p.name='SDK Subject';p.settings={LocalExposure2012=.1}
local result=Masks.run(p,function() selected=photo end)
assert(result.status == 'created' and result.catalog_applied and result.segmentation_data_present)
assert(calls == 1 and result.correction_id == 'new-id', 'nil SDK return must poll, not repeat creation')
assert(photo.settings.Exposure2012 == .32 and photo.settings.Temperature == 4050)
assert(photo.settings.MaskGroupBasedCorrections[1].CorrectionName == 'Keep me')
assert(photo.settings.MaskGroupBasedCorrections[2].LocalExposure2012 == .1)
assert(module == 'library' and result.ui_context.status == 'restored')
local u=args('update');u.mask_type=nil;u.correction_id='new-id';u.settings={LocalTint=.2}
assert(Masks.run(u,function() selected=photo end).status == 'applied')
photo.virtual=false
assert(not pcall(Masks.run,args('create'),function() end), 'master writes must fail')
photo.virtual=true
local bad=args('create');bad.expected_token='stale'
assert(not pcall(Masks.run,bad,function() end), 'stale token must fail')
assert(calls == 1)
photo.settings=clone(initial);generate=false;pending=false
result=Masks.run(args('create'),function() selected=photo end)
assert(result.status == 'creation_pending_or_failed' and calls == 2 and not result.catalog_applied)
generate=true;pending=false;interfere=true
result=Masks.run(args('create'),function() selected=photo end)
assert(result.status == 'failed' and result.failure_phase == 'prepare' and calls == 2)
interfere=false;selected=photo;pending=false;failCreate=true
result=Masks.run(args('create'),function() selected=photo end)
assert(result.status == 'failed' and result.failure_phase == 'create_requested' and calls == 3)
failCreate=false
photo.settings.MaskGroupBasedCorrections[1].CorrectionMasks[1].MaskDigest=nil
assert(Masks.run(args('recompute'),function() selected=photo end).status == 'applied')
print('native-mask-test: passed')
