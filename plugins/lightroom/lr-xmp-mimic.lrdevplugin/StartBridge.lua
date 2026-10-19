-- Menu entry: start Lightroom's outbound poller; does not start lr-ops.
local LrDialogs = import 'LrDialogs'

local Bridge = require 'Bridge'

Bridge.start()
LrDialogs.showBezel(Bridge.statusLine())
