-- Menu entry: start the local bridge if it is not running yet.
local LrDialogs = import 'LrDialogs'

local Bridge = require 'Bridge'

Bridge.start()
LrDialogs.showBezel(Bridge.statusLine())
