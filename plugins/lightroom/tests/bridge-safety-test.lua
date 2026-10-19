-- Bridge dispatch must pass through the single Safety.authorize entry point.
-- Run with: luajit bridge-safety-test.lua <plugin-dir>

local plugin = assert(arg[1], 'plugin directory required')
package.path = plugin .. '/?.lua;' .. package.path

local Json = dofile(plugin .. '/Json.lua')
local State = { bridgeRunning = false, bridgeLastError = nil }
local currentJob
local task
local resultBodies = {}
local authorizeCalls = {}
local denyAuthorization = false
local commandCalls = 0
local httpCalls = 0

_G._PLUGIN = { path = '/tmp/plugin' }
_G.import = function(name)
  if name == 'LrDialogs' then return {} end
  if name == 'LrFileUtils' then
    return { exists = function() return false end }
  end
  if name == 'LrPathUtils' then
    return { child = function(a, b) return a .. '/' .. b end }
  end
  if name == 'LrHttp' then
    return {
      get = function()
        httpCalls = httpCalls + 1
        return Json.encode(currentJob), {}
      end,
      post = function(url, body)
        httpCalls = httpCalls + 1
        if string.find(url, '/result', 1, true) then resultBodies[#resultBodies + 1] = body end
        return 'ok', {}
      end,
    }
  end
  if name == 'LrTasks' then
    return {
      pcall = function(fn, ...) return pcall(fn, ...) end,
      startAsyncTask = function(fn) task = fn end,
      sleep = function() State.bridgeRunning = false end,
    }
  end
  if name == 'LrApplication' then
    return { activeCatalog = function()
      return { getPath = function() return '/secret/catalog.lrcat' end }
    end }
  end
  if name == 'LrLogger' then
    return function() return { enable = function() end, trace = function() end } end
  end
  error('unexpected import ' .. tostring(name))
end

package.loaded.Json = Json
package.loaded.State = State
package.loaded.Safety = {
  authorize = function(command, params, path)
    authorizeCalls[#authorizeCalls + 1] = { command = command, params = params, path = path }
    if denyAuthorization then error('authorization denied by test') end
  end,
}
package.loaded.Commands = {
  list_photos = function(params)
    commandCalls = commandCalls + 1
    return { echoed = params.expected_catalog_identity }
  end,
  run_batch = function(entries)
    commandCalls = commandCalls + 1
    return { results = entries }
  end,
}

local Bridge = dofile(plugin .. '/Bridge.lua')
local failures = 0

local function check(label, condition, detail)
  print((condition and '  ✓ ' or '  ✗ ') .. label .. (detail and (' -> ' .. detail) or ''))
  if not condition then failures = failures + 1 end
end

local function run(job, deny)
  currentJob = job
  denyAuthorization = deny == true
  task = nil
  local callsBeforeStart = httpCalls
  Bridge.start()
  check('startup schedules polling without synchronous HTTP', httpCalls == callsBeforeStart)
  if task then task() end
  return Json.decode(resultBodies[#resultBodies])
end

print('========== Bridge Safety dispatch ==========')

local response = run({
  job_id = 'j1', command = 'list_photos', params = {
    expected_access_mode = 'full', expected_catalog_identity = 'catalog-md5-v1:abc',
  },
}, false)
check('bridge authorizes before executing a command', #authorizeCalls == 1 and commandCalls == 1
  and response.ok == true)
check('authorization receives command, full envelope and active path',
  authorizeCalls[1].command == 'list_photos'
  and authorizeCalls[1].params.expected_access_mode == 'full'
  and authorizeCalls[1].params.expected_catalog_identity == 'catalog-md5-v1:abc'
  and authorizeCalls[1].path == '/secret/catalog.lrcat')

response = run({
  job_id = 'j2', command = 'list_photos', params = {
    expected_access_mode = 'full', expected_catalog_identity = 'catalog-md5-v1:def',
  },
}, true)
check('authorization failure prevents command execution', #authorizeCalls == 2
  and commandCalls == 1 and response.ok == false
  and string.find(response.error, 'authorization denied', 1, true) ~= nil, response.error)

response = run({
  job_id = 'j3', command = 'command_batch', params = {
    expected_access_mode = 'full', expected_catalog_identity = 'catalog-md5-v1:ghi',
    commands = { { command = 'list_photos', params = {} } },
  },
}, false)
check('batch is authorized once at its outer non-interleavable boundary', #authorizeCalls == 3
  and authorizeCalls[3].command == 'command_batch' and commandCalls == 2 and response.ok == true)

print('failures: ' .. failures)
os.exit(failures == 0 and 0 or 1)
