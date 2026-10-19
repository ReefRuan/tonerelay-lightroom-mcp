-- Manifest regression: the bridge must initialize at Lightroom startup.
-- Run with: /opt/homebrew/bin/luajit info-test.lua <plugin-dir>

local plugin = assert(arg[1], 'plugin directory required')
local manifest = assert(dofile(plugin .. '/Info.lua'))
assert(manifest.LrInitPlugin == 'AutoStart.lua', 'LrInitPlugin must point to AutoStart.lua')
assert(manifest.LrShutdownPlugin == 'Shutdown.lua', 'plugin shutdown must retire the polling generation')
assert(manifest.LrForceInitPlugin == true, 'LrForceInitPlugin must force startup initialization')
assert(manifest.VERSION.major == 0 and manifest.VERSION.minor == 6
  and manifest.VERSION.revision == 2, 'plug-in manifest version must match the 0.6.2 Runtime')
assert(manifest.VERSION.build == 3, 'plug-in manifest build must identify polling lifecycle support')
assert(type(manifest.LrLibraryMenuItems) == 'table' and #manifest.LrLibraryMenuItems > 0,
  'plugin must expose at least one Library menu item')
assert(type(manifest.LrExportMenuItems) == 'table', 'plugin must expose File > Plug-in Extras recovery')
assert(manifest.LrExportMenuItems == manifest.LrLibraryMenuItems,
  'File and Library recovery menus must use the same entries')
local expectedFiles = { 'StartBridge.lua', 'SelfTest.lua', 'StopBridge.lua' }
assert(#manifest.LrExportMenuItems == #expectedFiles, 'all bridge recovery commands must be exposed')
for i, file in ipairs(expectedFiles) do
  local item = manifest.LrExportMenuItems[i]
  assert(item.file == file and type(item.title) == 'string' and item.title ~= '', 'invalid bridge recovery menu entry')
  local handle = assert(io.open(plugin .. '/' .. item.file, 'r'), 'menu command file must exist')
  handle:close()
end
print('info-test: ok (startup initialization forced)')
