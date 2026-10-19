local LrApplication = import 'LrApplication'
local LrView = import 'LrView'

local Safety = require 'Safety'

local bind = LrView.bind

local function activeCatalogPath()
    local ok, path = pcall(function()
        return LrApplication.activeCatalog():getPath()
    end)
    if ok and type(path) == 'string' and path ~= '' then
        return path
    end
    return nil
end

local function registeredLabel()
    return Safety.registeredSandboxIdentity() or 'Not registered'
end

local function refresh(props, message)
    local path = activeCatalogPath()
    props.currentCatalogPath = path or 'Unavailable'
    props.registeredSandbox = registeredLabel()
    props.guardStatus = path and Safety.catalogGuardMessage(path)
        or 'No active Catalog is available.'
    if message ~= nil then
        props.status = message
    end
end

return {
    sectionsForTopOfDialog = function(f, props)
        props.accessMode = Safety.getAccessMode()
        props.status = ''
        refresh(props)

        return {
            {
                title = 'ToneRelay · Runtime access',
                f:column {
                    bind_to_object = props,
                    spacing = f:control_spacing(),
                    f:static_text {
                        title = 'Full access is the default. Safe mode limits every operational command to one explicitly registered Catalog.',
                        width_in_chars = 72,
                        height_in_lines = 2,
                    },
                    f:row {
                        spacing = f:label_spacing(),
                        f:static_text { title = 'Access mode:', width_in_chars = 12 },
                        f:popup_menu {
                            value = bind 'accessMode',
                            items = {
                                { title = 'Full access', value = Safety.ACCESS_MODE_FULL },
                                { title = 'Safe (registered sandbox only)', value = Safety.ACCESS_MODE_SAFE },
                            },
                        },
                        f:push_button {
                            title = 'Save mode',
                            action = function()
                                local ok, err = pcall(Safety.setAccessMode, props.accessMode)
                                refresh(props, ok and 'Access mode saved.' or ('Could not save mode: ' .. tostring(err)))
                            end,
                        },
                    },
                    f:static_text {
                        title = bind 'guardStatus',
                        width_in_chars = 72,
                        height_in_lines = 2,
                    },
                    f:static_text {
                        title = 'Active Catalog:',
                        font = '<system/bold>',
                    },
                    f:static_text {
                        title = bind 'currentCatalogPath',
                        width_in_chars = 72,
                        truncation = 'middle',
                    },
                    f:row {
                        spacing = f:label_spacing(),
                        f:push_button {
                            title = 'Register active Catalog as sandbox',
                            action = function()
                                local path = activeCatalogPath()
                                local ok, err = pcall(Safety.registerSandboxCatalog, path)
                                refresh(props, ok and 'Active Catalog registered as the safe sandbox.'
                                    or ('Could not register Catalog: ' .. tostring(err)))
                            end,
                        },
                        f:push_button {
                            title = 'Clear sandbox registration',
                            action = function()
                                Safety.clearSandboxCatalog()
                                refresh(props, 'Sandbox registration cleared.')
                            end,
                        },
                    },
                    f:static_text {
                        title = 'Registered identity:',
                        font = '<system/bold>',
                    },
                    f:static_text {
                        title = bind 'registeredSandbox',
                        width_in_chars = 72,
                        truncation = 'middle',
                    },
                    f:static_text {
                        title = bind 'status',
                        width_in_chars = 72,
                        height_in_lines = 2,
                    },
                },
            },
        }
    end,

    endDialog = function(props, _)
        -- Also persist the popup selection when the user closes Plugin Manager
        -- without pressing Save mode.
        pcall(Safety.setAccessMode, props.accessMode)
    end,
}
