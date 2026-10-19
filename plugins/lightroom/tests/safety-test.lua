-- Dual-mode catalog authorization regression seam.
-- Run with: luajit safety-test.lua <plugin-dir>

local plugin = assert(arg[1], 'plugin directory required')
package.path = plugin .. '/?.lua;' .. package.path

local prefs = {}

local function fakeDigest(value)
  local hash = 17
  for i = 1, #value do
    hash = (hash * 131 + string.byte(value, i)) % 4294967296
  end
  return string.rep('0', 24) .. string.format('%08x', hash)
end

_G.import = function(name)
  if name == 'LrPrefs' then
    return { prefsForPlugin = function() return prefs end }
  elseif name == 'LrMD5' then
    return { digest = fakeDigest }
  end
  error('unexpected import ' .. tostring(name))
end

local Safety = dofile(plugin .. '/Safety.lua')
local failures = 0

local function check(label, condition, detail)
  print((condition and '  ✓ ' or '  ✗ ') .. label .. (detail and (' -> ' .. detail) or ''))
  if not condition then failures = failures + 1 end
end

local function expectError(label, fn, needle)
  local ok, err = pcall(fn)
  err = tostring(err)
  check(label, not ok and (needle == nil or string.find(err, needle, 1, true) ~= nil), err)
end

local pathA = 'C:\\Users\\Example\\Catalogs\\Main.lrcat'
local pathAEquivalent = 'c:/users/example/catalogs/main.lrcat/'
local pathB = 'C:\\Users\\Example\\Catalogs\\Other.lrcat'
local identityA = Safety.catalogIdentity(pathA)
local identityB = Safety.catalogIdentity(pathB)

print('========== Safety dual access modes ==========')

check('default mode is full and is persisted', Safety.getAccessMode() == 'full'
  and prefs.accessMode == 'full', tostring(prefs.accessMode))
check('catalog identity is stable across path spelling', identityA == Safety.catalogIdentity(pathAEquivalent),
  identityA)
check('catalog identity is opaque and versioned', string.find(identityA, 'catalog-md5-v1:', 1, true) == 1
  and string.find(identityA, 'users', 1, true) == nil
  and string.find(identityA, 'main.lrcat', 1, true) == nil, identityA)
check('different catalogs get different identities', identityA ~= identityB, identityB)

local probe = Safety.authorize('probe_client', {}, pathA)
check('probe is the only command allowed without a binding', probe.access_mode == 'full'
  and probe.catalog_identity == identityA, probe.catalog_identity)
expectError('non-probe command requires expected_catalog_identity', function()
  Safety.authorize('list_photos', { expected_access_mode = 'full' }, pathA)
end, 'expected_catalog_identity')
expectError('non-probe command requires expected_access_mode', function()
  Safety.authorize('list_photos', { expected_catalog_identity = identityA }, pathA)
end, 'expected_access_mode')

local fullBinding = {
  expected_access_mode = 'full',
  expected_catalog_identity = identityA,
}
local authorized = Safety.authorize('list_photos', fullBinding, pathA)
check('full mode accepts any Catalog when all bindings match', authorized.access_mode == 'full'
  and authorized.sandbox == false)

expectError('full mode still rejects identity drift', function()
  Safety.authorize('list_photos', {
    expected_access_mode = 'full', expected_catalog_identity = identityB,
  }, pathA)
end, 'Catalog identity binding rejected')
expectError('full mode still rejects access-mode drift', function()
  Safety.authorize('list_photos', {
    expected_access_mode = 'safe', expected_catalog_identity = identityA,
  }, pathA)
end, 'Access mode binding rejected')
expectError('path bindings are rejected instead of silently ignored', function()
  Safety.authorize('list_photos', {
    expected_access_mode = 'full', expected_catalog_identity = identityA,
    expected_catalog_path = pathB,
  }, pathA)
end, 'path/name bindings are unsupported')
expectError('basename bindings are rejected instead of silently ignored', function()
  Safety.authorize('list_photos', {
    expected_access_mode = 'full', expected_catalog_identity = identityA,
    expected_catalog_id = 'other.lrcat',
  }, pathA)
end, 'path/name bindings are unsupported')

Safety.setAccessMode('safe')
local safeBindingA = { expected_access_mode = 'safe', expected_catalog_identity = identityA }
expectError('safe mode blocks when no sandbox is registered', function()
  Safety.authorize('list_photos', safeBindingA, pathA)
end, 'no sandbox Catalog is registered')

Safety.registerSandboxCatalog(pathA)
authorized = Safety.authorize('list_photos', safeBindingA, pathAEquivalent)
check('safe mode allows the registered Catalog only', authorized.access_mode == 'safe'
  and authorized.sandbox == true and prefs.sandboxCatalogIdentity == identityA)

expectError('safe mode rejects another correctly bound Catalog', function()
  Safety.authorize('list_photos', {
    expected_access_mode = 'safe', expected_catalog_identity = identityB,
  }, pathB)
end, 'active Catalog is not the registered sandbox')
local outsideProbe = Safety.authorize('probe_client', {}, pathB)
check('probe remains available while safe mode blocks active Catalog', outsideProbe.access_mode == 'safe'
  and outsideProbe.catalog_identity == identityB and outsideProbe.sandbox == false)

Safety.clearSandboxCatalog()
check('sandbox registration can be cleared', prefs.sandboxCatalogIdentity == nil
  and Safety.registeredSandboxIdentity() == nil)

expectError('invalid configured mode is rejected', function() Safety.setAccessMode('other') end,
  'access mode must be safe or full')
expectError('malformed identity envelope is rejected', function()
  Safety.validateEnvelope({ expected_catalog_identity = false })
end, 'expected_catalog_identity')
expectError('unknown expected mode is rejected', function()
  Safety.validateEnvelope({ expected_access_mode = 'other' })
end, 'expected_access_mode')

print('failures: ' .. failures)
os.exit(failures == 0 and 0 or 1)
