-- Offline Lightroom SDK mocks only: no Python Bridge, Catalog or real HTTP.
-- Run with: luajit bridge-lifecycle-test.lua <plugin-dir>
local plugin = assert(arg[1], 'plugin directory required')
package.path = plugin .. '/?.lua;' .. package.path
local Json = dofile(plugin .. '/Json.lua')

local function harness()
    local h = { state = dofile(plugin .. '/State.lua'), tasks = {}, results = {},
        gets = 0, posts = 0, writes = 0, authorized = 0 }
    _G._PLUGIN = { path = '/mock/plugin' }
    _G.import = function(name)
        if name == 'LrDialogs' then return {} end
        if name == 'LrFileUtils' then return { exists = function() return false end } end
        if name == 'LrPathUtils' then return { child = function(a, b) return a .. '/' .. b end } end
        if name == 'LrLogger' then
            return function() return { enable = function() end, trace = function() end } end
        end
        if name == 'LrTasks' then return {
            pcall = pcall,
            startAsyncTask = function(fn) h.tasks[#h.tasks + 1] = coroutine.create(fn) end,
            sleep = function()
                if h.failSleep then h.failSleep = false; error('task sleep failed') end
                coroutine.yield('sleep')
            end,
        } end
        if name == 'LrHttp' then return {
            get = function()
                h.gets = h.gets + 1
                if h.unavailable then error('mock Bridge unavailable') end
                local job = h.job
                h.job = nil
                if h.suspendClaim then h.suspendClaim = false; coroutine.yield('claim') end
                return job and Json.encode(job) or '', {}
            end,
            post = function(url, body)
                h.posts = h.posts + 1
                if url:find('/result', 1, true) then
                    if h.suspendResult then h.suspendResult = false; coroutine.yield('result') end
                    if h.failResult then error('mock result unavailable') end
                    h.results[#h.results + 1] = Json.decode(body)
                end
                return 'ok', {}
            end,
        } end
        if name == 'LrApplication' then return { activeCatalog = function()
            return { getPath = function() return '/mock/disposable.lrcat' end }
        end } end
        error('unexpected SDK import ' .. name)
    end
    package.loaded.State = h.state
    package.loaded.Json = Json
    package.loaded.Safety = { authorize = function() h.authorized = h.authorized + 1 end }
    package.loaded.Commands = { apply_settings = function()
        h.writes = h.writes + 1
        if h.suspendWrite then h.suspendWrite = false; coroutine.yield('write') end
        return { applied = true }
    end }
    h.bridge = dofile(plugin .. '/Bridge.lua')
    package.loaded.Bridge = h.bridge
    function h:init() dofile(plugin .. '/AutoStart.lua'); return self.tasks[#self.tasks] end
    function h:shutdown() dofile(plugin .. '/Shutdown.lua') end
    function h:jobNext()
        self.job = { job_id = 'mock-claimed', command = 'apply_settings', params = {} }
    end
    return h
end

local function resume(task, expected)
    local ok, value = coroutine.resume(task)
    assert(ok, value)
    assert(value == expected, 'unexpected yield: ' .. tostring(value))
end

-- LR may start before a separately managed Bridge. Failure remains retryable;
-- disabling and re-enabling never leaves a stale running flag with no task.
local h = harness()
h.unavailable = true
local old = h:init()
assert(h.gets == 0 and h.posts == 0, 'plugin init performed synchronous HTTP')
resume(old, 'sleep')
assert(h.state.bridgeRunning and h.state.bridgeLastError:find('unavailable', 1, true))
local networkAtShutdown = h.gets + h.posts
h:shutdown()
assert(not h.state.bridgeRunning and h.state.bridgeTask == nil)
assert(h.gets + h.posts == networkAtShutdown, 'shutdown performed synchronous HTTP')
local current = h:init()
resume(old)
assert(coroutine.status(old) == 'dead', 'disabled loop resumed polling')
h.unavailable = false
h:jobNext()
resume(current, 'sleep')
assert(h.writes == 1 and h.authorized == 1 and #h.results == 1,
    'late Bridge recovery failed to execute through Safety')
h:shutdown(); resume(current)
assert(h.state.bridgeTask == nil and h.state.bridgeOperationTask == nil)

-- Forced lifecycle init retires even a scheduled task that Lightroom never ran.
h = harness()
old = h:init()
current = h:init()
resume(old)
assert(h.gets == 0 and h.posts == 0, 'superseded scheduled init performed HTTP')
resume(current, 'sleep')
h:shutdown(); resume(current)

-- Sticky state, and a known dead coroutine, are both recoverable via menu start.
h = harness()
h.state.bridgeRunning = true
h.bridge.start()
assert(#h.tasks == 1, 'a bare stale running flag prevented task creation')
resume(h.tasks[1], 'sleep'); h:shutdown(); resume(h.tasks[1])
local dead = coroutine.create(function() end)
resume(dead)
h.state.bridgeRunning = true
h.state.bridgeTask = { thread = dead }
h.state.bridgeOperationTask = { thread = dead, generation = -1 }
h.state.bridgeOperationGeneration = -1
h.bridge.start()
resume(h.tasks[2], 'sleep')
assert(h.gets == 2 and h.writes == 0 and h.state.bridgeOperationTask == nil,
    'dead task retained the claim lock or replayed an unknown operation')
h:shutdown(); resume(h.tasks[2])

-- Unexpected task exit releases liveness state and can be started again.
h = harness()
h.failSleep = true
old = h:init()
resume(old)
assert(coroutine.status(old) == 'dead' and not h.state.bridgeRunning
    and h.state.bridgeTask == nil and h.state.bridgeOperationTask == nil,
    'task failure retained running/operation state')
h.bridge.start()
resume(h.tasks[2], 'sleep')
h:shutdown(); resume(h.tasks[2])

-- Every yielding claim/write/result boundary serializes old and new lifecycles.
-- Claimed work finishes once; no unknown job is re-submitted by a replacement.
for _, boundary in ipairs({ 'claim', 'write', 'result' }) do
    h = harness()
    h:jobNext()
    if boundary == 'claim' then h.suspendClaim = true end
    if boundary == 'write' then h.suspendWrite = true end
    if boundary == 'result' then h.suspendResult = true end
    old = h:init()
    resume(old, boundary)
    h:shutdown()
    current = h:init()
    local getsBefore = h.gets
    resume(current, 'sleep')
    assert(h.gets == getsBefore, 'replacement claimed while old ' .. boundary .. ' was in flight')
    resume(old)
    assert(h.writes == 1 and h.authorized == 1 and #h.results == 1,
        'in-flight ' .. boundary .. ' did not finish exactly once through Safety')
    assert(coroutine.status(old) == 'dead' and h.state.bridgeRunning,
        'retired task cleared the new lifecycle')
    resume(current, 'sleep')
    assert(h.gets == getsBefore + 1 and h.writes == 1 and #h.results == 1,
        'replacement replayed retired ' .. boundary .. ' work')
    h:shutdown(); resume(current)
end

-- An old result's failure must not overwrite the current lifecycle diagnostic.
h = harness()
h:jobNext(); h.suspendResult = true
old = h:init()
resume(old, 'result')
h:shutdown(); current = h:init()
h.state.bridgeLastError = 'new lifecycle diagnostic'
h.failResult = true
resume(old)
assert(h.state.bridgeLastError == 'new lifecycle diagnostic',
    'retired result failure replaced the new lifecycle diagnostic')
resume(current, 'sleep')
h:shutdown(); resume(current)
print('Bridge lifecycle regression passed (offline SDK mocks)')
