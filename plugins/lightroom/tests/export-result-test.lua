-- Offline rendition contract: no real images, Catalog or filesystem writes.
local plugin = assert(arg[1], 'plugin directory required')
package.path = plugin .. '/?.lua;' .. package.path
local files, waits = {}, 0
import = function(name)
  if name == 'LrFileUtils' then return {
    exists = function(path) return files[path] and files[path].kind end,
    fileAttributes = function(path) return files[path] end,
    delete = function() error('never delete diagnostic or unrelated files') end,
  } end
  if name == 'LrPathUtils' then return {
    parent = function(path) return path:gsub('\\', '/'):match('^(.*)/[^/]+$') end,
  } end
  error('unexpected SDK import: ' .. name)
end
local ExportResult = require 'ExportResult'
local photo, other = {}, {}
local directory = 'C:\\ToneRelay 空格\\staging\\job\\output'
local path = directory .. '\\实际副本-2.JPG'
local function rendition(options)
  options = options or {}
  return {
    photo = options.photo or photo, wasSkipped = options.skipped,
    waitForRender = function()
      waits = waits + 1
      if options.failure then return false, options.failure end
      return true, options.path or path
    end,
  }
end
local function session(renditions)
  return { renditions = function() return ipairs(renditions) end,
    doExportOnCurrentTask = function() error('do not export twice') end }
end
local function fails(code, items, dir)
  local ok, errorMessage = pcall(ExportResult.render, session(items), photo, dir or directory)
  assert(not ok and tostring(errorMessage):find(code, 1, true), tostring(errorMessage))
end
files[path] = { kind='file', fileSize=123 }
assert(ExportResult.render(session({rendition()}), photo, directory) == path and waits == 1,
  'use the actual SDK filename, with spaces, Unicode and Windows separators')
assert(ExportResult.render(session({rendition()}), photo, 'c:/tonerelay 空格/staging/job/output/') == path)
fails('render_failed: Disk full', {rendition({failure='Disk full'})})
files[path] = nil
fails('rendered_file_missing', {rendition()})
files[path] = { kind='directory', fileSize=123 }
fails('rendered_file_missing', {rendition()})
files[path] = { kind='file', fileSize=0 }
fails('rendered_file_empty_or_unreadable', {rendition()})
files[path] = { kind='file' }
fails('rendered_file_empty_or_unreadable', {rendition()})
files[path] = { kind='file', fileSize=123 }
fails('render_skipped', {rendition({skipped=true})})
fails('output_directory_mismatch', {rendition({path='C:/elsewhere/actual.jpg'})})
fails('output_directory_mismatch', {rendition({path=directory .. '/child/actual.jpg'})})
fails('output_directory_mismatch', {rendition({path=directory .. '/../actual.jpg'})})
fails('output_format_mismatch', {rendition({path=directory .. '/actual.png'})})
fails('missing_rendition_path', {rendition({path=''})})
fails('rendition_photo_mismatch', {rendition({photo=other})})
fails('unexpected_rendition_count', {})
fails('unexpected_rendition_count', {rendition(), rendition()})
local macPath = '/tmp/Mixed Case/实际名字.jpeg'
files[macPath] = { kind='file', fileSize=123 }
assert(ExportResult.render(session({rendition({path=macPath})}), photo, '/tmp/Mixed Case') == macPath)
fails('output_directory_mismatch', {rendition({path=macPath})}, '/tmp/mixed case')
local unc = '\\\\server\\共享 空格\\output\\actual.jpg'
files[unc] = { kind='file', fileSize=123 }
assert(ExportResult.render(session({rendition({path=unc})}), photo, '//SERVER/共享 空格/output') == unc)
print('Export rendition status, actual filename, missing/empty files, path boundaries and single render passed')
