-- F1 regression seam for Commands.lua.
-- Run with: /opt/homebrew/bin/luajit foundation-f1-test.lua <plugin-dir>
-- The mock models the documented array return shape.  A separate false-return
-- case locks down rejection of an unsuccessful SDK call.  The regression also
-- proves virtual-copy creation is not nested in a catalog write gate: LR's
-- selection API is an async-task API and LR 15.4.1 fails when the copy call is
-- made from inside withWriteAccessDo.

local plugin = assert(arg[1], 'plugin directory required')
package.path = plugin .. '/?.lua;' .. package.path

local source = {
  uuid = 'source-uuid',
  virtual = false,
  file = 'source.jpg',
  settings = { Exposure2012 = 0 },
}
local copy = {
  uuid = 'copy-uuid',
  virtual = true,
  file = 'source.jpg',
  settings = { Exposure2012 = 0 },
  master = source,
}
-- LR never chains virtual copies: a copy made from a virtual copy still
-- reports the master photo, not the source virtual copy.
local copy2 = {
  uuid = 'copy2-uuid',
  virtual = true,
  file = 'source.jpg',
  settings = copy.settings,
  master = source,
}
local stranger = {
  uuid = 'stranger-uuid',
  virtual = false,
  file = 'other.jpg',
  settings = { Exposure2012 = 0 },
}
local badCopy = {
  uuid = 'badcopy-uuid',
  virtual = true,
  file = 'other.jpg',
  settings = copy.settings,
  master = stranger,
}
local selected = source
local exportSettings
local exportMissing = false
local selectFailure = false
local writeFailure = false
local copyCreateFailure = false
local copyInfoFailure = false
local thumbnailUnavailable = false
local thumbnailNoCallback = false
local thumbnailShortBytes = false
local thumbnailData = string.char(0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x01, 0xFF, 0xD9)
local thumbnailWritten

local function metadata(photo, key)
  if key == 'uuid' then return photo.uuid end
  if key == 'isVirtualCopy' then return photo.virtual end
  if key == 'masterPhoto' then
    if copyInfoFailure and photo == copy then error('mock master lookup failed') end
    return photo.master
  end
  if key == 'dimensions' then return { width = 4000, height = 2000 } end
  return nil
end

for _, photo in ipairs({ source, copy, copy2, badCopy }) do
  function photo:getRawMetadata(key) return metadata(self, key) end
  function photo:getFormattedMetadata(key)
    if key == 'fileName' then return self.file end
    if key == 'copyName' then return self.virtual and 'candidate' or '' end
    return ''
  end
  function photo:getDevelopSettings() return self.settings end
  function photo:requestJpegThumbnail(_, _, callback)
    if thumbnailNoCallback then
      return { state = 'pending' }
    elseif thumbnailUnavailable then
      callback(nil, 'notAvailable')
    elseif thumbnailShortBytes then
      callback(string.char(0xFF, 0xD8), 'ready')
    else
      callback(thumbnailData, 'ready')
    end
    return { state = 'complete' }
  end
end

local catalog = {
  path = '/var/tmp/tonerelay-fixtures/sandbox/sandbox.lrcat',
}
function catalog:getPath() return self.path end
function catalog:findPhotoByUuid(uuid)
  if uuid == source.uuid then return source end
  if uuid == copy.uuid then return copy end
  if uuid == copy2.uuid then return copy2 end
end
function catalog:setSelectedPhotos(photo)
  if selectFailure then error('mock selection failed') end
  selected = photo
end
function catalog:getTargetPhoto() return selected end
function catalog:getTargetPhotos() return { selected } end
function catalog:getMultipleSelectedOrAllPhotos() return { source, copy } end
function catalog:withWriteAccessDo(_, callback)
  if writeFailure then error('mock write gate failed') end
  callback(); return 'executed'
end
function catalog:withReadAccessDo(callback)
  callback(); return 'executed'
end
function catalog:createVirtualCopies()
  if copyCreateFailure then error('mock createVirtualCopies failed') end
  if self.returnFalse then return false end
  if self.copyFromVirtual then selected = copy2; return { copy2 } end
  if self.returnBadMaster then selected = badCopy; return { badCopy } end
  selected = copy
  return { copy }
end

import = function(name)
  if name == 'LrApplication' then
    return { activeCatalog = function() return catalog end, versionString = function() return '15.4.1' end,
      addDevelopPresetForPlugin = function() return {} end }
  elseif name == 'LrExportSession' then
    return function(options)
      exportSettings = options.exportSettings
      return { renditions = function() return ipairs({ { photo = options.photosToExport[1], waitForRender = function() return true, '/tmp/f1/source.jpg' end } }) end }
    end
  elseif name == 'LrFileUtils' then
    return { createAllDirectories = function() end,
      exists = function() return not exportMissing and 'file' or false end, fileAttributes = function() return { fileSize = 8 } end, delete = function() end,
      writeFile = function(path, data) thumbnailWritten = { path = path, data = data } end }
  elseif name == 'LrPathUtils' then
    return { child = function(dir, name) return dir .. '/' .. name end, parent = function(path) return path:match('^(.*)/[^/]+$') end }
  elseif name == 'LrTasks' then
    return { pcall = function(fn, ...) return pcall(fn, ...) end,
      sleep = function() end }
  end
  return {}
end

local Commands = assert(loadfile(plugin .. '/Commands.lua'))()
local failures = 0
local function check(label, ok, detail)
  print((ok and '  ✓ ' or '  ✗ ') .. label .. (detail and (' -> ' .. detail) or ''))
  if not ok then failures = failures + 1 end
end

print('========== F1 Lua SDK regression ==========' )
local ok, result = pcall(Commands.create_virtual_copy, { photo_uuid = source.uuid })
check('array createVirtualCopies result yields verified copy', ok and result and result.copy
  and result.copy.uuid == copy.uuid, tostring(result))
catalog.returnFalse = true
ok, result = pcall(Commands.create_virtual_copy, { photo_uuid = source.uuid })
check('false createVirtualCopies result is rejected', not ok and tostring(result):match('Expected exactly one virtual copy') ~= nil,
  tostring(result))
catalog.returnFalse = false

selectFailure = true; selected = copy
ok, result = pcall(Commands.create_virtual_copy, { photo_uuid = source.uuid })
check('selection phase errors retain phase label', not ok and tostring(result):match('phase=select_source') ~= nil,
  tostring(result))
selectFailure = false

writeFailure = true
ok, result = pcall(Commands.create_virtual_copy, { photo_uuid = source.uuid })
check('virtual-copy creation does not require a catalog write gate', ok and result and result.copy
  and result.copy.uuid == copy.uuid, tostring(result))
writeFailure = false

copyCreateFailure = true
ok, result = pcall(Commands.create_virtual_copy, { photo_uuid = source.uuid })
check('failure inside createVirtualCopies reports create_copies phase',
  not ok and tostring(result):match('phase=create_copies') ~= nil,
  tostring(result))
copyCreateFailure = false

copyInfoFailure = true
ok, result = pcall(Commands.create_virtual_copy, { photo_uuid = source.uuid })
check('copy verification phase errors retain phase label', not ok and tostring(result):match('phase=read_copy_info') ~= nil,
  tostring(result))
copyInfoFailure = false

catalog.copyFromVirtual = true
ok, result = pcall(Commands.create_virtual_copy, { photo_uuid = copy.uuid })
check('copy of a virtual copy is allowed; master resolves via the shared master',
  ok and result and result.copy and result.copy.uuid == copy2.uuid, tostring(result))
catalog.copyFromVirtual = false

catalog.returnBadMaster = true
ok, result = pcall(Commands.create_virtual_copy, { photo_uuid = source.uuid })
check('copy whose master matches neither source nor source master is rejected',
  not ok and tostring(result):match('master mismatch') ~= nil, tostring(result))
catalog.returnBadMaster = false

ok, result = pcall(Commands.export_preview, { photo_uuid = source.uuid, long_edge = 600, output_dir = '/tmp/f1' })
check('export completes in mock', ok, tostring(result))
check('long edge uses SDK LR_size_maxHeight', exportSettings and exportSettings.LR_size_maxHeight == 600,
  exportSettings and tostring(exportSettings.LR_size_maxHeight) or 'missing')
check('legacy LR_size_longEdge is absent', exportSettings and exportSettings.LR_size_longEdge == nil,
  exportSettings and tostring(exportSettings.LR_size_longEdge))
exportMissing = true
ok, result = pcall(Commands.export_preview, { photo_uuid = source.uuid, long_edge = 600, output_dir = '/tmp/f1' })
check('export missing output is rejected', not ok and tostring(result):match('rendered_file_missing') ~= nil,
  tostring(result))

exportMissing = false
exportSettings = nil
thumbnailWritten = nil
ok, result = pcall(Commands.request_jpeg_thumbnail, {
  photo_uuid = source.uuid, size = 640, output_dir = '/tmp/fast-preview'
})
check('requestJpegThumbnail writes a fast JPEG without export', ok and result
  and result.method == 'requestJpegThumbnail' and result.purpose == 'fast'
  and thumbnailWritten and thumbnailWritten.data == thumbnailData, tostring(result))
check('fast preview does not construct an export session', exportSettings == nil,
  tostring(exportSettings))

thumbnailUnavailable = true
ok, result = pcall(Commands.request_jpeg_thumbnail, {
  photo_uuid = source.uuid, size = 640, output_dir = '/tmp/fast-preview'
})
check('missing fast preview has stable unavailable marker',
  not ok and tostring(result):match('TRLR_FAST_THUMBNAIL_UNAVAILABLE:') ~= nil,
  tostring(result))
thumbnailUnavailable = false

thumbnailNoCallback = true
ok, result = pcall(Commands.request_jpeg_thumbnail, {
  photo_uuid = source.uuid, size = 640, output_dir = '/tmp/fast-preview'
})
check('missing callback is a propagated timeout, not thumbnail unavailable',
  not ok and tostring(result):match('timed out waiting for the Lightroom callback') ~= nil
    and tostring(result):match('TRLR_FAST_THUMBNAIL_UNAVAILABLE:') == nil,
  tostring(result))
thumbnailNoCallback = false

thumbnailShortBytes = true
ok, result = pcall(Commands.request_jpeg_thumbnail, {
  photo_uuid = source.uuid, size = 640, output_dir = '/tmp/fast-preview'
})
check('short callback bytes are propagated corruption, not thumbnail unavailable',
  not ok and tostring(result):match('truncated JPEG bytes') ~= nil
    and tostring(result):match('TRLR_FAST_THUMBNAIL_UNAVAILABLE:') == nil,
  tostring(result))
thumbnailShortBytes = false

print('failures: ' .. failures)
os.exit(failures == 0 and 0 or 1)
