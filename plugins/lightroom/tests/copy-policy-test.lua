-- Name based copy protection must match the Python-side policy.
local plugin = assert(arg[1], 'plugin directory required')
package.path = plugin .. '/?.lua;' .. package.path

local Policy = require 'CopyPolicy'
local protected = {
    '保护', '锁定', '勿改', '只读', '留档', 'Protected', 'LOCKED', 'readonly',
    'Read-only', 'read only', 'do-not-edit', 'Do Not Edit',
}
for _, marker in ipairs(protected) do
    assert(Policy.protectedName('portrait ' .. marker .. ' v2'), marker)
end
for _, name in ipairs({'unprotected', 'Unlocked', 'readonlyish', 'do-not-editor',
                       'xlocked', 'TRLR', 'Warm portrait'}) do
    assert(not Policy.protectedName(name), name)
end

local photo = {name = 'TRLR'}
function photo:getFormattedMetadata(key)
    assert(key == 'copyName')
    return self.name
end
Policy.requireEditable(photo)
photo.name = '归档-勿改'
assert(not pcall(Policy.requireEditable, photo))
print('copy-policy-test: passed')
