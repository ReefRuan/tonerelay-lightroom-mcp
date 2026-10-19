-- token-log-test.lua — 验证 Bridge.lua 的 log() 会把 X-LR-XMP-Token 带上
--
-- 起因（2026-09-12）：桥的 POST /log 现在也校验 token（原来只有 /jobs/next 与
-- /jobs/<id>/result 校验，/jobs、/log、/echo 都是裸的）。插件的 log() 曾经把
-- headers 写死成只有 Content-Type，于是「一旦启用 token，日志通道就会 403」——
-- 轮询照常但服务侧 plugin.log 静默断掉。这个测试把那句话固化成断言。
--
-- 做法：把 Bridge.lua 当普通 Lua 加载，桩掉 LR 的 API，捕获 LrHttp.post 的 headers。
--   用 Bridge.start() 排队，再显式运行捕获到的异步任务。
--   在首轮 sleep 停止循环，既检查启动不发同步 HTTP，也检查日志鉴权。
--
-- 用法：
--   mkdir -p /tmp/lr-bridge-token && \
--   luajit plugins/lightroom/tests/token-log-test.lua \
--          plugins/lightroom/lr-xmp-mimic.lrdevplugin /tmp/lr-bridge-token
--
-- 退出码：0 全部通过；1 有失败项。需要 luajit（brew install luajit）。

local PLUGIN = arg[1]          -- 插件目录
local TMP = arg[2]             -- 放 local-token.txt 的临时目录

if not PLUGIN or not TMP then
  print('用法：luajit token-log-test.lua <插件目录> <临时目录>')
  os.exit(2)
end

local captured = {}            -- 捕获到的所有 HTTP 调用
local task

-- ---- 桩 LR API ----
import = function(name)
  if name == 'LrDialogs' then return {} end
  if name == 'LrFileUtils' then
    return {
      exists = function(p) return p == TMP .. '/local-token.txt' end,
      readFile = function(p)
        if p == TMP .. '/local-token.txt' then return '  my-secret-token\n' end
        return nil
      end,
      writeFile = function() return true end,
    }
  end
  if name == 'LrPathUtils' then
    return { child = function(a, b) return a .. '/' .. b end }
  end
  if name == 'LrHttp' then
    return {
      post = function(url, body, headers) captured[#captured + 1] = { url = url, headers = headers } return 'ok', {} end,
      get = function(url, headers) captured[#captured + 1] = { url = url, headers = headers } return '', {} end,
    }
  end
  if name == 'LrTasks' then
    return {
      pcall = function(fn, ...) return pcall(fn, ...) end,
      sleep = function() package.loaded.State.bridgeRunning = false end,
      startAsyncTask = function(fn) task = fn end,
    }
  end
  if name == 'LrApplication' then
    return { activeCatalog = function() return nil end, versionString = function() return 'stub' end }
  end
  if name == 'LrLogger' then
    return function() return { enable = function() end, trace = function() end } end
  end
  return {}
end

-- ---- 桩被 require 的模块（避免加载真实的 Commands/Safety 触发 LR 依赖）----
package.loaded['Json'] = { encode = function() return '{}' end, decode = function() return {} end }
package.loaded['Safety'] = { authorize = function() end }
package.loaded['Commands'] = {}
package.loaded['State'] = { bridgeRunning = false, bridgeLastError = nil }

-- ---- 环境准备 ----
_G._PLUGIN = { path = TMP }

-- ---- 加载真实的 Bridge.lua ----
local chunk = assert(loadfile(PLUGIN .. '/Bridge.lua'))
local Bridge = chunk()

-- ---- 启动应只安排任务；HTTP 日志在异步任务内发送 ----
Bridge.start()
assert(#captured == 0, 'Bridge.start must not send HTTP before the async task')
assert(type(task) == 'function', 'Bridge.start must schedule an async task')
task()

-- ---- 断言 ----
local failures = 0
local function check(name, ok, detail)
  print((ok and '  ✓ ' or '  ✗ ') .. name .. (detail and ('  → ' .. detail) or ''))
  if not ok then failures = failures + 1 end
end

print('\n========== Bridge.lua /log token 断言 ==========')
local logCall
for _, c in ipairs(captured) do
  if c.url:match('/log$') then logCall = c end
end

check('Bridge.start 会发出 POST /log', logCall ~= nil, logCall and logCall.url or '未捕获到 /log 请求')

if logCall then
  local contentType
  for _, h in ipairs(logCall.headers) do
    if h.field == 'Content-Type' then contentType = h.value end
  end
  check('Content-Type 仍为 text/plain', contentType == 'text/plain', tostring(contentType))

  local tokenValue
  for _, h in ipairs(logCall.headers) do
    if h.field == 'X-LR-XMP-Token' then tokenValue = h.value end
  end
  check('携带 X-LR-XMP-Token', tokenValue ~= nil, tostring(tokenValue))
  check('token 值正确且已去除首尾空白', tokenValue == 'my-secret-token', tostring(tokenValue))
end

print('\n失败项：' .. failures)
os.exit(failures == 0 and 0 or 1)
