"""LR targets: route the Runtime to one configured Lightroom bridge.

The workbench used to hold a single :class:`~workbench.lrops.LrOpsClient` built
once at startup from one base URL. That is enough to *point* the workbench at one
Lightroom, but not to *choose* between several -- and a workbench that can drive
either the local Lightroom or one on another machine needs the latter.

Three pieces, deliberately separate:

``Target``
    An immutable description: id, label, bridge base URL, optional token.

``TargetRegistry``
    The persisted list, plus which one is active, in ``config.json/targets``.
    Managed installations read this section; isolated embedded callers can
    seed an absent configuration from a factory argument or ``LROPS_BASE``.
    The current MCP surface does not expose general target mutation.

``LrOpsRouter``
    Duck-types :class:`LrOpsClient` (``run``/``reachable``/``upload``/
    ``fetch_output``/``base``) but delegates to whichever target is active.
    This is the seam that matters: Runtime services and protocol adapters keep
    talking to "the client" and never learn that the destination can change
    underneath them.

Why the router rather than passing a registry everywhere: ``Studio`` received a
client in its constructor and calls ``self.client.run(...)``. Giving it a router
with the same shape means target switching needs **no change to Studio's
internals**, and a caller that wants to pin one target can still pass a plain
client.

Binding an episode to a target is a separate concern -- see
``Studio.require_target``.
"""

from __future__ import annotations

import os
import threading
from functools import wraps
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from lr_ops.configuration import ConfigError, defaults, for_workbench

DEFAULT_TARGET_ID = "local"
DEFAULT_LABEL = "本机 Lightroom"
ACCESS_MODES = frozenset({"safe", "full"})


class TargetError(RuntimeError):
    pass


def normalize_access_mode(value) -> str:
    """Validate the target's bridge access contract.

    An omitted value in an API or embedded seed means ``full``. Persisted
    config.json values are validated before reaching this helper.
    """
    if value is None:
        return "full"
    if not isinstance(value, str) or value.strip().lower() not in ACCESS_MODES:
        raise TargetError("access_mode must be safe or full")
    return value.strip().lower()


def normalize_catalog_identity(value) -> str | None:
    """Validate, but never reinterpret, the identity reported by Lightroom."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise TargetError("catalog_identity must be a non-empty string")
    return value


def is_loopback_base(base: str) -> bool:
    """Whether ``base`` addresses a bridge on this machine."""
    host = urllib.parse.urlsplit(base or "").hostname or ""
    return host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")


@dataclass(frozen=True)
class Target:
    """One Lightroom the workbench may drive."""

    id: str
    label: str
    base: str
    token: str | None = None
    catalog_id: str | None = None
    access_mode: str = "full"
    catalog_identity: str | None = None
    is_default: bool = False

    def __post_init__(self):
        object.__setattr__(self, "access_mode", normalize_access_mode(self.access_mode))
        object.__setattr__(self, "catalog_identity",
                           normalize_catalog_identity(self.catalog_identity))

    def to_json(self) -> dict:
        return {"id": self.id, "label": self.label, "base": self.base,
                "token": self.token, "catalog_id": self.catalog_id,
                "access_mode": self.access_mode,
                "catalog_identity": self.catalog_identity,
                "is_default": self.is_default}

    @classmethod
    def from_json(cls, data: dict) -> "Target":
        target_id = data.get("id")
        base = data.get("base")
        if not isinstance(target_id, str) or not target_id:
            raise TargetError("target needs an id")
        if not isinstance(base, str) or not base.startswith(("http://", "https://")):
            raise TargetError(f"target {target_id} needs an http(s) base URL")
        catalog_id = data.get("catalog_id")
        if catalog_id is not None and (not isinstance(catalog_id, str) or not catalog_id.strip()):
            catalog_id = None
        return cls(id=target_id, label=data.get("label") or target_id,
                   base=base.rstrip("/"), token=data.get("token") or None,
                   catalog_id=catalog_id.strip().lower() if catalog_id else None,
                   access_mode=data.get("access_mode"),
                   catalog_identity=data.get("catalog_identity"),
                   is_default=bool(data.get("is_default")))

    def client_kwargs(self) -> dict:
        return {"base": self.base, "token": self.token}


def _seed_from_environment(base: str | None = None) -> Target:
    """Derive the initial target from the existing single-target configuration.

    Order matches :class:`LrOpsClient`: explicit argument, then ``LROPS_BASE``,
    then loopback. A deployment that never touches the registry therefore sees
    exactly the behaviour it had before this module existed.
    """
    resolved = base or os.environ.get("LROPS_BASE") or "http://127.0.0.1:17777"
    token = os.environ.get("LR_XMP_MIMIC_LOCAL_TOKEN") or None
    access_mode = normalize_access_mode(os.environ.get("LR_XMP_MIMIC_ACCESS_MODE"))
    is_local = "127.0.0.1" in resolved or "localhost" in resolved
    return Target(id=DEFAULT_TARGET_ID,
                  label=DEFAULT_LABEL if is_local else resolved,
                  base=resolved.rstrip("/"), token=token,
                  access_mode=access_mode, is_default=True)


def _registry_lock(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._thread_lock:
            return method(self, *args, **kwargs)
    return wrapped


def _config_write(method):
    @wraps(method)
    @_registry_lock
    def wrapped(self, *args, **kwargs):
        result = []
        def change(payload):
            if not self.path.exists():
                payload = {"active": self._seed.id, "targets": [self._seed.to_json()]}
            self._load_payload(payload)
            self._writing = True
            try:
                result.append(method(self, *args, **kwargs))
                return {"active": self._active_id,
                        "targets": [target.to_json() for target in self._targets.values()]}
            finally:
                self._writing = False
        try:
            self.config.update("targets", change)
        except ConfigError as exc:
            raise TargetError(str(exc)) from exc
        return result[0]
    return wrapped


class TargetRegistry:
    """Persisted set of LR targets plus the active selection.

    The file is optional. When it is absent the registry synthesises a single
    target from the environment and never writes anything, so read-only or
    first-run deployments are unaffected. It only starts persisting once someone
    actually adds or selects a target.
    """

    def __init__(self, path: Path, seed_base: str | None = None, *, config=None):
        self.config = config or for_workbench(Path(path).parent)
        self.path = self.config.path
        self._seed_base = seed_base
        self._thread_lock = threading.RLock()
        self._seed = (Target.from_json(defaults()["targets"]["targets"][0])
                      if self.path.exists() else _seed_from_environment(seed_base))
        self._writing = False
        self._targets: dict[str, Target] = {}
        self._active_id: str = DEFAULT_TARGET_ID
        self._loaded_from_disk = False
        self._load()

    # ---- persistence -------------------------------------------------------

    def _load(self) -> None:
        if self._writing:
            return
        try:
            data = self.config.read()["targets"]
        except ConfigError as exc:
            raise TargetError(str(exc)) from exc
        self._loaded_from_disk = self.path.exists()
        if not self._loaded_from_disk:
            data = {"active": self._seed.id, "targets": [self._seed.to_json()]}
        self._load_payload(data)

    def _load_payload(self, data):
        targets = [Target.from_json(row) for row in data["targets"]]
        self._targets = {target.id: target for target in targets}
        self._active_id = data["active"]

    def _persist(self) -> None:
        # The enclosing config transaction saves the complete section under
        # one process lock, retaining concurrent changes to other sections.
        if not self._writing:
            raise TargetError("target writes require a config transaction")

    # ---- reads -------------------------------------------------------------

    @property
    @_registry_lock
    def active_id(self) -> str:
        self._load()
        return self._active_id

    @property
    @_registry_lock
    def active(self) -> Target:
        self._load()
        return self._targets[self._active_id]

    @_registry_lock
    def list(self) -> list[Target]:
        self._load()
        return sorted(self._targets.values(), key=lambda t: (not t.is_default, t.label))

    @_registry_lock
    def get(self, target_id: str) -> Target:
        self._load()
        target = self._targets.get(target_id)
        if target is None:
            raise TargetError(f"未知的 LR 目标：{target_id}")
        return target

    def persisted(self) -> bool:
        return self.path.is_file()

    # ---- writes ------------------------------------------------------------

    @_config_write
    def add(self, label: str, base: str, token: str | None = None,
            target_id: str | None = None, catalog_id: str | None = None,
            access_mode: str = "full", catalog_identity: str | None = None) -> Target:
        base = (base or "").strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise TargetError("请填写完整的 http:// 或 https:// 地址")
        label = (label or "").strip() or base
        if target_id is None:
            target_id = self._unique_id(label)
        elif target_id in self._targets:
            raise TargetError(f"目标 id 已存在：{target_id}")
        target = Target(id=target_id, label=label, base=base, token=token or None,
                        catalog_id=catalog_id.strip().lower() if catalog_id else None,
                        access_mode=access_mode, catalog_identity=catalog_identity)
        self._targets[target_id] = target
        self._persist()
        return target

    @_config_write
    def update(self, target_id: str, *, label: str | None = None,
               base: str | None = None, token: str | None = None,
               catalog_id: str | None = None, access_mode: str | None = None,
               catalog_identity: str | None = None) -> Target:
        current = self.get(target_id)
        changes = {}
        if label is not None:
            changes["label"] = label.strip() or current.label
        if base is not None:
            base = base.strip().rstrip("/")
            if not base.startswith(("http://", "https://")):
                raise TargetError("请填写完整的 http:// 或 https:// 地址")
            changes["base"] = base
        if token is not None:
            changes["token"] = token or None
        if catalog_id is not None:
            changes["catalog_id"] = catalog_id.strip().lower() or None
        if access_mode is not None:
            changes["access_mode"] = access_mode
        if catalog_identity is not None:
            changes["catalog_identity"] = catalog_identity if catalog_identity else None
        if changes:
            self._targets[target_id] = replace(current, **changes)
            self._persist()
        return self._targets[target_id]

    @_config_write
    def remove(self, target_id: str) -> None:
        target = self.get(target_id)
        if target.is_default or len(self._targets) == 1:
            raise TargetError("本机默认目标不能删除")
        del self._targets[target_id]
        if self._active_id == target_id:
            self._active_id = next(iter(self._targets))
        self._persist()

    @_config_write
    def select(self, target_id: str) -> Target:
        target = self.get(target_id)
        if target_id != self._active_id:
            self._active_id = target_id
            self._persist()
        return target

    @_config_write
    def bind_active_catalog(self, *, access_mode: str,
                            catalog_identity: str, catalog_id: str | None,
                            expected_target: Target | None = None) -> Target:
        """Persist one explicitly confirmed live Catalog on the active target."""
        current = self.active
        if expected_target is not None and current != expected_target:
            raise TargetError("Target changed while confirming Catalog; read it again")
        updated = replace(
            current,
            access_mode=access_mode,
            catalog_identity=catalog_identity,
            catalog_id=(catalog_id.strip().lower()
                        if isinstance(catalog_id, str) and catalog_id.strip()
                        else None),
        )
        self._targets[current.id] = updated
        try:
            self._persist()
        except Exception:
            self._targets[current.id] = current
            raise
        return updated

    def _unique_id(self, label: str) -> str:
        stem = "".join(ch if ch.isalnum() else "-" for ch in label.lower()).strip("-")
        stem = stem or "target"
        candidate, n = stem, 2
        while candidate in self._targets:
            candidate, n = f"{stem}-{n}", n + 1
        return candidate


class LrOpsRouter:
    """Presents the active target's client under the ``LrOpsClient`` interface.

    ``Studio`` takes a client and calls ``run``/``reachable`` on it. Handing it
    this router instead means the destination can change at runtime without
    Studio knowing. Clients are cached per target so switching back and forth
    does not rebuild them, and ``target_id`` is exposed so callers can record
    which Lightroom they were talking to.
    """

    def __init__(self, registry: TargetRegistry, client_factory=None):
        self.registry = registry
        if client_factory is None:
            from .lrops import LrOpsClient
            client_factory = LrOpsClient
        self._factory = client_factory
        self._clients: dict[str, object] = {}

    def _client_for(self, target: Target):
        client = self._clients.get(target.id)
        if (client is None or getattr(client, "base", None) != target.base
                or getattr(client, "token", None) != target.token):
            client = self._factory(**target.client_kwargs())
            self._clients[target.id] = client
        return client

    @property
    def target(self) -> Target:
        return self.registry.active

    @property
    def target_id(self) -> str:
        return self.registry.active_id

    @property
    def catalog_id(self) -> str | None:
        return self.registry.active.catalog_id

    @property
    def access_mode(self) -> str:
        return self.registry.active.access_mode

    @property
    def catalog_identity(self) -> str | None:
        return self.registry.active.catalog_identity

    @property
    def base(self) -> str:
        return self.registry.active.base

    def _current(self):
        return self._client_for(self.registry.active)

    def pin(self):
        """Capture the active target for one multi-step read operation."""
        target = self.registry.active
        return PinnedLrOpsClient(target, self._client_for(target))

    def run(self, command: str, params: dict | None = None) -> dict:
        target = self.registry.active
        if command != "probe_client":
            if target.catalog_identity is None:
                raise TargetError(
                    "Catalog 尚未确认绑定；先调用 trlr_list_catalogs，"
                    "再由用户确认当前 Lightroom Catalog。")
            params = params or {}
            if (params.get("expected_catalog_identity") != target.catalog_identity
                    or params.get("expected_access_mode") != target.access_mode):
                raise TargetError(
                    "当前 Lightroom Catalog 与已确认的 Target 配置不一致；"
                    "请切回已绑定 Catalog，或在用户明确要求后重新确认。")
        return self._client_for(target).run(command, params)

    def reachable(self) -> bool:
        return self._current().reachable()

    def upload(self, filename: str, content: bytes) -> str:
        """Stage bytes on the *active* target's bridge.

        Worth being explicit about: a stage id only means something to the
        bridge that issued it. Uploading and then switching targets before the
        import job runs would hand the new bridge a handle it never minted.
        Multi-step callers must therefore use :meth:`pin`.
        """
        return self._current().upload(filename, content)

    def fetch_output(self, output_ref: str, destination):
        return self._current().fetch_output(output_ref, destination)

    def discard_stage(self, stage_id: str) -> None:
        return self._current().discard_stage(stage_id)

class PinnedLrOpsClient:
    """A target snapshot whose delegated calls cannot follow selection changes."""
    def __init__(self, target: Target, client):
        self.target_id, self.catalog_id, self.base = target.id, target.catalog_id, target.base
        self.access_mode = target.access_mode
        self.catalog_identity = target.catalog_identity
        self._client = client

    def run(self, *args, **kwargs):
        command = args[0] if args else kwargs.get("command")
        if command != "probe_client":
            if self.catalog_identity is None:
                raise TargetError(
                    "Catalog 尚未确认绑定；先调用 trlr_list_catalogs，"
                    "再由用户确认当前 Lightroom Catalog。")
            params = args[1] if len(args) > 1 else kwargs.get("params")
            params = params or {}
            if (params.get("expected_catalog_identity") != self.catalog_identity
                    or params.get("expected_access_mode") != self.access_mode):
                raise TargetError(
                    "当前 Lightroom Catalog 与已确认的 Target 配置不一致；"
                    "请切回已绑定 Catalog，或在用户明确要求后重新确认。")
        return self._client.run(*args, **kwargs)

    def fetch_output(self, *args, **kwargs):
        return self._client.fetch_output(*args, **kwargs)

    def upload(self, *args, **kwargs):
        return self._client.upload(*args, **kwargs)

    def discard_stage(self, *args, **kwargs):
        return self._client.discard_stage(*args, **kwargs)
