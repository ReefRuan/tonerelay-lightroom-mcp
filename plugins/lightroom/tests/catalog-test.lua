-- Current read-only Catalog contract for Catalog.lua and the
-- Commands.search_photos / probe wiring.
-- Run with: luajit plugins/lightroom/tests/catalog-test.lua plugins/lightroom/lr-xmp-mimic.lrdevplugin
-- The fake SDK records every catalog call. Query calls record the read-gate
-- depth they observed, write paths and develop-settings reads raise on use,
-- and the opaque Catalog envelope must pass through without becoming a filter.
local plugin = assert(arg[1], 'plugin directory required')
package.path = plugin .. '/?.lua;' .. package.path

-- ---- tiny test harness ------------------------------------------------------
local function check(cond, msg)
  if not cond then error(msg or 'assertion failed', 2) end
end

local function expectError(fn, needle, label)
  local ok, err = pcall(fn)
  check(not ok, (label or 'call') .. ' did not raise')
  err = tostring(err)
  if needle then
    check(err:find(needle, 1, true), (label or 'call') .. ' raised unexpected error: ' .. err)
  end
end

local tests = 0
local function test(name, fn)
  local ok, err = pcall(fn)
  if not ok then error(name .. ': ' .. tostring(err), 0) end
  tests = tests + 1
  print('ok - ' .. name)
end

local function asciiLower(value)
  return (string.gsub(value, '[A-Z]', function(c)
    return string.char(string.byte(c) + 32)
  end))
end

-- ---- fake Lightroom SDK -----------------------------------------------------
local sandboxCatalogPath =
  '/var/tmp/tonerelay-fixtures/sandbox/sandbox.lrcat'

local state
local function resetState()
  state = {
    readLockDepth = 0,
    readGateCalls = 0,
    queryCalls = 0,
    findCalls = 0,
    getAllCalls = 0,
    queryLog = {},
    metadataReads = 0,
    formattedReads = 0,
    developReads = 0,
    writeCalls = 0,
    selectionChanges = 0,
    pcallCalls = 0,
  }
end
resetState()

local catalog = { path = sandboxCatalogPath, photos = {} }

local function noWrites(name)
  return function()
    state.writeCalls = state.writeCalls + 1
    error('catalog:' .. name .. ' must not be called by read-only search')
  end
end

catalog.withWriteAccessDo = noWrites('withWriteAccessDo')
catalog.addPhoto = noWrites('addPhoto')
catalog.createVirtualCopies = noWrites('createVirtualCopies')
catalog.setSelectedPhotos = function()
  state.selectionChanges = state.selectionChanges + 1
  state.writeCalls = state.writeCalls + 1
  error('catalog:setSelectedPhotos must not be called by read-only search')
end

function catalog:getPath()
  return self.path
end

function catalog:getAllPhotos()
  state.queryCalls = state.queryCalls + 1
  state.getAllCalls = state.getAllCalls + 1
  state.queryLog[#state.queryLog + 1] = { kind = 'getAllPhotos', lock = state.readLockDepth }
  if self.queryError then error(self.queryError) end
  return self.photos
end

-- Emulates the native criteria semantics the plugin builds: filename "any"
-- is a literal case-insensitive substring, rating "==" is exact and drops
-- nil-rated photos.
local function nativeMatch(photo, desc)
  for i = 1, #desc do
    local criterion = desc[i]
    if criterion.criteria == 'filename' then
      if not string.find(asciiLower(photo.file), asciiLower(criterion.value), 1, true) then
        return false
      end
    elseif criterion.criteria == 'rating' then
      if photo.rating ~= criterion.value then return false end
    else
      error('unexpected native criterion: ' .. tostring(criterion.criteria))
    end
  end
  return true
end

function catalog:findPhotos(args)
  state.queryCalls = state.queryCalls + 1
  state.findCalls = state.findCalls + 1
  local desc = args and args.searchDesc
  state.queryLog[#state.queryLog + 1] = { kind = 'findPhotos', lock = state.readLockDepth, desc = desc }
  if self.findError then error(self.findError) end
  if self.findResult ~= nil then return self.findResult end
  if type(desc) ~= 'table' then error('findPhotos requires a searchDesc table') end
  local out = {}
  for _, photo in ipairs(self.photos) do
    if nativeMatch(photo, desc) then out[#out + 1] = photo end
  end
  return out
end

function catalog:withReadAccessDo(fn)
  if self.readError then error(self.readError) end
  state.readGateCalls = state.readGateCalls + 1
  state.readLockDepth = state.readLockDepth + 1
  local ok, err = pcall(fn)
  state.readLockDepth = state.readLockDepth - 1
  if not ok then error(err, 0) end
end

local Photo = {}
Photo.__index = Photo

function Photo:getRawMetadata(key)
  state.metadataReads = state.metadataReads + 1
  if self.flags.raw then error(self.flags.raw) end
  if key == 'uuid' then return self.uuid end
  if key == 'isVideo' then return self.video end
  if key == 'rating' then return self.rating end
  if key == 'isVirtualCopy' then return self.virtual end
  if key == 'dimensions' then return { width = self.width, height = self.height } end
  error('unexpected raw metadata read: ' .. tostring(key))
end

function Photo:getFormattedMetadata(key)
  state.formattedReads = state.formattedReads + 1
  if self.flags.formatted then error(self.flags.formatted) end
  if key == 'fileName' then return self.file end
  if key == 'copyName' then return self.copy end
  error('unexpected formatted metadata read: ' .. tostring(key))
end

function Photo:getDevelopSettings()
  state.developReads = state.developReads + 1
  error('search must not read develop settings')
end

function Photo:applyDevelopPreset()
  state.writeCalls = state.writeCalls + 1
  error('search must not apply develop presets')
end

local function photo(opts)
  return setmetatable({
    uuid = opts.uuid,
    file = opts.file,
    copy = opts.copy or '',
    rating = opts.rating,
    virtual = opts.virtual or false,
    video = opts.video or false,
    width = opts.width or 6000,
    height = opts.height or 4000,
    flags = opts.flags or {},
  }, Photo)
end

-- Raw order is deliberately hostile: the video leads and the photos that
-- match the narrow filters sit at the back, so a page-then-filter bug cannot
-- produce the expected page.
local function defaultPhotos()
  return {
    photo{ uuid = 'v1', file = 'clip.mp4', video = true, rating = 5 },
    photo{ uuid = 'u3', file = 'A.jpg' },
    photo{ uuid = 'u2', file = 'B.jpg', rating = 0 },
    photo{ uuid = 'u6', file = 'photo 中文.jpg' },
    photo{ uuid = 'u0', file = 'b.jpg', rating = 5 },
    photo{ uuid = 'u1', file = 'a.jpg', rating = 2, virtual = true, copy = 'candidate' },
    photo{ uuid = 'u4', file = 'IMG_100.RAW', rating = 3 },
    photo{ uuid = 'u5', file = 'noext', rating = 4 },
  }
end

local function usePhotos(photos)
  resetState()
  catalog.path = sandboxCatalogPath
  catalog.photos = photos
  catalog.queryError = nil
  catalog.findError = nil
  catalog.findResult = nil
  catalog.readError = nil
end

local function uuidsOf(result)
  local out = {}
  for i, item in ipairs(result.photos) do out[i] = item.uuid end
  return table.concat(out, ',')
end

local function checkOutsideGate(label)
  local queries = state.queryLog
  check(#queries > 0, label .. ': no query happened')
  for _, q in ipairs(queries) do
    check(q.lock == 0, label .. ': query ran while holding the read gate')
  end
end

_G.import = function(name)
  if name == 'LrApplication' then
    return {
      activeCatalog = function() return catalog end,
      versionString = function() return '15.4.1' end,
      addDevelopPresetForPlugin = function() return {} end,
    }
  elseif name == 'LrExportSession' then
    return function() return { doExportOnCurrentTask = function() end } end
  elseif name == 'LrFileUtils' then
    return { exists = function() return false end, createAllDirectories = function() end,
      delete = function() end }
  elseif name == 'LrPathUtils' then
    return { child = function(dir, name) return dir .. '/' .. name end }
  elseif name == 'LrTasks' then
    return {
      pcall = function(fn, ...)
        state.pcallCalls = state.pcallCalls + 1
        return pcall(fn, ...)
      end,
    }
  elseif name == 'LrApplicationView' then
    return {}
  elseif name == 'LrDevelopController' then
    return {}
  end
  error('unexpected import ' .. name)
end
_G._PLUGIN = {}

local Catalog = dofile(plugin .. '/Catalog.lua')
local Safety = require 'Safety'

-- ---- shape and paging -------------------------------------------------------
test('no-filter search enumerates, sorts ASCII-lowercase and reports totals', function()
  usePhotos(defaultPhotos())
  local result = Catalog.search()
  check(result.count == 7 and result.total == 7, 'counts')
  check(result.limit == 100 and result.offset == 0 and result.has_more == false, 'window defaults')
  check(uuidsOf(result) == 'u1,u3,u0,u2,u4,u5,u6', 'sort order: ' .. uuidsOf(result))
  check(state.getAllCalls == 1 and state.findCalls == 0, 'no-filter path uses getAllPhotos')
  checkOutsideGate('no-filter query')
  local allowed = { uuid = true, file_name = true, copy_name = true, is_virtual_copy = true,
    width = true, height = true, rating = true }
  for _, item in ipairs(result.photos) do
    local fields = 0
    for key in pairs(item) do
      fields = fields + 1
      check(allowed[key], 'unexpected photo field ' .. tostring(key))
    end
    check(fields == 7, 'photo record must have exactly seven fields')
    check(item.path == nil, 'photo record must not expose a path')
  end
  local byUuid = {}
  for _, item in ipairs(result.photos) do byUuid[item.uuid] = item end
  check(byUuid.u3.rating == 0 and byUuid.u6.rating == 0, 'nil ratings normalize to 0')
  check(byUuid.u1.is_virtual_copy == true and byUuid.u1.copy_name == 'candidate', 'virtual copy fields')
  check(byUuid.u3.is_virtual_copy == false and byUuid.u3.copy_name == '', 'master fields')
  check(byUuid.u0.width == 6000 and byUuid.u0.height == 4000, 'dimensions')
  check(state.developReads == 0 and state.writeCalls == 0, 'read-only contract')
  check(state.readGateCalls >= 1, 'metadata scan must use the read gate')
  check(state.pcallCalls >= 2, 'SDK calls must go through LrTasks.pcall')
end)

test('name queries use the native candidate query outside the read lock', function()
  usePhotos(defaultPhotos())
  local result = Catalog.search({ name_contains = 'a.JPG' })
  check(result.total == 2 and uuidsOf(result) == 'u1,u3', 'case-insensitive name match')
  check(state.findCalls == 1 and state.getAllCalls == 0, 'name filter uses findPhotos')
  checkOutsideGate('name query')
  local desc = state.queryLog[1].desc
  check(desc.combine == 'intersect' and #desc == 1, 'search desc shape')
  check(desc[1].criteria == 'filename' and desc[1].operation == 'any' and desc[1].value == 'a.JPG',
    'filename criterion is literal and verbatim')
end)

test('literal substring matching ignores case but not patterns', function()
  usePhotos({
    photo{ uuid = 'p1', file = 'A%B.jpg', rating = 5 },
    photo{ uuid = 'p2', file = 'aXb.jpg', rating = 5 },
    photo{ uuid = 'p3', file = '100%_final.jpg' },
  })
  check(uuidsOf(Catalog.search({ name_contains = 'a%b' })) == 'p1', 'percent is literal')
  check(uuidsOf(Catalog.search({ name_contains = '100%' })) == 'p3', 'percent in query is literal')
  check(uuidsOf(Catalog.search({ name_contains = 'a_b' })) == '', 'underscore is literal, not a class')
  check(Catalog.search({ name_contains = 'a%b' }).total == 1, 'total follows the literal filter')
end)

test('rating filters are exact and rating 0 keeps nil-rated photos', function()
  usePhotos(defaultPhotos())
  local three = Catalog.search({ rating = 3 })
  check(three.total == 1 and uuidsOf(three) == 'u4', 'rating 3')
  check(state.findCalls == 1, 'non-zero rating uses findPhotos')
  checkOutsideGate('rating query')
  local desc = state.queryLog[1].desc
  check(#desc == 1 and desc[1].criteria == 'rating' and desc[1].operation == '=='
    and desc[1].value == 3, 'rating criterion')

  usePhotos(defaultPhotos())
  local five = Catalog.search({ rating = 5 })
  check(five.total == 1 and uuidsOf(five) == 'u0', 'video with rating 5 stays excluded')

  usePhotos(defaultPhotos())
  local zero = Catalog.search({ rating = 0 })
  check(zero.total == 3 and uuidsOf(zero) == 'u3,u2,u6', 'rating 0 includes nil: ' .. uuidsOf(zero))
  check(state.getAllCalls == 1 and state.findCalls == 0,
    'rating 0 must not use the native rating criterion (it drops nil)')

  usePhotos(defaultPhotos())
  local none = Catalog.search({ rating = 1 })
  check(none.total == 0 and none.count == 0 and #none.photos == 0 and none.has_more == false,
    'empty result')
  check(state.findCalls == 1, 'non-zero rating still quizzes the native query')
end)

test('combined name and rating filters keep the nil-rated contract', function()
  usePhotos(defaultPhotos())
  local exact = Catalog.search({ name_contains = 'a.jpg', rating = 2 })
  check(exact.total == 1 and uuidsOf(exact) == 'u1', 'name plus non-zero rating')
  local desc = state.queryLog[1].desc
  check(#desc == 2 and desc[1].criteria == 'filename' and desc[2].criteria == 'rating'
    and desc[2].value == 2, 'both native criteria')

  usePhotos(defaultPhotos())
  local namedZero = Catalog.search({ name_contains = 'b.jpg', rating = 0 })
  check(namedZero.total == 1 and uuidsOf(namedZero) == 'u2', 'unrated 0 survives a name query')
  local zeroDesc = state.queryLog[1].desc
  check(state.findCalls == 1 and #zeroDesc == 1 and zeroDesc[1].criteria == 'filename',
    'rating 0 must not add a native rating criterion that drops nil')
end)

test('filtering happens before pagination', function()
  usePhotos(defaultPhotos())
  local one = Catalog.search({ rating = 3, limit = 1 })
  check(one.total == 1 and uuidsOf(one) == 'u4', 'small limit reaches a match at the back')
  local firstPage = Catalog.search({ name_contains = 'a.jpg', limit = 1 })
  check(firstPage.total == 2 and uuidsOf(firstPage) == 'u1' and firstPage.has_more == true,
    'page 1 of a filtered set')
  local secondPage = Catalog.search({ name_contains = 'a.jpg', limit = 1, offset = 1 })
  check(secondPage.total == 2 and uuidsOf(secondPage) == 'u3' and secondPage.has_more == false,
    'page 2 of a filtered set')
  local empty = Catalog.search({ name_contains = 'a.jpg', limit = 10, offset = 2 })
  check(empty.total == 2 and empty.count == 0 and empty.has_more == false, 'past-the-end page')
end)

test('same-name pagination is stable across calls', function()
  usePhotos({
    photo{ uuid = 's3', file = 'Same.jpg', rating = 5 },
    photo{ uuid = 's1', file = 'same.JPG', rating = 5 },
    photo{ uuid = 's2', file = 'SAME.jpg', rating = 5 },
    photo{ uuid = 't1', file = 't.jpg' },
  })
  local pages = {}
  for offset = 0, 3 do
    pages[#pages + 1] = uuidsOf(Catalog.search({ limit = 1, offset = offset }))
  end
  check(table.concat(pages, '|') == 's1|s2|s3|t1', 'uuid tie-break order: ' .. table.concat(pages, '|'))
  local empty = Catalog.search({ limit = 1, offset = 4 })
  check(empty.count == 0 and empty.total == 4 and empty.has_more == false, 'empty tail page')
  local first = Catalog.search()
  local second = Catalog.search()
  check(uuidsOf(first) == uuidsOf(second), 'two scans agree on the order')
  check(first.limit == 100 and first.offset == 0, 'echoed window')
end)

test('offset past the end keeps the total and has_more contract', function()
  usePhotos(defaultPhotos())
  local result = Catalog.search({ limit = 2, offset = 100 })
  check(result.count == 0 and result.total == 7 and #result.photos == 0, 'empty page')
  check(result.limit == 2 and result.offset == 100 and result.has_more == false, 'window echo')
  local all = Catalog.search({ limit = 2000, offset = 0 })
  check(all.count == 7 and all.total == 7 and all.limit == 2000, 'maximum limit is accepted')
end)

-- ---- validation -------------------------------------------------------------
test('defaults, envelope and unknown fields follow the bridge contract', function()
  usePhotos(defaultPhotos())
  local viaNil = Catalog.search(nil)
  check(viaNil.limit == 100 and viaNil.offset == 0 and viaNil.count == 7, 'nil params use defaults')
  local viaEnvelope = Catalog.search({
    expected_catalog_identity = Safety.catalogIdentity(sandboxCatalogPath),
    expected_access_mode = 'full',
    limit = 1,
  })
  check(viaEnvelope.total == 7 and viaEnvelope.count == 1,
    'identity and mode envelopes are accepted as transport fields')
  usePhotos(defaultPhotos())
  expectError(function() Catalog.search({ expected_catalog_path = '/tmp/old.lrcat' }) end,
    'unknown field')
  expectError(function() Catalog.search({ expected_catalog_id = 'old.lrcat' }) end,
    'unknown field')
  expectError(function() Catalog.search({ expected_catalog_identity = 42 }) end, 'expected_catalog_identity')
  expectError(function() Catalog.search({ expected_access_mode = 'other' }) end, 'expected_access_mode')
  expectError(function() Catalog.search(false) end, 'search arguments must be an object')
  expectError(function() Catalog.search(42) end, 'search arguments must be an object')
  expectError(function() Catalog.search('x') end, 'search arguments must be an object')
  expectError(function() Catalog.search({ bogus = true }) end, 'unknown field')
  expectError(function() Catalog.search({ query = 'a' }) end, 'unknown field')
  expectError(function() Catalog.search({ photo_uuid = 'u1' }) end, 'unknown field')
  check(state.queryCalls == 0 and state.metadataReads == 0 and state.formattedReads == 0,
    'validation must precede every SDK read')
end)

test('invalid types, NaN, inf and out-of-range values are rejected before the SDK', function()
  local cases = {
    { { limit = 0 }, 'limit must be an integer' },
    { { limit = 2001 }, 'limit must be an integer' },
    { { limit = 1.5 }, 'limit must be an integer' },
    { { limit = true }, 'limit must be an integer' },
    { { limit = '10' }, 'limit must be an integer' },
    { { limit = 0 / 0 }, 'limit must be an integer' },
    { { limit = math.huge }, 'limit must be an integer' },
    { { limit = -math.huge }, 'limit must be an integer' },
    { { offset = -1 }, 'offset must be a non-negative integer' },
    { { offset = 0.5 }, 'offset must be a non-negative integer' },
    { { offset = 0 / 0 }, 'offset must be a non-negative integer' },
    { { offset = math.huge }, 'offset must be a non-negative integer' },
    { { offset = false }, 'offset must be a non-negative integer' },
    { { offset = '0' }, 'offset must be a non-negative integer' },
    { { name_contains = 3 }, 'name_contains must be a string' },
    { { name_contains = true }, 'name_contains must be a string' },
    { { name_contains = {} }, 'name_contains must be a string' },
    { { rating = -1 }, 'rating must be an integer' },
    { { rating = 6 }, 'rating must be an integer' },
    { { rating = 0.5 }, 'rating must be an integer' },
    { { rating = 0 / 0 }, 'rating must be an integer' },
    { { rating = math.huge }, 'rating must be an integer' },
    { { rating = true }, 'rating must be an integer' },
    { { rating = '3' }, 'rating must be an integer' },
  }
  for _, case in ipairs(cases) do
    usePhotos(defaultPhotos())
    expectError(function() Catalog.search(case[1]) end, case[2], 'case ' .. tostring(case[1].limit or case[1].offset or case[1].name_contains or case[1].rating))
    check(state.queryCalls == 0 and state.metadataReads == 0,
      'rejected input reached the SDK: ' .. case[2])
  end

  usePhotos(defaultPhotos())
  check(Catalog.search({ limit = 1 }).limit == 1, 'limit 1 accepted')
  check(Catalog.search({ limit = 2000 }).limit == 2000, 'limit 2000 accepted')
  check(Catalog.search({ offset = 0 }).offset == 0, 'offset 0 accepted')
  check(Catalog.search({ rating = 0 }).total == 3, 'rating 0 accepted')
  check(Catalog.search({ rating = 5 }).total == 1, 'rating 5 accepted')
  check(Catalog.search({ name_contains = '' }).total == 7, 'empty name is no filter')
end)

-- ---- failures ---------------------------------------------------------------
test('SDK failures surface with the failing call named', function()
  usePhotos(defaultPhotos())
  catalog.queryError = 'getAll boom'
  expectError(function() Catalog.search() end, 'getAllPhotos failed', 'enumeration failure')

  usePhotos(defaultPhotos())
  catalog.findError = 'find boom'
  expectError(function() Catalog.search({ name_contains = 'a' }) end, 'findPhotos failed', 'query failure')

  usePhotos(defaultPhotos())
  catalog.findResult = 'not a table'
  expectError(function() Catalog.search({ name_contains = 'a' }) end, 'findPhotos failed', 'bad query result')

  usePhotos(defaultPhotos())
  catalog.photos = 'not a table'
  expectError(function() Catalog.search() end, 'getAllPhotos failed', 'bad enumeration result')

  usePhotos(defaultPhotos())
  catalog.readError = 'gate boom'
  expectError(function() Catalog.search() end, 'metadata read failed', 'read gate failure')

  usePhotos({ photo{ uuid = 'e1', file = 'e.jpg', flags = { formatted = 'metadata boom' } } })
  expectError(function() Catalog.search({ name_contains = 'e' }) end, 'metadata boom', 'metadata failure detail')
  expectError(function() Catalog.search() end, 'metadata boom', 'metadata failure on the enumeration path')
end)

test('search never calls write paths or reads develop settings', function()
  usePhotos(defaultPhotos())
  Catalog.search()
  Catalog.search({ name_contains = 'a', rating = 2 })
  Catalog.search({ rating = 0 })
  Catalog.search({ name_contains = 'zzz' })
  check(state.writeCalls == 0, 'write calls: ' .. state.writeCalls)
  check(state.developReads == 0, 'develop reads: ' .. state.developReads)
  check(state.selectionChanges == 0, 'selection changes: ' .. state.selectionChanges)
  local exported = {}
  for key in pairs(Catalog) do exported[#exported + 1] = key end
  table.sort(exported)
  check(table.concat(exported, ',') == 'search', 'module must expose only search')
end)

-- ---- JSON boundary ----------------------------------------------------------
test('Json roundtrip preserves the search payload', function()
  local Json = dofile(plugin .. '/Json.lua')
  usePhotos(defaultPhotos())
  local result = Catalog.search({ limit = 3 })
  local back = Json.decode(Json.encode(result))
  check(back.count == 3 and back.total == 7 and back.limit == 3 and back.offset == 0, 'scalars')
  check(#back.photos == 3 and back.photos[1].uuid == 'u1' and back.photos[1].rating == 2
    and back.photos[2].uuid == 'u3' and back.photos[2].rating == 0,
    'photo records survive encode/decode')
  local empty = Catalog.search({ offset = 100 })
  check(Json.decode(Json.encode(empty)).total == 7, 'empty page still carries the total')
end)

-- ---- Commands wiring --------------------------------------------------------
package.loaded.Catalog = Catalog
local Commands = dofile(plugin .. '/Commands.lua')

test('search_photos delegates catalog authorization to the bridge', function()
  usePhotos(defaultPhotos())
  local result = Commands.search_photos({ limit = 2 })
  check(result.count == 2 and result.total == 7 and uuidsOf(result) == 'u1,u3',
    'wired result: ' .. uuidsOf(result))
  check(state.getAllCalls == 1, 'Commands path reaches the real Catalog module')

  usePhotos(defaultPhotos())
  local enveloped = Commands.search_photos({
    expected_catalog_identity = Safety.catalogIdentity(sandboxCatalogPath),
    expected_access_mode = 'full', limit = 1,
  })
  check(enveloped.count == 1 and enveloped.total == 7, 'bridge envelope passes through')

  usePhotos(defaultPhotos())
  catalog.path = '/tmp/not-a-sandbox/other.lrcat'
  local outside = Commands.search_photos({ limit = 1 })
  check(outside.count == 1 and state.queryCalls == 1,
    'command core is mode-agnostic; Bridge.lua owns the single authorization step')

  usePhotos(defaultPhotos())
  expectError(function() Commands.search_photos({ expected_catalog_path = '/tmp/old.lrcat' }) end,
    'unknown field', 'retired path envelope through Commands')
  check(state.queryCalls == 0, 'malformed envelope must not query')
end)

test('probe advertises Catalog search and keeps every previous capability', function()
  usePhotos(defaultPhotos())
  local probe = Commands.probe_client({})
  check(probe.build_id == 'bridge-generation-20261006-1',
    'build_id: ' .. tostring(probe.build_id))
  check(probe.capabilities.catalog_search == true, 'catalog_search capability')
  check(probe.capabilities.expected_catalog_path == nil, 'path binding capability is retired')
  check(probe.capabilities.expected_catalog_identity == true, 'expected_catalog_identity capability')
  check(probe.capabilities.expected_access_mode == true, 'expected_access_mode capability')
  check(probe.capabilities.catalog_identity == true, 'catalog_identity capability')
  check(probe.capabilities.develop_snapshot == true, 'develop_snapshot capability')
  check(probe.capabilities.develop_presets_read == true, 'develop_presets_read capability')
  check(probe.capabilities.hsl == true and probe.capabilities.white_balance == true
    and probe.capabilities.curves == true, 'develop capabilities preserved')
  check(probe.capabilities.preview_fast_request_jpeg_thumbnail == true,
    'fast thumbnail capability')
  check(probe.capabilities.preview_fast_thumbnail_unavailable_code == true,
    'coded fast-thumbnail unavailable capability')
  check(probe.catalog_path == nil and probe.catalog_id == 'sandbox.lrcat'
    and probe.catalog_identity == Safety.catalogIdentity(sandboxCatalogPath)
    and probe.access_mode == 'full' and probe.sandbox == false, 'catalog and access probe fields')
  check(probe.plugin_version == '0.4.0' and probe.lr_version == '15.4.1', 'version fields preserved')
end)

test('list_photos keeps its original ungated read-only shape', function()
  usePhotos(defaultPhotos())
  catalog.path = '/tmp/outside-sandbox/other.lrcat'
  local listed = Commands.list_photos({ limit = 2 })
  check(listed.count == 2 and listed.photos[1].uuid == 'u3' and listed.photos[2].uuid == 'u2',
    'first non-video photos in raw order')
  check(listed.photos[1].rating == nil and listed.photos[1].develop == nil, 'no new fields')
  check(state.writeCalls == 0 and state.developReads == 0, 'still read-only')
end)

print('catalog-test: ok (' .. tests .. ' tests)')
