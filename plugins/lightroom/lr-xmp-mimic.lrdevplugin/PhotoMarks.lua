-- Catalog organization only. Never changes Develop settings or selection.
local LrApplication = import 'LrApplication'
local LrTasks = import 'LrTasks'
local Safety = require 'Safety'
local Json = require 'Json'
local Marks = {}
local colors = { none=true, red=true, yellow=true, green=true, blue=true, purple=true }
local flags = { pick=1, unflagged=0, reject=-1 }
local fields = { 'rating', 'flag', 'color_label' }

local function nonempty(value, name)
    if type(value) ~= 'string' or value == '' or #value > 4096 then
        error(name .. ' must be a non-empty bounded string')
    end
end

local function array(value)
    if type(value) ~= 'table' or #value < 1 or #value > 200 then
        error('Expected an array of 1 to 200 photos')
    end
    for k in pairs(value) do
        if type(k) ~= 'number' or k < 1 or k > #value or k ~= math.floor(k) then
            error('Expected a dense photo array')
        end
    end
end

local function patch(value)
    if type(value) ~= 'table' or next(value) == nil then error('Empty marks patch') end
    for k, v in pairs(value) do
        if k == 'rating' then
            if type(v) ~= 'number' or v ~= math.floor(v) or v < 0 or v > 5 then
                error('rating must be an integer from 0 to 5')
            end
        elseif k == 'flag' then
            if flags[v] == nil then error('Invalid flag') end
        elseif k == 'color_label' then
            if colors[v] ~= true then error('Invalid color_label') end
        else error('Unsupported marks field: ' .. tostring(k)) end
    end
end

local function snapshotValues(uuid, rating, pick, label, color)
    if type(rating) ~= 'number' or rating ~= math.floor(rating) or rating < 0 or rating > 5
        or (pick ~= -1 and pick ~= 0 and pick ~= 1) or type(label) ~= 'string'
        or type(color) ~= 'string' then error('Unsupported Lightroom mark metadata') end
    color = string.lower(color)
    local nativeColor = color
    -- Recent SDKs can report white/grey (or an empty string) for no label.
    -- Only an empty assigned label permits this normalization: nonempty
    -- custom/white labels remain protected, even with these native colors.
    if label == '' and (color == 'white' or color == 'grey' or color == 'gray' or color == '') then
        color = 'none'
    end
    local status = 'mapped'
    if not colors[color] or (color == 'none' and label ~= '') then
        color, status = nil, 'unmapped'
    elseif color == 'none' then status = 'empty' end
    -- A value snapshot, not an SDK revision or a security token. Include raw
    -- label text so white/custom labels and changed label sets stay protected.
    local token = 'marks-v1:' .. tostring(rating) .. ':' .. tostring(pick)
        .. ':' .. #label .. ':' .. label .. ':' .. (color or '?') .. ':native=' .. nativeColor
    return { photo_uuid=uuid, rating=rating,
        flag=pick == 1 and 'pick' or (pick == -1 and 'reject' or 'unflagged'),
        color_label=color, color_label_status=status, label_text=label,
        marks_token=token,
        has_any_marks=rating > 0 or pick ~= 0 or label ~= '' or color ~= 'none' }
end

function Marks.snapshot(photo, uuid)
    -- Assigned label text is formatted metadata, not a raw SDK key.
    return snapshotValues(uuid, photo:getRawMetadata('rating') or 0,
        photo:getRawMetadata('pickStatus') or 0,
        photo:getFormattedMetadata('label') or '',
        photo:getRawMetadata('colorNameForLabel') or 'none')
end

local function empty(snapshot, field)
    if field == 'rating' then return snapshot.rating == 0 end
    if field == 'flag' then return snapshot.flag == 'unflagged' end
    return snapshot.color_label == 'none' and snapshot.label_text == ''
end

local function find(catalog, uuid)
    local photo = catalog:findPhotoByUuid(uuid)
    if not photo then error('Photo not found') end
    return photo
end

function Marks.get(params)
    array(params.photo_uuids)
    local seen, results = {}, Json.array({})
    for _, uuid in ipairs(params.photo_uuids) do
        nonempty(uuid, 'photo_uuid')
        if seen[uuid] then error('Duplicate photo_uuid') end
        seen[uuid] = true
    end
    local catalog = LrApplication.activeCatalog()
    for _, uuid in ipairs(params.photo_uuids) do
        local ok, value = LrTasks.pcall(function() return Marks.snapshot(find(catalog, uuid), uuid) end)
        results[#results+1] = ok and { photo_uuid=uuid, status='read', marks=value }
            or { photo_uuid=uuid, status='failed', error=tostring(value) }
    end
    return { results=results }
end

local function write(photo, field, value, restoring)
    if field == 'rating' then
        local native = value
        if value == 0 then native = nil end
        photo:setRawMetadata('rating', native)
    elseif field == 'flag' then photo:setRawMetadata('pickStatus', flags[value])
    else photo:setRawMetadata('label', value) end
end

local function same(a, b, field)
    if field == 'color_label' then return a.label_text == b.label_text end
    return a[field] == b[field]
end

local function validateSnapshot(value)
    if type(value) ~= 'table' or type(value.label_text) ~= 'string' then
        error('Invalid restore snapshot')
    end
    patch({rating=value.rating, flag=value.flag})
end

local function apply(params, restoring)
    array(params.items)
    if not restoring and params.protection_mode ~= 'skip_marked_photos'
        and params.protection_mode ~= 'fill_empty_fields' then error('Invalid protection_mode') end
    local seen = {}
    for _, item in ipairs(params.items) do
        if type(item) ~= 'table' then error('Invalid marks item') end
        nonempty(item.photo_uuid, 'photo_uuid')
        if seen[item.photo_uuid] then error('Duplicate photo_uuid') end
        seen[item.photo_uuid] = true
        if restoring then
            validateSnapshot(item.before)
            validateSnapshot(item.after)
            array(item.fields)
            local selected = {}
            for _, field in ipairs(item.fields) do
                if (field ~= 'rating' and field ~= 'flag' and field ~= 'color_label')
                    or selected[field] then error('Invalid restore fields') end
                selected[field] = true
            end
        else
            nonempty(item.expected_marks_token, 'expected_marks_token')
            patch(item.patch)
        end
    end
    local catalog = LrApplication.activeCatalog()
    local results = Json.array({})
    local gate = catalog:withWriteAccessDo('ToneRelay: photo marks', function()
        for _, item in ipairs(params.items) do
            local row = { photo_uuid=item.photo_uuid, changed_fields=Json.array({}),
                skipped_fields=Json.array({}), readback_mismatches=Json.array({}) }
            results[#results+1] = row
            local ok, err = LrTasks.pcall(function()
                -- Gate acquisition may yield. Recheck the exact Catalog at the
                -- execution point, not only in Bridge's pre-dispatch guard.
                local active = LrApplication.activeCatalog()
                if active ~= catalog then error('Active Catalog changed') end
                Safety.authorize(restoring and 'restore_photo_marks' or 'set_photo_marks',
                    params, catalog:getPath())
                local photo = find(catalog, item.photo_uuid)
                row.before = Marks.snapshot(photo, item.photo_uuid)
                if not restoring and row.before.marks_token ~= item.expected_marks_token then
                    row.status = 'conflict'; return
                end
                if not restoring and params.protection_mode == 'skip_marked_photos'
                    and row.before.has_any_marks then row.status='skipped'; row.reason='photo_already_marked'; return end
                row.attempted_fields = Json.array({})
                row.accepted_fields = Json.array({})
                local projected = { rating=row.before.rating, flag=row.before.flag,
                    label=row.before.label_text, color=photo:getRawMetadata('colorNameForLabel') or 'none' }
                row.after = row.before
                for _, field in ipairs(fields) do
                    local wanted
                    if restoring then
                        for _, selected in ipairs(item.fields) do if selected == field then
                            wanted = field == 'color_label' and item.before.label_text or item.before[field]
                        end end
                        if wanted ~= nil and not same(row.before, item.after, field) then
                            row.skipped_fields[#row.skipped_fields+1]=field; wanted=nil
                        end
                    else
                        wanted = item.patch[field]
                        if wanted ~= nil and not empty(row.before, field) then
                            row.skipped_fields[#row.skipped_fields+1]=field; wanted=nil
                        end
                    end
                    if wanted ~= nil then
                        local old = restoring and field == 'color_label' and row.before.label_text or row.before[field]
                        if old ~= wanted then
                            local assignedLabel
                            if field == 'color_label' then
                                if restoring then assignedLabel = wanted
                                elseif wanted == 'none' then assignedLabel = ''
                                else
                                    local labelMap = catalog:getLabelMapToColorName()
                                    assignedLabel = labelMap and labelMap[wanted]
                                    if type(assignedLabel) ~= 'string' or assignedLabel == '' then
                                        -- SDK releases also return label->color,
                                        -- matching the method name. Accept both
                                        -- documented and observed orientations.
                                        for label, color in pairs(labelMap or {}) do
                                            if color == wanted and type(label) == 'string' and label ~= '' then
                                                assignedLabel = label; break
                                            end
                                        end
                                    end
                                    if type(assignedLabel) ~= 'string' or assignedLabel == '' then
                                        error('Current label set does not map requested color: ' .. Json.encode(labelMap))
                                    end
                                end
                            end
                            row.attempted_fields[#row.attempted_fields+1]=field
                            write(photo, field, assignedLabel or wanted, restoring)
                            row.accepted_fields[#row.accepted_fields+1]=field
                            if field == 'color_label' then
                                projected.label = assignedLabel
                                projected.color = restoring and (item.before.color_label or 'white') or wanted
                            else projected[field] = wanted end
                            -- SDK setters commit when the gate closes. Capture
                            -- intended values from accepted setters, never a
                            -- post-gate user's edit or an inside-gate stale read.
                            row.after = snapshotValues(item.photo_uuid, projected.rating,
                                flags[projected.flag], projected.label, projected.color)
                        end
                    end
                end
                row.status = 'written'
            end)
            if not ok then row.status='failed'; row.error=tostring(err) end
        end
    end, { timeout=30 })
    if gate ~= nil and gate ~= 'executed' then error('Catalog marks write gate did not execute') end
    -- Read back after the gate closes. Setters can fail after earlier fields
    -- have committed; record those facts rather than claiming a rollback.
    for index, row in ipairs(results) do
        if row.attempted_fields and row.after then
            local item = params.items[index]
            local ok, after = LrTasks.pcall(function()
                if LrApplication.activeCatalog() ~= catalog then error('Active Catalog changed') end
                return Marks.snapshot(find(catalog, row.photo_uuid), row.photo_uuid)
            end)
            if not ok then row.status='unknown'; row.error=tostring(after)
            else
                row.readback=after
                for _, field in ipairs(row.accepted_fields) do
                    if same(row.after, after, field) and not same(row.before, after, field) then
                        row.changed_fields[#row.changed_fields+1]=field
                    end
                end
                for _, field in ipairs(row.attempted_fields) do
                    local expected = restoring and (field == 'color_label' and item.before.label_text or item.before[field])
                        or item.patch[field]
                    local actual = restoring and field == 'color_label' and after.label_text or after[field]
                    if actual ~= expected or not same(row.after, after, field) then
                        row.readback_mismatches[#row.readback_mismatches+1]=field
                    end
                end
                if row.status ~= 'failed' then
                    if #row.readback_mismatches > 0 then row.status='mismatch'
                    elseif #row.changed_fields > 0 then
                        row.status=restoring and #row.skipped_fields > 0 and 'partial' or 'applied'
                    elseif #row.skipped_fields > 0 then row.status=restoring and 'conflict' or 'skipped'
                    else row.status='unchanged' end
                end
            end
        end
    end
    return { results=results }
end

function Marks.set(params) return apply(params, false) end
function Marks.restore(params) return apply(params, true) end
return Marks
