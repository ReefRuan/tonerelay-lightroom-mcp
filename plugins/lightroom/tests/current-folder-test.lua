-- Read-only current-target-folder contract for Commands.get_current_folder.
-- Run with: luajit current-folder-test.lua <plugin-dir>

local plugin = assert(arg[1], 'plugin directory required')
package.path = plugin .. '/?.lua;' .. package.path

local failures = 0
local function check(label, condition, detail)
    print((condition and '  ✓ ' or '  ✗ ') .. label .. (detail and (' -> ' .. detail) or ''))
    if not condition then failures = failures + 1 end
end

local function expectError(label, fn, needle)
    local ok, err = pcall(fn)
    err = tostring(err)
    check(label, not ok and (needle == nil or string.find(err, needle, 1, true) ~= nil), err)
    return err
end

local state = { readDepth = 0, readGates = 0, folderLookupDepth = nil, folderPhotosDepth = nil }
local folderPath = '/private/photos/Current Folder'
local catalogPath = '/private/catalogs/Private Catalog.lrcat'

local Photo = {}
Photo.__index = Photo

function Photo:getRawMetadata(key)
    if key == 'uuid' then return self.uuid end
    if key == 'path' then return self.path end
    if key == 'isVideo' then return self.video end
    if key == 'isVirtualCopy' then return self.virtual end
    if key == 'dimensions' then return { width = self.width, height = self.height } end
    return nil
end

function Photo:getFormattedMetadata(key)
    if key == 'fileName' then return self.file end
    if key == 'copyName' then return self.copy end
    return ''
end

function Photo:getDevelopSettings() return {} end
function Photo:applyDevelopPreset() end

local function photo(uuid, file, opts)
    opts = opts or {}
    return setmetatable({
        uuid = uuid,
        file = file,
        path = opts.path or (folderPath .. '/' .. file),
        copy = opts.copy or '',
        virtual = opts.virtual == true,
        video = opts.video == true,
        width = opts.width or 6000,
        height = opts.height or 4000,
    }, Photo)
end

local a = photo('u1', 'a.jpg')
local upperB = photo('u2', 'B.jpg', { virtual = true, copy = 'candidate' })
local lowerB = photo('u3', 'b.jpg')
local video = photo('v1', 'clip.mp4', { video = true })
local selected = lowerB

local folder = { name = 'Current Folder', photos = { video, lowerB, upperB, a } }
function folder:getName() return self.name end
function folder:getPhotos(includeChildren)
    check('folder query excludes child folders', includeChildren == false)
    state.folderPhotosDepth = state.readDepth
    return self.photos
end

local catalog = {}
function catalog:getPath() return catalogPath end
function catalog:getTargetPhoto() return selected end
function catalog:getFolderByPath(path)
    state.folderLookupDepth = state.readDepth
    if path == folderPath then return folder end
    return nil
end
function catalog:withReadAccessDo(fn)
    state.readGates = state.readGates + 1
    state.readDepth = state.readDepth + 1
    local ok, err = pcall(fn)
    state.readDepth = state.readDepth - 1
    if not ok then error(err, 0) end
    return 'executed'
end

_G.import = function(name)
    if name == 'LrApplication' then
        return {
            activeCatalog = function() return catalog end,
            versionString = function() return '15.4.1' end,
            addDevelopPresetForPlugin = function() return {} end,
        }
    elseif name == 'LrPathUtils' then
        return {
            child = function(dir, name_) return dir .. '/' .. name_ end,
            parent = function(path) return string.match(path, '^(.*)/[^/]+$') end,
        }
    elseif name == 'LrTasks' then
        return { pcall = function(fn, ...) return pcall(fn, ...) end, sleep = function() end }
    elseif name == 'LrFileUtils' then
        return { exists = function() return false end, createAllDirectories = function() end,
            delete = function() end }
    elseif name == 'LrExportSession' then
        return function() return { doExportOnCurrentTask = function() end } end
    elseif name == 'LrApplicationView' then
        return {}
    end
    return {}
end
_G._PLUGIN = {}

local Json = dofile(plugin .. '/Json.lua')
local Commands = assert(loadfile(plugin .. '/Commands.lua'))()

print('========== current folder ==========' )

local result = Commands.get_current_folder({ limit = 2, offset = 1 })
check('target is the current Lightroom target', result.target and result.target.uuid == 'u3')
check('folder is leaf-only and opaque', result.folder and result.folder.label == 'Current Folder'
    and string.match(result.folder.id, '^folder%-hash%-v1:[0-9a-f]+$') ~= nil
    and string.find(result.folder.id, 'private', 1, true) == nil, result.folder and result.folder.id)
check('video is excluded and page is stable ASCII case-insensitive', result.total == 3
    and result.count == 2 and result.limit == 2 and result.offset == 1 and result.has_more == false
    and result.photos[1].uuid == 'u2' and result.photos[2].uuid == 'u3')
check('photo records use the public metadata shape', result.photos[1].file_name == 'B.jpg'
    and result.photos[1].copy_name == 'candidate' and result.photos[1].is_virtual_copy == true
    and result.photos[1].width == 6000 and result.photos[1].height == 4000
    and result.photos[1].path == nil)
check('folder lookup and enumeration happen outside the metadata read gate', state.folderLookupDepth == 0
    and state.folderPhotosDepth == 0 and state.readGates == 1,
    'lookup=' .. tostring(state.folderLookupDepth) .. ' photos=' .. tostring(state.folderPhotosDepth)
        .. ' gates=' .. tostring(state.readGates))
local encoded = Json.encode(result)
check('result JSON contains no absolute catalog or folder path', string.find(encoded, '/private/', 1, true) == nil, encoded)

folder.name = ''
result = Commands.get_current_folder({ limit = 1 })
check('empty SDK label falls back internally to the folder leaf', result.folder.label == 'Current Folder')
folder.name = 'Current Folder'

selected = nil
result = Commands.get_current_folder({})
check('no current target is a valid empty page', result.target == nil and result.folder == nil
    and result.total == 0 and result.count == 0 and result.limit == 10 and result.offset == 0
    and result.has_more == false)
check('empty page survives the bridge as an array, not an object',
    string.find(Json.encode(result), '"photos":[]', 1, true) ~= nil, Json.encode(result))
selected = lowerB

expectError('limit rejects non-integers', function()
    Commands.get_current_folder({ limit = 1.5 })
end, 'limit must be an integer')
expectError('unknown fields are rejected before catalog access', function()
    Commands.get_current_folder({ path = folderPath })
end, 'unknown field')

local originalFolder = catalog.getFolderByPath
function catalog:getFolderByPath(_) return nil end
local errorText = expectError('unavailable folder does not echo local path', function()
    Commands.get_current_folder({})
end, 'unavailable in the active Catalog')
check('folder failure does not expose the path', string.find(errorText, '/private/', 1, true) == nil, errorText)
catalog.getFolderByPath = originalFolder

print('failures: ' .. failures)
os.exit(failures == 0 and 0 or 1)
