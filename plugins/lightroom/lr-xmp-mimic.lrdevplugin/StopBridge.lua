-- Menu entry: stop the local bridge poll loop.
local LrDialogs = import 'LrDialogs'

local Bridge = require 'Bridge'

Bridge.stop()
LrDialogs.showBezel('Bridge stopped.')
