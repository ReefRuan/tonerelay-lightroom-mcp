-- A replaced bridge loop must not reclaim work; already claimed work completes.
-- Run with: luajit bridge-generation-test.lua <plugin-dir>
local plugin = assert(arg[1], 'plugin directory required')
package.path = plugin .. '/?.lua;' .. package.path
local Json = dofile(plugin .. '/Json.lua')
local State = { bridgeRunning = false }
local tasks, results = {}, {}
local gets, posts, commands, authorized = 0, 0, 0, 0
local claimDuringGet = false
local invalidClaim = false
_G._PLUGIN = { path = '/tmp/test-plugin' }
_G.import = function(name)
    if name == 'LrDialogs' then return {} end
    if name == 'LrFileUtils' then return { exists = function() return false end } end
    if name == 'LrPathUtils' then return { child = function(a, b) return a .. '/' .. b end } end
    if name == 'LrLogger' then
        return function() return { enable = function() end, trace = function() end } end
    end
    if name == 'LrTasks' then return {
        pcall = pcall,
        startAsyncTask = function(fn) tasks[#tasks + 1] = coroutine.create(fn) end,
        sleep = function() coroutine.yield('sleep') end,
    } end
    if name == 'LrHttp' then return {
        get = function()
            gets = gets + 1
            if claimDuringGet then
                claimDuringGet = false
                coroutine.yield('claimed')
                if invalidClaim then return 'malformed', {} end
                return Json.encode({ job_id = 'claimed-job', command = 'apply_settings', params = {} }), {}
            end
            return '', {}
        end,
        post = function(url, body)
            posts = posts + 1
            if url:find('/result', 1, true) then results[#results + 1] = Json.decode(body) end
            return 'ok', {}
        end,
    } end
    if name == 'LrApplication' then return { activeCatalog = function()
        return { getPath = function() return '/test/disposable.lrcat' end }
    end } end
    error('unexpected import ' .. name)
end
package.loaded.Json = Json
package.loaded.State = State
package.loaded.Safety = { authorize = function() authorized = authorized + 1 end }
package.loaded.Commands = { apply_settings = function()
    commands = commands + 1
    return { applied = true }
end }
local Bridge = dofile(plugin .. '/Bridge.lua')
local function resume(task, expected)
    local ok, value = coroutine.resume(task)
    assert(ok, value)
    if expected then assert(value == expected, 'unexpected yield: ' .. tostring(value)) end
end

-- A scheduled, but never started, loop has no network effects after replacement.
Bridge.start()
local abandoned = tasks[#tasks]
local taskCount, firstGeneration = #tasks, State.bridgeGeneration
Bridge.start()
assert(#tasks == taskCount and State.bridgeGeneration == firstGeneration,
    'starting an already running bridge created another generation')
Bridge.stop()
Bridge.start()
local active = tasks[#tasks]
resume(abandoned)
assert(coroutine.status(abandoned) == 'dead' and gets == 0 and posts == 0,
    'superseded scheduled loop performed network work')
resume(active, 'sleep')
assert(gets == 1)

-- A sleeping old loop must terminate when a new start sets running=true again.
Bridge.stop()
Bridge.start()
local replacement = tasks[#tasks]
resume(replacement, 'sleep')
local before = gets
resume(active)
assert(coroutine.status(active) == 'dead' and gets == before,
    'old sleeping loop resumed polling after restart')
resume(replacement, 'sleep')
assert(gets == before + 1, 'current generation did not continue polling')
Bridge.stop()
resume(replacement)
assert(coroutine.status(replacement) == 'dead')

-- Stop/start during a yielding claim cannot drop or replay the claimed job.
claimDuringGet = true
Bridge.start()
local claiming = tasks[#tasks]
resume(claiming, 'claimed')
Bridge.stop()
Bridge.start()
local successor = tasks[#tasks]
local getsAtClaim = gets
resume(successor, 'sleep')
assert(gets == getsAtClaim and commands == 0,
    'new loop claimed work while old claim was still in flight')
resume(claiming)
assert(commands == 1 and authorized == 1 and #results == 1 and results[1].ok == true,
    'claimed work did not finish exactly once through Safety')
assert(coroutine.status(claiming) == 'dead', 'claimed old loop did not terminate')
resume(successor, 'sleep')
assert(commands == 1 and #results == 1, 'successor replayed the old job')
Bridge.stop()
resume(successor)
assert(coroutine.status(successor) == 'dead')
assert(State.bridgeOperationGeneration == nil, 'finished operation retained its owner')

-- A superseded response cannot overwrite the current generation's diagnostic.
claimDuringGet, invalidClaim = true, true
Bridge.start()
local invalidating = tasks[#tasks]
resume(invalidating, 'claimed')
Bridge.stop()
Bridge.start()
local current = tasks[#tasks]
State.bridgeLastError = 'current diagnostic'
resume(invalidating)
assert(State.bridgeLastError == 'current diagnostic', 'old malformed poll replaced new diagnostic')
resume(current, 'sleep')
Bridge.stop()
resume(current)
print('Bridge generation regression passed')
