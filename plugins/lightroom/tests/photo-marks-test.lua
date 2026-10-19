local plugin = assert(arg[1])
package.path = plugin .. '/?.lua;' .. package.path
local catalog, depth, writes, hook, afterGate, deferred
catalog = { photos={}, path='/tmp/tonerelay-marks-fixture/test.lrcat' }
function catalog:getPath() return self.path end
function catalog:findPhotoByUuid(id) return self.photos[id] end
function catalog:getLabelMapToColorName()
    if self.reverseMap then
        local reverse={};for color,label in pairs(self.labelMap) do reverse[label]=color end
        return reverse
    end
    return self.labelMap
end
function catalog:withWriteAccessDo(_, fn)
    if hook then local f=hook; hook=nil; f() end
    depth=depth+1; fn(); depth=depth-1
    for _, p in pairs(self.photos) do
        if p.pending then p.metadata=p.pending; p.pending=nil end
    end
    if afterGate then local f=afterGate; afterGate=nil; f() end
    return 'executed'
end
function import(name)
    if name == 'LrApplication' then return {activeCatalog=function() return catalog end} end
    if name == 'LrTasks' then return {pcall=pcall} end
    error('Unavailable namespace: ' .. name)
end
local Marks = require 'PhotoMarks'
local Safety = require 'Safety'
local function check(value, message) if not value then error(message or 'assertion failed', 2) end end
local function photo(id, metadata)
    local p = { metadata=metadata or {}, id=id, develop='untouched' }
    function p:getRawMetadata(key)
        if key == 'label' then error('Unknown key: "label"') end
        return self.metadata[key]
    end
    function p:getFormattedMetadata(key)
        check(key=='label', 'Unexpected formatted metadata key')
        return self.metadata.label
    end
    function p:setRawMetadata(key, value)
        check(depth > 0, 'SDK write outside gate')
        writes=writes+1
        if self.fail == key then error('setter failed') end
        if self.ignore == key then return end
        if key == 'rating' then check(value == nil or value >= 1, 'zero must map to nil') end
        local target=self.metadata
        if deferred then
            if not self.pending then
                self.pending={};for k,v in pairs(self.metadata) do self.pending[k]=v end
            end
            target=self.pending
        end
        target[key]=value
        if key == 'colorNameForLabel' then target.label=value == 'none' and '' or catalog.labelMap[value] end
        if key == 'label' then
            target.colorNameForLabel=value=='' and 'none' or 'white'
            for color,label in pairs(catalog.labelMap) do if value==label then target.colorNameForLabel=color end end
        end
    end
    catalog.photos[id]=p
    return p
end
local function envelope(params)
    params.expected_access_mode='full'
    params.expected_catalog_identity=Safety.catalogIdentity(catalog.path)
    return params
end
local function set(id, patch, mode, token)
    return Marks.set(envelope({protection_mode=mode or 'skip_marked_photos',items={
        {photo_uuid=id,patch=patch,expected_marks_token=token or Marks.snapshot(catalog.photos[id],id).marks_token}
    }})).results[1]
end
local tests=0
local function test(name, fn)
    catalog.photos={}; depth=0; writes=0; hook=nil; afterGate=nil; deferred=false;catalog.reverseMap=false
    catalog.labelMap={red='localized-red',yellow='localized-yellow',green='localized-green',blue='localized-blue',purple='localized-purple'}
    Safety.setAccessMode('full')
    local ok, err=pcall(fn)
    if not ok then error(name .. ': ' .. tostring(err), 0) end
    tests=tests+1; print('ok - ' .. name)
end

test('SDK deferred commit records confirmed fields and supports exact undo', function()
    deferred=true
    local p=photo('p',{colorNameForLabel='gray',label=''})
    local row=set('p',{rating=3,flag='pick',color_label='green'})
    check(row.status=='applied' and #row.changed_fields==3 and row.after.rating==3)
    local undone=Marks.restore(envelope({items={{photo_uuid='p',before=row.before,after=row.after,fields=row.changed_fields}}})).results[1]
    check(undone.status=='applied' and #undone.changed_fields==3)
    check(p.metadata.rating==nil and p.metadata.pickStatus==0 and p.metadata.label=='')
end)
test('current custom color label map defines intended assigned text', function()
    catalog.labelMap.green='客户通过'
    photo('p')
    local row=set('p',{color_label='green'})
    check(row.status=='applied' and row.after.label_text=='客户通过' and row.readback.label_text=='客户通过')
end)
test('SDK label-to-color map orientation also defines intended text', function()
    catalog.reverseMap=true;catalog.labelMap.green='绿色'
    photo('p')
    local row=set('p',{color_label='green'})
    check(row.status=='applied' and row.after.label_text=='绿色' and row.readback.label_text=='绿色')
end)
test('empty SDK white/grey labels are writable; assigned custom labels stay protected', function()
    for _, color in ipairs({'white','grey','gray',''}) do
        photo('p',{colorNameForLabel=color,label=''})
        local before=Marks.snapshot(catalog.photos.p,'p')
        check(before.color_label=='none' and not before.has_any_marks)
        check(before.marks_token:find(':native=' .. color,1,true))
        check(set('p',{rating=3}).status=='applied')
        photo('p',{colorNameForLabel=color,label='client-label'})
        check(set('p',{rating=3}).status=='skipped')
    end
end)
test('mode 1 protects ratings, either flag, named colors and custom white labels', function()
    for _, metadata in ipairs({{rating=3},{pickStatus=1},{pickStatus=-1},
        {colorNameForLabel='red',label='localized-red'}, {colorNameForLabel='none',label='my-client'}}) do
        photo('p',metadata)
        local row=set('p',{rating=5,flag='pick',color_label='green'})
        check(row.status=='skipped' and writes==0)
    end
end)
test('mode 2 preserves stars and color while adding an empty flag', function()
    local p=photo('p',{rating=3,label='localized-red',colorNameForLabel='red'})
    local row=set('p',{rating=5,flag='pick',color_label='green'},'fill_empty_fields')
    check(p.metadata.rating==3 and p.metadata.pickStatus==1 and p.metadata.label=='localized-red')
    check(row.status=='applied' and #row.changed_fields==1 and row.changed_fields[1]=='flag')
    check(#row.skipped_fields==2 and p.develop=='untouched' and writes==1)
end)
test('mode 2 protects custom labels even when SDK reports none', function()
    local p=photo('p',{label='customer',colorNameForLabel='none'})
    local row=set('p',{rating=4,color_label='green'},'fill_empty_fields')
    check(p.metadata.rating==4 and p.metadata.label=='customer' and writes==1)
    check(row.after.color_label==nil and row.after.color_label_status=='unmapped')
end)
test('mode 2 protects both flag states while filling stars and color', function()
    for _, flag in ipairs({1,-1}) do
        local p=photo('p',{pickStatus=flag})
        local row=set('p',{rating=4,flag='unflagged',color_label='yellow'},'fill_empty_fields')
        check(p.metadata.pickStatus==flag and p.metadata.rating==4 and p.metadata.label=='localized-yellow')
        check(row.status=='applied' and #row.changed_fields==2 and row.skipped_fields[1]=='flag')
    end
end)
test('empty values never clear any existing mark in mode 2', function()
    local p=photo('p',{rating=3,pickStatus=-1,label='localized-red',colorNameForLabel='red'})
    local row=set('p',{rating=0,flag='unflagged',color_label='none'},'fill_empty_fields')
    check(row.status=='skipped' and #row.skipped_fields==3 and writes==0)
    check(p.metadata.rating==3 and p.metadata.pickStatus==-1 and p.metadata.label=='localized-red')
end)
test('write gate rechecks concurrent marks against the read token', function()
    local p=photo('p')
    local token=Marks.snapshot(p,'p').marks_token
    hook=function() p.metadata.pickStatus=-1 end
    check(set('p',{rating=4},'fill_empty_fields',token).status=='conflict' and writes==0)
end)
test('all three fields on an unmarked master and exact virtual copy isolation', function()
    local master=photo('master'); local copy=photo('copy')
    local row=set('master',{rating=4,flag='pick',color_label='green'})
    check(row.status=='applied' and #row.changed_fields==3 and copy.metadata.rating==nil)
    check(master.develop=='untouched' and copy.develop=='untouched')
end)
test('invalid batch fails before any setter', function()
    photo('p')
    local ok=pcall(function() set('p',{rating=6}) end)
    check(not ok and writes==0)
    ok=pcall(function() set('p',{rating=3},'overwrite') end)
    check(not ok and writes==0)
    ok=pcall(function() Marks.set(envelope({protection_mode='skip_marked_photos',items={
        {photo_uuid='p',patch={rating=4},expected_marks_token=Marks.snapshot(catalog.photos.p,'p').marks_token},
        {photo_uuid='second',patch={flag='invalid'},expected_marks_token='snapshot'}
    }})) end)
    check(not ok and writes==0)
end)
test('setter mismatch is visible', function()
    local p=photo('p'); p.ignore='rating'
    local row=set('p',{rating=4})
    check(row.status=='mismatch' and #row.readback_mismatches==1)
end)
test('partial setter failure records actual earlier changes', function()
    local p=photo('p'); p.fail='pickStatus'
    local row=set('p',{rating=4,flag='pick'})
    check(row.status=='failed' and row.after.rating==4 and #row.changed_fields==1)
    check(row.changed_fields[1]=='rating' and p.metadata.pickStatus==nil)
end)
test('post-gate user changes cannot become operation-owned undo values', function()
    local p=photo('p')
    afterGate=function() p.metadata.rating=5 end
    local row=set('p',{rating=4})
    check(row.status=='mismatch' and row.after.rating==4 and row.readback.rating==5)
    check(#row.changed_fields==0 and p.metadata.rating==5 and writes==1)
end)
test('ignored setter cannot claim a subsequent user change', function()
    local p=photo('p'); p.ignore='rating'
    afterGate=function() p.metadata.rating=5 end
    local row=set('p',{rating=4})
    check(row.status=='mismatch' and row.after.rating==4 and row.readback.rating==5)
    check(#row.changed_fields==0)
end)
test('undo clears stars with nil and preserves later changes to unrelated fields', function()
    local p=photo('p')
    local row=set('p',{rating=4})
    p.metadata.pickStatus=-1
    local result=Marks.restore(envelope({items={{photo_uuid='p',before=row.before,after=row.after,fields=row.changed_fields}}})).results[1]
    check(result.status=='applied' and p.metadata.rating==nil and p.metadata.pickStatus==-1)
end)
test('undo skips conflicting fields and restores remaining owned fields', function()
    local p=photo('p')
    local row=set('p',{rating=4,flag='pick',color_label='green'})
    p.metadata.rating=5
    local result=Marks.restore(envelope({items={{photo_uuid='p',before=row.before,after=row.after,fields=row.changed_fields}}})).results[1]
    check(p.metadata.rating==5 and p.metadata.pickStatus==0 and p.metadata.label=='')
    check(#result.skipped_fields==1 and #result.changed_fields==2)
end)
test('safe and identity guards block before any setter', function()
    photo('p')
    Safety.setAccessMode('safe')
    check(set('p',{rating=4}).status=='failed' and writes==0)
    Safety.setAccessMode('full')
    local args=envelope({protection_mode='skip_marked_photos',items={{photo_uuid='p',patch={rating=4},expected_marks_token=Marks.snapshot(catalog.photos.p,'p').marks_token}}})
    args.expected_catalog_identity='wrong'
    check(Marks.set(args).results[1].status=='failed' and writes==0)
end)
test('batch reports missing photo while continuing valid explicit UUIDs', function()
    photo('p')
    local rows=Marks.set(envelope({protection_mode='skip_marked_photos',items={
        {photo_uuid='missing',expected_marks_token='none',patch={rating=4}},
        {photo_uuid='p',expected_marks_token=Marks.snapshot(catalog.photos.p,'p').marks_token,patch={flag='reject'}}
    }})).results
    check(rows[1].status=='failed' and rows[2].status=='applied' and writes==1)
end)
print('passed ' .. tests .. ' photo marks tests')
