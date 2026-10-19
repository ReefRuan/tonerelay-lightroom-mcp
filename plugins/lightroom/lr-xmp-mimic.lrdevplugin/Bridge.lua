-- Local bridge: polls the lr-ops service on 127.0.0.1 for jobs, executes
-- them through Commands.lua and posts results back. Never listens on any
-- port itself; it only makes outbound calls to loopback.
local LrDialogs = import 'LrDialogs'
local LrFileUtils = import 'LrFileUtils'
local LrHttp = import 'LrHttp'
local LrPathUtils = import 'LrPathUtils'
local LrTasks = import 'LrTasks'
local LrApplication = import 'LrApplication'
local Safety = require 'Safety'

local Commands = require 'Commands'
local Json = require 'Json'
local State = require 'State'

local Bridge = {
    host = '127.0.0.1',
    port = 17777,
    pollIntervalSec = 0.5,
}

local LrLogger = import 'LrLogger'
local bridgeLogger = LrLogger('LrXmpMimicBridge')
bridgeLogger:enable('logfile')  -- ~/Documents/LrClassicLogs/LrXmpMimicBridge.log

local logLines = {}
local MAX_LOG_LINES = 200

-- LR's Lua (5.1) cannot yield across a plain pcall; LrHttp / catalog writes
-- yield internally, so every wrapper around them must be LrTasks.pcall.
local ypcall = LrTasks.pcall

-- Forward declaration: log() is defined before readToken() but needs the token
-- so that POST /log stays authorized once a token is configured. Without this,
-- enabling the token would 403 the log-shipping path while polling kept working.
local readToken

local function log(message)
    local line = os.date('%H:%M:%S') .. ' ' .. message
    bridgeLogger:trace(line)
    -- Also ship every line to the service; this HTTP path is proven to work.
    -- The service guards POST /log with the same token as /jobs, so send it.
    ypcall(function()
        local logHeaders = { { field = 'Content-Type', value = 'text/plain' } }
        local token = readToken and readToken()
        if token then
            logHeaders[#logHeaders + 1] = { field = 'X-LR-XMP-Token', value = token }
        end
        LrHttp.post('http://127.0.0.1:17777/log', line, logHeaders)
    end)
    logLines[#logLines + 1] = line
    if #logLines > MAX_LOG_LINES then
        table.remove(logLines, 1)
    end
    if LrFileUtils.writeFile then
        pcall(function()
            LrFileUtils.writeFile(
                LrPathUtils.child(_PLUGIN.path, 'bridge-log.txt'),
                table.concat(logLines, '\n') .. '\n'
            )
        end)
    end
end

local function pluginDir()
    return _PLUGIN.path
end

readToken = function()
    -- Optional shared token. The service only enforces it when
    -- LR_XMP_MIMIC_LOCAL_TOKEN is set on the Python side; the plugin reads
    -- the same value from local-token.txt next to this plugin.
    local tokenFile = LrPathUtils.child(pluginDir(), 'local-token.txt')
    if LrFileUtils.exists(tokenFile) then
        local content = LrFileUtils.readFile(tokenFile)
        if type(content) == 'string' then
            local trimmed = string.gsub(content, '^%s+', '')
            trimmed = string.gsub(trimmed, '%s+$', '')
            if trimmed ~= '' then
                return trimmed
            end
        end
    end
    return nil
end

local function headers()
    local result = { { field = 'Content-Type', value = 'application/json' } }
    local token = readToken()
    if token then
        result[#result + 1] = { field = 'X-LR-XMP-Token', value = token }
    end
    return result
end

local function baseUrl()
    return 'http://' .. Bridge.host .. ':' .. tostring(Bridge.port)
end

local function postResult(jobId, payload, generation)
    local ok, err = ypcall(function()
        LrHttp.post(baseUrl() .. '/jobs/' .. jobId .. '/result', Json.encode(payload), headers())
    end)
    if not ok then
        -- Nothing else can be done from inside Lightroom; the service will
        -- time the job out and the next poll keeps working.
        if State.bridgeGeneration == generation then
            State.bridgeLastError = 'result post failed: ' .. tostring(err)
        end
        log('postResult FAILED for ' .. jobId .. ': ' .. tostring(err))
    else
        log('postResult ok for ' .. jobId .. ' (ok=' .. tostring(payload.ok) .. ')')
    end
end

-- Forward declaration: executeJob branches into the batch runner, which is
-- defined below it.
local executeBatch

local function executeJob(job, generation)
    if job.command == 'command_batch' then
        executeBatch(job, generation)
        return
    end
    local command = Commands[job.command]
    if not command then
        postResult(job.job_id, {
            ok = false,
            error = 'Unknown command: ' .. tostring(job.command),
        }, generation)
        return
    end
    log('executing ' .. tostring(job.command) .. ' (' .. job.job_id .. ')')
    local ok, result = ypcall(function()
        local params = job.params or {}
        local activeCatalog = LrApplication.activeCatalog()
        local catalogPath = activeCatalog and activeCatalog:getPath()
        if type(catalogPath) ~= 'string' or catalogPath == '' then
            if job.command == 'probe_client' then
                return command(params)
            end
            error('No active Lightroom Catalog is open.')
        end
        Safety.authorize(job.command, params, catalogPath)
        return command(params)
    end)
    if ok then
        postResult(job.job_id, { ok = true, data = result }, generation)
    else
        log('command error: ' .. tostring(result))
        postResult(job.job_id, { ok = false, error = tostring(result) }, generation)
    end
end

-- A batch is one queue item carrying a whole logical operation, so nothing can
-- interleave between its commands. The catalog binding is checked ONCE from
-- the job-level params -- per-entry expected_catalog fields are meaningless,
-- the batch either runs against the right catalog or not at all.
executeBatch = function(job, generation)
    local params = job.params or {}
    local entries = params.commands
    if type(entries) ~= 'table' or #entries == 0 then
        postResult(job.job_id, {
            ok = false,
            error = 'command_batch requires a non-empty commands array.',
        }, generation)
        return
    end
    log('executing batch of ' .. tostring(#entries) .. ' commands (' .. job.job_id .. ')')
    local ok, outcome = ypcall(function()
        local activeCatalog = LrApplication.activeCatalog()
        local catalogPath = activeCatalog and activeCatalog:getPath()
        if type(catalogPath) ~= 'string' or catalogPath == '' then
            error('No active Lightroom Catalog is open.')
        end
        Safety.authorize(job.command, params, catalogPath)
        return Commands.run_batch(entries)
    end)
    if not ok then
        log('batch error: ' .. tostring(outcome))
        postResult(job.job_id, { ok = false, error = tostring(outcome) }, generation)
        return
    end
    if outcome.failed_at_index then
        log('batch failed at index ' .. tostring(outcome.failed_at_index) .. ': ' .. tostring(outcome.error))
        postResult(job.job_id, {
            ok = false,
            error = outcome.error,
            failed_at_index = outcome.failed_at_index,
            results_so_far = outcome.results,
        }, generation)
    else
        postResult(job.job_id, { ok = true, data = { results = outcome.results } }, generation)
    end
end

local function pollOnce(generation)
    local body, responseHeaders = LrHttp.get(baseUrl() .. '/jobs/next', headers(), 5)
    if type(body) ~= 'string' or body == '' then
        return nil
    end
    log('poll got body: ' .. string.sub(body, 1, 120))
    local ok, job = pcall(Json.decode, body)
    if not ok or type(job) ~= 'table' or not job.job_id then
        if State.bridgeGeneration == generation then
            State.bridgeLastError = 'bad job payload from service'
        end
        log('bad job payload: ' .. tostring(job))
        return nil
    end
    return job
end

local function taskAlive(task)
    -- A scheduled task has no thread until Lightroom first enters it. Once it
    -- has started, the coroutine is the liveness evidence, not a sticky bool.
    return task and (not task.thread or coroutine.status(task.thread) ~= 'dead')
end

local function releaseOperation(task)
    if State.bridgeOperationTask == task then
        State.bridgeOperationTask = nil
        State.bridgeOperationGeneration = nil
    end
end

function Bridge.start(forceRestart)
    if not forceRestart and State.bridgeRunning and taskAlive(State.bridgeTask) then
        return
    end
    State.bridgeGeneration = (State.bridgeGeneration or 0) + 1
    local generation = State.bridgeGeneration
    local task = { generation = generation }
    local function isCurrent()
        return State.bridgeRunning and State.bridgeTask == task
            and State.bridgeGeneration == generation
    end
    State.bridgeTask = task
    State.bridgeRunning = true
    State.bridgeLastError = nil
    LrTasks.startAsyncTask(function()
        if not isCurrent() then return end
        task.thread = coroutine.running()
        -- log() sends HTTP as well as writing the local logger. Initialization
        -- must schedule the task before any yielding network operation.
        local ok, err = ypcall(function()
            log('Lightroom polling started: ' .. baseUrl())
            while isCurrent() do
                local owner = State.bridgeOperationTask
                if owner and owner ~= task and taskAlive(owner) then
                    -- Wait for the old generation's yielding claim/command/result
                    -- boundary before claiming another job. No writes overlap.
                    LrTasks.sleep(Bridge.pollIntervalSec)
                else
                    -- A canceled/dead task cannot keep a future lifecycle blocked.
                    -- Unknown claimed writes are not resubmitted here.
                    if owner and owner ~= task then releaseOperation(owner) end
                    State.bridgeOperationTask = task
                    State.bridgeOperationGeneration = generation
                    local ok, err = ypcall(function()
                        local job = pollOnce(generation)
                        if job then
                            -- A claim may yield across stop/start. Finish it through
                            -- Safety exactly once before retiring; never replay it.
                            executeJob(job, generation)
                        end
                    end)
                    releaseOperation(task)
                    if not isCurrent() then break end
                    if not ok then
                        State.bridgeLastError = tostring(err)
                        log('poll error: ' .. tostring(err))
                        LrTasks.sleep(2)
                    else
                        LrTasks.sleep(Bridge.pollIntervalSec)
                    end
                end
            end
        end)
        releaseOperation(task)
        if State.bridgeTask == task then
            State.bridgeTask = nil
            State.bridgeRunning = false
            if not ok then State.bridgeLastError = tostring(err) end
            log('Lightroom polling stopped' .. (ok and '' or ': ' .. tostring(err)))
        end
    end)
end

function Bridge.stop()
    State.bridgeRunning = false
    State.bridgeGeneration = (State.bridgeGeneration or 0) + 1
    State.bridgeTask = nil
    -- Keep a live in-flight operation's token until it reaches its result
    -- boundary; a replacement poller must not execute writes in parallel.
    if State.bridgeOperationTask and not taskAlive(State.bridgeOperationTask) then
        releaseOperation(State.bridgeOperationTask)
    end
end

function Bridge.statusLine()
    if not State.bridgeRunning or not taskAlive(State.bridgeTask) then
        return 'Lightroom polling stopped.'
    end
    if State.bridgeLastError then
        return 'Lightroom polling. Last error: ' .. State.bridgeLastError
    end
    return 'Lightroom polling ' .. baseUrl()
end

return Bridge
