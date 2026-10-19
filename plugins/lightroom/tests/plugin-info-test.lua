-- Plugin Manager access-mode controls and persistent preference wiring.
-- Run with: luajit plugin-info-test.lua <plugin-dir>

local plugin = assert(arg[1], 'plugin directory required')
package.path = plugin .. '/?.lua;' .. package.path

local prefs = {}
local activePath = '/var/tmp/tonerelay-fixtures/Active.lrcat'

_G.import = function(name)
  if name == 'LrApplication' then
    return { activeCatalog = function()
      return { getPath = function() return activePath end }
    end }
  elseif name == 'LrView' then
    return { bind = function(key) return { binding = key } end }
  elseif name == 'LrPrefs' then
    return { prefsForPlugin = function() return prefs end }
  elseif name == 'LrMD5' then
    return { digest = function() return '0123456789abcdef0123456789abcdef' end }
  end
  error('unexpected import ' .. tostring(name))
end

local factory = {}
for _, kind in ipairs({ 'column', 'row', 'static_text', 'popup_menu', 'push_button' }) do
  factory[kind] = function(_, spec)
    spec.kind = kind
    return spec
  end
end
function factory:control_spacing() return 4 end
function factory:label_spacing() return 3 end
function factory:label_column_width() return 12 end

local provider = dofile(plugin .. '/PluginInfoProvider.lua')
local props = {}
local sections = provider.sectionsForTopOfDialog(factory, props)
local failures = 0

local function check(label, condition, detail)
  print((condition and '  ✓ ' or '  ✗ ') .. label .. (detail and (' -> ' .. detail) or ''))
  if not condition then failures = failures + 1 end
end

local function findByTitle(value, title)
  if type(value) ~= 'table' then return nil end
  if value.title == title then return value end
  for _, child in pairs(value) do
    local found = findByTitle(child, title)
    if found then return found end
  end
  return nil
end

print('========== Plugin Manager access controls ==========')
check('dialog exposes the full default', props.accessMode == 'full' and prefs.accessMode == 'full')
local save = findByTitle(sections, 'Save mode')
local register = findByTitle(sections, 'Register active Catalog as sandbox')
local clear = findByTitle(sections, 'Clear sandbox registration')
check('dialog exposes mode and sandbox actions', save ~= nil and register ~= nil and clear ~= nil
  and type(save.action) == 'function')

props.accessMode = 'safe'
save.action()
check('Save mode persists safe through LrPrefs', prefs.accessMode == 'safe'
  and string.find(props.guardStatus, 'Safe mode blocked', 1, true) ~= nil, props.guardStatus)

register.action()
check('register action persists opaque active Catalog identity',
  prefs.sandboxCatalogIdentity == 'catalog-md5-v1:0123456789abcdef0123456789abcdef'
  and props.registeredSandbox == prefs.sandboxCatalogIdentity
  and string.find(props.guardStatus, 'matches the registered sandbox', 1, true) ~= nil,
  tostring(props.registeredSandbox))

clear.action()
check('clear action removes registration', prefs.sandboxCatalogIdentity == nil
  and props.registeredSandbox == 'Not registered')

props.accessMode = 'full'
provider.endDialog(props, 'ok')
check('closing Plugin Manager also persists the popup', prefs.accessMode == 'full')

print('failures: ' .. failures)
os.exit(failures == 0 and 0 or 1)
