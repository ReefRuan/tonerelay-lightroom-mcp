-- Stop claiming jobs when Lightroom disables or unloads this plugin. This is
-- synchronous: an already claimed operation may finish, but is never replayed.
local Bridge = require 'Bridge'

Bridge.stop()
