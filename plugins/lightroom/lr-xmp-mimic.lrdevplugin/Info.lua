return {
    LrSdkVersion = 5.0,
    LrSdkMinimumVersion = 5.0,
    LrToolkitIdentifier = 'com.lr-xmp-mimic.runtime',
    LrPluginName = 'ToneRelay for Lightroom (Development)',

    VERSION = {
        major = 0,
        minor = 4,
        revision = 0,
        build = 5,
    },

    LrPluginInfoProvider = 'PluginInfoProvider.lua',

    LrInitPlugin = 'AutoStart.lua',
    -- Without this flag Lightroom lazily runs LrInitPlugin only after the
    -- first Plug-in Extras/Plug-in Manager use, leaving the bridge offline
    -- after a normal restart.
    LrForceInitPlugin = true,

    LrLibraryMenuItems = {
        {
            title = 'Start local bridge',
            file = 'StartBridge.lua',
        },
        {
            title = 'Bridge self test',
            file = 'SelfTest.lua',
        },
        {
            title = 'Stop local bridge',
            file = 'StopBridge.lua',
        },
    },
}
