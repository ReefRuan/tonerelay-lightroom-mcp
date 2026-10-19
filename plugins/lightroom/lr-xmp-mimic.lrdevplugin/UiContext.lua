-- Ephemeral SDK objects stay inside one command. Only restoration facts leave
-- Lightroom; a failed restoration must never replay a successful copy write.
local LrApplication = import 'LrApplication'
local LrApplicationView = import 'LrApplicationView'
local LrSelection = import 'LrSelection'
local LrTasks = import 'LrTasks'
local Json = require 'Json'

local UiContext = {}
local parts = { 'sources', 'selection', 'module' }

local function array(values)
    if type(values) ~= 'table' then error('SDK array required') end
    local result = {}
    for index, value in ipairs(values) do result[index] = value end
    return result
end

local function sameMembers(left, right)
    if type(left) ~= 'table' or type(right) ~= 'table' or #left ~= #right then return false end
    local found = {}
    for _, value in ipairs(left) do
        local matched = false
        for index, other in ipairs(right) do
            if not found[index] and value == other then
                found[index] = true; matched = true; break
            end
        end
        if not matched then return false end
    end
    return true
end

local function warning(report, part, code)
    report.warnings[#report.warnings + 1] = { part = part, code = code }
end

function UiContext.unavailable(code)
    local report = { schema_version = 'lr-ui-context-v1', status = 'unavailable', parts = {}, warnings = Json.array() }
    for _, part in ipairs(parts) do report.parts[part] = { status = 'unavailable' } end
    warning(report, 'all', code or 'restore_failed')
    return report
end

function UiContext.capture(catalog)
    local context = { catalog = catalog, report = UiContext.unavailable('snapshot_unavailable'), allowed_targets = {} }
    context.report.warnings = Json.array()
    local ok, path = LrTasks.pcall(function() return catalog:getPath() end)
    context.catalog_path = ok and path or nil
    local captures = {
        sources = function()
            if not catalog.getActiveSources then error('unsupported') end
            local sources = array(catalog:getActiveSources())
            if #sources == 0 then error('empty sources') end
            context.sources = sources
        end,
        selection = function()
            if not catalog.getTargetPhoto then error('unsupported') end
            context.target = catalog:getTargetPhoto()
            -- Without a target getTargetPhotos means all visible photos,
            -- rather than an empty selection. Never save that as a selection.
            context.selected = {}
            if context.target then
                if not catalog.getTargetPhotos then error('unsupported') end
                context.selected = array(catalog:getTargetPhotos())
                local present = false
                for _, photo in ipairs(context.selected) do if photo == context.target then present = true end end
                if not present then error('incomplete selection') end
            end
        end,
        module = function()
            if not LrApplicationView.getCurrentModuleName then error('unsupported') end
            context.module = LrApplicationView.getCurrentModuleName()
            if type(context.module) ~= 'string' or context.module == '' then error('unknown module') end
        end,
    }
    for _, part in ipairs(parts) do
        local captured = LrTasks.pcall(captures[part])
        context.report.parts[part].status = captured and 'pending' or 'unavailable'
        if not captured then warning(context.report, part, 'snapshot_unavailable') end
    end
    return context
end

function UiContext.catalogMatches(context)
    local ok, matches = LrTasks.pcall(function()
        local active = LrApplication.activeCatalog()
        return active ~= nil and context.catalog_path ~= nil and active:getPath() == context.catalog_path
    end)
    return ok and matches
end

local function selectionMatches(context)
    if context.catalog:getTargetPhoto() ~= context.target then return false end
    if not context.target then return true end
    return sameMembers(context.catalog:getTargetPhotos(), context.selected)
end

function UiContext.noteSelection(context)
    local ok, selection = LrTasks.pcall(function()
        local target = context.catalog:getTargetPhoto()
        return { target = target, selected = target and array(context.catalog:getTargetPhotos()) or {} }
    end)
    context.expected_selection = ok and selection or nil
end

local function stillOwned(context)
    local catalog = context.catalog
    if context.report.parts.sources.status ~= 'unavailable' then
        local current = catalog:getActiveSources()
        if not sameMembers(current, context.sources)
                and not sameMembers(current, context.expected_sources) then return false end
    end
    if context.report.parts.module.status ~= 'unavailable' then
        local current = LrApplicationView.getCurrentModuleName()
        if current ~= context.module and current ~= context.expected_module then return false end
    end
    if context.report.parts.selection.status ~= 'unavailable' and not selectionMatches(context) then
        local target = catalog:getTargetPhoto()
        local expected = context.expected_selection
        if expected and target == expected.target
                and (not target or sameMembers(catalog:getTargetPhotos(), expected.selected)) then return true end
        local owned = false
        for _, photo in ipairs(context.allowed_targets) do if target == photo then owned = true end end
        if not owned then return false end
        if not sameMembers(catalog:getTargetPhotos(), { target }) then return false end
    end
    return true
end

function UiContext.owns(context)
    local ok, owned = LrTasks.pcall(function() return stillOwned(context) end)
    return ok and owned
end

function UiContext.restore(context)
    local report = context.report
    local function skip(code)
        for _, part in ipairs(parts) do
            if report.parts[part].status == 'pending' then report.parts[part].status = 'skipped' end
        end
        warning(report, 'all', code)
        report.status = 'unavailable'
        return report
    end
    if not UiContext.catalogMatches(context) then return skip('catalog_changed') end
    local guarded, owned = LrTasks.pcall(function() return stillOwned(context) end)
    if not guarded then return skip('ownership_unavailable') end
    if not owned then return skip('user_interference') end
    local catalog = context.catalog
    local restorers = {
        sources = function()
            if sameMembers(catalog:getActiveSources(), context.sources) then return true end
            if not catalog.setActiveSources then return false, 'restore_unavailable' end
            if catalog:setActiveSources(context.sources) == false then return false, 'restore_failed' end
            -- Source changes themselves can clear/reselect the target. This
            -- immediate consequence belongs to our setter, not another user.
            UiContext.noteSelection(context)
            return true
        end,
        selection = function()
            if selectionMatches(context) then return true end
            if context.target then
                if not catalog.setSelectedPhotos then return false, 'restore_unavailable' end
                local others = {}
                for _, photo in ipairs(context.selected) do
                    if photo ~= context.target then others[#others + 1] = photo end
                end
                catalog:setSelectedPhotos(context.target, others)
            else
                if not LrSelection.selectNone then return false, 'restore_unavailable' end
                LrSelection.selectNone()
            end
            UiContext.noteSelection(context)
            return true
        end,
        module = function()
            if LrApplicationView.getCurrentModuleName() == context.module then return true end
            if not LrApplicationView.switchToModule then return false, 'restore_unavailable' end
            LrApplicationView.switchToModule(context.module)
            return true
        end,
    }
    local checks = {
        sources = function() return sameMembers(catalog:getActiveSources(), context.sources) end,
        selection = function() return selectionMatches(context) end,
        module = function() return LrApplicationView.getCurrentModuleName() == context.module end,
    }
    for _, part in ipairs(parts) do
        if report.parts[part].status == 'pending' then
            if not UiContext.catalogMatches(context) then return skip('catalog_changed') end
            local guarded, owned = LrTasks.pcall(function() return stillOwned(context) end)
            if not guarded then return skip('ownership_unavailable') end
            if not owned then return skip('user_interference') end
            if part == 'selection' and context.expected_sources
                    and report.parts.sources.status == 'not_restored' then
                report.parts.selection.status = 'skipped'
                warning(report, 'selection', 'restore_dependency_failed')
            else
                local ok, restored, reason = LrTasks.pcall(restorers[part])
                if ok and restored then
                    local verified = false
                    for attempt = 1, 10 do
                        if not UiContext.catalogMatches(context) then return skip('catalog_changed') end
                        local readOk, matches = LrTasks.pcall(checks[part])
                        if readOk and matches then verified = true; break end
                        if attempt < 10 then
                            LrTasks.sleep(0.05)
                            if part == 'selection' then
                                -- LR may silently ignore selection while the
                                -- restored source/filmstrip is still settling.
                                -- Repeat only this UI setter, never creation.
                                if not UiContext.catalogMatches(context) then return skip('catalog_changed') end
                                local guarded, owned = LrTasks.pcall(function() return stillOwned(context) end)
                                if not guarded then return skip('ownership_unavailable') end
                                if not owned then return skip('user_interference') end
                                local retryOk, retryRestored = LrTasks.pcall(restorers.selection)
                                if not retryOk or not retryRestored then break end
                            end
                        end
                    end
                    report.parts[part].status = verified and 'restored' or 'not_restored'
                    if not verified then warning(report, part, 'readback_mismatch') end
                    if verified then
                        if part == 'sources' then context.expected_sources = context.sources end
                        if part == 'selection' then context.allowed_targets = {}; context.expected_selection = nil end
                        if part == 'module' then context.expected_module = context.module end
                    end
                else
                    report.parts[part].status = 'not_restored'
                    warning(report, part, ok and reason or 'restore_failed')
                end
            end
        end
    end
    -- Switching modules can affect sources or selection: verify the final
    -- combined state, instead of claiming success from setter return values.
    local restoredCount = 0
    for _, part in ipairs(parts) do
        if report.parts[part].status == 'restored' then
            local ok, matches = LrTasks.pcall(checks[part])
            if not ok or not matches then
                report.parts[part].status = 'not_restored'
                warning(report, part, 'readback_mismatch')
            else restoredCount = restoredCount + 1 end
        end
    end
    report.status = restoredCount == #parts and 'restored' or (restoredCount > 0 and 'partial' or 'unavailable')
    return report
end

return UiContext
