-- Minimal JSON encode/decode for the bridge wire format.
-- Only supports what jobs and results need: objects, arrays, strings,
-- numbers, booleans and null. Not a general-purpose library.
local Json = {}

local escapes = {
    ['"'] = '\\"',
    ['\\'] = '\\\\',
    ['\n'] = '\\n',
    ['\r'] = '\\r',
    ['\t'] = '\\t',
}

local function encodeString(value)
    return '"' .. string.gsub(value, '[%c"\\]', function(c)
        return escapes[c] or string.format('\\u%04x', string.byte(c))
    end) .. '"'
end

local function isMarkedArray(value)
    local metatable = getmetatable(value)
    return metatable ~= nil and metatable.__tonerelay_json_array == true
end

local function encode(value)
    local t = type(value)
    if t == 'nil' then
        return 'null'
    elseif t == 'boolean' or t == 'number' then
        return tostring(value)
    elseif t == 'string' then
        return encodeString(value)
    elseif t == 'table' then
        local isArray = #value > 0 or isMarkedArray(value)
        if isArray then
            local parts = {}
            for _, item in ipairs(value) do
                parts[#parts + 1] = encode(item)
            end
            return '[' .. table.concat(parts, ',') .. ']'
        end
        local parts = {}
        for key, item in pairs(value) do
            if type(key) == 'string' then
                parts[#parts + 1] = encodeString(key) .. ':' .. encode(item)
            end
        end
        return '{' .. table.concat(parts, ',') .. '}'
    end
    error('Json.encode: unsupported type ' .. t)
end

local function decodeError(text, pos, message)
    error('Json.decode: ' .. message .. ' at byte ' .. tostring(pos))
end

local parseValue

local function skipWhitespace(text, pos)
    local _, last = string.find(text, '^%s*', pos)
    return (last or pos - 1) + 1
end

local function parseString(text, pos)
    -- pos points at the opening quote.
    local i = pos + 1
    local parts = {}
    while i <= #text do
        local c = string.sub(text, i, i)
        if c == '"' then
            return table.concat(parts), i + 1
        elseif c == '\\' then
            local next_ = string.sub(text, i + 1, i + 1)
            if next_ == 'u' then
                local hex = string.sub(text, i + 2, i + 5)
                local code = tonumber(hex, 16)
                if not code then
                    decodeError(text, i, 'bad \\u escape')
                end
                if code < 128 then
                    parts[#parts + 1] = string.char(code)
                else
                    -- Non-ASCII escapes are rare on our wire format; keep the
                    -- code point as UTF-8.
                    if code < 2048 then
                        parts[#parts + 1] = string.char(192 + math.floor(code / 64), 128 + code % 64)
                    else
                        parts[#parts + 1] = string.char(
                            224 + math.floor(code / 4096),
                            128 + math.floor(code / 64) % 64,
                            128 + code % 64
                        )
                    end
                end
                i = i + 6
            else
                local mapping = { n = '\n', r = '\r', t = '\t', ['"'] = '"', ['\\'] = '\\', ['/'] = '/' }
                local mapped = mapping[next_]
                if not mapped then
                    decodeError(text, i, 'bad escape \\' .. next_)
                end
                parts[#parts + 1] = mapped
                i = i + 2
            end
        else
            parts[#parts + 1] = c
            i = i + 1
        end
    end
    decodeError(text, pos, 'unterminated string')
end

local function parseNumber(text, pos)
    local match = string.match(text, '^-?%d+%.?%d*[eE]?[+-]?%d*', pos)
    if not match or match == '' or match == '-' then
        decodeError(text, pos, 'bad number')
    end
    local value = tonumber(match)
    if not value then
        decodeError(text, pos, 'bad number')
    end
    return value, pos + #match
end

local function parseObject(text, pos)
    local result = {}
    local i = skipWhitespace(text, pos + 1)
    if string.sub(text, i, i) == '}' then
        return result, i + 1
    end
    while true do
        i = skipWhitespace(text, i)
        if string.sub(text, i, i) ~= '"' then
            decodeError(text, i, 'expected object key')
        end
        local key
        key, i = parseString(text, i)
        i = skipWhitespace(text, i)
        if string.sub(text, i, i) ~= ':' then
            decodeError(text, i, 'expected colon')
        end
        local value
        value, i = parseValue(text, skipWhitespace(text, i + 1))
        result[key] = value
        i = skipWhitespace(text, i)
        local c = string.sub(text, i, i)
        if c == '}' then
            return result, i + 1
        elseif c ~= ',' then
            decodeError(text, i, 'expected comma or closing brace')
        end
        i = i + 1
    end
end

local function parseArray(text, pos)
    local result = {}
    local i = skipWhitespace(text, pos + 1)
    if string.sub(text, i, i) == ']' then
        return result, i + 1
    end
    while true do
        local value
        value, i = parseValue(text, i)
        result[#result + 1] = value
        i = skipWhitespace(text, i)
        local c = string.sub(text, i, i)
        if c == ']' then
            return result, i + 1
        elseif c ~= ',' then
            decodeError(text, i, 'expected comma or closing bracket')
        end
        i = skipWhitespace(text, i + 1)
    end
end

parseValue = function(text, pos)
    pos = skipWhitespace(text, pos)
    local c = string.sub(text, pos, pos)
    if c == '"' then
        return parseString(text, pos)
    elseif c == '{' then
        return parseObject(text, pos)
    elseif c == '[' then
        return parseArray(text, pos)
    elseif c == 't' and string.sub(text, pos, pos + 3) == 'true' then
        return true, pos + 4
    elseif c == 'f' and string.sub(text, pos, pos + 4) == 'false' then
        return false, pos + 5
    elseif c == 'n' and string.sub(text, pos, pos + 3) == 'null' then
        return nil, pos + 4
    else
        return parseNumber(text, pos)
    end
end

local function decode(text)
    if type(text) ~= 'string' or text == '' then
        return nil
    end
    local value, pos = parseValue(text, 1)
    pos = skipWhitespace(text, pos)
    if pos <= #text then
        decodeError(text, pos, 'trailing content')
    end
    return value
end

Json.encode = encode
Json.decode = decode

-- Lua has no literal distinction between {} as an object and [] as an array.
-- Keep that distinction at the bridge boundary for commands that can return an
-- empty page of photo metadata.
function Json.array(values)
    if values == nil then
        values = {}
    end
    if type(values) ~= 'table' then
        error('Json.array requires a table')
    end
    return setmetatable(values, { __tonerelay_json_array = true })
end

return Json
