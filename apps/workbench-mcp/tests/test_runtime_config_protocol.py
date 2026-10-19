"""Conversational Runtime configuration flow through MCP's real tool router."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch as mock_patch

from workbench.catalog_discovery import catalog_identity_for_path
from workbench.configuration_help import CONFIG_TOPICS
from workbench.folders import CONFIG_ENV
from workbench.runtime import Workbench
from workbench.store import Store
from workbench.targets import LrOpsRouter, TargetRegistry
from workbench_mcp.server import TOOLS, McpServer


class BridgeState:
    def __init__(self, catalog_identity, catalog_id):
        self.reachable = True
        self.catalog_identity = catalog_identity
        self.catalog_id = catalog_id
        self.access_mode = "full"
        self.sandbox_registered = False
        self.sandbox_matches_active = False


class FakeBridge:
    def __init__(self, base, token, state):
        self.base = base
        self.token = token
        self.state = state

    def reachable(self):
        return self.state.reachable

    def run(self, command, params=None):
        assert command == "probe_client"
        return {
            "access_mode": self.state.access_mode,
            "catalog_identity": self.state.catalog_identity,
            "catalog_id": self.state.catalog_id,
            "sandbox_registered": self.state.sandbox_registered,
            "sandbox_matches_active": self.state.sandbox_matches_active,
            "catalog_authorized": (
                self.state.access_mode == "full" or
                (self.state.sandbox_registered and self.state.sandbox_matches_active)),
            "capabilities": {
                "expected_access_mode": True,
                "expected_catalog_identity": True,
                "current_folder_selection": True,
            },
        }


def call(server: McpServer, name: str, args=None) -> dict:
    response = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args or {}},
    })
    return response["result"]


def value(result: dict) -> dict:
    if result.get("isError"):
        raise AssertionError(result["content"][0]["text"])
    return json.loads(result["content"][0]["text"])


class RuntimeConfigInteractionTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder_env = mock_patch.dict(os.environ, {CONFIG_ENV: ""}, clear=False)
        self.folder_env.start()
        self.addCleanup(self.folder_env.stop)
        self.root = Path(self.temp.name) / "runtime"
        self.root.mkdir()
        self.current_path = self.root / "Current Catalog.lrcat"
        self.target_path = self.root / "Studio Catalog.lrcat"
        self.current_path.write_bytes(b"test catalog metadata only")
        self.target_path.write_bytes(b"test catalog metadata only")
        self.current_identity = catalog_identity_for_path(self.current_path)
        self.target_identity = catalog_identity_for_path(self.target_path)
        registry = TargetRegistry(self.root / "targets.json")
        self.fixture_token = secrets.token_hex(16)
        local = registry.add("本机", "http://127.0.0.1:17777",
                             token=self.fixture_token, access_mode="full")
        registry.select(local.id)
        self.bridge_state = BridgeState(self.current_identity, self.current_path.name)
        router = LrOpsRouter(
            registry,
            client_factory=lambda base, token: FakeBridge(base, token, self.bridge_state),
        )
        self.server = McpServer(Workbench(Store(self.root), router))

    def catalog_scan(self):
        return {
            "scan_status": "complete",
            "scan_scope": "home_and_mounted_local_volumes",
            "scan_roots": ["Home"],
            "scan_errors": 0,
            "catalogs": [
                {"catalog_identity": self.current_identity,
                 "catalog_id": self.current_path.name, "location_hint": "~/Test",
                 "size_bytes": self.current_path.stat().st_size, "modified_at": None,
                 "is_active": True, "_local_path": str(self.current_path)},
                {"catalog_identity": self.target_identity,
                 "catalog_id": self.target_path.name, "location_hint": "~/Test",
                 "size_bytes": self.target_path.stat().st_size, "modified_at": None,
                 "is_active": False, "_local_path": str(self.target_path)},
            ],
        }

    def test_effective_expected_mode_does_not_claim_persistence_or_write(self):
        for case, environment, persist, expected in (
            ("default", None, False, "full"),
            ("environment", "safe", False, "safe"),
            ("saved_target", "safe", True, "full"),
        ):
            with self.subTest(case=case), mock_patch.dict(os.environ):
                os.environ.pop("LR_XMP_MIMIC_ACCESS_MODE", None)
                os.environ["LROPS_BASE"] = "http://127.0.0.1:17777"
                if environment is not None:
                    os.environ["LR_XMP_MIMIC_ACCESS_MODE"] = environment
                root = self.root / case
                root.mkdir()
                path = root / "targets.json"
                registry = TargetRegistry(path)
                if persist:
                    saved = registry.add("本机", "http://127.0.0.1:17777",
                                         access_mode="full")
                    registry.select(saved.id)
                    registry = TargetRegistry(path)
                before = path.read_bytes() if path.exists() else None
                router = LrOpsRouter(
                    registry,
                    client_factory=lambda base, token: FakeBridge(
                        base, token, self.bridge_state),
                )
                server = McpServer(Workbench(Store(root), router))
                snapshot = value(call(server, "trlr_get_runtime_config", {
                    "topic": "access_mode",
                }))
                self.assertEqual(snapshot["catalog"]["access_mode"]["expected"],
                                 expected)
                self.assertEqual(path.read_bytes() if path.exists() else None,
                                 before)
                self.assertIn("期望值不等于已保存",
                              snapshot["help"]["guide"]["description"])
                self.assertNotIn(str(root), json.dumps(snapshot))

    def test_user_can_inspect_and_change_runtime_settings_without_leaks(self):
        # A conversational configuration request lands on the redacted page
        # and reveals that this Catalog needs one explicit binding confirmation.
        overview = value(call(self.server, "trlr_get_runtime_config"))
        self.assertEqual(overview["schema_version"], "runtime-config-v1")
        self.assertEqual(overview["catalog"]["binding_status"], "confirmation_required")
        self.assertEqual(overview["image_presentation"]["codex_preview_max_edge"], 1024)
        self.assertEqual(overview["image_presentation"]["edit_render_max_edge"], 2048)
        serialized = json.dumps(overview, ensure_ascii=False)
        for secret in (str(self.root), "127.0.0.1", self.fixture_token,
                       self.current_identity, self.target_identity):
            self.assertNotIn(secret, serialized)
        self.assertEqual(overview["folder_access"]["mode"], "full")
        self.assertEqual(overview["folder_access"]["roots"][0]["label"],
                         "整个 Runtime 主机")

        # Simulate the user's exact request to reduce the image budget.
        self.assertEqual(value(call(self.server, "trlr_set_presentation_config", {
            "codex_preview_max_edge": 512,
        }))["codex_preview_max_edge"], 512)
        readback = value(call(self.server, "trlr_get_runtime_config"))
        self.assertEqual(readback["image_presentation"]["codex_preview_max_edge"], 512)
        self.assertEqual(readback["image_presentation"]["edit_render_max_edge"], 2048)

        # Discovery offers the inactive Catalog as switchable, but the tool
        # cannot act until the user confirms this disruptive operation.
        with mock_patch("workbench.runtime.discover_catalogs",
                        side_effect=lambda **kwargs: self.catalog_scan()):
            catalogs = value(call(self.server, "trlr_list_catalogs"))
            target_row = next(row for row in catalogs["catalogs"]
                              if row["catalog_identity"] == self.target_identity)
            self.assertTrue(target_row["switchable"])
            self.assertNotIn("_local_path", target_row)
            denied = call(self.server, "trlr_switch_catalog", {
                "catalog_identity": self.target_identity,
                "confirm_switch_and_bind": False,
            })
            self.assertTrue(denied["isError"])
            self.assertIsNone(self.server.wb.lrops.registry.active.catalog_identity)

            def request_open(path):
                self.assertEqual(Path(path), self.target_path)
                # Lightroom can report a canonical path after the OS opened
                # the discovered spelling (for example /var -> /private/var).
                self.bridge_state.catalog_identity = catalog_identity_for_path(
                    self.target_path.resolve())
                self.bridge_state.catalog_id = "Studio Catalog.lrcat"

            with mock_patch("workbench.runtime.open_catalog_file", side_effect=request_open):
                switched = value(call(self.server, "trlr_switch_catalog", {
                    "catalog_identity": self.target_identity,
                    "confirm_switch_and_bind": True,
                }))
        self.assertTrue(switched["switched"])
        self.assertTrue(switched["bound"])
        self.assertEqual(
            switched["health"]["catalog_policy"]["catalog_identity"],
            catalog_identity_for_path(self.target_path.resolve()),
        )
        confirmed = value(call(self.server, "trlr_get_runtime_config"))
        self.assertEqual(confirmed["catalog"]["binding_status"], "authorized")
        self.assertEqual(confirmed["catalog"]["bound_catalog"], "Studio Catalog.lrcat")

        # Offline Lightroom does not hide local Runtime preferences.
        self.bridge_state.reachable = False
        offline = value(call(self.server, "trlr_get_runtime_config"))
        self.assertFalse(offline["target"]["reachable"])
        self.assertEqual(offline["image_presentation"]["codex_preview_max_edge"], 512)
        self.assertEqual(offline["catalog"]["binding_status"], "lightroom_unavailable")

    def test_tools_are_specific_and_require_catalog_switch_confirmation(self):
        by_name = {tool["name"]: tool for tool in TOOLS}
        self.assertIn("trlr_get_runtime_config", by_name)
        self.assertEqual(
            by_name["trlr_switch_catalog"]["inputSchema"]["properties"][
                "confirm_switch_and_bind"]["const"],
            True,
        )

    def test_overview_exposes_tutorial_entries_without_catalog_authorization(self):
        overview = value(call(self.server, "trlr_get_runtime_config"))
        help_index = overview["help"]
        self.assertEqual(help_index["schema_version"], "runtime-config-help-v1")
        self.assertEqual(help_index["topic"], "overview")
        self.assertIsNone(help_index["guide"])
        self.assertEqual({row["id"] for row in help_index["topics"]},
                         set(CONFIG_TOPICS) - {"overview"})
        self.assertEqual(help_index["next_step"]["topic"], "catalog")
        self.assertFalse((self.root / "presentation-settings.json").exists())

    def test_each_tutorial_returns_live_state_steps_and_a_verification_entry(self):
        schema = next(tool for tool in TOOLS
                      if tool["name"] == "trlr_get_runtime_config")["inputSchema"]
        self.assertEqual(schema["properties"]["topic"]["enum"], list(CONFIG_TOPICS))
        for topic in CONFIG_TOPICS[1:]:
            with self.subTest(topic=topic):
                result = value(call(self.server, "trlr_get_runtime_config", {"topic": topic}))
                guide = result["help"]["guide"]
                self.assertEqual(result["catalog"]["active_catalog"], self.current_path.name)
                self.assertEqual(result["help"]["topic"], topic)
                self.assertTrue(guide["steps"])
                self.assertTrue(guide["takes_effect"])
                self.assertEqual(guide["verification"]["tool"], "trlr_get_runtime_config")
                self.assertEqual(guide["verification"]["arguments"], {"topic": topic})
                serialized = json.dumps(result, ensure_ascii=False)
                for secret in (str(self.root), "127.0.0.1", self.fixture_token,
                               self.current_identity, self.target_identity):
                    self.assertNotIn(secret, serialized)

    def test_safe_tutorial_is_available_when_sandbox_blocks_operations(self):
        self.bridge_state.access_mode = "safe"
        self.bridge_state.sandbox_registered = True
        self.bridge_state.sandbox_matches_active = True
        value(call(self.server, "trlr_confirm_catalog", {
            "catalog_identity": self.current_identity,
            "confirm_current_catalog": True,
        }))
        binding_before = (self.root / "targets.json").read_bytes()
        self.bridge_state.sandbox_registered = False
        self.bridge_state.sandbox_matches_active = False
        result = value(call(self.server, "trlr_get_runtime_config", {"topic": "access_mode"}))
        self.assertEqual(result["catalog"]["access_mode"]["actual"], "safe")
        self.assertEqual(result["catalog"]["reason"], "safe_sandbox_not_registered")
        self.assertEqual(result["help"]["next_step"]["topic"], "sandbox")
        self.assertFalse(result["help"]["guide"]["editable_in_conversation"])
        self.assertIn("Save mode", " ".join(result["help"]["guide"]["steps"]))
        self.assertEqual((self.root / "targets.json").read_bytes(), binding_before)
        self.assertEqual(self.bridge_state.access_mode, "safe")

    def test_mode_mismatch_tutorial_preserves_expected_mode_and_binding(self):
        value(call(self.server, "trlr_confirm_catalog", {
            "catalog_identity": self.current_identity,
            "confirm_current_catalog": True,
        }))
        binding_before = (self.root / "targets.json").read_bytes()
        self.bridge_state.access_mode = "safe"
        result = value(call(self.server, "trlr_get_runtime_config", {"topic": "access_mode"}))
        self.assertEqual(result["catalog"]["access_mode"]["expected"], "full")
        self.assertEqual(result["catalog"]["access_mode"]["actual"], "safe")
        self.assertEqual(result["help"]["next_step"]["topic"], "access_mode")
        self.assertEqual((self.root / "targets.json").read_bytes(), binding_before)

    def test_offline_query_still_returns_local_settings_and_complete_tutorial(self):
        self.bridge_state.reachable = False
        result = value(call(self.server, "trlr_get_runtime_config", {"topic": "sandbox"}))
        self.assertIsNone(result["catalog"]["access_mode"]["actual"])
        self.assertIsNone(result["catalog"]["sandbox_registered"])
        self.assertEqual(result["image_presentation"]["codex_preview_max_edge"], 1024)
        self.assertEqual(result["help"]["next_step"]["topic"], "connection")
        self.assertIn("Register active Catalog as sandbox",
                      " ".join(result["help"]["guide"]["steps"]))

    def test_unknown_active_identity_without_reason_still_routes_to_connection(self):
        before = (self.root / "targets.json").read_bytes()
        with mock_patch.object(self.server.wb, "health", return_value={
                "lrops": True, "catalog_policy": {}}):
            result = value(call(self.server, "trlr_get_runtime_config"))
        self.assertEqual(result["catalog"]["binding_status"], "active_catalog_unknown")
        self.assertIsNone(result["catalog"]["reason"])
        self.assertEqual(result["help"]["next_step"]["topic"], "connection")
        self.assertEqual((self.root / "targets.json").read_bytes(), before)

    def test_unanswered_probe_does_not_claim_catalog_mismatch_or_change_binding(self):
        value(call(self.server, "trlr_confirm_catalog", {
            "catalog_identity": self.current_identity,
            "confirm_current_catalog": True,
        }))
        binding_before = (self.root / "targets.json").read_bytes()
        stored_catalog_name = self.server.wb.lrops.registry.active.catalog_id
        with mock_patch.object(FakeBridge, "run", side_effect=TimeoutError("probe timeout")):
            result = value(call(self.server, "trlr_get_runtime_config", {"topic": "connection"}))
        self.assertTrue(result["target"]["reachable"])
        self.assertEqual(result["catalog"]["reason"], "probe_failed")
        self.assertEqual(result["catalog"]["binding_status"], "active_catalog_unknown")
        self.assertIsNone(result["catalog"]["active_catalog"])
        self.assertFalse(result["catalog"]["authorized"])
        # With no live probe, preserve the persisted display metadata rather
        # than inventing the casing Lightroom would have returned.
        self.assertEqual(result["catalog"]["bound_catalog"], stored_catalog_name)
        self.assertEqual(result["help"]["next_step"]["topic"], "connection")
        self.assertEqual(result["image_presentation"]["codex_preview_max_edge"], 1024)
        self.assertEqual((self.root / "targets.json").read_bytes(), binding_before)

    def test_observed_other_catalog_still_reports_real_mismatch(self):
        value(call(self.server, "trlr_confirm_catalog", {
            "catalog_identity": self.current_identity,
            "confirm_current_catalog": True,
        }))
        self.bridge_state.catalog_identity = self.target_identity
        self.bridge_state.catalog_id = self.target_path.name
        result = value(call(self.server, "trlr_get_runtime_config"))
        self.assertEqual(result["catalog"]["binding_status"], "active_catalog_mismatch")
        self.assertEqual(result["catalog"]["reason"], "catalog_identity_mismatch")
        self.assertEqual(result["catalog"]["active_catalog"], self.target_path.name)

    def test_unbound_target_with_failed_probe_still_has_unknown_active_catalog(self):
        binding_before = (self.root / "targets.json").read_bytes()
        with mock_patch.object(FakeBridge, "run", side_effect=TimeoutError("probe timeout")):
            result = value(call(self.server, "trlr_get_runtime_config"))
        self.assertEqual(result["catalog"]["binding_status"], "active_catalog_unknown")
        self.assertIsNone(result["catalog"]["bound_catalog"])
        self.assertEqual(result["help"]["next_step"]["topic"], "connection")
        self.assertEqual((self.root / "targets.json").read_bytes(), binding_before)

    def test_binding_record_is_independent_of_display_name_and_live_probe(self):
        cases = (
            ("unbound_online", False, None, "online", "confirmation_required", "known"),
            ("unbound_probe_failed", False, None, "probe_failed", "active_catalog_unknown", "unknown"),
            ("bound_without_name_probe_failed", True, None, "probe_failed", "active_catalog_unknown", "unknown"),
            ("bound_named_probe_failed", True, "Stored Catalog.lrcat", "probe_failed", "active_catalog_unknown", "unknown"),
            ("bound_mismatch", True, "Stored Catalog.lrcat", "mismatch", "active_catalog_mismatch", "known"),
            ("bound_offline", True, None, "offline", "lightroom_unavailable", "unknown"),
            ("bound_online", True, None, "online", "authorized", "known"),
            ("unbound_with_display_name", False, "Display Only.lrcat", "online", "confirmation_required", "known"),
        )
        for name, bound, display_name, live, status, observation in cases:
            with self.subTest(name=name):
                case_root = self.root / name
                case_root.mkdir()
                registry = TargetRegistry(case_root / "targets.json")
                target = registry.add(
                    "本机", "http://127.0.0.1:17777", access_mode="full",
                    catalog_identity=self.current_identity if bound else None,
                    catalog_id=display_name,
                )
                registry.select(target.id)
                state = BridgeState(
                    self.target_identity if live == "mismatch" else self.current_identity,
                    self.current_path.name,
                )
                state.reachable = live != "offline"
                router = LrOpsRouter(
                    registry, client_factory=lambda base, token: FakeBridge(base, token, state))
                server = McpServer(Workbench(Store(case_root), router))
                before = (case_root / "targets.json").read_bytes()
                probe = (mock_patch.object(FakeBridge, "run", side_effect=TimeoutError("probe timeout"))
                         if live == "probe_failed" else nullcontext())
                with probe:
                    result = value(call(server, "trlr_get_runtime_config", {"topic": "catalog"}))
                catalog = result["catalog"]
                self.assertIs(catalog["binding_recorded"], bound)
                self.assertEqual(catalog["active_identity_status"], observation)
                self.assertEqual(catalog["binding_status"], status)
                if observation == "unknown":
                    self.assertEqual(catalog["bound_catalog"], registry.active.catalog_id)
                self.assertEqual((case_root / "targets.json").read_bytes(), before)
                serialized = json.dumps(result, ensure_ascii=False)
                for secret in (self.current_identity, self.target_identity, str(case_root)):
                    self.assertNotIn(secret, serialized)
                self.assertIn("catalog.binding_recorded", result["help"]["guide"]["read_fields"])
                self.assertIn("名称为空不代表未绑定", result["help"]["guide"]["description"])

    def test_invalid_topics_are_rejected_before_bridge_probe(self):
        for topic in ("safe", "unknown", None, True, [], {}):
            with self.subTest(topic=topic), mock_patch.object(self.server.wb, "health") as health:
                result = call(self.server, "trlr_get_runtime_config", {"topic": topic})
                self.assertTrue(result["isError"])
                self.assertIn("未知配置主题", result["content"][0]["text"])
                health.assert_not_called()

    def test_tutorial_values_are_independent_across_calls(self):
        result = self.server.wb.runtime_config(topic="access_mode")
        result["help"]["guide"]["steps"].clear()
        next_result = self.server.wb.runtime_config(topic="access_mode")
        self.assertTrue(next_result["help"]["guide"]["steps"])

    def test_unreadable_image_config_preserves_other_state_and_help(self):
        settings = self.root / "presentation-settings.json"
        settings.write_text("not valid JSON", encoding="utf-8")
        result = value(call(self.server, "trlr_get_runtime_config", {"topic": "image_presentation"}))
        self.assertEqual(result["image_presentation"]["config_status"], "unreadable")
        self.assertIsNone(result["image_presentation"]["codex_preview_max_edge"])
        self.assertFalse(result["help"]["guide"]["editable_in_conversation"])
        self.assertEqual(result["catalog"]["active_catalog"], self.current_path.name)
        overview = value(call(self.server, "trlr_get_runtime_config"))
        entry = next(row for row in overview["help"]["topics"]
                     if row["id"] == "image_presentation")
        self.assertFalse(entry["editable_in_conversation"])
        self.assertEqual(settings.read_text(encoding="utf-8"), "not valid JSON")

    def test_catalog_inventory_only_marks_supported_local_hosts_switchable(self):
        with mock_patch("workbench.runtime.discover_catalogs",
                        side_effect=lambda **kwargs: self.catalog_scan()), \
             mock_patch("workbench.runtime.is_supported_host", return_value=False):
            catalogs = value(call(self.server, "trlr_list_catalogs"))
        self.assertFalse(any(row["switchable"] for row in catalogs["catalogs"]))

    def test_switch_timeout_leaves_the_existing_binding_unchanged(self):
        with mock_patch("workbench.runtime.discover_catalogs",
                        side_effect=lambda **kwargs: self.catalog_scan()):
            call(self.server, "trlr_list_catalogs")
            with mock_patch("workbench.runtime.open_catalog_file") as open_file, \
                 mock_patch("workbench.runtime.time.monotonic", side_effect=[0.0, 21.0]), \
                 mock_patch("workbench.runtime.time.sleep"):
                result = value(call(self.server, "trlr_switch_catalog", {
                    "catalog_identity": self.target_identity,
                    "confirm_switch_and_bind": True,
                }))
        open_file.assert_called_once_with(self.target_path)
        self.assertFalse(result["switched"])
        self.assertFalse(result["bound"])
        self.assertIsNone(self.server.wb.lrops.registry.active.catalog_identity)

    def test_remote_target_cannot_be_switched_from_this_runtime_host(self):
        registry = self.server.wb.lrops.registry
        remote = registry.add("Remote", "https://bridge.remote.invalid:17777")
        registry.select(remote.id)
        with mock_patch("workbench.runtime.open_catalog_file") as open_file:
            result = call(self.server, "trlr_switch_catalog", {
                "catalog_identity": self.target_identity,
                "confirm_switch_and_bind": True,
            })
        self.assertTrue(result["isError"])
        self.assertIn("远端 Lightroom Target", result["content"][0]["text"])
        open_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()
