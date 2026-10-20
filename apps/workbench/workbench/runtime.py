"""Protocol-neutral Lightroom Runtime capabilities and composition.

The MCP adapter composes this module directly. Nothing here parses a wire
protocol or manufactures transport URLs.
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
import tempfile
from pathlib import Path

from .catalog import CatalogTools
from .artifacts import build_artifact_ref
from .folders import DEFAULT_EXTENSIONS, FolderLibrary
from .lrops import FastThumbnailUnavailable, LrOpsClient, LrOpsError
from .state import RuntimeState, StoreError
from .targets import DEFAULT_TARGET_ID, LrOpsRouter, TargetRegistry
from .presets import PresetTools
from .preview import PreviewStore
from .issues import IssueStore
from .presentation import PresentationSettings
from .selection import SelectionTools
from .catalog_discovery import catalog_identity_for_path, discover_catalogs
from .catalog_switch import (CatalogSwitchError, is_supported_host,
                             open_catalog_file)
from .configuration_help import configuration_help, validate_config_topic
from .photo_marks import PhotoMarks, MarkConnection
from lr_ops.sidecar import validate_request as validate_sidecar_request
from lr_ops.native_mask import validate_request as validate_native_mask_request

# One list, not two: the folder library owns the extension set, and Runtime's
# upload capability consumes it rather than keeping a copy that can drift.
ALLOWED_UPLOAD_SUFFIXES = set(DEFAULT_EXTENSIONS)


class CatalogBoundClient:
    """Pin one target and bind every LR command to one probed Catalog.

    Folder operations need upload/fetch methods as well as ``run``. Routing
    those calls through the raw target client would bypass Studio's envelope;
    duplicating the envelope here would create a second policy implementation.
    This facade therefore delegates command binding to one lazily constructed
    Studio while keeping byte handles on the same pinned client.
    """

    def __init__(self, root: Path, client):
        self.root = Path(root)
        self.client = client
        self._studio = None
        self._catalog_identity = None

    def _bind(self):
        if self._studio is None:
            from .studio.engine import Studio
            studio = Studio(self.root, self.client)
            probe = studio.probe()
            self._studio = studio
            self._catalog_identity = probe["catalog_identity"]
        return self._studio

    def run(self, command, params=None):
        studio = self._bind()
        return studio.run(
            command, params, catalog_identity=self._catalog_identity)

    def upload(self, *args, **kwargs):
        self._bind()
        return self.client.upload(*args, **kwargs)

    def fetch_output(self, *args, **kwargs):
        self._bind()
        return self.client.fetch_output(*args, **kwargs)

    def discard_stage(self, *args, **kwargs):
        return self.client.discard_stage(*args, **kwargs)


def preview_source(client, photo_uuid: str, probe: dict) -> dict:
    """The identity a fast preview is stored, cached and attributed under.

    The endpoint is recorded as a hash because a split deployment must not put
    a raw bridge URL into durable records. The bridge-issued
    ``catalog_identity`` is opaque and works
    whether or not the two processes share a filesystem; a local path is never
    used as cache identity or exposed in the artifact record.
    """
    base = str(getattr(client, "base", "") or "")
    identity = probe.get("catalog_identity")
    if not isinstance(identity, str) or not identity:
        raise StoreError("Lightroom 未返回可用的 Catalog 身份")
    source = {
        "kind": "catalog_photo",
        "target_id": getattr(client, "target_id", None) or DEFAULT_TARGET_ID,
        "catalog_identity": identity,
        "endpoint_fingerprint": hashlib.sha256(base.encode("utf-8")).hexdigest()[:16],
        "photo_uuid": photo_uuid,
    }
    # Basename is display-only legacy metadata. It never authorizes a call.
    catalog_id = probe.get("catalog_id")
    if isinstance(catalog_id, str) and catalog_id:
        source["catalog_id"] = catalog_id
    return source


class Workbench:
    """Protocol-neutral facade over Runtime state and Lightroom capabilities."""

    def __init__(self, store: RuntimeState, lrops: LrOpsClient):
        self.store = store
        self.lrops = lrops
        self._studio = None
        self._folders = None
        self._image_tools = None
        self._preset_tools = None
        self._catalog_tools = None
        self._photo_marks = None
        self._previews = PreviewStore(self.store.root)
        self.presentation = PresentationSettings(self.store.root)
        self.issues = IssueStore(self.store.root / "issues.jsonl")
        self._catalog_paths: dict[str, Path] = {}
        self._catalog_metadata: dict[str, dict] = {}

    @property
    def studio(self):
        if self._studio is None:
            from .studio.engine import Studio
            self._studio = Studio(self.store.root, self.lrops)
        return self._studio

    @property
    def folders(self):
        """Runtime-host folders used by scan and preview capabilities."""
        if self._folders is None:
            self._folders = FolderLibrary(self.store.root)
        return self._folders

    @property
    def image_tools(self):
        """Read-only measurements of published episode renders (no Lightroom)."""
        if self._image_tools is None:
            from .image_tools import ImageTools
            self._image_tools = ImageTools(self.store.root / "episodes")
        return self._image_tools

    @property
    def preset_tools(self):
        if self._preset_tools is None:
            self._preset_tools = PresetTools(self.run)
        return self._preset_tools

    @property
    def catalog_tools(self):
        """Paginated catalog queries; the same module the MCP server calls."""
        if self._catalog_tools is None:
            self._catalog_tools = CatalogTools(self.run)
        return self._catalog_tools

    def run(self, command, params=None):
        probe = self.studio.probe()
        return self.studio.run(
            command, params, catalog_identity=probe["catalog_identity"])

    @property
    def photo_marks(self):
        if self._photo_marks is None:
            self._photo_marks = PhotoMarks(self.store.root, self._mark_connection)
        return self._photo_marks

    def _mark_connection(self):
        from .studio.engine import Studio
        client = self.lrops.pin() if hasattr(self.lrops, "pin") else self.lrops
        probe = Studio(self.store.root, client).probe()
        return MarkConnection(client, probe)

    def catalog_client(self) -> CatalogBoundClient:
        """A lazy, target-pinned client for one compound Catalog operation."""
        client = self.lrops.pin() if hasattr(self.lrops, "pin") else self.lrops
        return CatalogBoundClient(self.store.root, client)

    def health(self) -> dict:
        """Report transport health and the current read-only Catalog policy.

        ``probe_client`` is intentionally the only unbound bridge command. It
        lets health explain a safe-mode or target-pin mismatch without granting
        any operational command or changing configuration.
        """
        reachable = self.lrops.reachable()
        result = {
            "ok": False,
            "lrops": reachable,
            "target": getattr(self.lrops, "target_id", None),
            "base": self.lrops.base,
            "time": time.time(),
            "catalog_policy": None,
            "versions": {"lightroom": None, "lightroom_plugin": None,
                         "lightroom_plugin_build": None},
        }
        manager = getattr(self.lrops, "bridge_manager", None)
        if manager is not None:
            result["bridge"] = dict(manager.status)
        if not reachable:
            return result

        client = self.lrops.pin() if hasattr(self.lrops, "pin") else self.lrops
        try:
            raw_client = getattr(client, "_client", client)
            if isinstance(raw_client, LrOpsClient):
                probe = client.run("probe_client", {}, timeout=3.0)
            else:
                probe = client.run("probe_client", {})
        except Exception as exc:
            result["catalog_policy"] = {
                "authorized": False,
                "reason": "probe_failed",
                "error": str(exc)[:500],
            }
            return result

        # Version labels are display facts, never paths or authorization data.
        # Reject malformed probe values rather than echoing arbitrary host text.
        for key, field in (("lightroom", "lr_version"),
                           ("lightroom_plugin", "plugin_version"),
                           ("lightroom_plugin_build", "build_id")):
            value = probe.get(field)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+()\[\]-]{0,127}", value):
                result["versions"][key] = value

        expected_mode = getattr(client, "access_mode", "full")
        expected_identity = getattr(client, "catalog_identity", None)
        actual_mode = probe.get("access_mode")
        actual_identity = probe.get("catalog_identity")
        capabilities = probe.get("capabilities") or {}
        binding_supported = (
            capabilities.get("expected_access_mode") is True
            and capabilities.get("expected_catalog_identity") is True
        )
        selection_supported = capabilities.get("current_folder_selection") is True
        mode_matches = actual_mode == expected_mode
        identity_matches = (expected_identity is None
                            or actual_identity == expected_identity)
        registered = probe.get("sandbox_registered") is True
        sandbox_matches = (probe.get("sandbox_matches_active") is True
                           or probe.get("sandbox") is True)
        plugin_authorized = probe.get("catalog_authorized")

        reason = None
        if probe.get("catalog_open") is False:
            reason = "catalog_not_open"
        elif (actual_mode not in ("safe", "full")
                or not isinstance(actual_identity, str)
                or not actual_identity
                or not binding_supported
                or not selection_supported
                or type(plugin_authorized) is not bool):
            reason = "unsupported_plugin_contract"
        elif expected_identity is None:
            reason = "catalog_confirmation_required"
        elif not mode_matches:
            reason = "access_mode_mismatch"
        elif not identity_matches:
            reason = "catalog_identity_mismatch"
        elif not plugin_authorized:
            if actual_mode == "safe" and not registered:
                reason = "safe_sandbox_not_registered"
            elif actual_mode == "safe" and not sandbox_matches:
                reason = "safe_catalog_mismatch"
            else:
                reason = "catalog_not_authorized"

        authorized = reason is None
        result["catalog_policy"] = {
            "access_mode": actual_mode,
            "expected_access_mode": expected_mode,
            "catalog_identity": actual_identity,
            "expected_catalog_identity": expected_identity,
            "catalog_id": probe.get("catalog_id"),
            "configuration_required": expected_identity is None,
            "sandbox_registered": registered,
            "sandbox_matches_active": sandbox_matches,
            "binding_supported": binding_supported,
            "current_folder_selection_supported": selection_supported,
            "authorized": authorized,
            "reason": reason,
        }
        result["ok"] = authorized
        return result

    def runtime_config(self, topic: str = "overview") -> dict:
        """Return one redacted overview of Runtime and Lightroom settings.

        The underlying stores remain authoritative. This is a conversational
        index, not a generic write surface; credentials, endpoint URLs,
        filesystem paths and opaque Catalog identities are intentionally
        omitted.
        """
        validate_config_topic(topic)
        registry = getattr(self.lrops, "registry", None)
        target = getattr(registry, "active", None)
        try:
            health = self.health()
        except Exception:
            health = {"lrops": None, "catalog_policy": None}

        policy = health.get("catalog_policy") or {}
        expected_identity = getattr(
            target, "catalog_identity", getattr(self.lrops, "catalog_identity", None))
        active_identity = policy.get("catalog_identity")
        binding_recorded = isinstance(expected_identity, str) and bool(expected_identity.strip())
        active_identity_known = isinstance(active_identity, str) and bool(active_identity.strip())
        if not health.get("lrops"):
            binding_status = "lightroom_unavailable"
        elif not active_identity_known:
            # A live Bridge does not establish which Catalog Lightroom has
            # open. Missing probe data cannot prove identity drift or justify
            # overwriting an existing binding.
            binding_status = "active_catalog_unknown"
        elif not binding_recorded:
            binding_status = "confirmation_required"
        elif active_identity != expected_identity:
            binding_status = "active_catalog_mismatch"
        elif policy.get("authorized") is True:
            binding_status = "authorized"
        else:
            binding_status = "authorization_blocked"

        def display_catalog_id(value):
            if not isinstance(value, str) or not value.strip():
                return None
            # Catalog ID is display metadata only, never used as authorization.
            return value.replace("\\", "/").rsplit("/", 1)[-1]

        bound_catalog_id = getattr(
            target, "catalog_id", getattr(self.lrops, "catalog_id", None))
        if expected_identity and active_identity == expected_identity:
            # The bridge's live name preserves the filename's display casing;
            # the persisted Catalog ID is normalized for comparison.
            bound_catalog_id = policy.get("catalog_id") or bound_catalog_id

        from .targets import is_loopback_base
        target_base = getattr(target, "base", getattr(self.lrops, "base", ""))
        target_token = getattr(target, "token", getattr(self.lrops, "token", None))
        expected_mode = getattr(
            target, "access_mode", getattr(self.lrops, "access_mode", None))
        local_target = bool(target_base and is_loopback_base(target_base))
        folder = self.folders.public_state()
        try:
            presentation = self.presentation.get()
            presentation_status = "ok"
        except Exception:
            # Keep the rest of the config page readable if one local setting
            # file needs repair; never include parser text that may echo data.
            presentation = None
            presentation_status = "unreadable"
        folder_config = folder["config"]
        try:
            tiers = self.studio.capabilities().get("capability_tiers", {})
            passthrough = tiers.get("passthrough", {})
        except Exception:
            passthrough = {}
        snapshot = {
            "schema_version": "runtime-config-v1",
            "versions": health.get("versions") or {
                "lightroom": None, "lightroom_plugin": None,
                "lightroom_plugin_build": None},
            "configuration": {
                "file": "config.json",
                "persisted": self.presentation.config.path.is_file(),
                "revision": self.presentation.config.read()["revision"],
                "reload": {
                    "targets": "next request; Catalog changes still require explicit confirmation",
                    "presentation": "next image response or new editing context",
                    "folders": "next folder request",
                    "paths": "restart MCP and Bridge; no data migration",
                    "bridge": "restart Bridge and affected clients",
                    "execution": "restart MCP",
                },
            },
            "target": {
                "kind": "local" if local_target else "remote",
                "label": "本机 Lightroom" if local_target else "远端 Lightroom",
                "reachable": health.get("lrops"),
                "credential_configured": bool(target_token),
            },
            "catalog": {
                # Identity presence and live observation are independent of
                # display metadata. An unanswered probe must not imply that
                # a remembered binding has disappeared.
                "binding_recorded": binding_recorded,
                "active_identity_status": "known" if active_identity_known else "unknown",
                "bound_catalog": display_catalog_id(bound_catalog_id),
                "active_catalog": display_catalog_id(policy.get("catalog_id")),
                "binding_status": binding_status,
                "authorized": policy.get("authorized"),
                "reason": policy.get("reason") or (
                    "lightroom_unavailable" if not health.get("lrops") else None),
                "access_mode": {
                    "expected": expected_mode,
                    "expected_source": ("config.json/targets" if registry and registry.persisted()
                                        else "environment_or_default" if registry else "injected_client"),
                    "expected_persisted": registry.persisted() if registry else False,
                    "actual": policy.get("access_mode"),
                    "managed_by": "Lightroom Plugin Manager",
                    "editable_in_runtime": False,
                },
                "sandbox_registered": policy.get("sandbox_registered"),
                "sandbox_matches_active": policy.get("sandbox_matches_active"),
                "can_switch_catalog": bool(local_target and is_supported_host()),
            },
            "image_presentation": {
                "codex_preview_max_edge": (
                    presentation["codex_preview_max_edge"] if presentation else None),
                "edit_render_max_edge": (
                    presentation["edit_render_max_edge"] if presentation else None),
                "status": presentation_status,
                "source": "config.json/presentation",
                "editable_in_conversation": presentation is not None,
                "config_status": presentation_status,
                "codex_preview_scope": "subsequent MCP image responses",
                "edit_render_scope": (
                    "the initial work-render cap of new Episodes; "
                    "existing artifacts are unchanged"),
            },
            "folder_access": {
                "mode": folder["access_mode"],
                "roots": folder["roots"],
                "extensions": folder["extensions"],
                "recursive": folder["recursive"],
                "source": folder_config["source"],
                "config_valid": not bool(folder_config["error"]),
                "editable_in_conversation": False,
                "policy": ("All filesystem paths allowed by the Runtime OS account; supported image types only."
                           if folder["access_mode"] == "full" else
                           "Restricted to config.json folders.roots."),
                "path_exposure": "root keys and labels only",
            },
            "admin_managed": [
                {"setting": "Lightroom safe/full mode and Sandbox", "managed_by":
                 "Lightroom Plugin Manager", "editable_in_runtime": False},
                {"setting": "Bridge endpoint, Runtime data root and execution flags",
                 "managed_by": "config.json", "editable_in_runtime": False},
                {"setting": "Unknown and structured Develop parameter passthrough",
                 "enabled": passthrough.get("enabled"),
                 "structured_enabled": passthrough.get("structured_enabled"),
                 "managed_by": "config.json/execution", "editable_in_runtime": False},
                {"setting": "Bridge credential", "configured":
                 bool(target_token), "value_returned": False,
                 "editable_in_runtime": False},
            ],
        }
        snapshot["help"] = configuration_help(snapshot, topic)
        return snapshot

    def catalog_inventory(self) -> dict:
        """List local Catalog file metadata and Lightroom's live active Catalog."""
        status = self.health()
        policy = status.get("catalog_policy") or {}
        local_target = False
        try:
            from .targets import is_loopback_base
            local_target = is_loopback_base(self.lrops.base)
        except (AttributeError, TypeError, ValueError):
            pass
        inventory = discover_catalogs(
            active_identity=policy.get("catalog_identity"),
            active_catalog_id=policy.get("catalog_id"),
            scan_local=local_target,
            include_paths=True,
        )
        self._catalog_paths = {}
        self._catalog_metadata = {}
        for row in inventory["catalogs"]:
            local_path = row.pop("_local_path", None)
            identity = row.get("catalog_identity")
            if identity:
                self._catalog_metadata[identity] = dict(row)
            if local_path:
                self._catalog_paths[identity] = Path(local_path)
                row["switchable"] = is_supported_host()
            else:
                row["switchable"] = False
        inventory["active_catalog"] = (
            {"catalog_identity": policy.get("catalog_identity"),
             "catalog_id": policy.get("catalog_id")}
            if policy.get("catalog_identity") else None
        )
        inventory["target"] = status.get("target")
        inventory["catalog_configuration_required"] = (
            policy.get("configuration_required", True))
        inventory["probe_reason"] = policy.get("reason")
        return inventory

    def switch_catalog(self, catalog_identity: str,
                       confirm_switch_and_bind: bool) -> dict:
        """Switch local Lightroom to a discovered Catalog and bind it.

        The caller must first tell the user which Catalog will open and wait
        for explicit confirmation. OS launch alone does not count as success:
        Runtime probes Lightroom and persists the binding only after the live
        plugin reports the requested identity.
        """
        if confirm_switch_and_bind is not True:
            raise StoreError(
                "切换 Catalog 会改变 Lightroom 当前工作环境；需要用户明确确认。")
        from .targets import is_loopback_base

        registry = getattr(self.lrops, "registry", None)
        target = getattr(registry, "active", None)
        base = getattr(target, "base", getattr(self.lrops, "base", ""))
        if not is_loopback_base(base):
            raise StoreError(
                "当前是远端 Lightroom Target；Runtime 主机无法替远端切换 Catalog。"
                "请在 Lightroom 主机执行切换，再由用户确认绑定。")
        if not is_supported_host():
            raise StoreError("Catalog 自动切换目前支持 macOS 和 Windows Runtime 主机。")

        initial = self.health()
        if not initial.get("lrops"):
            raise StoreError("Lightroom Bridge 当前不可达；未发起 Catalog 切换。")
        active_identity = (initial.get("catalog_policy") or {}).get("catalog_identity")

        path = self._catalog_paths.get(catalog_identity)
        row = self._catalog_metadata.get(catalog_identity)
        if path is None:
            inventory = self.catalog_inventory()
            path = self._catalog_paths.get(catalog_identity)
            row = next((entry for entry in inventory["catalogs"]
                        if entry.get("catalog_identity") == catalog_identity), None)
        else:
            row = row or self._catalog_metadata.get(catalog_identity)
        if path is None:
            raise StoreError(
                "所选 Catalog 不在当前 Runtime 主机可切换的发现清单中；"
                "请重新调用 trlr_list_catalogs。")

        try:
            resolved = path.resolve(strict=True)
            if (path.is_symlink() or not resolved.is_file()
                    or resolved.suffix.lower() != ".lrcat"):
                raise StoreError("所选 Catalog 文件已变化或无法访问；请重新发现。")
        except StoreError:
            raise
        except (OSError, RuntimeError, ValueError):
            raise StoreError("所选 Catalog 文件已变化或无法访问；请重新发现。") from None
        # Catalog identity mirrors Lightroom's path string (slash/case
        # normalization only). The OS or Lightroom can canonicalize aliases
        # such as /var -> /private/var, so both spellings identify this file.
        path_identities = {
            catalog_identity_for_path(path),
            catalog_identity_for_path(resolved),
        }
        if catalog_identity not in path_identities:
            raise StoreError("所选 Catalog 与发现记录不一致；请重新调用 trlr_list_catalogs。")

        active_matches_file = active_identity in path_identities
        if not active_matches_file:
            try:
                open_catalog_file(path)
            except CatalogSwitchError as exc:
                raise StoreError(str(exc)) from None

            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                try:
                    current = self.health()
                except Exception:
                    current = {}
                current_policy = current.get("catalog_policy") or {}
                if (current.get("lrops")
                        and current_policy.get("catalog_identity") in path_identities):
                    active_identity = current_policy["catalog_identity"]
                    break
                time.sleep(0.5)
            else:
                return {
                    "switch_requested": True,
                    "switched": False,
                    "bound": False,
                    "catalog_id": row.get("catalog_id") if row else resolved.name,
                    "location_hint": row.get("location_hint") if row else None,
                    "reason": "switch_not_observed",
                    "message": (
                        "已请求 Lightroom 打开该 Catalog，但 Runtime 尚未观察到切换；"
                        "当前绑定未改。请检查 Lightroom 提示，并重新读取健康状态。"),
                }

        binding = self.confirm_catalog(active_identity, True)
        return {
            "switch_requested": not active_matches_file,
            "switched": not active_matches_file,
            "bound": binding["saved"],
            "catalog_id": binding.get("catalog_id") or resolved.name,
            "location_hint": row.get("location_hint") if row else None,
            "authorized": (binding.get("health", {}).get("catalog_policy") or {}).get(
                "authorized"),
            "health": binding["health"],
        }

    def confirm_catalog(self, catalog_identity: str,
                        confirm_current_catalog: bool) -> dict:
        """Bind and persist the user's explicitly confirmed active Catalog."""
        if confirm_current_catalog is not True:
            raise StoreError("需要用户明确确认当前打开的 Lightroom Catalog。")
        expected_target = getattr(self.lrops, "target", None)
        status = self.health()
        policy = status.get("catalog_policy") or {}
        actual_identity = policy.get("catalog_identity")
        actual_mode = policy.get("access_mode")
        if not status.get("lrops") or not actual_identity:
            raise StoreError("无法读取 Lightroom 当前 Catalog；请确认 Lightroom 与插件已就绪。")
        if catalog_identity != actual_identity:
            raise StoreError(
                "所选 Catalog 不是 Lightroom 当前打开的 Catalog；"
                "请先在 Lightroom 打开它，再重新发现并确认。")
        if policy.get("binding_supported") is not True:
            raise StoreError("Lightroom 插件不支持 Catalog 身份绑定；请重启 Lightroom 加载当前插件。")
        if actual_mode not in ("safe", "full"):
            raise StoreError("Lightroom 插件未返回有效的 Catalog 访问模式。")
        registry = getattr(self.lrops, "registry", None)
        if registry is None:
            raise StoreError("当前 Runtime Target 不支持持久化 Catalog 配置。")
        target = registry.bind_active_catalog(
            access_mode=actual_mode,
            catalog_identity=actual_identity,
            catalog_id=policy.get("catalog_id"),
            expected_target=expected_target,
        )
        return {
            "saved": True,
            "target": target.id,
            "catalog_id": target.catalog_id,
            "catalog_identity": target.catalog_identity,
            "access_mode": target.access_mode,
            "health": self.health(),
        }


    def _pinned_current_folder(self, limit: int, offset: int):
        """Bind one volatile selection query to one target/Catalog probe.

        The folder list is UI state.  Pinning the lr-ops target before probing
        means the subsequent page and all of its thumbnails cannot drift to a
        different configured bridge if another caller changes targets.
        """
        client = self.lrops.pin() if hasattr(self.lrops, "pin") else self.lrops
        from .studio.engine import Studio
        studio = Studio(self.store.root, client)
        probe = studio.probe()
        if probe.get("capabilities", {}).get("current_folder_selection") is not True:
            raise LrOpsError(
                "Lightroom plugin is incompatible with current-selection support; "
                "restart Lightroom to load the current ToneRelay plugin")
        tools = SelectionTools(
            lambda command, params: studio.run(
                command, params, catalog_identity=probe["catalog_identity"]))
        return client, studio, probe, tools.current_folder(limit=limit, offset=offset)

    def current_selection(self) -> dict:
        """Return the current target and leaf-folder summary, without images."""
        _, _, _, page = self._pinned_current_folder(limit=1, offset=0)
        return {
            "target": page["target"],
            "folder": page["folder"],
            "total": page["total"],
        }

    def update_xmp_sidecar(self, **params) -> dict:
        """Explicit master-sidecar operation, separate from Episode edits."""
        validate_sidecar_request("update_xmp_sidecar", params)
        result = self.run("update_xmp_sidecar", params)
        if "_sidecar_context" in result:
            raise StoreError("Upgrade lr-ops before using sidecar operations")
        return result

    def apply_mask(self, **params) -> dict:
        """Inspect, write or verify native mask records; never automate the UI."""
        validate_sidecar_request("apply_mask", params)
        result = self.run("apply_mask", params)
        if "_sidecar_context" in result:
            raise StoreError("Upgrade lr-ops before using sidecar operations")
        return result

    def native_mask(self, **params) -> dict:
        """Native SDK AI mask generation and local edits on virtual copies."""
        validate_native_mask_request(params)
        return self.run("native_mask", params)

    def preview_photo(self, photo_uuid: str, size: int = 1024,
                      force: bool = False) -> dict:
        """Return a fast thumbnail for an already catalogued photo.

        This is intentionally separate from the verified episode renderer:
        Lightroom's ``requestJpegThumbnail`` is a read-only cache operation and
        does not create a virtual copy or run an export session. The returned
        record states that its colour/freshness claims are unverified; callers
        that need acceptance evidence must use an episode's controlled export.
        """
        self._validate_preview_request(photo_uuid, size=size, force=force)
        # One pinned connection answers the probe, the thumbnail request and
        # the fetch. A target switch mid-request can then only affect the next
        # request, never hand a handle minted by one bridge to another.
        client = self.lrops.pin() if hasattr(self.lrops, "pin") else self.lrops
        from .studio.engine import Studio
        preview_studio = Studio(self.store.root, client)
        probe = preview_studio.probe()
        return self._preview_photo_on_client(
            client, preview_studio, probe, photo_uuid, size=size, force=force)

    @staticmethod
    def _validate_preview_request(photo_uuid: str, *, size: int, force: bool) -> None:
        """Reject malformed fast-preview requests before touching Lightroom."""
        if not isinstance(photo_uuid, str) or not photo_uuid:
            raise StoreError("photo_uuid is required")
        if type(size) is not int:
            raise StoreError("size must be an integer")
        if not 64 <= size <= 2048:
            raise StoreError("size must be 64..2048")
        if type(force) is not bool:
            raise StoreError("force must be a boolean")

    def _preview_photo_on_client(self, client, preview_studio, probe: dict,
                                 photo_uuid: str, *, size: int, force: bool) -> dict:
        """Fetch/cached-read one thumbnail under an already pinned probe."""
        self._validate_preview_request(photo_uuid, size=size, force=force)
        source = preview_source(client, photo_uuid, probe)
        if not force:
            cached = self._previews.current(source, size)
            if cached is not None:
                key, destination, manifest = cached
                return self._preview_record(source, manifest, destination, size, cached=True)

        data = preview_studio.run("request_jpeg_thumbnail", {
            "photo_uuid": photo_uuid,
            "size": size,
        }, catalog_identity=probe["catalog_identity"])
        ref = data.get("output_ref")
        if not ref:
            raise LrOpsError("快速预览未返回文件句柄；请检查 lr-ops 与插件版本")
        # fetch_output reopens the destination path to write the bytes, so the
        # handle must be closed first: Windows denies that second write while a
        # NamedTemporaryFile(delete=True) still holds the file open.
        with tempfile.NamedTemporaryFile(prefix="photo-preview-", suffix=".jpg", delete=False) as handle:
            temp_path = Path(handle.name)
        try:
            client.fetch_output(ref, temp_path)
            content = temp_path.read_bytes()
        finally:
            temp_path.unlink(missing_ok=True)
        if not content:
            raise LrOpsError("快速预览返回空文件")
        manifest = self._previews.publish(
            content, source, size,
            method=data.get("method", "requestJpegThumbnail"),
            orientation=data.get("orientation"),
        )
        destination = self._previews.read(manifest["key"])
        return self._preview_record(source, manifest, destination, size, cached=False)

    def current_folder_gallery(self, *, limit: int, offset: int, size: int) -> dict:
        """Return one live folder page plus private records for fast previews.

        The public MCP adapter removes the local preview paths before replying.
        Only Lightroom's explicit per-photo "no JPEG" callback is local to one
        item. Transport, authorization, protocol, timeout, corrupt-byte and
        cache failures propagate so the MCP call fails and records the issue.
        """
        client, studio, probe, page = self._pinned_current_folder(limit, offset)
        items = []
        for photo in page["photos"]:
            try:
                record = self._preview_photo_on_client(
                    client, studio, probe, photo["uuid"], size=size, force=False)
            except FastThumbnailUnavailable:
                items.append({
                    "photo": photo,
                    "preview": {"state": "unavailable",
                                "reason": "fast_thumbnail_unavailable"},
                })
            else:
                items.append({"photo": photo, "preview": {"state": "ready"},
                              "record": record})
        return {"page": page, "items": items}

    def _preview_record(self, source: dict, manifest: dict, destination: Path,
                        requested_size: int, *, cached: bool) -> dict:
        """One shape for both the rendered and the cached answer.

        A cache hit is not a different kind of fact, so it carries the same
        pixel size, byte count and orientation as the render that produced it;
        only ``cached`` and ``freshness`` say where the answer came from.
        """
        key = manifest["key"]
        method = manifest.get("method") or "requestJpegThumbnail"
        artifact = build_artifact_ref(
            destination,
            artifact_id=f"photo-preview:{key}",
            source=source,
            purpose="fast",
            method=method,
            freshness="cached_unverified" if cached else "unverified",
            evidence_status="unverified",
            color_space="unknown",
            orientation=manifest.get("orientation"),
        )
        return {
            "key": key,
            "photo_uuid": source["photo_uuid"],
            "preview_path": str(destination),
            "requested_size": requested_size,
            "actual_size": manifest.get("actual_size"),
            "bytes": manifest.get("bytes"),
            "cached": cached,
            "method": method,
            "purpose": "fast",
            "freshness": "cached_unverified" if cached else "unverified",
            "color_space": manifest.get("color_space", "unknown"),
            "icc_profile": manifest.get("icc_profile"),
            "exif_color_space_code": manifest.get("exif_color_space_code"),
            "orientation": manifest.get("orientation"),
            "artifact": artifact,
        }


    def report_issue(self, summary: str, kind: str = "manual",
                     episode_id: str | None = None,
                     detail: dict | None = None) -> dict:
        if not summary or not str(summary).strip():
            raise StoreError("report_issue requires a non-empty summary")
        return self.issues.record(kind, summary, episode_id=episode_id, detail=detail)

    def list_issues(self, kind: str | None = None, episode_id: str | None = None,
                    limit: int = 50) -> dict:
        entries = self.issues.list(kind=kind, episode_id=episode_id, limit=limit)
        return {"count": len(entries), "issues": entries}

    def upload_photo(self, filename: str, content: bytes) -> dict:
        """Stage an uploaded image on the bridge and import it into the catalog.

        The bytes are handed to the bridge rather than written to a local
        ``uploads/`` directory, because the file has to exist on the machine
        Lightroom runs on -- not necessarily on the machine the caller uses. On a
        single-machine install this is a loopback copy; on a split install it is
        the only reason the import can work at all.
        """
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_UPLOAD_SUFFIXES:
            raise StoreError(f"unsupported file type {suffix or '(none)'}")
        safe_name = f"{uuid.uuid4().hex[:12]}{suffix}"
        client = self.catalog_client()
        stage_id = client.upload(safe_name, content)
        try:
            imported = client.run("import_photo", {"stage_id": stage_id})
        except Exception:
            # A stage whose import never ran is pure garbage; drop it now
            # rather than waiting for the bridge's age-based prune.
            try:
                client.discard_stage(stage_id)
            except Exception:
                pass
            raise
        return {"stage_id": stage_id, "name": safe_name, "photo": imported.get("photo")}

    def import_photos(self, paths, request_id, recursive=False, move=False, move_root=None):
        from .photo_import import PhotoImport
        return PhotoImport(self.store.root, self.catalog_client).import_photos(
            paths, request_id, recursive=recursive, move=move, move_root=move_root)

def build_workbench(seed_base: str | None = None, *,
                    store: RuntimeState | None = None, manage_bridge: bool = False) -> Workbench:
    """Create the one protocol-neutral Runtime composition used by MCP."""
    runtime_store = store if store is not None else RuntimeState()
    registry = TargetRegistry(
        runtime_store.root / "config.json", seed_base=seed_base)
    if not manage_bridge:
        return Workbench(runtime_store, LrOpsRouter(registry))
    from .bundled_bridge import make_router
    return Workbench(runtime_store, make_router(registry, runtime_store.root.parent))
