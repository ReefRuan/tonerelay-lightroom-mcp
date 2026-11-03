-- User-visible names can restrict writes, never authorize new operations.
local Policy = {}

function Policy.name(photo)
    return photo.getFormattedMetadata and (photo:getFormattedMetadata('copyName') or '') or ''
end

function Policy.protectedName(name)
    for _, marker in ipairs({'保护', '锁定', '勿改', '只读', '留档'}) do
        if name:find(marker, 1, true) then return true end
    end
    local lower = name:lower()
    for _, word in ipairs({'protected', 'locked', 'readonly'}) do
        if lower:find('%f[%w]' .. word .. '%f[%W]') then return true end
    end
    return lower:find('%f[%w]read[%s_-]+only%f[%W]') ~= nil
        or lower:find('%f[%w]do[%s_-]+not[%s_-]+edit%f[%W]') ~= nil
end

function Policy.protected(photo)
    return Policy.protectedName(Policy.name(photo))
end

function Policy.requireEditable(photo)
    if Policy.protected(photo) then error('Virtual copy is protected by its name; refusing Develop writes.') end
end

return Policy
