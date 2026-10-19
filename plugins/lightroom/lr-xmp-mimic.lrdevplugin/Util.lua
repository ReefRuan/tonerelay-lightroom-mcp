local Util = {}

function Util.clamp(value, minimum, maximum)
    if value < minimum then
        return minimum
    end
    if value > maximum then
        return maximum
    end
    return value
end

function Util.deepEqual(left, right, seen)
    if left == right then
        return true
    end

    if type(left) ~= type(right) then
        return false
    end

    if type(left) ~= 'table' then
        return false
    end

    seen = seen or {}
    if seen[left] == right then
        return true
    end
    seen[left] = right

    for key, value in pairs(left) do
        if not Util.deepEqual(value, right[key], seen) then
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

function Util.photoDescription(photo)
    if not photo then
        return 'Not set'
    end

    local fileName = photo:getFormattedMetadata('fileName') or '(unnamed)'
    local dimensions = photo:getRawMetadata('dimensions') or {}
    local width = dimensions.width or '?'
    local height = dimensions.height or '?'
    local copyName = photo:getFormattedMetadata('copyName')

    if copyName and copyName ~= '' then
        return string.format('%s — %s — %sx%s', fileName, copyName, width, height)
    end

    return string.format('%s — %sx%s', fileName, width, height)
end

function Util.safeRestoreSelection(catalog, activePhoto, selectedPhotos)
    if not activePhoto then
        return
    end

    local others = {}
    for _, photo in ipairs(selectedPhotos or {}) do
        if photo ~= activePhoto then
            others[#others + 1] = photo
        end
    end

    pcall(function()
        catalog:setSelectedPhotos(activePhoto, others)
    end)
end

return Util
