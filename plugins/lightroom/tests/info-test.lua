-- Manifest regression: the bridge must initialize at Lightroom startup.
-- Run with: /opt/homebrew/bin/luajit info-test.lua <plugin-dir>

local plugin = assert(arg[1], 'plugin directory required')
local manifest = assert(dofile(plugin .. '/Info.lua'))
assert(manifest.LrInitPlugin == 'AutoStart.lua', 'LrInitPlugin must point to AutoStart.lua')
assert(manifest.LrForceInitPlugin == true, 'LrForceInitPlugin must force startup initialization')
assert(manifest.VERSION.major == 0 and manifest.VERSION.minor == 4
  and manifest.VERSION.revision == 0, 'plug-in manifest version must be 0.4.0')
assert(manifest.VERSION.build == 5, 'plug-in manifest build must identify bridge generation isolation')
assert(type(manifest.LrLibraryMenuItems) == 'table' and #manifest.LrLibraryMenuItems > 0,
  'plugin must expose at least one Library menu item')
print('info-test: ok (startup initialization forced)')
