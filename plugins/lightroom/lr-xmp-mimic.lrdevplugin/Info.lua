-- The File menu uses LrExportMenuItems; Library menu registration alone does
-- not expose bridge recovery from Develop. Keep both entry points identical.
local bridgeMenuItems = {
    { title = 'Start Lightroom polling', file = 'StartBridge.lua' },
    { title = 'Bridge self test', file = 'SelfTest.lua' },
    { title = 'Stop Lightroom polling', file = 'StopBridge.lua' },
}

return {
    LrSdkVersion = 5.0,
    LrSdkMinimumVersion = 5.0,
    LrToolkitIdentifier = 'com.lr-xmp-mimic.runtime',
    LrPluginName = 'ToneRelay for Lightroom (Development)',

    VERSION = {
        major = 0,
        minor = 7,
        revision = 0,
        build = 1,
    },

    LrPluginInfoProvider = 'PluginInfoProvider.lua',

    LrInitPlugin = 'AutoStart.lua',
    LrShutdownPlugin = 'Shutdown.lua',
    -- Without this flag Lightroom lazily runs LrInitPlugin only after the
    -- first Plug-in Extras/Plug-in Manager use, leaving the bridge offline
    -- after a normal restart.
    LrForceInitPlugin = true,

    LrLibraryMenuItems = bridgeMenuItems,
    LrExportMenuItems = bridgeMenuItems,
}
