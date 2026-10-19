-- Read-only Lightroom develop-preset adapter maintained by ToneRelay.
--
-- The public surface mirrors the LR-Ops command protocol: list metadata, read
-- one preset, or compare two presets. Catalog authorization remains owned by
-- Safety and is accepted here as the shared expected_* request envelope.
local LrApplication = import 'LrApplication'
local LrTasks = import 'LrTasks'

local Safety = require 'Safety'

local Presets = {}

local DEFAULT_PAGE_SIZE = 100
local MAX_PAGE_SIZE = 100
local MAX_SETTINGS_DEPTH = 6
local MAX_TABLE_ENTRIES = 2000
local PLUGIN_PRESET_FOLDER = 'Plugin Develop Presets'

local LIST_FIELDS = { query = true, limit = true, offset = true }
local SELECTOR_FIELDS = {
    preset_name = true,
    preset_uuid = true,
    preset_folder = true,
    preset_scope = true,
}
local COMPARE_FIELDS = { base = true, candidate = true }
local VOLATILE_SETTING_KEYS = { CorrectionID = true, MaskID = true }

local function finiteNumber(value)
    return type(value) == 'number'
        and value == value
        and value < math.huge
        and value > -math.huge
end

local function validateFields(value, allowed, context)
    for key, _ in pairs(value) do
        if not allowed[key] and not Safety.isEnvelopeField(key) then
            error(context .. ' contains unknown field: ' .. tostring(key))
        end
    end
end

local function validateEnvelope(value)
    Safety.validateEnvelope(value)
end

local function requireNonEmptyString(value, label)
    if type(value) ~= 'string' or value == '' then
        error(label .. ' must be a non-empty string')
    end
end

local function validateSelector(value)
    if type(value) ~= 'table' then
        error('preset selector is required')
    end

    validateFields(value, SELECTOR_FIELDS, 'preset selector')
    validateEnvelope(value)

    if value.preset_name ~= nil then
        requireNonEmptyString(value.preset_name, 'preset_name')
    end
    if value.preset_uuid ~= nil then
        requireNonEmptyString(value.preset_uuid, 'preset_uuid')
    end
    if value.preset_name == nil and value.preset_uuid == nil then
        error('preset_name or preset_uuid is required')
    end
    if value.preset_folder ~= nil then
        requireNonEmptyString(value.preset_folder, 'preset_folder')
    end
    if value.preset_scope ~= nil
        and value.preset_scope ~= 'lightroom'
        and value.preset_scope ~= 'plugin' then
        error('preset_scope must be lightroom or plugin')
    end
end

local function invokePresetMethod(object, methodName)
    local method = object and object[methodName]
    if type(method) ~= 'function' then
        error('Preset SDK method unavailable: ' .. methodName)
    end

    local ok, result = LrTasks.pcall(function()
        return method(object)
    end)
    if not ok then
        error('Preset ' .. methodName .. ' failed: ' .. tostring(result))
    end
    return result
end

local function newEntry(preset, folderName, scope)
    return {
        handle = preset,
        name = invokePresetMethod(preset, 'getName'),
        folder = folderName,
        scope = scope,
        uuid = invokePresetMethod(preset, 'getUuid'),
    }
end

local function appendFolderPresets(entries, folder)
    local folderName = invokePresetMethod(folder, 'getName') or ''
    local ok, folderPresets = LrTasks.pcall(function()
        return folder:getDevelopPresets()
    end)
    if not ok or type(folderPresets) ~= 'table' then
        error('getDevelopPresets failed: ' .. tostring(folderPresets))
    end

    for _, preset in ipairs(folderPresets) do
        entries[#entries + 1] = newEntry(preset, folderName, 'lightroom')
    end
end

local function appendPluginPresets(entries)
    if not _PLUGIN or type(LrApplication.getDevelopPresetsForPlugin) ~= 'function' then
        return
    end

    local ok, pluginPresets = LrTasks.pcall(function()
        return LrApplication.getDevelopPresetsForPlugin(_PLUGIN)
    end)
    if not ok then
        error('getDevelopPresetsForPlugin failed: ' .. tostring(pluginPresets))
    end
    if pluginPresets == nil then
        pluginPresets = {}
    end
    if type(pluginPresets) ~= 'table' then
        error('getDevelopPresetsForPlugin failed: ' .. tostring(pluginPresets))
    end

    for _, preset in ipairs(pluginPresets) do
        entries[#entries + 1] = newEntry(preset, PLUGIN_PRESET_FOLDER, 'plugin')
    end
end

local function readLibrary()
    local ok, folders = LrTasks.pcall(function()
        return LrApplication.developPresetFolders()
    end)
    if not ok or type(folders) ~= 'table' then
        error('developPresetFolders failed: ' .. tostring(folders))
    end

    local entries = {}
    for _, folder in ipairs(folders) do
        appendFolderPresets(entries, folder)
    end
    appendPluginPresets(entries)
    return entries
end

local function publicMetadata(entry)
    return {
        name = entry.name,
        folder = entry.folder,
        scope = entry.scope,
        uuid = entry.uuid,
    }
end

local function matchesSelector(entry, selector)
    return (selector.preset_uuid == nil or entry.uuid == selector.preset_uuid)
        and (selector.preset_name == nil or entry.name == selector.preset_name)
        and (selector.preset_folder == nil or entry.folder == selector.preset_folder)
        and (selector.preset_scope == nil or entry.scope == selector.preset_scope)
end

local function matchesShareUuid(matches)
    local uuid = matches[1].uuid
    if uuid == nil then
        return false
    end
    for index = 2, #matches do
        if matches[index].uuid ~= uuid then
            return false
        end
    end
    return true
end

local function resolveEntry(selector, entries)
    local matches = {}
    for _, entry in ipairs(entries) do
        if matchesSelector(entry, selector) then
            matches[#matches + 1] = entry
        end
    end

    if #matches == 0 then
        error('Preset not found')
    end
    if #matches > 1 and selector.preset_uuid == nil and not matchesShareUuid(matches) then
        error('Preset selector is ambiguous; provide preset_uuid or preset_folder')
    end
    return matches[1]
end

local function copyJsonValue(value, depth, active)
    local valueType = type(value)
    if valueType == 'number' then
        if not finiteNumber(value) then
            error('Preset settings contain a non-finite number')
        end
        return value
    end
    if valueType == 'string' or valueType == 'boolean' then
        return value
    end
    if valueType ~= 'table' then
        error('Preset settings contain an unsupported value')
    end
    if depth >= MAX_SETTINGS_DEPTH then
        error('Preset settings exceed maximum depth 6')
    end
    if active[value] then
        error('Preset settings contain a cycle')
    end

    active[value] = true
    local copy = {}
    local total = 0
    local numericKeys = 0
    local stringKeys = 0
    local largestIndex = 0

    for key, item in pairs(value) do
        local keyType = type(key)
        if keyType == 'number' then
            if not finiteNumber(key) or key < 1 or key ~= math.floor(key) then
                error('Preset settings numeric keys must be positive integers')
            end
            numericKeys = numericKeys + 1
            if key > largestIndex then
                largestIndex = key
            end
        elseif keyType == 'string' then
            stringKeys = stringKeys + 1
        else
            error('Preset settings keys must be strings or integers')
        end

        total = total + 1
        if total > MAX_TABLE_ENTRIES then
            error('Preset settings are too large')
        end
        copy[key] = copyJsonValue(item, depth + 1, active)
    end

    active[value] = nil
    if numericKeys > 0 and stringKeys > 0 then
        error('Preset settings tables cannot mix array and object keys')
    end
    if numericKeys > 0 and largestIndex ~= numericKeys then
        error('Preset settings arrays must be contiguous')
    end
    return copy
end

local function readSettings(entry)
    local ok, rawSettings = LrTasks.pcall(function()
        return entry.handle:getSetting()
    end)
    if not ok then
        error('Preset settings are unavailable: ' .. tostring(rawSettings))
    end
    if type(rawSettings) ~= 'table' then
        error('Preset settings are unavailable')
    end
    for key, _ in pairs(rawSettings) do
        if type(key) ~= 'string' then
            error('Preset settings top-level keys must be strings')
        end
    end
    return copyJsonValue(rawSettings, 0, {})
end

local function removeVolatileSettings(value)
    if type(value) ~= 'table' then
        return value
    end

    local cleaned = {}
    for key, item in pairs(value) do
        if type(key) ~= 'string' or not VOLATILE_SETTING_KEYS[key] then
            cleaned[key] = removeVolatileSettings(item)
        end
    end
    return cleaned
end

local function sameValue(left, right)
    if type(left) ~= type(right) then
        return false
    end
    if type(left) ~= 'table' then
        return left == right
    end

    for key, value in pairs(left) do
        if not sameValue(value, right[key]) then
            return false
        end
    end
    for key, _ in pairs(right) do
        if left[key] == nil then
            return false
        end
    end
    return true
end

local function sortKey(metadata)
    return (metadata.scope or '') .. '\0'
        .. (metadata.folder or '') .. '\0'
        .. (metadata.name or '') .. '\0'
        .. (metadata.uuid or '')
end

local function metadataMatchesQuery(metadata, normalizedQuery)
    if normalizedQuery == '' then
        return true
    end
    local searchable = (metadata.name or '') .. ' '
        .. (metadata.folder or '') .. ' '
        .. (metadata.scope or '') .. ' '
        .. (metadata.uuid or '')
    return string.find(string.lower(searchable), normalizedQuery, 1, true) ~= nil
end

function Presets.list(args)
    if args == nil then
        args = {}
    end
    if type(args) ~= 'table' then
        error('list arguments must be an object')
    end

    validateFields(args, LIST_FIELDS, 'list')
    validateEnvelope(args)

    local query = args.query == nil and '' or args.query
    if type(query) ~= 'string' then
        error('query must be a string')
    end

    local limit = args.limit == nil and DEFAULT_PAGE_SIZE or args.limit
    if not finiteNumber(limit)
        or limit < 1
        or limit > MAX_PAGE_SIZE
        or limit ~= math.floor(limit) then
        error('limit must be an integer from 1 to 100')
    end

    local offset = args.offset == nil and 0 or args.offset
    if not finiteNumber(offset) or offset < 0 or offset ~= math.floor(offset) then
        error('offset must be a non-negative integer')
    end

    local normalizedQuery = string.lower(query)
    local filtered = {}
    for _, entry in ipairs(readLibrary()) do
        local metadata = publicMetadata(entry)
        if metadataMatchesQuery(metadata, normalizedQuery) then
            filtered[#filtered + 1] = metadata
        end
    end

    table.sort(filtered, function(left, right)
        return sortKey(left) < sortKey(right)
    end)

    local page = {}
    local last = math.min(offset + limit, #filtered)
    for index = offset + 1, last do
        page[#page + 1] = filtered[index]
    end
    return {
        presets = page,
        count = #filtered,
        offset = offset,
        limit = limit,
    }
end

function Presets.get(args)
    validateSelector(args)
    local entry = resolveEntry(args, readLibrary())
    local result = publicMetadata(entry)
    result.settings = readSettings(entry)
    return result
end

function Presets.compare(args)
    if type(args) ~= 'table'
        or type(args.base) ~= 'table'
        or type(args.candidate) ~= 'table' then
        error('base and candidate preset selectors are required')
    end

    validateFields(args, COMPARE_FIELDS, 'compare')
    validateEnvelope(args)
    validateSelector(args.base)
    validateSelector(args.candidate)

    local entries = readLibrary()
    local baseEntry = resolveEntry(args.base, entries)
    local candidateEntry = resolveEntry(args.candidate, entries)
    local baseSettings = removeVolatileSettings(readSettings(baseEntry))
    local candidateSettings = removeVolatileSettings(readSettings(candidateEntry))

    local allKeys = {}
    for key, _ in pairs(baseSettings) do
        allKeys[key] = true
    end
    for key, _ in pairs(candidateSettings) do
        allKeys[key] = true
    end

    local changes = {}
    for key, _ in pairs(allKeys) do
        if not sameValue(baseSettings[key], candidateSettings[key]) then
            changes[#changes + 1] = {
                key = key,
                before_present = baseSettings[key] ~= nil,
                after_present = candidateSettings[key] ~= nil,
                before = baseSettings[key],
                after = candidateSettings[key],
            }
        end
    end
    table.sort(changes, function(left, right)
        return left.key < right.key
    end)

    return {
        base = publicMetadata(baseEntry),
        candidate = publicMetadata(candidateEntry),
        changes = changes,
        changed_count = #changes,
        ignored_setting_keys = { 'CorrectionID', 'MaskID' },
    }
end

return Presets
