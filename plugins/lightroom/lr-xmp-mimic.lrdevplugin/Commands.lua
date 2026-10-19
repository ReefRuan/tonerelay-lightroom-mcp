-- Phase 2 bridge commands. Every command takes the job params table and
-- returns a result table (or raises via error()). Bridge.lua authorizes every
-- job through Safety before dispatch; write commands never touch master photos.
local LrApplication = import 'LrApplication'
local LrApplicationView = import 'LrApplicationView'
local LrExportSession = import 'LrExportSession'
local LrFileUtils = import 'LrFileUtils'
local LrPathUtils = import 'LrPathUtils'
local LrTasks = import 'LrTasks'

local Safety = require 'Safety'
local Catalog = require 'Catalog'
local Json = require 'Json'
local Presets = require 'Presets'
local State = require 'State'
local Util = require 'Util'

local Commands = {}

-- Develop settings exposed to the runtime. Anything outside this whitelist
-- is never written by the bridge.
-- Lightroom Classic 15.4.1 本地校验：
-- 分离色调 5 键、三轮调色 9 键、裁剪后暗角 6 键，写入后均被原样回读。
-- ColorGradeShadowHue/Sat、ColorGradeHighlightHue/Sat 四个键 LR 15.4.1 不认
-- （回读为 nil），故不收录——它们仍走透传，由 readback_mismatches 如实报告。
Commands.WRITABLE_SETTINGS = {
    'Exposure2012', 'Contrast2012', 'Highlights2012', 'Shadows2012',
    'Whites2012', 'Blacks2012', 'Temperature', 'Tint',
    'Vibrance', 'Saturation', 'IncrementalTemperature', 'IncrementalTint',
    'ToneCurvePV2012', 'ToneCurvePV2012Red', 'ToneCurvePV2012Green', 'ToneCurvePV2012Blue',
    -- 分离色调
    'SplitToningShadowHue', 'SplitToningShadowSaturation',
    'SplitToningHighlightHue', 'SplitToningHighlightSaturation', 'SplitToningBalance',
    -- 三轮调色
    'ColorGradeMidtoneHue', 'ColorGradeMidtoneSat', 'ColorGradeMidtoneLum',
    'ColorGradeShadowLum', 'ColorGradeHighlightLum',
    'ColorGradeGlobalHue', 'ColorGradeGlobalSat', 'ColorGradeGlobalLum',
    'ColorGradeBlending',
    -- 裁剪后暗角
    'PostCropVignetteAmount', 'PostCropVignetteMidpoint', 'PostCropVignetteFeather',
    'PostCropVignetteRoundness', 'PostCropVignetteStyle', 'PostCropVignetteHighlightContrast',
}

for _, family in ipairs({ 'Hue', 'Saturation', 'Luminance' }) do
    for _, color in ipairs({ 'Red', 'Orange', 'Yellow', 'Green', 'Aqua', 'Blue', 'Purple', 'Magenta' }) do
        Commands.WRITABLE_SETTINGS[#Commands.WRITABLE_SETTINGS + 1] = family .. 'Adjustment' .. color
    end
end

local function catalog()
    return LrApplication.activeCatalog()
end

local function findPhoto(uuid)
    if type(uuid) ~= 'string' or uuid == '' then
        error('Missing or invalid photo_uuid.')
    end
    local photo = catalog():findPhotoByUuid(uuid)
    if not photo then
        error('No photo with uuid ' .. uuid .. ' in the active catalog.')
    end
    return photo
end

-- Shared write-gate wrapper for the solo command path. The cores below are
-- gate-free on purpose: LR's SDK forbids nested catalog write gates, so the
-- batch runner (run_batch, end of file) can open ONE gate around a run of
-- cores. A solo command therefore behaves exactly as before -- one gate
-- around the write -- and a batch gets the same guarantee for the whole run.
local function withWriteGate(label, timeout, fn)
    local result
    local writeResult = catalog():withWriteAccessDo(label, function()
        result = fn()
    end, { timeout = timeout })
    if writeResult ~= nil and writeResult ~= 'executed' then
        error('Catalog write did not execute (result: ' .. tostring(writeResult) .. ').')
    end
    return result
end

-- 不是显影参数：LR 自己维护的元数据，写它只会制造不一致。
Commands.DENY_SETTINGS = {
    PresetType = true, Cluster = true, UUID = true, HasSettings = true,
    CameraProfileDigest = true, History = true, Snapshots = true,
    ToneCurveName = true, ToneCurveName2012 = true,
}

-- 已知字段的范围。**不在这张表里的字段不做范围校验** —— 范围是 Lightroom 的事，
-- 底座只保证类型对、写进去、读回来。拒绝一个没人写下边界的字段，等于把
-- 「引擎不认识它」变成「用户不能改它」。
local knownBounds = {
    Exposure2012 = { -5, 5 }, Temperature = { 2000, 50000 }, Tint = { -150, 150 },
}

local function isNumber(v)
    return type(v) == 'number' and v == v and v < math.huge and v > -math.huge
end

local function isNumericArray(v)
    if type(v) ~= 'table' then return false end
    local n = #v
    if n < 2 or n > 512 then return false end
    for i = 1, n do
        if not isNumber(v[i]) then return false end
    end
    return true
end

-- 透传的准入：数字、布尔（开关）、数值数组（曲线/混色）、以及嵌套表（蒙版/修饰）。
-- 嵌套表交给 Lightroom，由写入后的回读来判断它到底认不认。
local function acceptableValue(key, value)
    if Commands.DENY_SETTINGS[key] then
        return false, '不是显影参数，底座不写它'
    end
    if isNumber(value) or type(value) == 'boolean' or isNumericArray(value) then
        return true
    end
    if type(value) == 'table' then
        return true
    end
    return false, '值类型不受支持：只接受数字、布尔、数值数组或嵌套表'
end

local curveKeys = { ToneCurvePV2012 = true, ToneCurvePV2012Red = true,
    ToneCurvePV2012Green = true, ToneCurvePV2012Blue = true }
local wbKeys = { Temperature = true, Tint = true, IncrementalTemperature = true, IncrementalTint = true }

-- 注意：这条**不是**回读路径。真正的回读是 get_develop_snapshot，它直接返回
-- photo:getDevelopSettings() 原始全量（实测 171 个键），从来就没有被白名单限制过 ——
-- 所以「用户在 Lightroom 界面里手动改的字段看不见」这个担心不成立。
-- normalizedSettings 服务的是另外两条路：插件内存 baseline（capture_baseline）与
-- reset_to_baseline 写回。留成白名单范围内的标量，是为了 reset 时不要拿一个
-- 171 键的表去构造 preset —— 那条路没有回读兜底，改坏了不容易发现。
local function normalizedSettings(photo)
    local raw = photo:getDevelopSettings() or {}
    local out = {}
    for _, key in ipairs(Commands.WRITABLE_SETTINGS) do
        local value = raw[key]
        if type(value) == 'number' or (curveKeys[key] and type(value) == 'table') then
            out[key] = value
        end
    end
    return out
end

local function photoInfo(photo)
    if not photo then
        return nil
    end
    local dimensions = photo:getRawMetadata('dimensions') or {}
    return {
        uuid = photo:getRawMetadata('uuid'),
        file_name = photo:getFormattedMetadata('fileName') or '',
        copy_name = photo:getFormattedMetadata('copyName') or '',
        is_virtual_copy = photo:getRawMetadata('isVirtualCopy') == true,
        width = dimensions.width or 0,
        height = dimensions.height or 0,
    }
end

function Commands.probe_client(_)
    local activeCatalog = LrApplication.activeCatalog()
    local catalogPath = activeCatalog and activeCatalog:getPath()
    if type(catalogPath) ~= 'string' or catalogPath == '' then
        return {
            lr_version = LrApplication.versionString(),
            plugin_version = '0.4.0',
            build_id = 'bridge-generation-20261006-1',
            catalog_open = false,
            access_mode = Safety.getAccessMode(),
            catalog_authorized = false,
            capabilities = {
                expected_catalog_identity = true,
                expected_access_mode = true,
                catalog_identity = true,
            },
        }
    end
    local policy = Safety.catalogPolicy(catalogPath)
    return {
        lr_version = LrApplication.versionString(),
        plugin_version = '0.4.0',
        build_id = 'bridge-generation-20261006-1',
        catalog_open = true,
        capabilities = { expected_catalog_identity = true, expected_access_mode = true,
            catalog_identity = true, develop_snapshot = true,
            develop_presets_read = true, hsl = true, white_balance = true, curves = true,
            catalog_search = true, current_folder_selection = true,
            preview_fast_request_jpeg_thumbnail = true,
            preview_fast_thumbnail_unavailable_code = true,
            passthrough_settings = true, split_toning = true, color_grade = true,
            post_crop_vignette = true, local_masks = true },
        access_mode = policy.access_mode,
        catalog_identity = policy.catalog_identity,
        catalog_id = Safety.catalogId(catalogPath),
        sandbox = policy.sandbox,
        sandbox_registered = policy.sandbox_registered,
        sandbox_matches_active = policy.sandbox_matches_active,
        catalog_authorized = policy.catalog_authorized,
    }
end

function Commands.list_develop_presets(params)
    return Presets.list(params)
end

function Commands.get_develop_preset(params)
    return Presets.get(params)
end

function Commands.compare_develop_presets(params)
    return Presets.compare(params)
end

function Commands.get_selection(_)
    local target = catalog():getTargetPhoto()
    return {
        target = photoInfo(target),
    }
end

-- Current target + the photos in exactly its own filesystem folder. The
-- folder path stays inside this module: callers receive only an opaque,
-- catalog-scoped identifier and the display-safe leaf name.
local DEFAULT_CURRENT_FOLDER_LIMIT = 10
local MAX_CURRENT_FOLDER_LIMIT = 100
local CURRENT_FOLDER_FIELDS = { limit = true, offset = true }

local function isFiniteInteger(value)
    return type(value) == 'number'
        and value == value
        and value > -math.huge
        and value < math.huge
        and value == math.floor(value)
end

local function currentFolderQuery(params)
    if params == nil then
        params = {}
    end
    if type(params) ~= 'table' then
        error('get_current_folder arguments must be an object.')
    end
    for key in pairs(params) do
        if CURRENT_FOLDER_FIELDS[key] ~= true and not Safety.isEnvelopeField(key) then
            error('get_current_folder contains unknown field: ' .. tostring(key))
        end
    end
    Safety.validateEnvelope(params)

    local limit = params.limit
    if limit == nil then
        limit = DEFAULT_CURRENT_FOLDER_LIMIT
    end
    if not isFiniteInteger(limit) or limit < 1 or limit > MAX_CURRENT_FOLDER_LIMIT then
        error('get_current_folder limit must be an integer from 1 to '
            .. MAX_CURRENT_FOLDER_LIMIT .. '.')
    end

    local offset = params.offset
    if offset == nil then
        offset = 0
    end
    if not isFiniteInteger(offset) or offset < 0 then
        error('get_current_folder offset must be a non-negative integer.')
    end
    return { limit = limit, offset = offset }
end

local function foldAscii(value)
    return (string.gsub(value, '[A-Z]', function(character)
        return string.char(string.byte(character) + 32)
    end))
end

local function sortFolderPhotos(photos)
    for _, photo in ipairs(photos) do
        photo._folded_file_name = foldAscii(photo.file_name)
    end
    table.sort(photos, function(left, right)
        if left._folded_file_name ~= right._folded_file_name then
            return left._folded_file_name < right._folded_file_name
        end
        return left.uuid < right.uuid
    end)
    for _, photo in ipairs(photos) do
        photo._folded_file_name = nil
    end
end

local function selectedFolder(activeCatalog, target)
    local okPath, photoPath = LrTasks.pcall(function()
        return target:getRawMetadata('path')
    end)
    if not okPath or type(photoPath) ~= 'string' or photoPath == '' then
        error('Selected photo folder is unavailable.')
    end
    local okParent, folderPath = LrTasks.pcall(function()
        return LrPathUtils.parent(photoPath)
    end)
    if not okParent or type(folderPath) ~= 'string' or folderPath == '' then
        error('Selected photo folder is unavailable.')
    end
    local okFolder, folder = LrTasks.pcall(function()
        return activeCatalog:getFolderByPath(folderPath)
    end)
    if not okFolder or folder == nil then
        error('Selected photo folder is unavailable in the active Catalog.')
    end
    return folder, folderPath
end

local function folderLabel(folder, folderPath)
    local ok, label = LrTasks.pcall(function()
        return folder:getName()
    end)
    if not ok or type(label) ~= 'string' or label == '' then
        local trimmed = string.gsub(folderPath, '[\\/]+$', '')
        label = string.match(trimmed, '([^\\/]+)$') or ''
    end
    if label == '' then
        error('Selected photo folder has no display name.')
    end
    return label
end

local function folderPhotos(folder)
    local ok, photos = LrTasks.pcall(function()
        return folder:getPhotos(false)
    end)
    if not ok or type(photos) ~= 'table' then
        error('Selected photo folder photos are unavailable.')
    end
    return photos
end

local function snapshotFolder(activeCatalog, target, siblings)
    local targetInfo
    local records = {}
    local ok = LrTasks.pcall(function()
        activeCatalog:withReadAccessDo(function()
            targetInfo = photoInfo(target)
            for _, photo in ipairs(siblings) do
                if photo:getRawMetadata('isVideo') ~= true then
                    records[#records + 1] = photoInfo(photo)
                end
            end
        end)
    end)
    if not ok then
        -- Do not propagate an SDK error: it can embed a local file path.
        error('Selected photo folder metadata is unavailable.')
    end
    return targetInfo, records
end

function Commands.get_current_folder(params)
    local query = currentFolderQuery(params)
    local activeCatalog = catalog()
    local target = activeCatalog:getTargetPhoto()
    if not target then
        return {
            total = 0,
            count = 0,
            limit = query.limit,
            offset = query.offset,
            has_more = false,
            photos = Json.array(),
        }
    end

    local folder, folderPath = selectedFolder(activeCatalog, target)
    local folderId = Safety.folderIdentity(activeCatalog:getPath(), folderPath)
    if folderId == '' then
        error('Selected photo folder could not be identified.')
    end
    local targetInfo, records = snapshotFolder(activeCatalog, target, folderPhotos(folder))
    sortFolderPhotos(records)

    local page = Json.array()
    local last = math.min(query.offset + query.limit, #records)
    for index = query.offset + 1, last do
        page[#page + 1] = records[index]
    end
    return {
        target = targetInfo,
        folder = { id = folderId, label = folderLabel(folder, folderPath) },
        total = #records,
        count = #page,
        limit = query.limit,
        offset = query.offset,
        has_more = query.offset + #page < #records,
        photos = page,
    }
end

function Commands.list_photos(params)
    local limit = tonumber(params.limit) or 500
    local photos = catalog():getAllPhotos()
    local out = {}
    for _, photo in ipairs(photos) do
        if photo:getRawMetadata('isVideo') ~= true then
            out[#out + 1] = photoInfo(photo)
            if #out >= limit then
                break
            end
        end
    end
    return { count = #out, photos = out }
end

-- Read-only library query. Catalog authorization happens once in Bridge.lua.
function Commands.search_photos(params)
    return Catalog.search(params)
end

local function importPhotoCore(params)
    local path = params.path
    if type(path) ~= 'string' or path == '' then
        error('import_photo requires a file path.')
    end
    if not LrFileUtils.exists(path) then
        error('File does not exist: ' .. path)
    end
    local photo = catalog():addPhoto(path)
    if not photo then
        error('Import returned no photo.')
    end
    return { photo = photoInfo(photo) }
end

function Commands.import_photo(params)
    return withWriteGate('ToneRelay: import photo', 30, function()
        return importPhotoCore(params)
    end)
end

-- Gate-free: the gate spans the whole loop now (solo path wraps once below,
-- batch wraps a run). A file whose addPhoto raises still aborts the job, as
-- before; a nil return is recorded per file instead of via gate result.
local function importPhotosCore(params)
    local paths = params.paths
    if type(paths) ~= 'table' or #paths == 0 then
        error('import_photos requires a non-empty paths array.')
    end
    if #paths > 1000 then
        error('Refusing to import more than 1000 files in one job.')
    end
    local imported = {}
    local failed = {}
    for _, path in ipairs(paths) do
        if LrFileUtils.exists(path) then
            local photo = catalog():addPhoto(path)
            if photo then
                imported[#imported + 1] = photoInfo(photo)
            else
                failed[#failed + 1] = { path = path, error = 'import returned no photo' }
            end
        else
            failed[#failed + 1] = { path = path, error = 'file does not exist' }
        end
    end
    return { imported = #imported, failed = failed }
end

function Commands.import_photos(params)
    return withWriteGate('ToneRelay: import photos', 30, function()
        return importPhotosCore(params)
    end)
end

function Commands.capture_baseline(params)
    local photo = findPhoto(params.photo_uuid)
    State.baselines[params.photo_uuid] = normalizedSettings(photo)
    return {
        photo = photoInfo(photo),
        baseline = State.baselines[params.photo_uuid],
    }
end

function Commands.get_develop_state(params)
    local photo = findPhoto(params.photo_uuid)
    return {
        photo = photoInfo(photo),
        settings = normalizedSettings(photo),
    }
end

-- Full read-only snapshot for detecting inherited state changes. Never accepted
-- as an arbitrary write payload; mutations remain on the develop whitelist.
function Commands.get_develop_snapshot(params)
    local photo = findPhoto(params.photo_uuid)
    return { photo = photoInfo(photo), settings = photo:getDevelopSettings() or {} }
end

-- createVirtualCopies is a UI-selection API.  Lightroom only exposes a
-- reliable selection when the photo belongs to the active source, and older
-- SDKs are particularly fragile outside the Library module.  Establish the
-- smallest deterministic UI state before invoking it.  The feature checks
-- keep the seam usable by the lightweight Lua test double.
local function prepareVirtualCopySelection(photo)
    local activeCatalog = catalog()
    if activeCatalog.setActiveSources and activeCatalog.kAllPhotos then
        local sourceResult = activeCatalog:setActiveSources(activeCatalog.kAllPhotos)
        if sourceResult == false then
            error('Unable to activate All Photographs before virtual-copy creation.')
        end
        LrTasks.sleep(0.2)
    end
    if LrApplicationView.getCurrentModuleName and LrApplicationView.switchToModule then
        local moduleName = LrApplicationView.getCurrentModuleName()
        if moduleName ~= 'library' then
            LrApplicationView.switchToModule('library')
            LrTasks.sleep(0.2)
        end
    end
    activeCatalog:setSelectedPhotos(photo, {})
    LrTasks.sleep(0.2)
    if activeCatalog.getTargetPhoto and activeCatalog:getTargetPhoto() ~= photo then
        error('Lightroom did not select the requested source photo.')
    end
end

function Commands.select_photo(params)
    local photo = findPhoto(params.photo_uuid)
    prepareVirtualCopySelection(photo)
    return { photo = photoInfo(photo) }
end

function Commands.create_virtual_copy(params)
    local source = findPhoto(params.photo_uuid)
    if source:getRawMetadata('isVirtualCopy') then
        error('create_virtual_copy expects a master photo, not a virtual copy.')
    end
    local name = params.name or 'ToneRelay candidate'
    local copy
    local ok, failure = LrTasks.pcall(function()
        prepareVirtualCopySelection(source)
    end)
    if not ok then
        error('create_virtual_copy phase=select_source: ' .. tostring(failure))
    end
    -- The SDK documents createVirtualCopies as an asynchronous-task API, not
    -- a catalog write-gate API.  Calling it inside withWriteAccessDo makes LR
    -- 15.4.1 fail internally ("attempt to index a boolean value").
    local copies
    local phase = 'create_copies'
    ok, failure = LrTasks.pcall(function()
        copies = catalog():createVirtualCopies(name)
        phase = 'after_create'
    end)
    if not ok then
        error('create_virtual_copy phase=' .. phase .. ': ' .. tostring(failure)
            .. ' (type(copies)=' .. type(copies) .. ')')
    end
    ok, failure = LrTasks.pcall(function()
        if type(copies) ~= 'table' or #copies ~= 1 then
            error('Expected exactly one virtual copy, got ' .. tostring(copies) .. '.')
        end
        copy = copies[1]
        if copy:getRawMetadata('masterPhoto') ~= source then
            error('Virtual-copy verification failed: master mismatch.')
        end
    end)
    if not ok then
        error('create_virtual_copy phase=read_copy_info: ' .. tostring(failure))
    end
    return {
        source = photoInfo(source),
        copy = photoInfo(copy),
    }
end

local function applyCandidateCore(params)
    local photo = findPhoto(params.photo_uuid)
    if not photo:getRawMetadata('isVirtualCopy') then
        error('apply_candidate only writes to virtual copies; refusing to touch a master photo.')
    end
    local settings = params.settings
    if type(settings) ~= 'table' then
        error('apply_candidate requires a settings object.')
    end
    local clean = {}
    for key, value in pairs(settings) do
        if type(key) ~= 'string' or not string.match(key, '^[A-Za-z][A-Za-z0-9]*$') then
            error('Invalid setting name: ' .. tostring(key))
        end
        local ok, why = acceptableValue(key, value)
        if not ok then
            error('Setting ' .. key .. ': ' .. why)
        end
        if curveKeys[key] then
            if type(value) ~= 'table' or #value < 4 or #value > 32 or #value % 2 ~= 0 then
                error('Curve requires 2..16 x,y pairs.')
            end
            for i, point in ipairs(value) do
                if type(point) ~= 'number' or point ~= point or point < 0 or point > 255
                    or point ~= math.floor(point) then error('Invalid curve coordinate.') end
                if i % 2 == 1 and i > 1 and point <= value[i - 2] then
                    error('Curve x coordinates must increase.')
                end
            end
            if value[1] ~= 0 or value[#value - 1] ~= 255 then error('Curve must span 0..255.') end
            clean.ToneCurveName2012 = 'Custom'
        else
            if wbKeys[key] then clean.WhiteBalance = 'Custom' end
            local bounds = knownBounds[key]
            if bounds then
                if not isNumber(value) or value < bounds[1] or value > bounds[2] then
                    error('Setting out of bounds: ' .. key)
                end
            end
            -- 其余字段不做范围校验：类型已经在 acceptableValue 里查过。
        end
        clean[key] = value
    end

    local before = normalizedSettings(photo)
    local preset = LrApplication.addDevelopPresetForPlugin(_PLUGIN, 'ToneRelay candidate', clean)
    photo:applyDevelopPreset(preset, _PLUGIN)
    -- applyDevelopPreset 是异步的，而且本函数可能跑在（solo 或 batch 的）写闸门里：
    -- 闸门内回读永远看不到本次写入，必须等闸门提交后再轮询（见 settleReadback）。
    -- 这里只负责写入；after 先放占位，_photo/_clean 供闸门外的轮询使用。
    return {
        photo = photoInfo(photo),
        before = before,
        after = before,
        _photo = photo,
        _clean = clean,
    }
end

local skipSettleKeys = { WhiteBalance = true, ToneCurveName2012 = true }

local function settleMatchesExpected(snap, clean)
    for key, value in pairs(clean) do
        if not skipSettleKeys[key] then
            local actual = snap[key]
            if type(value) == 'table' then
                if type(actual) ~= 'table' or #actual ~= #value then return false end
                for i, v in ipairs(value) do
                    if actual[i] ~= v then return false end
                end
            elseif type(value) == 'number' then
                if type(actual) ~= 'number' or math.abs(actual - value) > 1e-6 then return false end
            elseif actual ~= value then
                return false
            end
        end
    end
    return true
end

-- 写闸门关闭后调用：轮询回读直到本次写入的键全部生效（最多 10 秒；
-- LR 把显影写入刷回目录的延迟实测在 1~6 秒之间波动）。
-- 超时照实返回最后一份快照并标记 after_pending。无论结果如何都会摘掉
-- _photo/_clean 内部字段，避免它们进入 JSON 序列化。
local function settleReadback(result)
    local photo = result._photo
    local clean = result._clean
    result._photo = nil
    result._clean = nil
    if not photo or not clean then
        return result
    end
    local after = result.after
    local settled = settleMatchesExpected(after, clean)
    local waited = 0
    while not settled and waited < 100 do
        LrTasks.sleep(0.1)
        waited = waited + 1
        after = normalizedSettings(photo)
        settled = settleMatchesExpected(after, clean)
    end
    result.after = after
    if not settled then
        -- 10 秒还没回读到写入值：照实报告这份快照可能仍是旧值。
        result.after_pending = true
    end
    return result
end

function Commands.apply_candidate(params)
    return settleReadback(withWriteGate('ToneRelay: apply candidate', 15, function()
        return applyCandidateCore(params)
    end))
end

local function resetToBaselineCore(params)
    local photo = findPhoto(params.photo_uuid)
    if not photo:getRawMetadata('isVirtualCopy') then
        error('reset_to_baseline only writes to virtual copies.')
    end
    local baseline = State.baselines[params.photo_uuid]
    if not baseline then
        error('No captured baseline for this photo. Run capture_baseline first.')
    end
    local preset = LrApplication.addDevelopPresetForPlugin(_PLUGIN, 'ToneRelay baseline', baseline)
    photo:applyDevelopPreset(preset, _PLUGIN)
    return {
        photo = photoInfo(photo),
        settings = normalizedSettings(photo),
    }
end

function Commands.reset_to_baseline(params)
    return withWriteGate('ToneRelay: reset to baseline', 15, function()
        return resetToBaselineCore(params)
    end)
end

function Commands.export_preview(params)
    local photo = findPhoto(params.photo_uuid)
    local longEdge = tonumber(params.long_edge) or 2048
    local exportDir = params.output_dir
    if type(exportDir) ~= 'string' or exportDir == '' then
        error('export_preview requires output_dir (managed by the lr-ops service).')
    end
    LrFileUtils.createAllDirectories(exportDir)
    -- Lightroom names the export after the photo's own filename, not our id.
    local fileName = photo:getFormattedMetadata('fileName') or params.photo_uuid
    local stem = string.gsub(fileName, '%.[^.]+$', '')
    local outputPath = LrPathUtils.child(exportDir, stem .. '.jpg')
    if LrFileUtils.exists(outputPath) then
        LrFileUtils.delete(outputPath)
    end

    local exportSession = LrExportSession {
        photosToExport = { photo },
        exportSettings = {
            LR_collisionHandling = 'overwrite',
            LR_export_bitDepth = '8',
            LR_export_colorSpace = 'sRGB',
            LR_export_destinationType = 'specificFolder',
            LR_export_destinationPathPrefix = exportDir,
            LR_export_useSubfolder = false,
            LR_export_videoFileHandling = 'exclude',
            LR_format = 'JPEG',
            LR_jpeg_quality = 0.9,
            LR_metadata_keywordOptions = 'lightroomHierarchical',
            LR_outputSharpeningOn = false,
            LR_reimportExportedPhoto = false,
            LR_renamingTokensOn = false,
            LR_size_doConstrain = true,
            LR_size_maxHeight = longEdge,
            LR_size_resizeType = 'longEdge',
            LR_size_units = 'pixels',
            LR_useWatermark = false,
        },
    }
    exportSession:doExportOnCurrentTask()

    if not LrFileUtils.exists(outputPath) then
        error('Export finished but the expected file is missing: ' .. outputPath)
    end
    return {
        photo = photoInfo(photo),
        output_path = outputPath,
        long_edge = longEdge,
        color_space = 'sRGB',
    }
end

-- Fast read-only preview for an already catalogued photo. This deliberately
-- does not create a virtual copy, invoke LrExportSession, or import bytes. LR
-- serves the cached JPEG thumbnail through the SDK callback; the bridge owns
-- the output directory and returns the bytes by output_ref.
-- Gate-free core; the solo wrapper below holds one read gate around the whole
-- body (previously two separate gates), and a batch run reuses that wrapper.
-- requestJpegThumbnail itself keeps its own callback protocol either way.
local function jpegThumbnailCore(params)
    local photo = findPhoto(params.photo_uuid)
    local size = tonumber(params.size) or 1024
    if size ~= size or size == math.huge or size == -math.huge then
        error('request_jpeg_thumbnail size must be finite.')
    end
    size = math.floor(size)
    if size < 64 or size > 2048 then
        error('request_jpeg_thumbnail size must be 64..2048.')
    end
    local outputDir = params.output_dir
    if type(outputDir) ~= 'string' or outputDir == '' then
        error('request_jpeg_thumbnail requires output_dir (managed by lr-ops).')
    end
    LrFileUtils.createAllDirectories(outputDir)

    local jpegData
    local callbackReason
    local callbackFired = false
    local thumbnailRequest
    local ok, failure = LrTasks.pcall(function()
        thumbnailRequest = photo:requestJpegThumbnail(size, size, function(jpeg, reason)
            callbackReason = reason
            jpegData = jpeg
            callbackFired = true
        end)
    end)
    if not ok then
        error('request_jpeg_thumbnail phase=request: ' .. tostring(failure))
    end
    local waited = 0
    while not callbackFired and waited < 3 do
        LrTasks.sleep(0.05)
        waited = waited + 0.05
    end
    -- The SDK requires callers to retain this request object until the
    -- callback fires. Release it only after the callback or our own timeout.
    thumbnailRequest = nil
    if not callbackFired then
        error('request_jpeg_thumbnail timed out waiting for the Lightroom callback.')
    end
    if jpegData == nil then
        error('TRLR_FAST_THUMBNAIL_UNAVAILABLE: request_jpeg_thumbnail unavailable (callback='
            .. tostring(callbackReason)
            .. ', fired=' .. tostring(callbackFired) .. '). Build Standard previews in Lightroom.')
    end
    if type(jpegData) ~= 'string' then
        error('request_jpeg_thumbnail returned an invalid callback payload type: '
            .. type(jpegData))
    end
    if #jpegData < 3 then
        error('request_jpeg_thumbnail returned truncated JPEG bytes (' .. #jpegData .. ').')
    end
    local b1, b2, b3 = jpegData:byte(1), jpegData:byte(2), jpegData:byte(3)
    if b1 ~= 0xFF or b2 ~= 0xD8 or b3 ~= 0xFF then
        error(string.format('request_jpeg_thumbnail returned non-JPEG bytes (%02X %02X %02X)', b1, b2, b3))
    end
    local orientation = photo:getRawMetadata('orientation')
    local outputPath = LrPathUtils.child(outputDir, tostring(params.photo_uuid) .. '.jpg')
    if LrFileUtils.exists(outputPath) then
        LrFileUtils.delete(outputPath)
    end
    local wrote = true
    if LrFileUtils.writeFile then
        wrote = LrFileUtils.writeFile(outputPath, jpegData) ~= false
    else
        local handle, openError = io.open(outputPath, 'wb')
        if not handle then
            error('request_jpeg_thumbnail could not open output: ' .. tostring(openError))
        end
        local okWrite, writeError = handle:write(jpegData)
        handle:close()
        wrote = okWrite ~= nil
        if not wrote then
            error('request_jpeg_thumbnail could not write output: ' .. tostring(writeError))
        end
    end
    if not wrote or not LrFileUtils.exists(outputPath) then
        error('request_jpeg_thumbnail failed to write output file: ' .. outputPath)
    end
    return {
        photo = photoInfo(photo),
        output_path = outputPath,
        requested_size = size,
        orientation = orientation,
        method = 'requestJpegThumbnail',
        purpose = 'fast',
        freshness = 'unverified',
        color_space = 'unknown',
        callback_fired = callbackFired,
    }
end

function Commands.request_jpeg_thumbnail(params)
    local result
    catalog():withReadAccessDo(function()
        result = jpegThumbnailCore(params)
    end)
    return result
end

-- ---------------------------------------------------------------------------
-- command_batch: many commands, one queue item, no interleaving.
--
-- The bridge's queue is strictly serial, but a *logical* operation (read back
-- + create copy + write + render) used to span several queue items, and another
-- caller's jobs could slip in between. run_batch executes one such item's
-- command list in order and stops at the first failure.
--
-- Gate discipline: LR's SDK forbids nested catalog write gates, so the cores
-- above never open their own. Here, a maximal run of CONSECUTIVE write-gated
-- commands shares a single withWriteAccessDo; read-gated commands get one
-- read gate each; everything else runs bare, exactly as on the solo path.
-- create_virtual_copy is deliberately NOT write-gated (see its comment: LR
-- 15.4.1 fails createVirtualCopies inside a write gate), which is why gating
-- is per-run rather than one gate around the whole batch.
-- ---------------------------------------------------------------------------

Commands.BATCH_WRITE_GATED = {
    import_photo = true, import_photos = true,
    apply_candidate = true, reset_to_baseline = true,
}
Commands.BATCH_READ_GATED = { request_jpeg_thumbnail = true }

-- Gate-free entry points for every batchable command. Bare commands map to
-- their public function; gated commands map to the core. command_batch itself
-- is absent, so a nested batch reports "unknown command" instead of recursing.
Commands._cores = {
    probe_client = Commands.probe_client,
    list_develop_presets = Commands.list_develop_presets,
    get_develop_preset = Commands.get_develop_preset,
    compare_develop_presets = Commands.compare_develop_presets,
    get_selection = Commands.get_selection,
    get_current_folder = Commands.get_current_folder,
    list_photos = Commands.list_photos,
    search_photos = Commands.search_photos,
    capture_baseline = Commands.capture_baseline,
    get_develop_state = Commands.get_develop_state,
    get_develop_snapshot = Commands.get_develop_snapshot,
    select_photo = Commands.select_photo,
    create_virtual_copy = Commands.create_virtual_copy,
    export_preview = Commands.export_preview,
    import_photo = importPhotoCore,
    import_photos = importPhotosCore,
    apply_candidate = applyCandidateCore,
    reset_to_baseline = resetToBaselineCore,
    request_jpeg_thumbnail = jpegThumbnailCore,
}

local function entryParams(entry)
    if type(entry) == 'table' and type(entry.params) == 'table' then
        return entry.params
    end
    return {}
end

-- Returns { results = [...] } on success, or
-- { results = <so far>, failed_at_index = n, error = msg } on the first
-- failure. Each result entry is { command = name, ok = true, data = ... }.
-- Writes committed before the failure stay committed -- a catalog gate is not
-- a transaction -- so the caller must treat failed_at_index as authoritative.
function Commands.run_batch(entries)
    local results = {}
    local index = 1
    while index <= #entries do
        local entry = entries[index]
        local name = type(entry) == 'table' and entry.command or nil
        if type(name) ~= 'string' or Commands._cores[name] == nil then
            return {
                results = results,
                failed_at_index = index,
                error = 'Unknown command in batch at index ' .. tostring(index) .. ': ' .. tostring(name),
            }
        end
        if Commands.BATCH_WRITE_GATED[name] then
            local stop = index
            while stop < #entries do
                local nextEntry = entries[stop + 1]
                if type(nextEntry) ~= 'table' or not Commands.BATCH_WRITE_GATED[nextEntry.command] then
                    break
                end
                stop = stop + 1
            end
            local failure = nil
            local writeResult = catalog():withWriteAccessDo(
                'ToneRelay: command batch',
                function()
                    for i = index, stop do
                        local e = entries[i]
                        local ok, res = LrTasks.pcall(function()
                            return Commands._cores[e.command](entryParams(e))
                        end)
                        if not ok then
                            failure = { index = i, error = tostring(res) }
                            return
                        end
                        results[#results + 1] = { command = e.command, ok = true, data = res }
                    end
                end,
                { timeout = 30 }
            )
            if failure then
                -- 闸门已因 closure 返回而关闭：先 settle 已写入的条目，摘掉内部字段。
                for i = index, stop do
                    local r = results[i]
                    if r and r.ok and type(r.data) == 'table' and r.data._photo ~= nil then
                        settleReadback(r.data)
                    end
                end
                return { results = results, failed_at_index = failure.index, error = failure.error }
            end
            if writeResult ~= nil and writeResult ~= 'executed' then
                return {
                    results = results,
                    failed_at_index = index,
                    error = 'Catalog write did not execute (result: ' .. tostring(writeResult) .. ').',
                }
            end
            -- 闸门已提交：对这批里的 apply_candidate 逐条做闸门外回读轮询，
            -- 并摘掉 _photo/_clean 内部字段。
            for i = index, stop do
                local r = results[i]
                if r and r.ok and type(r.data) == 'table' and r.data._photo ~= nil then
                    settleReadback(r.data)
                end
            end
            index = stop + 1
        elseif Commands.BATCH_READ_GATED[name] then
            local ok, res = LrTasks.pcall(function()
                local out
                catalog():withReadAccessDo(function()
                    out = Commands._cores[name](entryParams(entry))
                end)
                return out
            end)
            if not ok then
                return { results = results, failed_at_index = index, error = tostring(res) }
            end
            results[#results + 1] = { command = name, ok = true, data = res }
            index = index + 1
        else
            local ok, res = LrTasks.pcall(function()
                return Commands._cores[name](entryParams(entry))
            end)
            if not ok then
                return { results = results, failed_at_index = index, error = tostring(res) }
            end
            results[#results + 1] = { command = name, ok = true, data = res }
            index = index + 1
        end
    end
    return { results = results }
end

return Commands
