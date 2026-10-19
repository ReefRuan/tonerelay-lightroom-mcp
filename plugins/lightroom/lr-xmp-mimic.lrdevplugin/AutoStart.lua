-- Runs automatically when Lightroom loads the plugin (LrInitPlugin).
-- Starts Lightroom's outbound poller, not the Python Bridge service.
local Bridge = require 'Bridge'

-- A fresh plugin lifecycle must replace stale scheduled/running state left by
-- a disabled plugin. An old in-flight job retains its execution boundary.
Bridge.start(true)
