-- Read the per-photo SDK result, never infer success from session completion
-- or guess Lightroom's filename. Failed artifacts remain in bridge-owned
-- staging for diagnosis; this module never deletes files or Catalog photos.
local LrFileUtils = import 'LrFileUtils'
local LrPathUtils = import 'LrPathUtils'
local ExportResult = {}

local function comparable(path)
    if type(path) ~= 'string' or path == '' then return nil end
    path = path:gsub('\\', '/'):gsub('/+$', '')
    for component in path:gmatch('[^/]+') do
        if component == '.' or component == '..' then return nil end
    end
    -- Windows drive and UNC paths are case insensitive. POSIX paths retain
    -- their case; do not alter Unicode names or rely on the host test OS.
    if path:match('^%a:/') or path:match('^//') then path = path:lower() end
    return path
end

function ExportResult.render(session, photo, outputDir)
    local count, outputPath = 0, nil
    for _, rendition in session:renditions() do
        count = count + 1
        if count > 1 then error('export_preview: unexpected_rendition_count') end
        if rendition.photo ~= photo then error('export_preview: rendition_photo_mismatch') end
        local success, pathOrMessage = rendition:waitForRender()
        if not success then
            error('export_preview: render_failed: ' .. tostring(pathOrMessage))
        end
        if rendition.wasSkipped then error('export_preview: render_skipped') end
        if type(pathOrMessage) ~= 'string' or pathOrMessage == '' then
            error('export_preview: missing_rendition_path')
        end
        local actual, expected = comparable(LrPathUtils.parent(pathOrMessage)), comparable(outputDir)
        if not actual or not expected or actual ~= expected then
            error('export_preview: output_directory_mismatch: ' .. pathOrMessage)
        end
        if not pathOrMessage:lower():match('%.jpe?g$') then
            error('export_preview: output_format_mismatch: ' .. pathOrMessage)
        end
        if LrFileUtils.exists(pathOrMessage) ~= 'file' then
            error('export_preview: rendered_file_missing: ' .. pathOrMessage)
        end
        local attributes = LrFileUtils.fileAttributes(pathOrMessage)
        if not attributes or type(attributes.fileSize) ~= 'number' or attributes.fileSize <= 0 then
            error('export_preview: rendered_file_empty_or_unreadable: ' .. pathOrMessage)
        end
        outputPath = pathOrMessage
    end
    if count ~= 1 then error('export_preview: unexpected_rendition_count') end
    return outputPath
end

return ExportResult
