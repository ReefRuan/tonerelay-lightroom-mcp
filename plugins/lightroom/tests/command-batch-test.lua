-- command_batch regression seam for Commands.lua.
-- Run with: /opt/homebrew/bin/luajit command-batch-test.lua <plugin-dir>
--
-- Locks down the gate discipline that makes batches safe: cores are gate-free,
-- the solo path wraps exactly one gate per command, and run_batch shares ONE
-- write gate across a consecutive run of write-gated commands (LR forbids
-- nested write gates). Also locks the failure contract: stop at the first
-- error and report failed_at_index + results-so-far.

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
local selected = source
local writeGateCount = 0
local readGateCount = 0
local thumbnailData = string.char(0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x01, 0xFF, 0xD9)

local function metadata(photo, key)
  if key == 'uuid' then return photo.uuid end
  if key == 'isVirtualCopy' then return photo.virtual end
  if key == 'masterPhoto' then return photo.master end
  if key == 'dimensions' then return { width = 4000, height = 2000 } end
  return nil
end

for _, photo in ipairs({ source, copy }) do
  function photo:getRawMetadata(key) return metadata(self, key) end
  function photo:getFormattedMetadata(key)
    if key == 'fileName' then return self.file end
    if key == 'copyName' then return self.virtual and 'candidate' or '' end
    return ''
  end
  function photo:getDevelopSettings() return self.settings end
  function photo:applyDevelopPreset() end
  function photo:requestJpegThumbnail(_, _, callback) callback(thumbnailData, 'ready') end
end

local catalog = {
  path = '/var/tmp/tonerelay-fixtures/sandbox/sandbox.lrcat',
}
function catalog:getPath() return self.path end
function catalog:findPhotoByUuid(uuid)
  if uuid == source.uuid then return source end
  if uuid == copy.uuid then return copy end
end
function catalog:setSelectedPhotos(photo) selected = photo end
function catalog:getTargetPhoto() return selected end
function catalog:getTargetPhotos() return { selected } end
function catalog:getMultipleSelectedOrAllPhotos() return { source, copy } end
function catalog:withWriteAccessDo(_, callback)
  writeGateCount = writeGateCount + 1
  callback(); return 'executed'
end
function catalog:withReadAccessDo(callback)
  readGateCount = readGateCount + 1
  callback(); return 'executed'
end
function catalog:createVirtualCopies() return { copy } end

import = function(name)
  if name == 'LrApplication' then
    return { activeCatalog = function() return catalog end, versionString = function() return '15.4.1' end,
      addDevelopPresetForPlugin = function() return {} end }
  elseif name == 'LrExportSession' then
    return function(options) return { renditions = function() return ipairs({ { photo = options.photosToExport[1], waitForRender = function() return true, '/tmp/batch/source.jpg' end } }) end } end
  elseif name == 'LrFileUtils' then
    return { createAllDirectories = function() end,
      exists = function() return 'file' end, fileAttributes = function() return { fileSize = 8 } end, delete = function() end,
      writeFile = function() return true end }
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

print('========== command_batch ==========')

-- Solo paths keep their historical gate behaviour: exactly one gate per call.
writeGateCount = 0
local ok, result = pcall(Commands.apply_candidate, { photo_uuid = copy.uuid, settings = { Exposure2012 = 0.5 } })
check('solo apply_candidate works and opens exactly one write gate',
  ok and writeGateCount == 1, tostring(result) .. ' gates=' .. writeGateCount)

readGateCount = 0
ok, result = pcall(Commands.request_jpeg_thumbnail, { photo_uuid = source.uuid, size = 640, output_dir = '/tmp/batch' })
check('solo request_jpeg_thumbnail works under exactly one read gate',
  ok and readGateCount == 1, tostring(result) .. ' gates=' .. readGateCount)

-- A batch with consecutive write-gated commands shares ONE write gate.
writeGateCount = 0
local outcome = Commands.run_batch({
  { command = 'capture_baseline', params = { photo_uuid = copy.uuid } },
  { command = 'apply_candidate', params = { photo_uuid = copy.uuid, settings = { Exposure2012 = 0.5 } } },
  { command = 'reset_to_baseline', params = { photo_uuid = copy.uuid } },
})
check('batch of baseline+apply+reset returns all three results',
  outcome and not outcome.failed_at_index and #outcome.results == 3,
  tostring(outcome and outcome.error))
check('consecutive write commands share one write gate', writeGateCount == 1,
  'gates=' .. writeGateCount)

-- First failure stops the batch and reports index + partial results.
writeGateCount = 0
outcome = Commands.run_batch({
  { command = 'apply_candidate', params = { photo_uuid = copy.uuid, settings = { Exposure2012 = 0.5 } } },
  { command = 'apply_candidate', params = { photo_uuid = copy.uuid, settings = { Exposure2012 = 99 } } },
  { command = 'apply_candidate', params = { photo_uuid = copy.uuid, settings = { Contrast2012 = 1 } } },
})
check('out-of-bounds second command stops the batch at index 2',
  outcome and outcome.failed_at_index == 2 and #outcome.results == 1
  and tostring(outcome.error):match('out of bounds') ~= nil,
  tostring(outcome and outcome.error))
check('failed write run used one gate, third command never ran', writeGateCount == 1,
  'gates=' .. writeGateCount)

-- Unknown commands fail cleanly instead of crashing the bridge loop.
writeGateCount = 0
outcome = Commands.run_batch({
  { command = 'probe_client', params = {} },
  { command = 'no_such_command', params = {} },
})
check('unknown batch command reports index instead of raising',
  outcome and outcome.failed_at_index == 2
  and tostring(outcome.error):match('Unknown command in batch') ~= nil,
  tostring(outcome and outcome.error))
check('unknown command kept the partial result', #outcome.results == 1)

-- Nested batches are not in the core registry: reported, not recursed.
outcome = Commands.run_batch({
  { command = 'command_batch', params = { commands = {} } },
})
check('nested command_batch is rejected as unknown',
  outcome and outcome.failed_at_index == 1
  and tostring(outcome.error):match('Unknown command') ~= nil,
  tostring(outcome and outcome.error))

-- create_virtual_copy must stay OUTSIDE any write gate (LR 15.4.1 fails
-- createVirtualCopies inside withWriteAccessDo; see the command's comment).
writeGateCount = 0
outcome = Commands.run_batch({
  { command = 'create_virtual_copy', params = { photo_uuid = source.uuid, name = 'b' } },
})
check('batched create_virtual_copy runs with no write gate',
  outcome and not outcome.failed_at_index and writeGateCount == 0,
  tostring(outcome and outcome.error) .. ' gates=' .. writeGateCount)

-- Bare commands interleaved with gated ones: each write run gets its own gate,
-- the read-gated command gets a read gate, bare commands none.
writeGateCount = 0
readGateCount = 0
outcome = Commands.run_batch({
  { command = 'get_develop_snapshot', params = { photo_uuid = copy.uuid } },
  { command = 'apply_candidate', params = { photo_uuid = copy.uuid, settings = { Exposure2012 = 0.1 } } },
  { command = 'request_jpeg_thumbnail', params = { photo_uuid = source.uuid, size = 256, output_dir = '/tmp/batch' } },
  { command = 'apply_candidate', params = { photo_uuid = copy.uuid, settings = { Exposure2012 = 0.2 } } },
  { command = 'export_preview', params = { photo_uuid = copy.uuid, output_dir = '/tmp/batch' } },
})
check('mixed batch completes all five commands',
  outcome and not outcome.failed_at_index and #outcome.results == 5,
  tostring(outcome and outcome.error))
check('split write runs opened two gates, read command one read gate',
  writeGateCount == 2 and readGateCount == 1,
  'write=' .. writeGateCount .. ' read=' .. readGateCount)
check('result entries carry their command names in order',
  outcome.results[1].command == 'get_develop_snapshot'
  and outcome.results[3].command == 'request_jpeg_thumbnail'
  and outcome.results[5].command == 'export_preview')

print('failures: ' .. failures)
os.exit(failures == 0 and 0 or 1)
