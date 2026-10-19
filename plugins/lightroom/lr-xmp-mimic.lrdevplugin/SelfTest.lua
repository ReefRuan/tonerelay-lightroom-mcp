-- Menu entry: bridge self test. Tries the exact HTTP calls the bridge uses
-- and shows the outcome in a persistent dialog (not a bezel).
local LrDialogs = import 'LrDialogs'
local LrHttp = import 'LrHttp'
local LrTasks = import 'LrTasks'

local Json = require 'Json'
local Bridge = require 'Bridge'

LrTasks.startAsyncTask(function()
    local lines = {}

    local okGet, bodyGet = pcall(function()
        return LrHttp.get('http://127.0.0.1:17777/health', nil, 5)
    end)
    lines[#lines + 1] = 'GET /health: ' .. (okGet and ('OK -> ' .. tostring(bodyGet)) or ('FAIL -> ' .. tostring(bodyGet)))

    local payload = Json.encode({ ok = true, data = { test = 1 } })
    lines[#lines + 1] = 'Json.encode: ' .. payload

    local okPost, errPost = pcall(function()
        return LrHttp.post(
            'http://127.0.0.1:17777/echo',
            payload,
            { { field = 'Content-Type', value = 'application/json' } }
        )
    end)
    lines[#lines + 1] = 'POST /echo: ' .. (okPost and ('OK -> ' .. tostring(errPost)) or ('FAIL -> ' .. tostring(errPost)))

    LrDialogs.message('Bridge self test', table.concat(lines, '\n\n'), 'info')
end)
