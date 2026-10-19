-- Read-only Catalog query owned by ToneRelay.
--
-- The module deliberately has one public operation. Bridge.lua performs the
-- Catalog authorization before dispatch; this layer validates its own query
-- fields, obtains candidates from Lightroom, snapshots public metadata under
-- a read gate, and returns a deterministic page without exposing file paths.
local LrApplication = import 'LrApplication'
local LrTasks = import 'LrTasks'

local Safety = require 'Safety'

local Catalog = {}

local DEFAULT_PAGE_SIZE = 100
local MAX_PAGE_SIZE = 2000

local QUERY_FIELDS = {
    limit = true,
    offset = true,
    name_contains = true,
    rating = true,
}

local function isFiniteNumber(value)
    return type(value) == 'number'
        and value == value
        and value > -math.huge
        and value < math.huge
end

local function requireInteger(value, minimum, maximum, message)
    if not isFiniteNumber(value)
        or value ~= math.floor(value)
        or value < minimum
        or (maximum ~= nil and value > maximum) then
        error(message)
    end
    return value
end

local function parseQuery(args)
    if args == nil then
        args = {}
    end
    if type(args) ~= 'table' then
        error('search arguments must be an object')
    end

    for key in pairs(args) do
        if QUERY_FIELDS[key] ~= true and not Safety.isEnvelopeField(key) then
            error('search contains unknown field: ' .. tostring(key))
        end
    end
    Safety.validateEnvelope(args)

    local limit = args.limit
    if limit == nil then
        limit = DEFAULT_PAGE_SIZE
    end
    limit = requireInteger(
        limit,
        1,
        MAX_PAGE_SIZE,
        'limit must be an integer from 1 to ' .. MAX_PAGE_SIZE
    )

    local offset = args.offset
    if offset == nil then
        offset = 0
    end
    offset = requireInteger(
        offset,
        0,
        nil,
        'offset must be a non-negative integer'
    )

    local nameContains = args.name_contains
    if nameContains == nil then
        nameContains = ''
    end
    if type(nameContains) ~= 'string' then
        error('name_contains must be a string')
    end

    local rating = args.rating
    if rating ~= nil then
        rating = requireInteger(
            rating,
            0,
            5,
            'rating must be an integer from 0 to 5'
        )
    end

    return {
        limit = limit,
        offset = offset,
        name_contains = nameContains,
        rating = rating,
    }
end

-- The wire contract promises ASCII case-insensitive matching. Performing this
-- conversion explicitly keeps matching and ordering independent of locale.
local function foldAscii(value)
    return (string.gsub(value, '[A-Z]', function(character)
        return string.char(string.byte(character) + 32)
    end))
end

local function nativeSearchDescription(query)
    local description = { combine = 'intersect' }

    if query.name_contains ~= '' then
        description[#description + 1] = {
            criteria = 'filename',
            operation = 'any',
            value = query.name_contains,
        }
    end

    -- Lightroom represents an unrated photo as nil. Its native rating == 0
    -- search excludes those photos, so zero is filtered after enumeration.
    if query.rating ~= nil and query.rating ~= 0 then
        description[#description + 1] = {
            criteria = 'rating',
            operation = '==',
            value = query.rating,
        }
    end

    if #description == 0 then
        return nil
    end
    return description
end

local function checkedSdkCall(label, operation)
    local ok, value = LrTasks.pcall(operation)
    if not ok then
        error('Catalog ' .. label .. ' failed: ' .. tostring(value))
    end
    if type(value) ~= 'table' then
        error('Catalog ' .. label .. ' failed: returned ' .. tostring(value))
    end
    return value
end

-- findPhotos is an asynchronous SDK operation and can yield. In particular,
-- invoking it while holding a Catalog read gate may deadlock on Windows, so
-- every candidate query is completed before metadata access begins.
local function loadCandidates(catalog, query)
    local description = nativeSearchDescription(query)
    if description ~= nil then
        return checkedSdkCall('findPhotos', function()
            return catalog:findPhotos({ searchDesc = description })
        end)
    end
    return checkedSdkCall('getAllPhotos', function()
        return catalog:getAllPhotos()
    end)
end

local function numberOrZero(value)
    if not isFiniteNumber(value) then
        return 0
    end
    return value
end

local function textOrEmpty(value)
    if type(value) ~= 'string' then
        return ''
    end
    return value
end

local function snapshotPhoto(photo)
    if photo:getRawMetadata('isVideo') == true then
        return nil
    end

    local dimensions = photo:getRawMetadata('dimensions')
    if type(dimensions) ~= 'table' then
        dimensions = {}
    end

    local rating = numberOrZero(photo:getRawMetadata('rating'))
    rating = math.floor(rating)

    return {
        uuid = textOrEmpty(photo:getRawMetadata('uuid')),
        file_name = textOrEmpty(photo:getFormattedMetadata('fileName')),
        copy_name = textOrEmpty(photo:getFormattedMetadata('copyName')),
        is_virtual_copy = photo:getRawMetadata('isVirtualCopy') == true,
        width = numberOrZero(dimensions.width),
        height = numberOrZero(dimensions.height),
        rating = rating,
    }
end

local function readMetadata(catalog, candidates)
    local records = {}
    local ok, failure = LrTasks.pcall(function()
        catalog:withReadAccessDo(function()
            for _, photo in ipairs(candidates) do
                local record = snapshotPhoto(photo)
                if record ~= nil then
                    records[#records + 1] = record
                end
            end
        end)
    end)
    if not ok then
        error('Catalog metadata read failed: ' .. tostring(failure))
    end
    return records
end

local function matches(record, query, foldedNeedle)
    if foldedNeedle ~= ''
        and string.find(foldAscii(record.file_name), foldedNeedle, 1, true) == nil then
        return false
    end
    if query.rating ~= nil and record.rating ~= query.rating then
        return false
    end
    return true
end

local function filterAndSort(records, query)
    local filtered = {}
    local needle = foldAscii(query.name_contains)

    for _, record in ipairs(records) do
        if matches(record, query, needle) then
            record._folded_file_name = foldAscii(record.file_name)
            filtered[#filtered + 1] = record
        end
    end

    table.sort(filtered, function(left, right)
        if left._folded_file_name ~= right._folded_file_name then
            return left._folded_file_name < right._folded_file_name
        end
        return left.uuid < right.uuid
    end)

    for _, record in ipairs(filtered) do
        record._folded_file_name = nil
    end
    return filtered
end

local function page(records, query)
    local photos = {}
    local last = math.min(query.offset + query.limit, #records)
    for index = query.offset + 1, last do
        photos[#photos + 1] = records[index]
    end

    return {
        count = #photos,
        total = #records,
        limit = query.limit,
        offset = query.offset,
        has_more = query.offset + #photos < #records,
        photos = photos,
    }
end

function Catalog.search(args)
    local query = parseQuery(args)
    local activeCatalog = LrApplication.activeCatalog()
    if activeCatalog == nil then
        error('No active Lightroom Catalog.')
    end

    local candidates = loadCandidates(activeCatalog, query)
    local records = readMetadata(activeCatalog, candidates)
    return page(filterAndSort(records, query), query)
end

return Catalog
