-- Offline SDK seam: UI restoration never retries a virtual-copy write.
local plugin = assert(arg[1], 'plugin directory required')
package.path = plugin .. '/?.lua;' .. package.path
local source = { uuid = 'source', file = 'source.jpg', virtual = false }
local original = { uuid = 'original', file = 'original.jpg', virtual = false }
local other = { uuid = 'other', file = 'other.jpg', virtual = false }
local copy = { uuid = 'copy', file = 'source.jpg', virtual = true, master = source }
local sources, selected, target, module, active, mode, catalog
local writes, creations, targetReads, restoreSelectionAttempts, sourceWrites, moduleWrites
for _, photo in ipairs({ source, original, other, copy }) do
  function photo:getRawMetadata(key)
    if mode == 'restore_catalog_changes' and active ~= catalog then error('photo object invalid after Catalog switch') end
    if key == 'uuid' then return self.uuid end
    if key == 'isVirtualCopy' then return self.virtual end
    if key == 'masterPhoto' then return self.master end
    if key == 'dimensions' then return { width = 40, height = 30 } end
  end
  function photo:getFormattedMetadata(key)
    if mode == 'restore_catalog_changes' and active ~= catalog then error('photo object invalid after Catalog switch') end
    return key == 'fileName' and self.file or ''
  end
end
local folder = { private_path = '/private/never-on-wire' }
local folder2 = { private_path = 'C:/private/never-on-wire' }
local crossCases = {
  restore_sources_false=true, restore_sources_throws=true, restore_sources_mismatch=true,
  source_clears_selection=true, source_changes_selection=true, restore_catalog_changes=true,
  prepare_catalog_changes=true, prepare_user_changes=true, user_during_restore_readback=true,
  cross_source=true, cross_selection_delayed=true,
}
function folder:getPhotos() return crossCases[mode] and { original, other } or { source, original, other, copy } end
function folder2:getPhotos() return { other } end
catalog = { kAllPhotos = 'AllPhotos' }
local replacementCatalog = { getPath = function() return 'different-catalog' end }
function catalog:getPath() return 'test-catalog' end
function catalog:findPhotoByUuid(uuid)
  for _, photo in ipairs({ source, original, other, copy }) do if photo.uuid == uuid then return photo end end
end
function catalog:getActiveSources() return sources end
function catalog:setActiveSources(value)
  writes = writes + 1; sourceWrites = sourceWrites + 1
  if value ~= self.kAllPhotos and mode == 'restore_sources_false' then return false end
  if value ~= self.kAllPhotos and mode == 'restore_sources_throws' then error('/private/SDK-error') end
  if value ~= self.kAllPhotos and mode == 'restore_sources_mismatch' then return true end
  if value ~= self.kAllPhotos and mode == 'user_during_restore_readback' then return true end
  sources = type(value) == 'table' and value or { value }
  if value ~= self.kAllPhotos and mode == 'source_clears_selection' then target = nil; selected = {} end
  if value ~= self.kAllPhotos and mode == 'source_changes_selection' then target = other; selected = { other } end
  if value ~= self.kAllPhotos and mode == 'restore_catalog_changes' then active = replacementCatalog end
  return true
end
function catalog:getTargetPhoto() return target end
function catalog:getTargetPhotos()
  targetReads = targetReads + 1
  return target and selected or { source, original, other }
end
function catalog:setSelectedPhotos(photo, others)
  writes = writes + 1
  if mode == 'filtered_out' and photo == source then return end
  if mode == 'prepare_throws' and photo == source then error('prepare original failure') end
  if (mode == 'selection_delayed' or mode == 'selection_never' or mode == 'selection_user_during_yield') and photo == original then
    restoreSelectionAttempts = restoreSelectionAttempts + 1
    -- Real LR can ignore the first selection setter while the restored
    -- source/filmstrip is still becoming usable; a getter already matches.
    if mode ~= 'selection_delayed' or restoreSelectionAttempts < 3 then return end
  end
  target = photo; selected = { photo }
  for _, otherPhoto in ipairs(others or {}) do selected[#selected + 1] = otherPhoto end
end
function catalog:createVirtualCopies()
  creations = creations + 1
  if mode == 'create_throws' then error('create original failure') end
  target = copy; selected = { copy }
  if mode == 'catalog_changed' then active = replacementCatalog end
  if mode == 'user_changed' then sources = { folder2 }; target = other; selected = { other }; module = 'map' end
  if mode == 'copy_info_throws' then copy.master = other else copy.master = source end
  return { copy }
end
local view = {
  getCurrentModuleName = function() return module end,
  switchToModule = function(value) writes = writes + 1; moduleWrites = moduleWrites + 1; module = value end,
}
local selection = { selectNone = function() writes = writes + 1; target = nil; selected = {} end }
import = function(name)
  if name == 'LrApplication' then return { activeCatalog = function() return active end } end
  if name == 'LrApplicationView' then return view end
  if name == 'LrSelection' then return selection end
  if name == 'LrTasks' then return { pcall = pcall, sleep = function(seconds)
    if mode == 'prepare_catalog_changes' then active = replacementCatalog end
    if mode == 'prepare_user_changes' then sources = { folder2 }; target = other; selected = { other }; module = 'map' end
    if mode == 'user_during_restore_readback' and seconds == 0.05 and creations > 0 then
      sources = { folder, folder2 }; target = other; selected = { other }; module = 'map'
    end
    if mode == 'selection_user_during_yield' and seconds == 0.05 then
      sources = { folder2 }; target = other; selected = { other }; module = 'map'
    end
  end } end
  return {}
end
local Commands = assert(loadfile(plugin .. '/Commands.lua'))()
local Json = require 'Json'
local function reset(case)
  sources = { folder, folder2 }; target = original; selected = { original, other }; module = 'develop'
  active = catalog; mode = case; writes = 0; creations = 0; targetReads = 0; restoreSelectionAttempts = 0; sourceWrites = 0; moduleWrites = 0
end
local function run(case)
  reset(case)
  return Commands.create_virtual_copy { photo_uuid = 'source' }
end
local function contains(report, code)
  for _, warning in ipairs(report.warnings) do if warning.code == code then return true end end
  return false
end
reset('single_current'); target = source; selected = { source }
local result = Commands.create_virtual_copy { photo_uuid = 'source' }
assert(result.ui_context.status == 'restored' and target == source and writes == 1
  and sourceWrites == 0 and moduleWrites == 0 and creations == 1,
  'current single photo skips all preparation setters and restores only SDK selection')
result = run('cross_source')
assert(result.ui_context.status == 'restored' and sourceWrites == 2 and moduleWrites == 0
  and sources[1] == folder and target == original and #selected == 2 and creations == 1,
  'cross-source requests temporarily use All Photographs then restore the original source and selection')
result = run('normal')
assert(sourceWrites == 0 and moduleWrites == 0 and writes == 2,
  'same-source different photo/multiple selection never changes sources or module')
assert(result.copy.uuid == 'copy' and creations == 1)
assert(result.ui_context.status == 'restored')
assert(Json.encode(result.ui_context):match('"warnings":%[%]'), 'empty warnings must be a JSON array on the Python boundary')
assert(sources[1] == folder and sources[2] == folder2 and target == original and #selected == 2 and module == 'develop')
assert(not Json.encode(result):match('never%-on%-wire'), 'SDK objects/paths must not leave the command')
result = run('source_clears_selection')
assert(result.ui_context.status == 'restored' and target == original and #selected == 2 and module == 'develop',
  'source setter clearing selection is owned by the plugin, and must not block selection restoration')
result = run('source_changes_selection')
assert(result.ui_context.status == 'restored' and target == original and #selected == 2,
  'source setter changing selection is owned by the plugin')
result = run('selection_delayed')
assert(result.ui_context.status == 'restored' and target == original and #selected == 2 and restoreSelectionAttempts == 3,
  'retry a silently ignored selection setter after a yield, without repeating copy creation')
assert(creations == 1)
result = run('selection_never')
assert(result.ui_context.status == 'partial' and result.ui_context.parts.selection.status == 'not_restored'
  and restoreSelectionAttempts == 10 and creations == 1, 'selection retries are bounded, and never repeat creation')
result = run('selection_user_during_yield')
assert(result.ui_context.status == 'unavailable' and contains(result.ui_context, 'user_interference')
  and target == other and module == 'map' and restoreSelectionAttempts == 1 and creations == 1,
  'a user change during the retry yield stops every later UI setter')
result = run('restore_catalog_changes')
assert(result.copy.uuid == 'copy' and result.source.uuid == 'source' and creations == 1,
  'verified copy metadata survives Catalog change and invalid SDK photo objects during restoration')
assert(contains(result.ui_context, 'catalog_changed'))
reset('prepare_catalog_changes')
local ok, failure = pcall(Commands.create_virtual_copy, { photo_uuid = 'source' })
assert(not ok and tostring(failure):match('phase=select_source') and creations == 0 and writes == 1,
  'Catalog drift during preparation must stop before changing the new Catalog module/selection')
reset('prepare_user_changes')
ok, failure = pcall(Commands.create_virtual_copy, { photo_uuid = 'source' })
assert(not ok and creations == 0 and writes == 1 and module == 'map' and target == other,
  'user changes during preparation must not be overwritten or cause a copy write')
result = run('user_during_restore_readback')
assert(result.copy.uuid == 'copy' and creations == 1 and writes == 3 and target == other and module == 'map',
  'user changes during restoration must stop later selection/module setters')
assert(contains(result.ui_context, 'user_interference'))

reset('none'); target = nil; selected = {}
result = Commands.create_virtual_copy { photo_uuid = 'source' }
assert(result.ui_context.status == 'restored' and target == nil and #selected == 0)
-- getTargetPhotos may be used after selecting source/copy, never while the
-- original selection is empty (it would capture the entire filmstrip).
local savedGetTargets = catalog.getTargetPhotos
catalog.getTargetPhotos = function(self) assert(target ~= nil, 'never read all-filmstrip as empty selection'); return savedGetTargets(self) end
reset('none'); target = nil; selected = {}
result = Commands.create_virtual_copy { photo_uuid = 'source' }
assert(result.ui_context.status == 'restored' and target == nil)
catalog.getTargetPhotos = savedGetTargets

for _, case in ipairs({ 'restore_sources_false', 'restore_sources_throws', 'restore_sources_mismatch' }) do
  result = run(case)
  assert(result.copy.uuid == 'copy' and creations == 1, case .. ': never repeat creation')
  assert(result.ui_context.status == 'partial', case .. ': preserve partial restoration')
  assert(result.ui_context.parts.sources.status == 'not_restored' and module == 'develop')
  assert(not Json.encode(result.ui_context):match('/private/'), 'SDK exception paths must be redacted')
end
for _, case in ipairs({ 'catalog_changed', 'user_changed' }) do
  result = run(case)
  assert(result.copy.uuid == 'copy' and creations == 1 and result.ui_context.status == 'unavailable')
  assert(contains(result.ui_context, case == 'catalog_changed' and 'catalog_changed' or 'user_interference'))
  assert(writes == 1, 'do not overwrite a new Catalog or user UI after creating the copy')
end
for _, case in ipairs({ 'prepare_throws', 'create_throws', 'copy_info_throws' }) do
  reset(case)
  local ok, failure = pcall(Commands.create_virtual_copy, { photo_uuid = 'source' })
  assert(not ok and tostring(failure):match('phase='), case .. ': retain original phase')
  assert(sources[1] == folder and target == original and #selected == 2 and module == 'develop', case .. ': restore on original failure')
  assert(creations == (case == 'prepare_throws' and 0 or 1), case .. ': no replay')
end

local savedGetSources = catalog.getActiveSources
catalog.getActiveSources = nil
reset('missing_sources'); target = source; selected = { source }
result = Commands.create_virtual_copy { photo_uuid = 'source' }
assert(result.copy.uuid == 'copy' and creations == 1 and result.ui_context.status == 'partial')
assert(result.ui_context.parts.sources.status == 'unavailable' and contains(result.ui_context, 'snapshot_unavailable'))
catalog.getActiveSources = savedGetSources
reset('selection_delayed')
result = Commands.select_photo { photo_uuid = 'copy', preserve_ui_context = true }
assert(result.photo.uuid == 'copy' and result.ui_context.status == 'restored' and creations == 0,
  'UI-only acceptance uses the same restoration path without creating another copy')
assert(target == original and #selected == 2 and module == 'develop' and restoreSelectionAttempts == 3)
reset('normal')
result = Commands.select_photo { photo_uuid = 'source' }
assert(result.photo.uuid == 'source' and result.ui_context == nil and target == source and module == 'develop' and sourceWrites == 0,
  'explicit selection keeps the current module and source when possible')
reset('normal')
local valid = pcall(Commands.select_photo, { photo_uuid = 'source', preserve_ui_context = 'true' })
assert(not valid and writes == 0 and creations == 0, 'invalid UI-only option fails before UI changes')
reset('filtered_out')
ok, failure = pcall(Commands.create_virtual_copy, { photo_uuid = 'source' })
assert(not ok and creations == 0 and sourceWrites == 0 and moduleWrites == 0,
  'hidden same-source photo fails without clearing filters or switching source')
local savedGetPhotos = folder.getPhotos
folder.getPhotos = nil
reset('unknown_source')
ok, failure = pcall(Commands.create_virtual_copy, { photo_uuid = 'source' })
assert(not ok and creations == 0 and writes == 0, 'unknown membership fails without UI writes')
folder.getPhotos = savedGetPhotos
print('UI-context restoration, failure safety, empty selection, interference and redaction passed')
