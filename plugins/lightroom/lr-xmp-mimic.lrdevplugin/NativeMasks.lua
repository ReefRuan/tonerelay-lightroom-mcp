-- Native SDK operations. No keystrokes, file imports or controller slider writes.
local LrApplication = import 'LrApplication'
local LrApplicationView = import 'LrApplicationView'
local LrTasks = import 'LrTasks'
local Json = require 'Json'
local Safety = require 'Safety'
local UiContext = require 'UiContext'
local Masks = {}
local fields = {}
for name in ('LocalExposure2012 LocalContrast2012 LocalHighlights2012 LocalShadows2012 LocalWhites2012 LocalBlacks2012 LocalClarity2012 LocalDehaze LocalTexture LocalSaturation LocalSharpness LocalLuminanceNoise LocalMoire LocalDefringe LocalTemperature LocalTint'):gmatch('%S+') do fields[name] = true end

local function canonical(value)
    if type(value) ~= 'table' then return Json.encode(value) end
    local keys, chunks = {}, {}
    for key in pairs(value) do keys[#keys + 1] = key end
    table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
    for _, key in ipairs(keys) do chunks[#chunks + 1] = Json.encode(tostring(key)) .. ':' .. canonical(value[key]) end
    return '{' .. table.concat(chunks, ',') .. '}'
end

local function token(photo, settings, cat)
    local LrMD5 = import 'LrMD5'
    return 'native-mask-md5-v1:' .. string.lower(LrMD5.digest(canonical({
        photo_uuid = photo:getRawMetadata('uuid'), catalog_identity = Safety.catalogIdentity(cat:getPath()), settings = settings })))
end

local function nonempty(value, limit) return type(value) == 'string' and #value > 0 and #value <= limit end
local function validate(params)
    local allowed = {photo_uuid=true, action=true, mask_type=true, correction_id=true, settings=true, name=true,
        request_id=true, expected_token=true, expected_access_mode=true, expected_catalog_identity=true}
    for key in pairs(params) do if not allowed[key] then error('Unknown native-mask argument') end end
    if not nonempty(params.photo_uuid, 256) then error('photo_uuid required') end
    local action = params.action or 'inspect'
    if action ~= 'inspect' and action ~= 'create' and action ~= 'update' and action ~= 'recompute' then error('Invalid action') end
    if action ~= 'inspect' and (not nonempty(params.request_id, 128) or not nonempty(params.expected_token, 64)) then error('Token and request_id required') end
    if action == 'create' and params.mask_type ~= 'subject' and params.mask_type ~= 'sky' and params.mask_type ~= 'background' then error('Unsupported AI selection') end
    if action == 'update' and not nonempty(params.correction_id, 256) then error('correction_id required') end
    if params.name ~= nil and not nonempty(params.name, 256) then error('Invalid name') end
    local patch = params.settings or {}
    if type(patch) ~= 'table' then error('settings must be an object') end
    for key, value in pairs(patch) do
        if not fields[key] or type(value) ~= 'number' or value ~= value or math.abs(value) == math.huge then error('Invalid local setting') end
    end
    if (action == 'inspect' or action == 'recompute') and (next(patch) or params.name ~= nil) then error('Action does not accept adjustments') end
    if action == 'update' and not next(patch) and params.name == nil then error('Empty update') end
    return action
end

local function groups(settings) return settings.MaskGroupBasedCorrections or {} end
local function groupById(settings, id)
    local found
    for _, group in ipairs(groups(settings)) do
        if group.CorrectionSyncID == id then
            if found then error('Ambiguous correction_id') end
            found = group
        end
    end
    return found
end

local function summary(settings)
    local result = Json.array()
    for _, group in ipairs(groups(settings)) do
        local entry = {correction_id=group.CorrectionSyncID, name=group.CorrectionName,
            active=group.CorrectionActive ~= false, settings={}, components=Json.array(), segmentation_data_present=true}
        for key, value in pairs(group) do if fields[key] then entry.settings[key] = value end end
        local components = group.CorrectionMasks or {}
        if #components == 0 then entry.segmentation_data_present = false end
        for _, component in ipairs(components) do
            local generated = component.What ~= 'Mask/Image' or (nonempty(component.MaskDigest, 256)
                and nonempty(component.WholeImageArea, 512) and nonempty(component.MaskID, 256))
            local active = component.MaskActive ~= false and (component.ErrorReason == nil or component.ErrorReason == 0)
            if not generated or not active then entry.segmentation_data_present = false end
            entry.components[#entry.components+1] = {mask_id=component.MaskID, mask_sync_id=component.MaskSyncID,
                what=component.What, name=component.MaskName, active=active, error_reason=component.ErrorReason,
                mask_digest=component.MaskDigest, mask_subtype=component.MaskSubType, geometry_present=generated}
        end
        result[#result+1] = entry
    end
    return result
end

local function same(a, b) return canonical(a) == canonical(b) end
local function mismatch(before, after, id, params)
    local out = Json.array()
    for key, value in pairs(before) do
        if key ~= 'MaskGroupBasedCorrections' and key ~= 'HasSettings' and key ~= 'UUID' and not same(value, after[key]) then out[#out+1] = key end
    end
    for _, group in ipairs(groups(before)) do
        if group.CorrectionSyncID ~= id then
            local current = groupById(after, group.CorrectionSyncID)
            if not current or not same(group, current) then out[#out+1] = 'existing_mask:' .. tostring(group.CorrectionSyncID) end
        end
    end
    local group = groupById(after, id)
    for key, value in pairs(params.settings or {}) do
        if not group or type(group[key]) ~= 'number' or math.abs(group[key]-value) > 1e-6 then out[#out+1] = key end
    end
    if params.name and (not group or group.CorrectionName ~= params.name) then out[#out+1] = 'CorrectionName' end
    return out
end

function Masks.run(params, prepareSelection)
    local action = validate(params)
    local cat = LrApplication.activeCatalog()
    local photo = cat:findPhotoByUuid(params.photo_uuid)
    if not photo then error('Photo not found') end
    local before = photo:getDevelopSettings() or {}
    if action == 'inspect' then
        return {status='inspected', photo_uuid=params.photo_uuid, expected_token=token(photo, before, cat),
            is_virtual_copy=photo:getRawMetadata('isVirtualCopy'), masks=summary(before), coverage_verified=false}
    end
    if not photo:getRawMetadata('isVirtualCopy') then error('Native SDK mask writes require an isolated virtual copy') end
    if token(photo, before, cat) ~= params.expected_token then error('Native mask conflict: inspect again') end
    if action == 'update' and not groupById(before, params.correction_id) then error('Mask not found') end
    local LrDevelopController = import 'LrDevelopController'
    local context = UiContext.capture(cat)
    local result = {photo_uuid=params.photo_uuid, status='not_started', mask_exists=false,
        segmentation_data_present=false, catalog_applied=false, coverage_verified=false,
        preview_required=true, before_settings=before}
    local phase = 'prepare'
    local ok, failure = LrTasks.pcall(function()
        prepareSelection(photo, context)
        context.expected_module = 'develop'
        LrApplicationView.switchToModule('develop')
        for attempt = 1, 30 do
            if cat:getTargetPhoto() == photo and LrApplicationView.getCurrentModuleName() == 'develop' then break end
            LrTasks.sleep(0.1)
        end
        local function guard()
            Safety.authorize('native_mask', params, cat:getPath())
            if not UiContext.catalogMatches(context) or not UiContext.owns(context) or cat:getTargetPhoto() ~= photo
                or LrApplicationView.getCurrentModuleName() ~= 'develop' then error('Develop target changed') end
        end
        guard()
        LrDevelopController.goToMasking()
        LrTasks.sleep(0.5)
        guard()
        if token(photo, photo:getDevelopSettings() or {}, cat) ~= params.expected_token then error('Native mask conflict after loading photo: inspect again') end
        local id = params.correction_id
        if action == 'create' then
            local old = {}
            for _, group in ipairs(groups(before)) do old[group.CorrectionSyncID] = true end
            phase = 'create_requested'
            -- The SDK can return nil while asynchronous inference continues.
            -- Issue creation ONCE, then observe new Catalog IDs; never replay it.
            local sdkReturn = LrDevelopController.createNewMask('aiSelection', params.mask_type)
            result.sdk_return = sdkReturn ~= nil and tostring(sdkReturn) or nil
            for attempt = 1, 300 do
                guard()
                local newlyCreated = {}
                for _, group in ipairs(groups(photo:getDevelopSettings() or {})) do
                    if group.CorrectionSyncID and not old[group.CorrectionSyncID] then newlyCreated[#newlyCreated+1] = group end
                end
                if #newlyCreated > 1 then error('Multiple new masks appeared; refusing to choose') end
                if #newlyCreated == 1 then
                    id = newlyCreated[1].CorrectionSyncID
                    local item = summary({MaskGroupBasedCorrections=newlyCreated})[1]
                    if item.segmentation_data_present then break end
                end
                LrTasks.sleep(0.1)
            end
            result.correction_id = id
            if not id then result.status='creation_pending_or_failed'; return end
        elseif action == 'recompute' then
            if type(photo.updateAISettings) ~= 'function' then error('SDK updateAISettings unavailable') end
            phase = 'recompute_requested'
            photo:updateAISettings()
        end
        result.correction_id = id
        if id and (next(params.settings or {}) or params.name) then
            guard()
            local settings = photo:getDevelopSettings() or {}
            local target = groupById(settings, id)
            if not target then error('Created mask disappeared') end
            for key, value in pairs(params.settings or {}) do target[key] = value end
            if params.name then target.CorrectionName = params.name end
            phase = 'adjustment_requested'
            local writeResult = cat:withWriteAccessDo('ToneRelay native mask adjustment', function()
                guard()
                photo:applyDevelopSettings({MaskGroupBasedCorrections=settings.MaskGroupBasedCorrections}, 'ToneRelay mask adjustment')
            end, {timeout=15})
            if writeResult ~= nil and writeResult ~= 'executed' then error('Mask write gate did not execute') end
        end
        phase = 'readback'
        for attempt = 1, 300 do
            guard()
            local after = photo:getDevelopSettings() or {}
            local entries = summary(after)
            local generated = #entries > 0
            local exists = false
            for _, item in ipairs(entries) do
                if (action == 'recompute' or item.correction_id == id) then
                    if item.correction_id == id then exists = true end
                    if not item.segmentation_data_present then generated = false end
                end
            end
            local mismatches = mismatch(before, after, id, params)
            if action == 'recompute' then
                mismatches = Json.array(); exists = #entries > 0
                for key, value in pairs(before) do
                    if key ~= 'MaskGroupBasedCorrections' and key ~= 'HasSettings' and key ~= 'UUID' and not same(value, after[key]) then mismatches[#mismatches+1] = key end
                end
                for _, old in ipairs(groups(before)) do
                    local current = groupById(after, old.CorrectionSyncID)
                    if not current then mismatches[#mismatches+1] = 'missing_mask:' .. tostring(old.CorrectionSyncID)
                    else
                        for key, value in pairs(old) do
                            if (fields[key] or key == 'CorrectionName' or key == 'CorrectionActive') and not same(value, current[key]) then mismatches[#mismatches+1] = key end
                        end
                    end
                end
            end
            result.masks=entries; result.mask_exists=exists; result.segmentation_data_present=generated and exists
            result.readback_mismatches=mismatches; result.after_settings=after
            if result.segmentation_data_present and #mismatches == 0 then
                result.catalog_applied=true; result.status=action == 'create' and 'created' or 'applied'; break
            end
            LrTasks.sleep(0.1)
        end
        if not result.catalog_applied then result.status='readback_pending_or_mismatch' end
        result.expected_token=token(photo, photo:getDevelopSettings() or {}, cat)
    end)
    if not ok then result.status='failed'; result.error=tostring(failure); result.failure_phase=phase end
    local restored, report = LrTasks.pcall(function() return UiContext.restore(context) end)
    result.ui_context=restored and report or UiContext.unavailable('restore_failed')
    return result
end

return Masks
