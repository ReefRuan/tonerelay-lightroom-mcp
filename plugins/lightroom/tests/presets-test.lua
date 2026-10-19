-- Bounded read-only preset extraction seam.
-- Run with: luajit plugins/lightroom/tests/presets-test.lua plugins/lightroom/lr-xmp-mimic.lrdevplugin
-- The fake SDK below never exposes a write path; the module is expected to
-- call only developPresetFolders, preset metadata, getSetting and the
-- plugin-preset reader.
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

local function deepEqual(a, b)
  if type(a) ~= type(b) then return false end
  if type(a) ~= 'table' then return a == b end
  for k, v in pairs(a) do if not deepEqual(v, b[k]) then return false end end
  for k in pairs(b) do if a[k] == nil then return false end end
  return true
end

local tests = 0
local function test(name, fn)
  local ok, err = pcall(fn)
  if not ok then error(name .. ': ' .. tostring(err), 0) end
  tests = tests + 1
  print('ok - ' .. name)
end

-- ---- fake Lightroom SDK -----------------------------------------------------
local sdk = { folders = {}, pluginPresets = nil, foldersError = nil, pluginError = nil }
local state = {}

local function makePreset(name, uuid, settings, flags)
  local p = { name = name, uuid = uuid, settings = settings, flags = flags or {} }
  function p:getName()
    if self.flags.name then error(self.flags.name) end
    return self.name
  end
  function p:getUuid()
    if self.flags.uuid then error(self.flags.uuid) end
    return self.uuid
  end
  function p:getSetting()
    state.settingReads = state.settingReads + 1
    if self.flags.setting then error(self.flags.setting) end
    return self.settings
  end
  return p
end

local function makeFolder(name, presets, flags)
  local f = { name = name, presets = presets, flags = flags or {} }
  function f:getName()
    if self.flags.name then error(self.flags.name) end
    return self.name
  end
  function f:getDevelopPresets()
    state.presetScans = state.presetScans + 1
    if self.flags.presets then error(self.flags.presets) end
    return self.presets
  end
  return f
end

-- Reset all counters and swap the library contents for one test.
local function useFolders(folders, pluginPresets, opts)
  opts = opts or {}
  sdk.folders = folders
  sdk.pluginPresets = pluginPresets
  sdk.foldersError = opts.foldersError
  sdk.pluginError = opts.pluginError
  state = { folderScans = 0, presetScans = 0, pluginScans = 0, settingReads = 0 }
end

_G.import = function(name)
  if name == 'LrApplication' then
    return {
      developPresetFolders = function()
        state.folderScans = state.folderScans + 1
        if sdk.foldersError then error(sdk.foldersError) end
        return sdk.folders
      end,
      getDevelopPresetsForPlugin = function()
        state.pluginScans = state.pluginScans + 1
        if sdk.pluginError then error(sdk.pluginError) end
        return sdk.pluginPresets
      end,
    }
  elseif name == 'LrTasks' then
    return { pcall = pcall }
  end
  error('unexpected import ' .. name)
end
_G._PLUGIN = {}

local Presets = dofile(plugin .. '/Presets.lua')
local Json = dofile(plugin .. '/Json.lua')

-- ---- fixtures ---------------------------------------------------------------
local function curve(x, y)
  return { { x = x, y = y }, { x = 255, y = 255 } }
end

local function defaultLibrary()
  local alpha = makePreset('Alpha', 'u1',
    { Exposure2012 = 1, Curve = curve(0, 1), CorrectionID = 'a', MaskID = 'm' })
  local dupA = makePreset('Dup', 'u10', { Exposure2012 = 10 })
  local zeta = makePreset('Zeta', 'u3', { Exposure2012 = 3 })
  local beta = makePreset('Beta', 'u5', { Exposure2012 = 5 })
  local dupZ = makePreset('Dup', 'u11', { Exposure2012 = 11 })
  local gamma = makePreset('Gamma', 'u9', { Exposure2012 = 9 })
  useFolders(
    { makeFolder('Zulu', { beta, dupZ }), makeFolder('Alpha', { dupA, alpha, zeta }) },
    { gamma })
  return { alpha = alpha, gamma = gamma, beta = beta }
end

local function uuidsOf(result)
  local out = {}
  for i, preset in ipairs(result.presets) do out[i] = preset.uuid end
  return table.concat(out, ',')
end

-- ---- list -------------------------------------------------------------------
test('list returns sorted metadata and never reads settings', function()
  defaultLibrary()
  local result = Presets.list()
  check(result.count == 6, 'count')
  check(result.offset == 0 and result.limit == 100, 'defaults')
  check(uuidsOf(result) == 'u1,u10,u3,u5,u11,u9', 'sort order: ' .. uuidsOf(result))
  check(state.settingReads == 0, 'list must not call getSetting')
  local allowed = { name = true, folder = true, scope = true, uuid = true }
  for _, preset in ipairs(result.presets) do
    for key in pairs(preset) do check(allowed[key], 'unexpected list field ' .. tostring(key)) end
    check(preset.settings == nil, 'list must not return settings')
  end
  check(result.presets[1].scope == 'lightroom' and result.presets[6].scope == 'plugin', 'scope')
  check(result.presets[6].folder == 'Plugin Develop Presets', 'plugin folder')
end)

test('list paginates across scope boundaries without rescanning', function()
  defaultLibrary()
  check(uuidsOf(Presets.list({ limit = 2 })) == 'u1,u10', 'page 1')
  check(uuidsOf(Presets.list({ limit = 2, offset = 2 })) == 'u3,u5', 'page 2')
  check(uuidsOf(Presets.list({ limit = 2, offset = 4 })) == 'u11,u9', 'page 3')
  local tail = Presets.list({ limit = 100, offset = 5 })
  check(tail.count == 6 and uuidsOf(tail) == 'u9', 'tail page keeps total count')
  check(tail.offset == 5 and tail.limit == 100, 'echoed window')
  local empty = Presets.list({ limit = 10, offset = 6 })
  check(empty.count == 6 and #empty.presets == 0, 'past-the-end page')
  check(state.settingReads == 0, 'pagination must not read settings')
end)

test('list filters by query across every metadata field', function()
  defaultLibrary()
  check(uuidsOf(Presets.list({ query = 'gamma' })) == 'u9', 'name query')
  check(uuidsOf(Presets.list({ query = 'ZULU' })) == 'u5,u11', 'folder query is case-insensitive')
  check(uuidsOf(Presets.list({ query = 'plugin' })) == 'u9', 'scope query')
  check(uuidsOf(Presets.list({ query = 'u10' })) == 'u10', 'uuid query')
  check(uuidsOf(Presets.list({ query = 'dup' })) == 'u10,u11', 'duplicate names')
  check(uuidsOf(Presets.list({ query = 'alpha' })) == 'u1,u10,u3', 'folder and name matches')
  local none = Presets.list({ query = '__no_such_preset__' })
  check(none.count == 0 and #none.presets == 0, 'no match')
  check(state.settingReads == 0, 'queries must not read settings')
end)

test('list rejects malformed inputs before touching the SDK', function()
  defaultLibrary()
  expectError(function() Presets.list(false) end, 'list arguments must be an object')
  expectError(function() Presets.list(42) end, 'list arguments must be an object')
  expectError(function() Presets.list({ bogus = true }) end, 'unknown field')
  expectError(function() Presets.list({ query = 3 }) end, 'query must be a string')
  expectError(function() Presets.list({ limit = 0 }) end, 'limit must be an integer')
  expectError(function() Presets.list({ limit = 101 }) end, 'limit must be an integer')
  expectError(function() Presets.list({ limit = 1.5 }) end, 'limit must be an integer')
  expectError(function() Presets.list({ limit = true }) end, 'limit must be an integer')
  expectError(function() Presets.list({ limit = '10' }) end, 'limit must be an integer')
  expectError(function() Presets.list({ limit = math.huge }) end, 'limit must be an integer')
  expectError(function() Presets.list({ offset = -1 }) end, 'offset must be a non-negative integer')
  expectError(function() Presets.list({ offset = 0.5 }) end, 'offset must be a non-negative integer')
  expectError(function() Presets.list({ offset = math.huge }) end, 'offset must be a non-negative integer')
  expectError(function() Presets.list({ offset = false }) end, 'offset must be a non-negative integer')
  check(state.folderScans == 0 and state.presetScans == 0 and state.settingReads == 0,
    'validation must happen before any SDK read')
end)

test('list accepts the bridge catalog envelope and rejects malformed ones', function()
  defaultLibrary()
  local result = Presets.list({ expected_catalog_identity = 'catalog-md5-v1:test',
    expected_access_mode = 'full', limit = 3 })
  check(result.count == 6 and #result.presets == 3, 'envelope is ignored, not a filter')
  useFolders({ makeFolder('Empty', {}) }, nil)
  expectError(function() Presets.list({ expected_catalog_path = '/tmp/old.lrcat' }) end,
    'unknown field')
  expectError(function() Presets.list({ expected_catalog_identity = false }) end,
    'expected_catalog_identity')
  expectError(function() Presets.list({ expected_access_mode = 'other' }) end,
    'expected_access_mode')
  check(state.folderScans == 0, 'malformed envelope must not scan')
end)

-- ---- get --------------------------------------------------------------------
test('get resolves selectors and preserves IDs', function()
  local lib = defaultLibrary()
  local one = Presets.get({ preset_uuid = 'u1',
    expected_catalog_identity = 'catalog-md5-v1:test', expected_access_mode = 'full' })
  check(one.name == 'Alpha' and one.uuid == 'u1' and one.folder == 'Alpha' and one.scope == 'lightroom',
    'summary fields')
  check(one.settings.Exposure2012 == 1, 'setting value')
  check(one.settings.CorrectionID == 'a' and one.settings.MaskID == 'm', 'get must preserve ignored IDs')
  check(one.settings.Curve[1].x == 0 and one.settings.Curve[2].y == 255, 'nested array')
  check(one.settings ~= lib.alpha.settings and one.settings.Curve ~= lib.alpha.settings.Curve,
    'settings must be copies, not shared references')
  check(state.settingReads == 1, 'get reads exactly one preset')

  local byName = Presets.get({ preset_name = 'Gamma', preset_scope = 'plugin' })
  check(byName.uuid == 'u9' and byName.scope == 'plugin', 'unique name')

  local narrowed = Presets.get({ preset_name = 'Dup', preset_folder = 'Zulu' })
  check(narrowed.uuid == 'u11', 'folder disambiguates')
  narrowed = Presets.get({ preset_name = 'Dup', preset_folder = 'Alpha' })
  check(narrowed.uuid == 'u10', 'other folder disambiguates')
end)

test('get rejects ambiguous, incomplete or malformed selectors before scanning', function()
  defaultLibrary()
  expectError(function() Presets.get(nil) end, 'selector is required')
  expectError(function() Presets.get('u1') end, 'selector is required')
  expectError(function() Presets.get({}) end, 'preset_name or preset_uuid is required')
  expectError(function() Presets.get({ preset_name = '' }) end, 'preset_name must be a non-empty string')
  expectError(function() Presets.get({ preset_name = 3 }) end, 'preset_name must be a non-empty string')
  expectError(function() Presets.get({ preset_uuid = 9 }) end, 'preset_uuid must be a non-empty string')
  expectError(function() Presets.get({ preset_uuid = 'u1', extra = true }) end, 'unknown field')
  expectError(function() Presets.get({ preset_uuid = 'u1', preset_folder = 2 }) end, 'preset_folder')
  expectError(function() Presets.get({ preset_uuid = 'u1', preset_scope = 'all' }) end, 'preset_scope')
  expectError(function() Presets.get({ expected_catalog_path = '/tmp/old.lrcat', preset_uuid = 'u1' }) end,
    'unknown field')
  check(state.folderScans == 0 and state.presetScans == 0, 'selector validation must precede the SDK')
  local ambiguous = defaultLibrary()
  expectError(function() Presets.get({ preset_name = 'Dup' }) end, 'ambiguous')
  expectError(function() Presets.get({ preset_name = 'Dup', preset_scope = 'lightroom' }) end, 'ambiguous')
  expectError(function() Presets.get({ preset_name = 'Dup', preset_scope = 'plugin' }) end, 'Preset not found')
  expectError(function() Presets.get({ preset_uuid = 'nope' }) end, 'Preset not found')
  expectError(function() Presets.get({ preset_name = 'Alpha', preset_folder = 'Zulu' }) end, 'Preset not found')
  check(ambiguous.alpha.settings.CorrectionID == 'a', 'failed lookups must not mutate presets')
end)

test('get rejects unsafe settings without silently dropping data', function()
  local function expectUnsafe(settings, needle)
    useFolders({ makeFolder('F', { makePreset('Unsafe', 'uX', settings) }) }, nil)
    expectError(function() Presets.get({ preset_uuid = 'uX' }) end, needle, 'unsafe settings')
  end
  expectUnsafe(nil, 'settings are unavailable')
  expectUnsafe({ [1] = 1 }, 'top-level keys must be strings')
  expectUnsafe({ Exposure2012 = 1 / 0 }, 'non-finite')
  expectUnsafe({ Exposure2012 = -1 / 0 }, 'non-finite')
  expectUnsafe({ Exposure2012 = 0 / 0 }, 'non-finite')
  expectUnsafe({ Curve = { { x = 0, y = 1 / 0 } } }, 'non-finite')
  expectUnsafe({ fn = function() end }, 'unsupported value')
  expectUnsafe({ co = coroutine.create(function() end) }, 'unsupported value')
  expectUnsafe({ Nested = { [1] = 1, label = 'mixed' } }, 'mix array and object keys')
  expectUnsafe({ Nested = { [1] = 1, [3] = 3 } }, 'contiguous')
  expectUnsafe({ Nested = { [2] = 2 } }, 'contiguous')
  expectUnsafe({ Nested = { [0] = 1 } }, 'positive integers')
  expectUnsafe({ Nested = { [1.5] = 1 } }, 'positive integers')
  expectUnsafe({ Nested = { [function() end] = 1 } }, 'keys must be strings or integers')
  local cyclic = {}
  cyclic.self = { back = cyclic }
  expectUnsafe(cyclic, 'cycle')

  useFolders({ makeFolder('F', {
    makePreset('Unsafe', 'uX', { Exposure2012 = 1 }, { setting = 'sdk boom' }),
  }) }, nil)
  expectError(function() Presets.get({ preset_uuid = 'uX' }) end, 'sdk boom', 'setting failure detail')
end)

test('get allows valid nesting at the documented boundaries', function()
  local function nested(depth)
    local root = {}
    local node = root
    for _ = 1, depth do node.next = {}; node = node.next end
    return root
  end
  useFolders({ makeFolder('F', { makePreset('Deep', 'uD', nested(5)) }) }, nil)
  check(Presets.get({ preset_uuid = 'uD' }).settings.next.next.next.next.next ~= nil, 'depth 5 is allowed')
  useFolders({ makeFolder('F', { makePreset('Deep', 'uD', nested(6)) }) }, nil)
  expectError(function() Presets.get({ preset_uuid = 'uD' }) end, 'maximum depth 6')

  local big = {}
  for i = 1, 2000 do big['k' .. i] = i end
  useFolders({ makeFolder('F', { makePreset('Big', 'uB', big) }) }, nil)
  check(Presets.get({ preset_uuid = 'uB' }).settings.k2000 == 2000, '2000 entries are allowed')
  big.k2001 = 2001
  useFolders({ makeFolder('F', { makePreset('Big', 'uB', big) }) }, nil)
  expectError(function() Presets.get({ preset_uuid = 'uB' }) end, 'too large')

  local shared = { x = 1 }
  useFolders({ makeFolder('F', { makePreset('Dag', 'uG', { a = shared, b = shared, ok = true,
    str = 's', flag = false, neg = -3, frac = 0.5 }) }) }, nil)
  local dag = Presets.get({ preset_uuid = 'uG' }).settings
  check(dag.a.x == 1 and dag.b.x == 1 and dag.flag == false and dag.neg == -3 and dag.frac == 0.5,
    'shared subtrees and scalar types survive')
end)

-- ---- JSON boundary ----------------------------------------------------------
test('Json.encode roundtrip preserves list and get payloads', function()
  defaultLibrary()
  local listing = Presets.list({ limit = 3 })
  local backList = Json.decode(Json.encode(listing))
  check(deepEqual(backList, listing), 'list payload changed across encode/decode')
  check(#backList.presets == 3, 'array length preserved')
  local empty = Presets.list({ query = '__no_such_preset__' })
  check(deepEqual(Json.decode(Json.encode(empty)), empty), 'empty list payload changed')

  local fixture = {
    Exposure2012 = 1.25, Contrast = -0.5, ProcessVersion = '11.0', Flag = true,
    Curve = curve(0, 0), Nested = { Tone = { Shadows = 0.1, Highlights = -0.2 } },
    Empty = {}, Text = 'quote " backslash \\ line\nbreak\ttab',
  }
  useFolders({ makeFolder('F', { makePreset('Round', 'uR', fixture) }) }, nil)
  local got = Presets.get({ preset_uuid = 'uR' })
  local decoded = Json.decode(Json.encode(got))
  check(deepEqual(decoded, got), 'get payload changed across encode/decode')
  check(deepEqual(decoded.settings, fixture), 'settings changed across encode/decode')
  check(#decoded.settings.Curve == 2 and decoded.settings.Curve[1].x == 0, 'curve stays an array')
end)

-- ---- compare ----------------------------------------------------------------
test('compare diffs settings and ignores CorrectionID/MaskID recursively', function()
  local base = makePreset('Base', 'uB', {
    Exposure2012 = 1, Curve = curve(0, 1), CorrectionID = 'a', MaskID = 'm',
    Nested = { MaskID = 'inner', Keep = 1 }, OnlyBase = { x = 1 },
  })
  local candidate = makePreset('Cand', 'uC', {
    Exposure2012 = 2, Curve = curve(0, 1), CorrectionID = 'b', MaskID = 'n',
    Nested = { MaskID = 'other', Keep = 1 }, OnlyCand = { y = 2 },
  })
  useFolders({ makeFolder('F', { base, candidate }) }, nil)
  local diff = Presets.compare({ base = { preset_uuid = 'uB' }, candidate = { preset_uuid = 'uC' },
    expected_catalog_identity = 'catalog-md5-v1:test', expected_access_mode = 'full' })
  check(diff.base.name == 'Base' and diff.candidate.name == 'Cand', 'summaries')
  check(diff.changed_count == 3 and #diff.changes == 3, 'only real changes')
  check(diff.changes[1].key == 'Exposure2012' and diff.changes[1].before == 1
    and diff.changes[1].after == 2 and diff.changes[1].before_present and diff.changes[1].after_present,
    'changed value')
  check(diff.changes[2].key == 'OnlyBase' and diff.changes[2].before_present
    and not diff.changes[2].after_present and diff.changes[2].before.x == 1, 'removed key')
  check(diff.changes[3].key == 'OnlyCand' and not diff.changes[3].before_present
    and diff.changes[3].after_present and diff.changes[3].after.y == 2, 'added key')
  local ignored = {}
  for _, key in ipairs(diff.ignored_setting_keys) do ignored[key] = true end
  check(ignored.CorrectionID and ignored.MaskID and #diff.ignored_setting_keys == 2, 'ignored key report')
  check(state.settingReads == 2, 'compare reads each selected preset once')
  check(base.settings.CorrectionID == 'a' and base.settings.Nested.MaskID == 'inner'
    and candidate.settings.CorrectionID == 'b', 'compare must not mutate source settings')
end)

test('compare reuses one library scan and self-compare is empty', function()
  defaultLibrary()
  local selfDiff = Presets.compare({ base = { preset_uuid = 'u1' }, candidate = { preset_uuid = 'u1' } })
  check(selfDiff.changed_count == 0 and #selfDiff.changes == 0, 'self-compare has no changes')
  check(selfDiff.base.uuid == 'u1' and selfDiff.candidate.uuid == 'u1', 'self-compare summaries')
  check(state.folderScans == 1, 'one library scan for both selectors')
  check(state.presetScans == 2, 'each folder read once per scan (two folders)')
  check(state.settingReads == 2, 'settings read once per side')
end)

test('compare validates both selectors and the envelope before scanning', function()
  defaultLibrary()
  expectError(function() Presets.compare(nil) end, 'base and candidate')
  expectError(function() Presets.compare({}) end, 'base and candidate')
  expectError(function() Presets.compare({ base = 'u1', candidate = { preset_uuid = 'u1' } }) end,
    'base and candidate')
  expectError(function() Presets.compare({ base = { preset_uuid = 'u1' } }) end, 'base and candidate')
  expectError(function() Presets.compare({ base = { preset_uuid = 'u1' },
    candidate = { preset_uuid = 'u1' }, extra = 1 }) end, 'unknown field')
  expectError(function() Presets.compare({ base = { preset_uuid = 'u1' },
    candidate = { preset_uuid = 'u1' }, expected_catalog_path = '/tmp/old.lrcat' }) end,
    'unknown field')
  expectError(function() Presets.compare({ base = { preset_uuid = 'u1', junk = 1 },
    candidate = { preset_uuid = 'u1' } }) end, 'unknown field')
  check(state.folderScans == 0 and state.settingReads == 0, 'bad compare must not read the library')
  expectError(function() Presets.compare({ base = { preset_uuid = 'u1' },
    candidate = { preset_name = 'Dup' } }) end, 'ambiguous')
end)

-- ---- SDK failure propagation ------------------------------------------------
test('SDK failures surface as errors', function()
  local preset = makePreset('P', 'uP', { Exposure2012 = 1 })
  useFolders({ makeFolder('F', { preset }, { name = 'folder boom' }) }, nil)
  expectError(function() Presets.list() end, 'Preset getName failed', 'folder name failure')
  useFolders({ makeFolder('F', { preset }, { presets = 'scan boom' }) }, nil)
  expectError(function() Presets.list() end, 'getDevelopPresets failed', 'preset scan failure')
  useFolders({ makeFolder('F', 'not a table') }, nil)
  expectError(function() Presets.list() end, 'getDevelopPresets failed', 'non-table preset scan')
  useFolders(nil, nil, { foldersError = 'folder list boom' })
  expectError(function() Presets.list() end, 'developPresetFolders failed', 'folder list failure')
  useFolders('not a table', nil)
  expectError(function() Presets.list() end, 'developPresetFolders failed', 'non-table folder list')
  useFolders({ makeFolder('F', { makePreset('P', 'uP', {}, { name = 'name boom' }) }) }, nil)
  expectError(function() Presets.get({ preset_uuid = 'uP' }) end, 'Preset getName failed', 'preset name failure')
  useFolders({ makeFolder('F', { makePreset('P', 'uP', {}, { uuid = 'uuid boom' }) }) }, nil)
  expectError(function() Presets.list() end, 'Preset getUuid failed', 'preset uuid failure')
  useFolders({ makeFolder('F', { makePreset('P', 'uP', {}) }) }, { 'plugin boom' },
    { pluginError = 'plugin boom' })
  expectError(function() Presets.list() end, 'getDevelopPresetsForPlugin failed', 'plugin failure')
  expectError(function() Presets.list({ query = 'P' }) end, 'getDevelopPresetsForPlugin failed',
    'failure must not be swallowed by filtering')
end)

test('plugin presets: nil means empty, tables are listed, write paths stay unused', function()
  useFolders({ makeFolder('F', { makePreset('P', 'uP', {}) }) }, nil)
  local empty = Presets.list()
  check(empty.count == 1, 'nil plugin presets are empty, not an error')
  check(state.pluginScans == 1, 'plugin reader still consulted')

  local gamma = makePreset('Gamma', 'u9', { Exposure2012 = 9 })
  useFolders({ makeFolder('F', { makePreset('P', 'uP', {}) }) }, { gamma })
  local listed = Presets.list()
  check(listed.count == 2 and listed.presets[2].uuid == 'u9', 'plugin preset listed')
  check(Presets.get({ preset_uuid = 'u9' }).settings.Exposure2012 == 9, 'plugin preset readable')

  local saved = _G._PLUGIN
  _G._PLUGIN = nil
  useFolders({}, { gamma })
  check(Presets.list().count == 0 and state.pluginScans == 0, 'no plugin identity means no plugin read')
  _G._PLUGIN = saved

  local exported = {}
  for key in pairs(Presets) do exported[#exported + 1] = key end
  table.sort(exported)
  check(table.concat(exported, ',') == 'compare,get,list', 'module must expose only read-only calls')
end)

print('presets-test: ok (' .. tests .. ' tests)')
