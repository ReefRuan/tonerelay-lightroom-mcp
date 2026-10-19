"""Runtime-host folders: scan, preview files, and write XMP back.

Why this module exists
----------------------
MCP folder arguments address paths on the Runtime host. Letting a remote
caller read arbitrary files would be unsafe, so access is limited to OS-visible
roots by default or an explicit operator-owned root list.

Five rules that are not negotiable:

1. **Default full host access.** Without an operator config, every filesystem
   root visible to the Runtime OS account is addressable. An
   explicit ``folder-library`` config opts into a restricted root list.
2. **Containment before existence.** Every path used for a read is passed
   through ``Path.resolve()`` (which also collapses symlinks) and compared
   against the resolved root *before* anything is stat()ed. Checking existence
   first turns the endpoint into an existence oracle -- ``/etc/passwd`` and
   ``/etc`` would answer with different errors, telling a caller which paths
   exist. Refusals for out-of-scope paths are worded identically whether or
   not the path exists, and never echo the resolved absolute path.
3. **Keys, not paths, on the wire.** Callers address files by an opaque ``key``
   (a truncated hash of the resolved path) plus a *relative* directory. MCP
   responses never carry a machine absolute path.
4. **Scanning never touches Lightroom.** ``scan``/``tree`` take no bridge
   client at all, so the mistake is impossible to make rather than merely
   avoided. Only an explicitly write-enabled ``preview`` and ``adopt`` can add
   a record to the active authorized Catalog, and
   ``write_xmp`` never does.
5. **Previews are honest about how they were made.** Formats Pillow can decode
   are read straight from disk (``direct``); RAW needs demosaicing and a camera
   profile, so it goes through Lightroom (``lightroom_export``) and the result
   records which path was used.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = "folder-preview-v1"

# Every extension the library will list or preview. A caller may narrow this in
# the config file; it may not widen it past the deny list below.
DEFAULT_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".tif", ".tiff",
    ".cr2", ".cr3", ".nef", ".arw", ".raf", ".rw2", ".dng",
)
# Readable without Lightroom.
DIRECT_EXTENSIONS = (".jpg", ".jpeg", ".png", ".tif", ".tiff")
MIME_BY_EXTENSION = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}

# Never listed, never previewed, never served -- even if a cached record claims
# otherwise. Sidecars, catalog internals, logs and configs are not photos.
DENY_SUFFIXES = frozenset({
    ".xmp", ".lrcat", ".lrdata", ".lrprev", ".json", ".db", ".sqlite",
    ".txt", ".md", ".log", ".plist", ".lock", ".tmp", ".ds_store",
})
DENY_DIR_NAMES = frozenset({
    ".lrdata", ".lrcat", ".lrprev", ".git", ".svn", ".hg", ".trash",
    ".fseventsd", ".spotlight-v100", ".documentrevisions-v100", ".temporaryitems",
})

CONFIG_ENV = "LR_XMP_MIMIC_FOLDER_CONFIG"
CONFIG_BASENAMES = ("folder-library.yaml", "folder-library.yml", "folder-library.json")
DEFAULT_CONFIG_BASENAME = "folder-library.yaml"
LEGACY_NAME = "folder-roots.json"
STATE_NAME = "folder-library-state.json"

KEY_RE = re.compile(r"\A[0-9a-f]{20}\Z")
ROOT_KEY_RE = re.compile(r"\A[0-9a-f]{12}\Z")
ROOT_KEY_LENGTH = 12
KEY_LENGTH = 20

# Ceiling on what we will hand to the bridge for staging.
MAX_STAGE_BYTES = 200 * 1024 * 1024
# Ceiling on what a direct preview will read into memory in one go.
MAX_DIRECT_BYTES = 64 * 1024 * 1024
# Recursion bounds live here, not in the config file: an operator switches
# recursion on or off, but cannot raise the guard rails themselves.
MAX_SCAN_FILES = 2000
MAX_SCAN_ENTRIES = 20000
MAX_SCAN_DEPTH = 8
MAX_TREE_CHILDREN = 500
MAX_RECENT = 12

_REFUSED = "该路径不在允许访问的根目录之内，已拒绝。"


class FolderError(RuntimeError):
    """A folder request refused by the Runtime capability boundary."""

    detail: dict = {}

    def __init__(self, message: str, **detail):
        super().__init__(message)
        if detail:
            self.detail = detail


class FolderNotFound(FolderError):
    pass


class FolderConflict(FolderError):
    pass


# --------------------------------------------------------------------------
# small helpers


def _atomic_text(path: Path, text: str) -> None:
    _atomic_bytes(path, text.encode("utf-8"))


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _contain(root_resolved: Path, candidate: Path) -> bool:
    """The single containment primitive. Both sides must already be resolved."""
    try:
        candidate.relative_to(root_resolved)
        return True
    except ValueError:
        return False


def _has_dotdot(raw: str) -> bool:
    return ".." in raw.replace("\\", "/").split("/")


def _bad_rel(rel) -> bool:
    """A relative directory must stay relative and must not climb."""
    if rel in (None, ""):
        return False
    if not isinstance(rel, str):
        return True
    if "\x00" in rel or rel.startswith("/") or "\\" in rel:
        return True
    return _has_dotdot(rel)


def _bad_name(name) -> bool:
    if not isinstance(name, str) or not name:
        return True
    if "\x00" in name or "/" in name or "\\" in name:
        return True
    if name in (".", "..") or name.startswith("."):
        return True
    return os.path.basename(name) != name


def _denied_dir(name: str) -> bool:
    """Directories the walk must never enter.

    Hidden directories are out, and so is anything shaped like Lightroom's own
    storage: ``sandbox.lrdata`` does not equal ``.lrdata``, but it is exactly
    the folder full of catalog internals we must not enumerate.
    """
    lowered = name.lower()
    if lowered.startswith("."):
        return True
    if lowered in DENY_DIR_NAMES:
        return True
    return lowered.endswith((".lrdata", ".lrprev", ".lrcat"))


def _default_access_roots() -> tuple[str, ...]:
    """All host filesystem roots when no explicit config narrows access."""
    if os.name != "nt":
        return (str(Path("/").resolve()),)
    try:
        import ctypes

        mask = ctypes.windll.kernel32.GetLogicalDrives()
        roots = tuple(f"{chr(ord('A') + index)}:\\"
                      for index in range(26) if mask & (1 << index))
        return roots or (str(Path.home().anchor),)
    except (AttributeError, OSError):
        return (str(Path.home().anchor),)


def normalize_extensions(values) -> tuple[str, ...]:
    """Lower-case, dot-prefixed, de-duplicated and ordered.

    ``"JPG"``, ``" .PNg "`` and ``"tif"`` all become usable entries. Anything
    empty, non-string, path-shaped or deny-listed is refused loudly rather than
    dropped: silently ignoring a typo in a config file is how a user ends up
    wondering why their photos are missing.
    """
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        raise FolderError("extensions 必须是一个列表。")
    out: list[str] = []
    for item in values:
        if not isinstance(item, str):
            raise FolderError(f"扩展名必须是字符串，收到：{item!r}")
        entry = item.strip().lower()
        if not entry:
            raise FolderError("扩展名不能为空字符串。")
        if not entry.startswith("."):
            entry = "." + entry
        if "/" in entry or "\\" in entry or "\x00" in entry:
            raise FolderError(f"扩展名不能包含路径分隔符：{item!r}")
        if len(entry) < 2 or entry == ".":
            raise FolderError(f"扩展名不合法：{item!r}")
        if entry in DENY_SUFFIXES:
            raise FolderError(f"不允许把非图片后缀放进扩展名白名单：{item!r}")
        if entry not in out:
            out.append(entry)
    return tuple(out)


def root_key_for(path) -> str:
    """Opaque, stable identifier for one Runtime folder-access root."""
    return hashlib.sha1(str(Path(path).resolve()).encode("utf-8")).hexdigest()[:ROOT_KEY_LENGTH]


def file_key_for(path) -> str:
    """The safe file key.

    Deliberately the same construction this module has always used --
    ``sha1(str(resolved_path))[:20]`` -- so preview blobs cached by earlier
    versions keep resolving instead of being orphaned (orphaning them would
    re-import photos that are already in the active Catalog).
    ``test_folders`` pins this against a real cached key so a later
    "improvement" to the hash cannot quietly break that.
    """
    return hashlib.sha1(str(Path(path).resolve()).encode("utf-8")).hexdigest()[:KEY_LENGTH]


def _stat_signature(path: Path) -> dict:
    try:
        info = path.stat()
    except OSError:
        return {}
    return {"size": info.st_size, "mtime_ns": info.st_mtime_ns}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def image_header_facts(content: bytes, extension: str):
    """``(width, height, format)`` read from the file header alone, or ``None``.

    Pure standard library: no pixel decoding happens on the direct path, which
    is what lets a JPEG preview be served without a third-party decoder.
    """
    try:
        if extension == ".png":
            return _png_facts(content)
        if extension in (".jpg", ".jpeg"):
            return _jpeg_facts(content)
        if extension in (".tif", ".tiff"):
            return _tiff_facts(content)
    except (IndexError, ValueError, TypeError, OSError):
        return None
    return None


def _png_facts(content: bytes):
    if len(content) < 24 or content[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    if content[12:16] != b"IHDR":
        return None
    width = int.from_bytes(content[16:20], "big")
    height = int.from_bytes(content[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return (width, height, "PNG")


_JPEG_SOF = (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF)


def _jpeg_facts(content: bytes):
    if len(content) < 4 or content[0] != 0xFF or content[1] != 0xD8:
        return None
    offset = 2
    while offset + 3 < len(content):
        if content[offset] != 0xFF:
            offset += 1
            continue
        marker = content[offset + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        if marker == 0xDA:  # start of scan: no frame header left to find
            return None
        length = int.from_bytes(content[offset + 2:offset + 4], "big")
        if length < 2:
            return None
        if marker in _JPEG_SOF:
            if offset + 9 >= len(content):
                return None
            height = int.from_bytes(content[offset + 5:offset + 7], "big")
            width = int.from_bytes(content[offset + 7:offset + 9], "big")
            if width <= 0 or height <= 0:
                return None
            return (width, height, "JPEG")
        offset += 2 + length
    return None


def _tiff_facts(content: bytes, max_entries: int = 512):
    if len(content) < 8:
        return None
    if content[:2] == b"II":
        order = "little"
    elif content[:2] == b"MM":
        order = "big"
    else:
        return None
    if int.from_bytes(content[2:4], order) != 42:
        return None
    ifd = int.from_bytes(content[4:8], order)
    if ifd + 2 > len(content):
        return None
    count = int.from_bytes(content[ifd:ifd + 2], order)
    if count > max_entries:
        return None
    width = height = None
    for index in range(count):
        entry = ifd + 2 + index * 12
        if entry + 12 > len(content):
            return None
        tag = int.from_bytes(content[entry:entry + 2], order)
        if tag not in (256, 257):
            continue
        field_type = int.from_bytes(content[entry + 2:entry + 4], order)
        item_count = int.from_bytes(content[entry + 4:entry + 8], order)
        if item_count != 1 or field_type not in (3, 4):
            continue
        size = 2 if field_type == 3 else 4
        raw = content[entry + 8:entry + 8 + size]
        if len(raw) < size:
            return None
        value = int.from_bytes(raw, order)
        if tag == 256:
            width = value
        else:
            height = value
    if not width or not height or width <= 0 or height <= 0:
        return None
    return (width, height, "TIFF")


# --------------------------------------------------------------------------
# configuration


def _strip_comment(line: str) -> str:
    if line.strip().startswith("#"):
        return ""
    marker = line.find(" #")
    if marker != -1:
        line = line[:marker]
    return line.rstrip()


def _scalar(text: str):
    lowered = text.strip().lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "~", ""):
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part) for part in inner.split(",")]
    return text.strip()


def _minimal_yaml(text: str, path: Path) -> dict:
    """A deliberately tiny YAML reader for exactly the documented shape.

    ``requirements.txt`` lists only Pillow and numpy, so this module must be
    able to read the shipped example without a YAML package. Rather than
    guessing at anything more interesting than ``key: value`` and ``key:``
    followed by ``- item`` lines, anything outside that subset is an error: a
    config file we half-understood is worse than one we refused.
    """
    result: dict = {}
    current: str | None = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw)
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith("-"):
            if current is None or not isinstance(result.get(current), list):
                raise FolderError(f"{path.name} 第 {number} 行：列表项不属于任何键。")
            result[current].append(_scalar(stripped[1:].strip()))
            continue
        if ":" not in line:
            raise FolderError(
                f"{path.name} 第 {number} 行不是 key: value 形式。内置解析器只支持 "
                "roots / extensions / recursive；请改用 JSON，或安装 PyYAML。")
        key, _, value = line.partition(":")
        key = key.strip()
        if not key or " " in key:
            raise FolderError(f"{path.name} 第 {number} 行：键名不合法。")
        value = value.strip()
        if value == "":
            result[key] = []
            current = key
        else:
            result[key] = _scalar(value)
            current = None
    return result


def _parse_config_text(text: str, path: Path) -> dict:
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if data is None:
        try:
            import yaml
        except ImportError:
            data = _minimal_yaml(text, path)
        else:
            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                raise FolderError(f"{path.name} 不是合法的 YAML：{exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise FolderError(f"{path.name} 的顶层必须是一个映射（roots / extensions / recursive）。")
    return data


@dataclass(frozen=True)
class FolderConfig:
    roots: tuple[str, ...]
    extensions: tuple[str, ...]
    recursive: bool
    path: Path
    source: str          # env | primary | legacy | default
    error: str | None = None

    def root_entries(self) -> list[tuple[str, Path]]:
        """``(root_key, resolved_path)`` sorted longest-first.

        Longest-first is what makes nested roots behave: the most specific
        configured root wins, so a root added *inside* another one does not
        silently inherit the outer one's identity.
        """
        pairs = [(root_key_for(entry), Path(entry)) for entry in self.roots]
        pairs.sort(key=lambda pair: len(str(pair[1])), reverse=True)
        return pairs

    def public(self) -> list[dict]:
        return [
            {"key": root_key_for(entry), "label": Path(entry).name or "root"}
            for entry in self.roots
        ]


# --------------------------------------------------------------------------
# the library


class FolderLibrary:
    """Runtime-host folder access, directory tree, previews and sidecar writing.

    With no operator config, all OS-visible local filesystem roots are
    addressable. An explicit folder config narrows access to its listed roots.
    Every request is still re-authorised at the moment of use.
    """

    def __init__(self, root, config_path=None):
        self.root = Path(root)
        self.cache = self.root / "folder-cache"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.entry_dir = self.cache / "index"
        self.legacy_index_path = self.cache / "index.json"
        self.state_path = self.root / STATE_NAME
        self._config_path = Path(config_path) if config_path else None
        self._config_memo: tuple | None = None
        self._legacy_memo: tuple | None = None

    # ---- configuration ----------------------------------------------------

    def default_config_path(self) -> Path:
        if self._config_path is not None:
            return self._config_path
        for name in CONFIG_BASENAMES:
            candidate = self.root / name
            if candidate.is_file():
                return candidate
        return self.root / DEFAULT_CONFIG_BASENAME

    def config(self) -> FolderConfig:
        path = self.default_config_path()
        key = _file_key(path)
        if self._config_memo and self._config_memo[0] == key:
            return self._config_memo[1]
        config = self._load_config(path)
        self._config_memo = (key, config)
        return config

    def _load_config(self, path: Path) -> FolderConfig:
        env_path = os.environ.get(CONFIG_ENV)
        if env_path:
            target = Path(env_path).expanduser()
            if not target.is_file():
                return FolderConfig((), DEFAULT_EXTENSIONS, False, target, "env",
                                    f"环境变量 {CONFIG_ENV} 指向的配置文件不存在：{target.name}")
            return self._config_from_file(target, source="env")
        if path.is_file():
            return self._config_from_file(path, source="primary")
        legacy = self.root / LEGACY_NAME
        if legacy.is_file():
            earlier = self._config_from_file(legacy, source="legacy", legacy_only=True)
            # Extensions and recursion were not part of the legacy file, so the
            # defaults apply; the first write migrates roots into the new file.
            return FolderConfig(earlier.roots, DEFAULT_EXTENSIONS, False, path,
                                "legacy", earlier.error)
        return FolderConfig(_default_access_roots(), DEFAULT_EXTENSIONS, False,
                            path, "default", None)

    def _config_from_file(self, path: Path, *, source: str, legacy_only: bool = False) -> FolderConfig:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return FolderConfig((), DEFAULT_EXTENSIONS, False, path, source,
                                f"配置文件无法读取：{path.name}（{exc.strerror or exc}）")
        try:
            raw = _parse_config_text(text, path)
        except FolderError as exc:
            return FolderConfig((), DEFAULT_EXTENSIONS, False, path, source, str(exc))
        if legacy_only:
            raw = {"roots": raw.get("roots", [])}
        # ``_``-prefixed keys are comments. JSON has no comment syntax, and a
        # commented example is worth more than a strictness point.
        unknown = {k for k in raw if k not in ("roots", "extensions", "recursive")
                   and not (isinstance(k, str) and k.startswith("_"))}
        if unknown:
            return FolderConfig((), DEFAULT_EXTENSIONS, False, path, source,
                                f"{path.name} 含未知配置项：{', '.join(sorted(unknown))}；"
                                "只支持 roots / extensions / recursive。")
        try:
            roots_raw = raw.get("roots") or []
            if not isinstance(roots_raw, (list, tuple)):
                raise FolderError("roots 必须是一个列表。")
            roots: list[str] = []
            for entry in roots_raw:
                if not isinstance(entry, str) or not entry.strip():
                    raise FolderError(f"根目录必须是字符串：{entry!r}")
                resolved = str(Path(entry).expanduser().resolve())
                if resolved not in roots:
                    roots.append(resolved)
            # An absent key means "use the defaults"; an explicitly empty list
            # is a mistake, and silently substituting defaults would hide it.
            if "extensions" in raw:
                extensions = normalize_extensions(raw["extensions"] or [])
            else:
                extensions = DEFAULT_EXTENSIONS
            if not extensions:
                raise FolderError("extensions 不能为空；没有可列出的图片扩展名。")
            recursive = raw.get("recursive", False)
            if not isinstance(recursive, bool):
                raise FolderError("recursive 必须是 true 或 false。")
        except FolderError as exc:
            return FolderConfig((), DEFAULT_EXTENSIONS, False, path, source,
                                f"{path.name} 配置无效：{exc}")
        return FolderConfig(tuple(roots), extensions, recursive, path, source, None)

    def roots(self) -> list[str]:
        """Resolved root paths. Server-side only; never put these on the wire."""
        return list(self.config().roots)

    def public_state(self) -> dict:
        config = self.config()
        direct = [e for e in config.extensions if e in DIRECT_EXTENSIONS]
        roots = config.public()
        if config.source == "default":
            roots = [
                {"key": root_key_for(entry),
                 "label": ("整个 Runtime 主机" if len(config.roots) == 1
                           else f"Runtime 主机磁盘 {Path(entry).anchor.strip()}" )}
                for entry in config.roots
            ]
        return {
            "access_mode": "full" if config.source == "default" else "restricted",
            "roots": roots,
            "extensions": list(config.extensions),
            "direct_extensions": direct,
            "recursive": config.recursive,
            "config": {
                "source": config.source,
                "name": config.path.name,
                # Parser diagnostics can contain a user-supplied absolute root
                # path. Keep the wire view useful without returning that path.
                "error": "目录配置无效；请在 Runtime 主机检查配置文件。"
                if config.error else None,
            },
            "capabilities": {
                "scan": True,
                "tree": True,
                "direct_preview": direct,
                "lightroom_preview": [e for e in config.extensions if e not in DIRECT_EXTENSIONS],
                "catalog_write_required_for": [e for e in config.extensions
                                               if e not in DIRECT_EXTENSIONS],
                "write_xmp": True,
                "lightroom_folders": False,
            },
        }

    # ---- authorization ----------------------------------------------------

    def _require_config(self) -> FolderConfig:
        config = self.config()
        if config.error:
            raise FolderError(config.error)
        if not config.roots:
            raise FolderError("当前 folder-library 配置没有允许访问的目录。")
        return config

    def _match_root(self, resolved: Path, config: FolderConfig):
        for root_key, root_path in config.root_entries():
            if _contain(root_path, resolved):
                return root_key, root_path
        return None

    def _authorize_dir(self, *, path=None, root=None, rel=""):
        """Resolve a directory request to ``(root_key, root_path, dir, rel)``.

        Containment is decided before anything is stat()ed, so the refusal a
        caller sees never depends on whether the path exists.
        """
        config = self._require_config()
        if path is not None:
            if not isinstance(path, str) or not path.strip():
                raise FolderError("请填写目录路径。")
            if "\x00" in path:
                raise FolderError("路径不合法。")
            if _has_dotdot(path):
                raise FolderError("路径包含 ..，已拒绝。")
            try:
                resolved = Path(path).expanduser().resolve()
            except (OSError, RuntimeError) as exc:
                raise FolderError(f"无法解析该路径：{exc}") from exc
            matched = self._match_root(resolved, config)
            if matched is None:
                raise FolderNotFound(_REFUSED)
            root_key, root_path = matched
        else:
            if not isinstance(root, str) or not ROOT_KEY_RE.match(root or ""):
                raise FolderError("根目录标识不合法。")
            matched = None
            for candidate_key, candidate_path in config.root_entries():
                if candidate_key == root:
                    matched = (candidate_key, candidate_path)
                    break
            if matched is None:
                raise FolderError("没有这个根目录。")
            root_key, root_path = matched
            if _bad_rel(rel):
                raise FolderError("相对目录不合法。")
            try:
                resolved = (root_path / (rel or "")).resolve()
            except (OSError, RuntimeError) as exc:
                raise FolderError(f"无法解析该路径：{exc}") from exc
            if not _contain(root_path, resolved):
                raise FolderNotFound(_REFUSED)
        rel_out = "" if resolved == root_path else resolved.relative_to(root_path).as_posix()
        if not resolved.exists():
            raise FolderNotFound(f"目录不存在：{resolved.name or resolved}")
        if not resolved.is_dir():
            raise FolderNotFound(f"不是目录：{resolved.name or resolved}")
        return root_key, root_path, resolved, rel_out

    def _resolve_file(self, root_path: Path, rel: str, name: str, config: FolderConfig) -> Path:
        """Authorise one file. Every key-addressed read goes through here."""
        if _bad_name(name):
            raise FolderError("文件名不合法。")
        if _bad_rel(rel):
            raise FolderError("相对目录不合法。")
        try:
            resolved = (root_path / (rel or "") / name).resolve()
        except (OSError, RuntimeError) as exc:
            raise FolderError(f"无法解析该路径：{exc}") from exc
        if not _contain(root_path, resolved):
            raise FolderNotFound(_REFUSED)
        extension = resolved.suffix.lower()
        if extension in DENY_SUFFIXES or extension not in config.extensions:
            allowed = ", ".join(config.extensions)
            raise FolderError(
                f"不是受支持的图片格式：{extension or '（无扩展名）'}（允许：{allowed}）")
        if not resolved.is_file():
            raise FolderNotFound(f"文件不存在：{name}")
        return resolved

    # ---- listing ----------------------------------------------------------

    def _listed(self, entry: os.DirEntry, config: FolderConfig, base: Path):
        """One listed file, or a reason it was skipped."""
        if entry.name.startswith("."):
            return None, "hidden"
        extension = Path(entry.name).suffix.lower()
        if extension in DENY_SUFFIXES or extension not in config.extensions:
            return None, "extension"
        try:
            resolved = Path(entry.path).resolve()
        except (OSError, RuntimeError):
            return None, "escape"
        if not _contain(base, resolved):
            return None, "escape"
        try:
            info = resolved.stat()
        except OSError:
            return None, "not_file"
        if not resolved.is_file():
            return None, "not_file"
        return {
            "name": entry.name,
            "size": info.st_size,
            "modified": info.st_mtime,
            "extension": extension,
            "key": file_key_for(resolved),
            "resolved": resolved,
        }, None

    def _walk(self, base: Path, root_path: Path, config: FolderConfig):
        skipped = {"hidden": 0, "not_file": 0, "extension": 0, "escape": 0, "symlink_dir": 0}
        files: list[dict] = []
        truncated = False
        visited = 0
        stack = [(base, 0)]
        while stack:
            directory, depth = stack.pop()
            try:
                entries = sorted(os.scandir(directory), key=lambda e: e.name.lower())
            except OSError:
                continue
            subdirs = []
            for entry in entries:
                visited += 1
                if visited > MAX_SCAN_ENTRIES:
                    truncated = True
                    break
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    is_dir = False
                if is_dir:
                    if depth + 1 > MAX_SCAN_DEPTH:
                        continue
                    if _denied_dir(entry.name):
                        skipped["hidden"] += 1
                        continue
                    subdirs.append(Path(entry.path))
                    continue
                if entry.is_symlink():
                    try:
                        target = Path(entry.path).resolve()
                    except (OSError, RuntimeError):
                        skipped["escape"] += 1
                        continue
                    if target.is_dir():
                        # Directory symlinks are never traversed: no loops, and
                        # no departure from the tree we were authorised to walk.
                        skipped["symlink_dir"] += 1
                        continue
                    if not _contain(root_path, target):
                        skipped["escape"] += 1
                        continue
                record, reason = self._listed(entry, config, root_path)
                if record is None:
                    skipped[reason] = skipped.get(reason, 0) + 1
                    continue
                if len(files) >= MAX_SCAN_FILES:
                    truncated = True
                    continue
                files.append(record)
            if visited > MAX_SCAN_ENTRIES:
                break
            if config.recursive:
                for sub in reversed(subdirs):
                    stack.append((sub, depth + 1))
        return files, skipped, truncated

    def _sidecar_state(self, path: Path) -> dict:
        """Whether a same-named ``.xmp`` sits next to ``path``.

        Only existence and size travel: the sidecar's *content* is exactly what
        this module promises not to read.
        """
        try:
            info = (path.parent / f"{path.stem}.xmp").stat()
        except OSError:
            return {"exists": False}
        return {"exists": True, "size": info.st_size, "modified": info.st_mtime}

    def scan(self, *, path=None, root=None, rel="") -> dict:
        """List image files in one directory inside the current access scope.

        Takes no bridge client, by design: this path cannot touch Lightroom.
        """
        config = self._require_config()
        root_key, root_path, directory, rel_out = self._authorize_dir(
            path=path, root=root, rel=rel)
        if config.recursive:
            records, skipped, truncated = self._walk(directory, root_path, config)
        else:
            skipped = {"hidden": 0, "not_file": 0, "extension": 0, "escape": 0, "symlink_dir": 0}
            records = []
            try:
                entries = sorted(os.scandir(directory), key=lambda e: e.name.lower())
            except PermissionError as exc:
                raise FolderNotFound("没有权限读取该目录。") from exc
            except OSError as exc:
                raise FolderNotFound(f"无法读取该目录：{exc.strerror or exc}") from exc
            for entry in entries:
                if len(records) >= MAX_SCAN_FILES:
                    break
                try:
                    if entry.is_dir(follow_symlinks=False):
                        skipped["not_file"] += 1
                        continue
                except OSError:
                    skipped["not_file"] += 1
                    continue
                if entry.is_symlink():
                    try:
                        target = Path(entry.path).resolve()
                    except (OSError, RuntimeError):
                        skipped["escape"] += 1
                        continue
                    if target.is_dir():
                        skipped["symlink_dir"] += 1
                        continue
                    if not _contain(root_path, target):
                        skipped["escape"] += 1
                        continue
                record, reason = self._listed(entry, config, root_path)
                if record is None:
                    skipped[reason] = skipped.get(reason, 0) + 1
                    continue
                records.append(record)
            truncated = len(records) >= MAX_SCAN_FILES

        if not records:
            allowed = ", ".join(config.extensions)
            raise FolderNotFound(
                f"该目录下没有符合已配置扩展名的图片"
                f"（已配置 {len(config.extensions)} 种：{allowed}）",
                root=root_key, dir=rel_out, extensions=list(config.extensions))

        files = []
        for record in records:
            resolved = record.pop("resolved")
            rel_file = "" if resolved.parent == root_path else \
                resolved.parent.relative_to(root_path).as_posix()
            route = self.preview_route(resolved.suffix.lower(), config)
            files.append({
                **record,
                "rel_dir": rel_file,
                "preview": route,
                "has_sidecar": self._sidecar_state(resolved)["exists"],
            })
            self._note_direct_address(record["key"], root_key, rel_file,
                                      record["name"], resolved, route)

        self.note_recent(root_key, rel_out)
        return {
            "root": root_key,
            "dir": rel_out,
            "recursive": config.recursive,
            "count": len(files),
            "skipped": sum(skipped.values()),
            "skipped_reasons": skipped,
            "truncated": truncated,
            "extensions": list(config.extensions),
            "files": files,
        }

    @staticmethod
    def preview_route(extension: str, config: FolderConfig) -> str:
        """``direct`` or ``lightroom_export`` for one extension, under ``auto``."""
        if extension in DIRECT_EXTENSIONS and extension in config.extensions:
            return "direct"
        return "lightroom_export"

    def tree(self, *, root=None, path=None, rel="") -> dict:
        """One level of subdirectories, plus this level's image count.

        Deliberately one ``scandir``: counts for *child* folders would cost one
        directory read each, so callers request those levels explicitly.
        """
        config = self._require_config()
        root_key, root_path, directory, rel_out = self._authorize_dir(
            path=path, root=root, rel=rel)
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name.lower())
        except OSError as exc:
            raise FolderError(f"无法读取该目录：{exc.strerror or exc}") from exc
        dirs, images, truncated = [], 0, False
        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if _denied_dir(entry.name):
                    continue
                child = Path(entry.path)
                try:
                    resolved_child = child.resolve()
                except (OSError, RuntimeError):
                    continue
                if not _contain(root_path, resolved_child):
                    continue
                if len(dirs) >= MAX_TREE_CHILDREN:
                    truncated = True
                    continue
                dirs.append({
                    "name": entry.name,
                    "dir": resolved_child.relative_to(root_path).as_posix(),
                })
                continue
            extension = Path(entry.name).suffix.lower()
            if entry.name.startswith(".") or extension in DENY_SUFFIXES \
                    or extension not in config.extensions:
                continue
            images += 1
        return {
            "root": root_key,
            "dir": rel_out,
            "recursive": config.recursive,
            "images": images,
            "truncated": truncated,
            "dirs": dirs,
        }

    # ---- recent directories ----------------------------------------------

    def _read_state(self) -> dict:
        if not self.state_path.is_file():
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def note_recent(self, root_key: str, rel_dir: str) -> None:
        """Remember a directory the user actually opened.

        This is cosmetic state. Atomic last-writer-wins updates are sufficient;
        losing one concurrent recent-directory entry does not affect access.
        """
        if not ROOT_KEY_RE.match(root_key or "") or _bad_rel(rel_dir):
            return
        state = self._read_state()
        recent = [item for item in state.get("recent", [])
                  if isinstance(item, dict)
                  and not (item.get("root") == root_key and item.get("dir") == (rel_dir or ""))]
        recent.insert(0, {"root": root_key, "dir": rel_dir or "", "at": time.time()})
        state["recent"] = recent[:MAX_RECENT]
        try:
            _atomic_text(self.state_path, json.dumps(state, ensure_ascii=False, indent=2))
        except OSError:
            pass

    def recent(self) -> list[dict]:
        config = self.config()
        known = {root_key_for(entry): Path(entry) for entry in config.roots}
        out = []
        for item in self._read_state().get("recent", []):
            if not isinstance(item, dict):
                continue
            root_key = item.get("root")
            rel_dir = item.get("dir") or ""
            if root_key not in known or _bad_rel(rel_dir):
                continue
            base = known[root_key]
            out.append({
                "root": root_key,
                "dir": rel_dir,
                "label": (base / rel_dir).name if rel_dir else base.name,
                "at": item.get("at"),
            })
        return out

    # ---- cached records ---------------------------------------------------

    def _entry_path(self, key: str) -> Path:
        return self.entry_dir / f"{key}.json"

    def entry(self, key: str) -> dict | None:
        if not KEY_RE.match(key or ""):
            return None
        path = self._entry_path(key)
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                return None
            return data if isinstance(data, dict) and data.get("key") == key else None
        return self._legacy_entry(key)

    def _legacy_index(self) -> dict:
        """``{key: record}`` from the pre-tree single-file index.

        That file is the uuid memory of every folder photo already imported
        into a Catalog. Ignoring it would re-import those photos the
        first time someone clicks them again.
        """
        key = _file_key(self.legacy_index_path)
        if self._legacy_memo and self._legacy_memo[0] == key:
            return self._legacy_memo[1]
        by_key: dict[str, dict] = {}
        if self.legacy_index_path.is_file():
            try:
                data = json.loads(self.legacy_index_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                data = {}
            if isinstance(data, dict):
                for absolute, record in data.items():
                    if not isinstance(record, dict) or not record.get("key"):
                        continue
                    try:
                        resolved = str(Path(absolute).resolve())
                    except (OSError, RuntimeError):
                        continue
                    by_key[record["key"]] = {
                        "key": record["key"], "uuid": record.get("uuid"),
                        "name": record.get("name"), "path": resolved,
                        "method": "lightroom_export", "legacy": True,
                    }
        self._legacy_memo = (key, by_key)
        return by_key

    def _legacy_entry(self, key: str) -> dict | None:
        """A legacy record is worth keeping even without its blob.

        The uuid is the expensive part: losing it means importing a photo that
        is already in the catalog. A missing blob only costs one export.
        """
        record = self._legacy_index().get(key)
        return dict(record) if record else None

    def blob_path(self, key: str) -> Path:
        """Lightroom-rendered bytes.

        The path is unchanged from earlier versions on purpose: moving it would
        orphan every cached render, and an orphaned render reads as "no cache",
        which re-imports the photo.
        """
        return self.cache / f"{key}.jpg"

    def _write_entry(self, record: dict) -> None:
        _atomic_text(self._entry_path(record["key"]),
                     json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))

    def _note_direct_address(self, key, root_key, rel_dir, name, resolved, route) -> None:
        """Record where a directly-readable file lives.

        Without this the "click shows the image immediately" behaviour would
        race the preview POST and the ``<img>`` would 404 first. Never
        downgrades an entry that already carries a Lightroom render.
        """
        existing = self.entry(key)
        if existing and existing.get("method") == "lightroom_export":
            return
        if route != "direct" and existing:
            return
        try:
            self._write_entry({
                "schema_version": SCHEMA_VERSION,
                "key": key,
                "root_key": root_key,
                "rel_dir": rel_dir or "",
                "name": name,
                "extension": resolved.suffix.lower(),
                "method": "direct",
                "mime_type": MIME_BY_EXTENSION.get(resolved.suffix.lower(),
                                                   "application/octet-stream"),
                "source": _stat_signature(resolved),
                "at": time.time(),
            })
        except OSError:
            pass

    def _address_for(self, record: dict):
        """Re-authorise a stored record back into a live path."""
        config = self._require_config()
        root_key = record.get("root_key")
        rel_dir = record.get("rel_dir") or ""
        name = record.get("name")
        if root_key and name:
            root_path = None
            for candidate_key, candidate_path in config.root_entries():
                if candidate_key == root_key:
                    root_path = candidate_path
                    break
            if root_path is None:
                return None
            try:
                return root_path, self._resolve_file(root_path, rel_dir, name, config)
            except FolderError:
                return None
        absolute = record.get("path")
        if not absolute:
            return None
        try:
            resolved = Path(absolute).resolve()
        except (OSError, RuntimeError):
            return None
        matched = self._match_root(resolved, config)
        if matched is None or not resolved.is_file():
            return None
        return matched[1], resolved

    def key_for_uuid(self, uuid) -> str | None:
        """Find the folder file a catalog photo came from, if we ever saw it."""
        if not isinstance(uuid, str) or not uuid:
            return None
        if self.entry_dir.is_dir():
            for path in sorted(self.entry_dir.glob("*.json")):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue
                if isinstance(record, dict) and record.get("uuid") == uuid:
                    return record.get("key")
        for key, record in self._legacy_index().items():
            if record.get("uuid") == uuid:
                return key
        return None

    def locate(self, uuid) -> dict:
        """Describe where a catalog photo lives, for the XMP write-back gate."""
        key = self.key_for_uuid(uuid)
        if not key:
            return {"found": False,
                    "reason": "这张原片不在当前目录访问范围内，无法写回 XMP。"}
        record = self.entry(key) or {}
        located = self._address_for(record)
        if located is None:
            return {"found": False, "key": key,
                    "reason": "该文件已不在当前目录访问范围内，已拒绝。"}
        _, target = located
        return {"found": True, "key": key, "name": target.name,
                "root": record.get("root_key"), "dir": record.get("rel_dir") or "",
                "sidecar": self._sidecar_state(target)}

    # ---- previews ---------------------------------------------------------

    def preview_bytes(self, key: str):
        """``(bytes, mime)`` for a thumb URL, or ``None``.

        A record is an addressing convenience, never an authorisation: the
        direct branch re-authorises the file before reading it. Any refusal
        along the way is reported as "no bytes" -- this route serves images,
        not diagnostics, and must not become a way to probe configuration.
        """
        if not KEY_RE.match(key or ""):
            return None
        try:
            return self._preview_bytes(key)
        except FolderError:
            return None

    def _preview_bytes(self, key: str):
        record = self.entry(key)
        if record and record.get("method") == "direct":
            located = self._address_for(record)
            if located is None:
                return None
            _, resolved = located
            try:
                if resolved.stat().st_size > MAX_DIRECT_BYTES:
                    return None
                content = resolved.read_bytes()
            except OSError:
                return None
            return content, record.get("mime_type") or MIME_BY_EXTENSION.get(
                resolved.suffix.lower(), "application/octet-stream")
        blob = self.blob_path(key)
        if blob.is_file():
            try:
                return blob.read_bytes(), "image/jpeg"
            except OSError:
                return None
        return None

    def cached_preview(self, key: str):
        """Path to the Lightroom-rendered blob, if present."""
        if not KEY_RE.match(key or ""):
            return None
        blob = self.blob_path(key)
        return blob if blob.is_file() else None

    def _import(self, client, target: Path) -> str:
        """Hand the bytes to the bridge, then let Lightroom add them to the catalog.

        ``import_photo`` no longer accepts a path: the bridge owns file
        placement, so a caller uploads bytes and passes the resulting
        ``stage_id``. The original filename is kept on purpose -- Lightroom
        names the photo after it, and the strip filters on that name.
        """
        size = target.stat().st_size
        if size > MAX_STAGE_BYTES:
            raise FolderError(
                f"文件过大（{size / 1e6:.0f} MB），超过 "
                f"{MAX_STAGE_BYTES // 1024 // 1024} MB 上限：{target.name}")
        stage_id = client.upload(target.name, target.read_bytes())
        try:
            data = client.run("import_photo", {"stage_id": stage_id})
        except Exception:
            # A stage whose import never ran is garbage; drop it now rather
            # than waiting for the bridge's age-based prune.
            try:
                client.discard_stage(stage_id)
            except Exception:
                pass
            raise
        uuid_ = (data or {}).get("photo", {}).get("uuid")
        if not uuid_:
            raise FolderError(f"Lightroom 导入失败，没有返回照片：{target.name}")
        return uuid_

    def preview(self, *, path=None, root=None, rel="", name="", client=None,
                edge=1024, render="auto", catalog_write=False, force=False) -> dict:
        """Serve one image: read it from disk, or have Lightroom render it.

        ``render`` is ``auto`` (route by extension), ``direct`` (raw bytes) or
        ``lightroom`` (force a render). ``catalog_write`` opts in to adding a
        persistent master-photo record to the active authorized Catalog,
        which a Lightroom render of an uncatalogued file requires.
        """
        config = self._require_config()
        if render not in ("auto", "direct", "lightroom"):
            raise FolderError("render 只能是 auto / direct / lightroom。")
        if type(edge) is not int or not 64 <= edge <= 4096:
            raise FolderError("edge 必须是 64 到 4096 之间的整数。")
        root_key, root_path, _, rel_out = self._authorize_dir(path=path, root=root, rel=rel)
        target = self._resolve_file(root_path, rel_out, name, config)
        extension = target.suffix.lower()
        key = file_key_for(target)
        method = self._choose_method(extension, config, render)

        if method == "direct":
            record = self._direct_preview(key, target, root_key, rel_out, extension,
                                          client, catalog_write)
        else:
            if not catalog_write:
                raise FolderError(
                    f"{extension} 必须由 Lightroom 渲染，这会在当前已授权 "
                    "Catalog 中新增主照片记录（引用原文件，不复制、不移动、"
                    "不修改原文件）。请传 catalog_write=allow 明确同意。")
            record = self._lightroom_preview(key, target, root_key, rel_out, name,
                                             extension, client, edge, bool(force))
        self.note_recent(root_key, rel_out)
        return {**record, "root": root_key}

    @staticmethod
    def _choose_method(extension: str, config: FolderConfig, render: str) -> str:
        if render == "direct":
            if extension not in DIRECT_EXTENSIONS:
                raise FolderError(
                    f"{extension} 不能直接读取：RAW 需要去马赛克与相机配置文件，"
                    "只能由 Lightroom 渲染。")
            if extension not in config.extensions:
                raise FolderError(f"不是受支持的图片格式：{extension}")
            return "direct"
        if render == "lightroom":
            return "lightroom_export"
        return FolderLibrary.preview_route(extension, config)

    def _artifact(self, served_from: Path, key: str, record: dict, source: dict):
        """Best-effort ``artifact-ref-v1`` for the bytes actually served."""
        try:
            from .artifacts import build_artifact_ref
        except ImportError:  # pragma: no cover - only when Pillow is absent
            return None, "artifact 需要 Pillow 才能生成"
        try:
            return build_artifact_ref(
                served_from,
                artifact_id=f"folder-preview:{key}",
                source=source,
                purpose="browse",
                method="direct_read" if record.get("method") == "direct" else "LrExportSession",
                freshness="live" if record.get("method") == "direct" else "verified_render",
                evidence_status="unverified",
                color_space="unknown" if record.get("method") == "direct" else "sRGB",
                orientation=None,
            ), None
        except (ValueError, OSError) as exc:
            return None, f"artifact 未能生成：{exc}"

    def _direct_preview(self, key, target, root_key, rel_dir, extension,
                        client=None, catalog_write=False) -> dict:
        try:
            info = target.stat()
        except OSError as exc:
            raise FolderNotFound(f"文件不存在：{target.name}") from exc
        if info.st_size > MAX_DIRECT_BYTES:
            raise FolderError(
                f"文件过大（{info.st_size / 1e6:.0f} MB），超过直读上限 "
                f"{MAX_DIRECT_BYTES // 1024 // 1024} MB。")
        try:
            content = target.read_bytes()
        except OSError as exc:
            raise FolderError(f"无法读取该文件：{exc.strerror or exc}") from exc
        facts = image_header_facts(content, extension)
        # Registering the file in the active Catalog is what makes it
        # selectable as an Episode source. It is a separate concern from
        # reading the pixels -- which is why it is opt-in, and why a caller
        # that only wants to look passes ``catalog_write=never``.
        uuid_ = (self.entry(key) or {}).get("uuid")
        imported = False
        if catalog_write and not uuid_:
            if client is None:
                raise FolderError(
                    "Lightroom 桥未连接，无法登记该文件；只取预览请传 catalog_write=never。")
            uuid_ = self._import(client, target)
            imported = True
        record = {
            "schema_version": SCHEMA_VERSION,
            "key": key,
            "root_key": root_key,
            "rel_dir": rel_dir or "",
            "name": target.name,
            "extension": extension,
            "method": "direct",
            "mime_type": MIME_BY_EXTENSION.get(extension, "application/octet-stream"),
            "width": facts[0] if facts else None,
            "height": facts[1] if facts else None,
            "dimensions_source": "header" if facts else None,
            "uuid": uuid_,
            "bytes": info.st_size,
            "cache_status": "direct",
            "cached": False,
            "rendered_now": False,
            "imported_now": imported,
            "source": _stat_signature(target),
            "size": info.st_size,
            "at": time.time(),
        }
        artifact, note = self._artifact(
            target, key, record,
            {"kind": "folder_file", "root_key": root_key, "rel_dir": rel_dir or "",
             "name": target.name, "extension": extension, "method": "direct"})
        record["artifact"] = artifact
        if note:
            record["artifact_note"] = note
        try:
            self._write_entry(record)
        except OSError:
            pass
        return record

    def _lightroom_preview(self, key, target, root_key, rel_dir, name, extension,
                           client, edge, force) -> dict:
        if client is None:
            raise FolderError("Lightroom 桥未连接，无法渲染该格式。")
        config = self._require_config()
        if extension not in config.extensions:
            raise FolderError(f"不是受支持的图片格式：{extension}")

        stored = self.entry(key) or {}
        signature = _stat_signature(target)
        blob = self.blob_path(key)
        has_render = blob.is_file() and stored.get("method") == "lightroom_export"
        state = self._cache_state(stored, signature, target, has_render, force)

        if state == "hit":
            record = self._render_record(key, root_key, rel_dir, name, target, stored,
                                         state="hit", rendered=False, imported=False,
                                         uuid_=stored.get("uuid"), edge=stored.get("edge"))
            if record.get("width") is None:
                # Records written before dimensions were tracked (or by an
                # earlier version) can still answer this from the blob header.
                facts = image_header_facts(self.blob_path(key).read_bytes(), ".jpg")
                if facts:
                    record["width"], record["height"] = facts[0], facts[1]
                    record["dimensions_source"] = "header"
            if not stored.get("source"):
                # Legacy entry: write down where the source stood, so the next
                # click can actually tell whether it changed.
                record["source"] = _stat_signature(target)
            try:
                self._write_entry(record)
            except OSError:
                pass
            return record

        uuid_ = stored.get("uuid") if state == "reused_uuid" else None
        imported = False
        if not uuid_:
            uuid_ = self._import(client, target)
            imported = True

        try:
            result = client.run("export_preview",
                                {"photo_uuid": uuid_, "long_edge": int(edge)})
        except Exception:
            # A remembered uuid can go stale (photo removed from the catalog by
            # hand). Import once more before giving up -- but never after a
            # fresh import, which would just repeat the same failure.
            if imported:
                raise
            uuid_ = self._import(client, target)
            imported = True
            result = client.run("export_preview",
                                {"photo_uuid": uuid_, "long_edge": int(edge)})
        if (result or {}).get("color_space") not in (None, "sRGB"):
            raise FolderError("Lightroom 返回的色彩空间不是 sRGB，已放弃该预览。")
        ref = (result or {}).get("output_ref")
        if not ref:
            raise FolderError("导出未返回文件句柄；请重启 lr-ops 桥以加载新协议。")

        destination = self.blob_path(key)
        client.fetch_output(ref, destination)
        if not destination.is_file() or destination.stat().st_size == 0:
            raise FolderError("Lightroom 导出的预览为空。")

        from .preview import inspect_jpeg
        try:
            actual = inspect_jpeg(destination.read_bytes())["actual_size"]
        except ValueError as exc:
            raise FolderError(f"Lightroom 导出的预览不是完整 JPEG：{exc}") from exc

        record = self._render_record(key, root_key, rel_dir, name, target, stored,
                                     state=state, rendered=True, imported=imported,
                                     uuid_=uuid_, edge=edge,
                                     width=actual[0], height=actual[1])
        try:
            self._write_entry(record)
        except OSError:
            pass
        return record

    @staticmethod
    def _cache_state(stored: dict, signature: dict, target: Path,
                     has_render: bool, force: bool) -> str:
        """Which of the six cache situations we are in.

        ``hit`` reuses the rendered bytes untouched; ``reused_uuid`` re-renders
        but may keep the catalog uuid; ``stale`` means the bytes on disk are no
        longer the ones the record describes, so a fresh import is needed;
        ``miss`` means there is nothing to reuse at all.

        A record with no stat signature at all is a legacy entry written before
        this module tracked freshness. Re-importing on every such entry would
        duplicate photos that are already in the catalog, so an unknown
        signature is treated as "assume unchanged" and upgraded in place on the
        next hit.
        """
        previous = stored.get("source") or {}
        known = bool(previous)
        same_stat = known and previous.get("size") == signature.get("size") \
            and previous.get("mtime_ns") == signature.get("mtime_ns")
        unchanged = same_stat or not known
        if has_render and unchanged and not force:
            return "hit"
        if not stored.get("uuid"):
            return "miss"
        if unchanged:
            return "reused_uuid"
        if stored.get("sha256"):
            try:
                if _sha256_file(target) == stored["sha256"]:
                    return "reused_uuid"
            except OSError:
                return "miss"
        return "stale"

    def _render_record(self, key, root_key, rel_dir, name, target, stored,
                       *, state, rendered, imported, uuid_, edge=None,
                       width=None, height=None) -> dict:
        blob = self.blob_path(key)
        try:
            blob_bytes = blob.stat().st_size
        except OSError:
            blob_bytes = 0
        if state == "hit":
            # A cache hit keeps every fact about the render exactly as recorded;
            # re-deriving them would be a way to silently change the answer.
            width = stored.get("width") if width is None else width
            height = stored.get("height") if height is None else height
            edge = stored.get("edge")
            signature = stored.get("source") or {}
            sha256 = stored.get("sha256")
            blob_sha256 = stored.get("blob_sha256")
        else:
            signature = _stat_signature(target)
            # ``sha256`` is the hash of the *source* file: it is what staleness
            # is decided on. The rendered bytes get their own field so a change
            # to the render cannot masquerade as a change to the original.
            try:
                sha256 = _sha256_file(target)
            except OSError:
                sha256 = None
            try:
                blob_sha256 = _sha256_file(blob)
            except OSError:
                blob_sha256 = None
        record = {
            "schema_version": SCHEMA_VERSION,
            "key": key,
            "root_key": root_key,
            "rel_dir": rel_dir or "",
            "name": name or target.name,
            "extension": target.suffix.lower(),
            "method": "lightroom_export",
            "mime_type": "image/jpeg",
            "width": width,
            "height": height,
            "dimensions_source": "lightroom" if width else None,
            "uuid": uuid_,
            "edge": edge,
            "bytes": blob_bytes,
            "cache_status": state,
            "cached": state == "hit",
            "rendered_now": rendered,
            "imported_now": imported,
            "sha256": sha256,
            "blob_sha256": blob_sha256,
            "size": signature.get("size"),
            "source": signature,
            "at": time.time(),
        }
        artifact, note = self._artifact(
            blob, key, record,
            {"kind": "folder_file", "root_key": root_key, "rel_dir": rel_dir or "",
             "name": record["name"], "extension": record["extension"],
             "method": "lightroom_export", "uuid": uuid_})
        record["artifact"] = artifact
        if note:
            record["artifact_note"] = note
        return record

    def adopt(self, *, path=None, root=None, rel="", name="", client=None) -> dict:
        """Import one file inside the current access scope into the active Catalog.

        The explicit counterpart to a direct preview: looking at a JPEG must
        not write to the catalog, so registering it as an Episode source is a
        separate, visible action.
        """
        config = self._require_config()
        root_key, root_path, _, rel_out = self._authorize_dir(path=path, root=root, rel=rel)
        target = self._resolve_file(root_path, rel_out, name, config)
        key = file_key_for(target)
        record = self.entry(key) or {}
        uuid_ = record.get("uuid")
        imported = False
        if not uuid_:
            if client is None:
                raise FolderError("Lightroom 桥未连接，无法登记该文件。")
            uuid_ = self._import(client, target)
            imported = True
        if record.get("uuid") != uuid_ or not record.get("root_key"):
            record.update({
                "schema_version": SCHEMA_VERSION,
                "key": key,
                "root_key": root_key,
                "rel_dir": rel_out,
                "name": target.name,
                "extension": target.suffix.lower(),
                "method": record.get("method") or self.preview_route(
                    target.suffix.lower(), config),
                "uuid": uuid_,
                "source": record.get("source") or _stat_signature(target),
                "at": time.time(),
            })
            try:
                self._write_entry(record)
            except OSError:
                pass
        self.note_recent(root_key, rel_out)
        return {"key": key, "name": target.name, "uuid": uuid_, "root": root_key,
                "dir": rel_out, "imported_now": imported}

    # ---- writing the verified XMP next to the original --------------------

    def write_xmp(self, *, key: str, content, overwrite: bool = False) -> dict:
        """Write a sidecar next to the original. Never touches the original.

        An existing sidecar is refused unless ``overwrite`` is set, and even
        then it is copied aside first: this system has no delete, so a backup
        is the only safety net there is.
        """
        config = self._require_config()
        record = self.entry(key)
        if not record:
            raise FolderNotFound("找不到这个文件；请先扫描它所在的目录。")
        located = self._address_for(record)
        if located is None:
            raise FolderNotFound("该文件已不在当前目录访问范围内，已拒绝写入。")
        root_path, target = located
        if isinstance(content, str):
            content = content.encode("utf-8")
        if not isinstance(content, (bytes, bytearray)) or not content:
            raise FolderError("XMP 内容为空，已拒绝写入。")

        sidecar = target.parent / f"{target.stem}.xmp"
        if _bad_name(sidecar.name):
            raise FolderError("sidecar 文件名不合法。")
        try:
            sidecar_parent = sidecar.parent.resolve()
        except (OSError, RuntimeError) as exc:
            raise FolderError(f"无法解析目标目录：{exc}") from exc
        if not _contain(root_path, sidecar_parent):
            raise FolderNotFound(_REFUSED)

        backup_name = None
        if sidecar.exists():
            if not overwrite:
                info = sidecar.stat()
                raise FolderConflict(
                    f"目录里已经有 {sidecar.name}，没有覆盖。确认后会先备份成 "
                    f"{target.stem}.xmp.bak-<时间戳> 再写入。",
                    sidecar=sidecar.name, size=info.st_size, modified=info.st_mtime)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup = sidecar.parent / f"{target.stem}.xmp.bak-{stamp}"
            counter = 0
            while backup.exists():
                counter += 1
                backup = sidecar.parent / f"{target.stem}.xmp.bak-{stamp}-{counter}"
            shutil.copy2(sidecar, backup)
            backup_name = backup.name

        _atomic_bytes(sidecar, bytes(content))
        digest = hashlib.sha256(bytes(content)).hexdigest()
        return {
            "key": key,
            "name": target.name,
            "root": record.get("root_key"),
            "dir": record.get("rel_dir") or "",
            "sidecar_name": sidecar.name,
            "written": True,
            "backed_up": backup_name,
            "bytes": len(content),
            "sha256": digest,
        }


def _file_key(path: Path):
    try:
        info = path.stat()
    except OSError:
        return None
    return (str(path), info.st_mtime_ns, info.st_size)
