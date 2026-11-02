-- Catalog folders and fixed candidate inventories; never alter UI selection.
local LrApplication = import 'LrApplication'
local LrTasks = import 'LrTasks'
local Safety = require 'Safety'
local PhotoRecord = require 'PhotoRecord'
local Json = require 'Json'
local Folders = {}
local MAX_FOLDERS, MAX_PHOTOS = 10000, 100000

local function read(catalog,fn)
    local ran=false
    local ok=LrTasks.pcall(function()
        catalog:withReadAccessDo(function() ran=true;fn() end)
    end)
    if not ok or not ran then error('Catalog folder metadata read failed') end
    if LrApplication.activeCatalog()~=catalog then error('Active Catalog changed during folder query') end
end

local function integer(value, low, high)
    return type(value)=='number' and value==value and value>=low and value<=high and value==math.floor(value)
end
local function validate(args, fields)
    if type(args)~='table' then error('folder arguments must be an object') end
    for key in pairs(args) do
        if not fields[key] and not Safety.isEnvelopeField(key) then error('Unknown folder query field') end
    end
    Safety.validateEnvelope(args)
end
local function label(folder)
    local name = folder:getName()
    if type(name)~='string' then error('Catalog folder label unavailable') end
    name = string.gsub(name, '[\\/]+$', '')
    return string.match(name, '([^\\/]+)$') or 'Volume'
end
local function inventory(catalog)
    local nodes, byId = {}, {}
    local function visit(folder, parent, level)
        if level>64 then error('Catalog folder tree exceeds depth limit') end
        local id = Safety.folderIdentity(catalog:getPath(), folder:getPath())
        if byId[id] then return byId[id] end
        if #nodes>=MAX_FOLDERS then error('Catalog folder tree exceeds folder limit; no complete inventory') end
        local path = {}
        if parent then
            for _,part in ipairs(parent.path_labels) do path[#path+1]=part end
        else
            local volume=string.match(folder:getPath(),'^([A-Za-z]:)') or string.match(folder:getPath(),'^/Volumes/([^/]+)')
            if volume then path[#path+1]=volume end
        end
        path[#path+1] = label(folder)
        local node = {id=id, label=path[#path], path_labels=path,
                      parent_id=parent and parent.id or nil, level=level,
                      folder=folder, children={}}
        nodes[#nodes+1]=node; byId[id]=node
        for _, child in ipairs(folder:getChildren()) do
            node.children[#node.children+1]=visit(child, node, level+1)
        end
        return node
    end
    for _,folder in ipairs(catalog:getFolders()) do visit(folder,nil,0) end
    return nodes,byId
end
local function public(node)
    return {id=node.id,parent_id=node.parent_id,label=node.label,path_labels=node.path_labels,
            child_count=#node.children,direct_photo_count=node.direct_photo_count,
            subtree_photo_count=node.subtree_photo_count,count_scope='catalog_photo_records',
            includes_virtual_copies=true,excludes_videos=true}
end
local function sortNodes(rows)
    table.sort(rows,function(a,b)
        local left,right=table.concat(a.path_labels,'/'),table.concat(b.path_labels,'/')
        if left~=right then return left<right end
        return a.id<b.id
    end)
end
function Folders.list(args)
    validate(args,{parent_id=true,name_contains=true,depth=true,limit=true,offset=true})
    local depth,limit,offset=args.depth or 1,args.limit or 100,args.offset or 0
    if not integer(depth,1,16) or not integer(limit,1,500) or not integer(offset,0,1000000) then error('Invalid folder pagination') end
    if args.parent_id~=nil and type(args.parent_id)~='string' then error('parent_id must be a folder ID') end
    local needle=args.name_contains or ''
    if type(needle)~='string' or #needle>256 then error('Invalid folder name filter') end
    local catalog=LrApplication.activeCatalog()
    local nodes,byId=inventory(catalog)
    if args.parent_id and not byId[args.parent_id] then error('Folder ID is not in the active Catalog') end
    local function counts(node)
        if node.direct_photo_count then return end
        local photos=node.folder:getPhotos(false)
        local seen,count={},0
        read(catalog,function()
            for _,photo in ipairs(photos) do
                local uuid=photo:getRawMetadata('uuid')
                if not seen[uuid] and photo:getRawMetadata('isVideo')~=true then seen[uuid]=true;count=count+1 end
            end
        end)
        node.direct_photo_count=count;node.subtree_photo_count=count
        for _,child in ipairs(node.children) do counts(child);node.subtree_photo_count=node.subtree_photo_count+child.subtree_photo_count end
    end
    local rows={}
    local function inside(node)
        if needle~='' then return string.find(string.lower(node.label),string.lower(needle),1,true)~=nil end
        local base=args.parent_id and byId[args.parent_id] or nil
        if not base then return node.level<depth end
        local current=node
        while current.parent_id do
            if current.parent_id==base.id then return node.level-base.level<=depth end
            current=byId[current.parent_id]
        end
        return false
    end
    for _,node in ipairs(nodes) do
        if inside(node) then
            if args.parent_id and needle~='' then
                local current=node
                while current and current.id~=args.parent_id do current=byId[current.parent_id] end
                if current then rows[#rows+1]=node end
            else rows[#rows+1]=node end
        end
    end
    sortNodes(rows)
    local result=Json.array()
    for index=offset+1,math.min(offset+limit,#rows) do counts(rows[index]);result[#result+1]=public(rows[index]) end
    return {folders=result,total=#rows,count=#result,limit=limit,offset=offset,has_more=offset+#result<#rows}
end

function Folders.photos(args)
    validate(args,{folder_ids=true,include_subfolders=true,rating=true,include_virtual_copies=true})
    if type(args.folder_ids)~='table' or #args.folder_ids<1 or #args.folder_ids>200 then error('Provide 1..200 folder IDs') end
    for key,id in pairs(args.folder_ids) do
        if not integer(key,1,#args.folder_ids) or type(id)~='string' then error('Invalid folder IDs') end
    end
    if args.rating~=nil and not integer(args.rating,0,5) then error('Invalid rating') end
    for _,key in ipairs({'include_subfolders','include_virtual_copies'}) do
        if args[key]~=nil and type(args[key])~='boolean' then error('Invalid folder query boolean') end
    end
    local catalog=LrApplication.activeCatalog()
    local _,byId=inventory(catalog)
    local selected={}
    local function select(node)
        if selected[node.id] then return end
        selected[node.id]=node
        if args.include_subfolders==true then for _,child in ipairs(node.children) do select(child) end end
    end
    for _,id in ipairs(args.folder_ids) do
        if not byId[id] then error('Folder ID is not in the active Catalog') end
        select(byId[id])
    end
    local ordered={};for _,node in pairs(selected) do ordered[#ordered+1]=node end;sortNodes(ordered)
    local photos,seen=Json.array(),{}
    for _,node in ipairs(ordered) do
        local candidates=node.folder:getPhotos(false)
        read(catalog,function()
            for _,photo in ipairs(candidates) do
                local record=PhotoRecord.snapshot(photo)
                if record and not seen[record.uuid] and (args.rating==nil or record.rating==args.rating)
                    and (args.include_virtual_copies~=false or not record.is_virtual_copy) then
                    if #photos>=MAX_PHOTOS then error('Folder query exceeds snapshot limit; narrow the scope') end
                    seen[record.uuid]=true;record.folder_id=node.id
                    local flag=photo:getRawMetadata('pickStatus') or 0
                    record.flag=flag==1 and 'pick' or flag==-1 and 'reject' or 'unflagged'
                    record.label_text=photo:getFormattedMetadata('label') or ''
                    photos[#photos+1]=record
                end
            end
        end)
    end
    table.sort(photos,function(a,b)
        if a.folder_id~=b.folder_id then return a.folder_id<b.folder_id end
        if a.file_name~=b.file_name then return a.file_name<b.file_name end
        return a.uuid<b.uuid
    end)
    local folders=Json.array();for _,node in ipairs(ordered) do folders[#folders+1]={id=node.id,label=node.label,path_labels=node.path_labels} end
    return {photos=photos,folders=folders,count=#photos,complete=true}
end
return Folders
