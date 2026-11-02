-- Shared public metadata shape for live search and folder candidate capture.
local PhotoRecord = {}
local function number(value)
    if type(value)~='number' or value~=value or value==math.huge or value==-math.huge then return 0 end
    return value
end
local function text(value) return type(value)=='string' and value or '' end
function PhotoRecord.snapshot(photo)
    if photo:getRawMetadata('isVideo')==true then return nil end
    local dimensions=photo:getRawMetadata('dimensions')
    if type(dimensions)~='table' then dimensions={} end
    return {uuid=text(photo:getRawMetadata('uuid')),
            file_name=text(photo:getFormattedMetadata('fileName')),
            copy_name=text(photo:getFormattedMetadata('copyName')),
            is_virtual_copy=photo:getRawMetadata('isVirtualCopy')==true,
            width=number(dimensions.width),height=number(dimensions.height),
            rating=math.floor(number(photo:getRawMetadata('rating')))}
end
return PhotoRecord
