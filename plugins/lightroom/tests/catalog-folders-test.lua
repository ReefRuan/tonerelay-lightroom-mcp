local plugin=assert(arg[1]);package.path=plugin..'/?.lua;'..package.path
local catalog={}
function import(name)
    if name=='LrApplication' then return {activeCatalog=function() return catalog end} end
    if name=='LrTasks' then return {pcall=pcall} end
    error('Unexpected SDK import '..name)
end
package.preload.Safety=function() return {
    isEnvelopeField=function(k) return k=='expected_catalog_identity' end,
    validateEnvelope=function(a) assert(a.expected_catalog_identity=='authorized') end,
    folderIdentity=function(_,path) return 'folder-md5-v1:'..path end,
} end
local function photo(id,rating,flag,color,virtual,video)
    local values={uuid=id,rating=rating,pickStatus=flag,isVirtualCopy=virtual,isVideo=video,dimensions={width=6000,height=4000}}
    return {getRawMetadata=function(_,key) return values[key] end,
            getFormattedMetadata=function(_,key) return key=='fileName' and id..'.jpg' or key=='label' and (color or '') or '' end}
end
local function folder(id,name,photos,children)
    return {getPath=function() return id end,getName=function() return name end,
            getPhotos=function(_,recursive) assert(recursive==false);return photos end,
            getChildren=function() return children end}
end
local p1,p2,p3=photo('p1',nil,1,'green'),photo('p2',nil,-1,'red'),photo('p3',3,0,'')
local vc=photo('vc',nil,0,'',true)
local video=photo('video',nil,0,'',false,true)
local a=folder('aa','普通',{p1,p3,video},{})
local b=folder('bb','普通',{p2,vc},{})
local root=folder('cc','2026',{}, {a,b})
function catalog:getFolders() return {root} end
function catalog:getPath() return 'catalog' end
function catalog:withReadAccessDo(fn) fn() end
function catalog:getTargetPhoto() error('Folder queries must not depend on selection') end
local F=require 'CatalogFolders'
local function args(t) t.expected_catalog_identity='authorized';return t end
local tree=F.list(args({name_contains='2026'}))
assert(tree.total==1 and tree.folders[1].direct_photo_count==0 and tree.folders[1].subtree_photo_count==4)
local leaves=F.list(args({parent_id='folder-md5-v1:cc'}))
assert(leaves.total==2 and leaves.folders[1].label==leaves.folders[2].label)
assert(leaves.folders[1].id~=leaves.folders[2].id)
local candidates=F.photos(args({folder_ids={'folder-md5-v1:cc','folder-md5-v1:aa'},include_subfolders=true,rating=0}))
assert(candidates.count==3) -- Parent/child overlap deduplicated, video and rated photo excluded.
assert(candidates.photos[1].uuid=='p1' and candidates.photos[1].flag=='pick' and candidates.photos[1].label_text=='green')
local masters=F.photos(args({folder_ids={'folder-md5-v1:cc'},include_subfolders=true,rating=0,include_virtual_copies=false}))
assert(masters.count==2 and masters.photos[2].flag=='reject')
local empty=F.photos(args({folder_ids={'folder-md5-v1:cc'}}));assert(empty.count==0)
assert(not pcall(function() F.photos(args({folder_ids={'not-a-folder'}})) end))
assert(not pcall(function() F.list({}) end))
assert(not pcall(function() F.photos(args({folder_ids={'folder-md5-v1:aa'},rating=true})) end))
print('passed Catalog folder tree, count, scope, deduplication, rating and selection-isolation checks')
