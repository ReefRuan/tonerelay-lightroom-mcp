"""Connect Runtime clients to the lr-ops core bundled with MCP."""
from lr_ops.bridge_lifecycle import BundledBridge, BridgeStartError
from .lrops import LrOpsClient, LrOpsError
from .targets import LrOpsRouter


def make_router(registry, data_root):
    manager = BundledBridge(data_root, registry.config)

    class BundledClient(LrOpsClient):
        def _urlopen(self, request):
            # Preflight all JSON and file paths. Never retry a submitted write
            # after a network failure. Journal recovery handles result queries.
            try:
                manager.ensure(self.base, self.token)
            except BridgeStartError as exc:
                raise LrOpsError(str(exc)) from exc
            return super()._urlopen(request)

    router = LrOpsRouter(registry, client_factory=BundledClient)
    router.bridge_manager = manager
    return router
