-- Catalog authorization for every bridge job.
--
-- full (the default) may operate on any active Catalog. safe may operate only
-- on the one Catalog explicitly registered by the user in Plugin Manager.
-- Both modes require the same opaque identity and access-mode bindings.
local Safety = {}

Safety.ACCESS_MODE_SAFE = 'safe'
Safety.ACCESS_MODE_FULL = 'full'

Safety.EXPECTED_CATALOG_IDENTITY = 'expected_catalog_identity'
Safety.EXPECTED_ACCESS_MODE = 'expected_access_mode'

local ENVELOPE_FIELDS = {
    [Safety.EXPECTED_CATALOG_IDENTITY] = true,
    [Safety.EXPECTED_ACCESS_MODE] = true,
}

local fallbackPrefs = {}
local LrMD5

-- LrPrefs is unavailable in the lightweight Lua test seams. Keep the module
-- loadable there while using persistent plug-in preferences in Lightroom.
local function loadPrefs()
    if type(import) == 'function' then
        local imported, LrPrefs = pcall(import, 'LrPrefs')
        if imported and type(LrPrefs) == 'table'
            and type(LrPrefs.prefsForPlugin) == 'function' then
            local loaded, persistent = pcall(function() return LrPrefs.prefsForPlugin() end)
            if loaded and persistent ~= nil then
                return persistent
            end
        end
    end
    return fallbackPrefs
end

local prefs = loadPrefs()
if prefs.accessMode == nil or prefs.accessMode == '' then
    prefs.accessMode = Safety.ACCESS_MODE_FULL
end

if type(import) == 'function' then
    local imported, module = pcall(import, 'LrMD5')
    if imported and type(module) == 'table' and type(module.digest) == 'function' then
        LrMD5 = module
    end
end

local function normalizePath(path)
    if type(path) ~= 'string' then
        return ''
    end
    local normalized = string.gsub(path, '\\', '/')
    normalized = string.gsub(normalized, '/+$', '')
    return string.lower(normalized)
end

local function requiredString(value, label)
    if value ~= nil and (type(value) ~= 'string' or value == '') then
        error(label .. ' must be a non-empty string when present')
    end
end

local function requireMode(value, label)
    if value ~= Safety.ACCESS_MODE_SAFE and value ~= Safety.ACCESS_MODE_FULL then
        error(label .. ' must be safe or full')
    end
    return value
end

local HEX = '0123456789abcdef'

local function bytesToHex(value)
    local out = {}
    for i = 1, #value do
        local byte = string.byte(value, i)
        local high = math.floor(byte / 16) + 1
        local low = (byte % 16) + 1
        out[#out + 1] = string.sub(HEX, high, high)
        out[#out + 1] = string.sub(HEX, low, low)
    end
    return table.concat(out)
end

local function hex32(value)
    local chars = {}
    for i = 8, 1, -1 do
        local digit = value % 16
        chars[i] = string.sub(HEX, digit + 1, digit + 1)
        value = math.floor(value / 16)
    end
    return table.concat(chars)
end

-- Test seams do not ship Lightroom's LrMD5 namespace. This deterministic
-- fallback keeps them loadable; production Lightroom always takes LrMD5.
local function fallbackDigest(value)
    local first, second = 5381, 2166136261
    for i = 1, #value do
        local byte = string.byte(value, i)
        first = (first * 33 + byte) % 4294967296
        second = (second * 65599 + byte) % 4294967296
    end
    return hex32(first) .. hex32(second)
end

local function opaqueDigest(value, scope)
    scope = scope or 'catalog'
    if LrMD5 then
        local ok, digest = pcall(LrMD5.digest, value)
        if ok and type(digest) == 'string' and digest ~= '' then
            if string.match(digest, '^[0-9A-Fa-f]+$') then
                return scope .. '-md5-v1:' .. string.lower(digest)
            end
            return scope .. '-md5-v1:' .. bytesToHex(digest)
        end
    end
    return scope .. '-hash-v1:' .. fallbackDigest(value)
end

function Safety.isEnvelopeField(key)
    return ENVELOPE_FIELDS[key] == true
end

-- Shared validation used by authorization and by command-specific parsers.
-- Command modules accept these transport fields but do not interpret them.
function Safety.validateEnvelope(params)
    if params == nil then
        return
    end
    if type(params) ~= 'table' then
        error('job params must be an object')
    end
    if params.expected_catalog_path ~= nil or params.expected_catalog_id ~= nil then
        error('Catalog path/name bindings are unsupported; use expected_catalog_identity.')
    end
    requiredString(params[Safety.EXPECTED_CATALOG_IDENTITY], Safety.EXPECTED_CATALOG_IDENTITY)
    local expectedMode = params[Safety.EXPECTED_ACCESS_MODE]
    if expectedMode ~= nil then
        if type(expectedMode) ~= 'string' or expectedMode == '' then
            error(Safety.EXPECTED_ACCESS_MODE .. ' must be a non-empty string when present')
        end
        requireMode(expectedMode, Safety.EXPECTED_ACCESS_MODE)
    end
end

function Safety.getAccessMode()
    return requireMode(prefs.accessMode, 'configured access mode')
end

function Safety.setAccessMode(mode)
    prefs.accessMode = requireMode(mode, 'access mode')
    return prefs.accessMode
end

-- Lightroom exposes no stable Catalog UUID, so hash the normalized absolute
-- path inside the plug-in. The path never crosses the bridge in probe data.
function Safety.catalogIdentity(catalogPath)
    local normalized = normalizePath(catalogPath)
    if normalized == '' then
        return ''
    end
    return opaqueDigest(normalized)
end

-- A folder path is needed locally to ask Lightroom for its LrFolder, but it
-- must never cross the bridge. Scope the digest to the active Catalog as well:
-- two catalogs that contain the same folder still get distinct opaque IDs.
function Safety.folderIdentity(catalogPath, folderPath)
    local catalog = normalizePath(catalogPath)
    local folder = normalizePath(folderPath)
    if catalog == '' or folder == '' then
        return ''
    end
    return opaqueDigest(catalog .. '\0' .. folder, 'folder')
end

function Safety.catalogId(catalogPath)
    local path = normalizePath(catalogPath)
    return path:match('([^/]+)$') or ''
end

function Safety.registerSandboxCatalog(catalogPath)
    local identity = Safety.catalogIdentity(catalogPath)
    if identity == '' then
        error('Cannot register a sandbox without an active Catalog path.')
    end
    prefs.sandboxCatalogIdentity = identity
    return identity
end

function Safety.clearSandboxCatalog()
    prefs.sandboxCatalogIdentity = nil
end

function Safety.registeredSandboxIdentity()
    local identity = prefs.sandboxCatalogIdentity
    if type(identity) == 'string' and identity ~= '' then
        return identity
    end
    return nil
end

-- Kept as the compatibility probe field. "sandbox" now means explicitly
-- registered, not merely located below a machine-specific directory.
function Safety.isSandboxCatalog(catalogPath)
    local registered = Safety.registeredSandboxIdentity()
    return registered ~= nil and Safety.catalogIdentity(catalogPath) == registered
end

-- One read-only policy snapshot for both authorization and probe reporting.
-- Keeping this calculation here prevents health/status code from growing a
-- second interpretation of what safe and full mean.
function Safety.catalogPolicy(catalogPath)
    local mode = Safety.getAccessMode()
    local registered = Safety.registeredSandboxIdentity()
    local identity = Safety.catalogIdentity(catalogPath)
    local sandbox = registered ~= nil and identity == registered
    return {
        access_mode = mode,
        catalog_identity = identity,
        sandbox = sandbox,
        sandbox_registered = registered ~= nil,
        sandbox_matches_active = sandbox,
        catalog_authorized = mode == Safety.ACCESS_MODE_FULL or sandbox,
    }
end

-- The only authorization policy entry point. probe_client is deliberately
-- available when safe mode is blocked so a caller can discover the active
-- mode and Catalog identity; supplied expected_* bindings still apply.
function Safety.authorize(command, params, catalogPath)
    if type(command) ~= 'string' or command == '' then
        error('Safety authorization requires a command name.')
    end
    params = params or {}
    Safety.validateEnvelope(params)

    local policy = Safety.catalogPolicy(catalogPath)
    local mode = policy.access_mode
    local expectedMode = params[Safety.EXPECTED_ACCESS_MODE]
    if command ~= 'probe_client' and expectedMode == nil then
        error('Access mode binding required: ' .. Safety.EXPECTED_ACCESS_MODE
            .. ' is mandatory for ' .. tostring(command) .. '.')
    end
    if expectedMode ~= nil and expectedMode ~= mode then
        error('Access mode binding rejected: expected ' .. tostring(expectedMode)
            .. ', active: ' .. tostring(mode))
    end

    local identity = policy.catalog_identity
    local expectedIdentity = params[Safety.EXPECTED_CATALOG_IDENTITY]
    if command ~= 'probe_client' and expectedIdentity == nil then
        error('Catalog identity binding required: ' .. Safety.EXPECTED_CATALOG_IDENTITY
            .. ' is mandatory for ' .. tostring(command) .. '.')
    end
    if expectedIdentity ~= nil and expectedIdentity ~= identity then
        error('Catalog identity binding rejected: expected ' .. tostring(expectedIdentity)
            .. ', active: ' .. tostring(identity))
    end

    if command ~= 'probe_client' and not policy.catalog_authorized then
        if not policy.sandbox_registered then
            error('Safe mode blocked the command: no sandbox Catalog is registered.')
        end
        error('Safe mode blocked the command: active Catalog is not the registered sandbox.')
    end

    return policy
end

function Safety.catalogGuardMessage(catalogPath)
    local mode = Safety.getAccessMode()
    if mode == Safety.ACCESS_MODE_FULL then
        return 'Full access mode. Expected Catalog and mode bindings remain enforced.'
    end
    if Safety.isSandboxCatalog(catalogPath) then
        return 'Safe mode. The active Catalog matches the registered sandbox.'
    end
    if Safety.registeredSandboxIdentity() == nil then
        return 'Safe mode blocked. Register the active Catalog as the sandbox in Plugin Manager.'
    end
    return 'Safe mode blocked. The active Catalog is not the registered sandbox.'
end

return Safety
