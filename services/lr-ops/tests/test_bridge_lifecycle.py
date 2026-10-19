"""Bundled local mode preserves credentials, ownership and existing configs."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from lr_ops.bridge_lifecycle import BundledBridge, BridgeStartError, bridge_lock, path_fingerprint
from lr_ops.configuration import ConfigError, ConfigStore, defaults, validate


class BridgeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.env = mock.patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = ConfigStore(self.root / "workbench" / "config.json")
        self.manager = BundledBridge(self.root, self.config)

    def matching(self, credential=False):
        return {"ok": True, "source_sha256": self.manager.expected_source,
                "data_root_sha256": path_fingerprint(self.root),
                "credential_configured": credential}

    def use_builtin_owner(self):
        """Ownership negotiation is opt-in; tests of that path must say so."""
        self.config.initialize()
        self.config.update("bridge", lambda b: b.update(mode="builtin"))

    def test_new_default_and_old_three_field_config(self):
        value = defaults()
        self.assertEqual(value["bridge"]["mode"], "external")
        value["bridge"].pop("mode")
        self.config._write(value)
        before = self.config.path.read_bytes()
        self.assertEqual(self.manager._settings()[1].get("mode", "external"), "external")
        self.assertEqual(self.config.path.read_bytes(), before)
        value["bridge"]["mode"] = "invalid"
        with self.assertRaises(ConfigError):
            validate(value, self.root)

    def test_remote_and_explicit_external_never_start_or_probe(self):
        with mock.patch.object(self.manager, "_health") as health, mock.patch("subprocess.Popen") as spawn:
            self.assertEqual(self.manager.ensure("http://192.0.2.1:17777")["mode"], "external")
            self.config.initialize()
            self.config.update("bridge", lambda b: b.update(mode="external"))
            self.assertEqual(self.manager.ensure("http://127.0.0.1:17777")["mode"], "external")
            health.assert_not_called()
            spawn.assert_not_called()

    def test_external_background_start_does_not_create_thread_or_probe(self):
        with (mock.patch("lr_ops.bridge_lifecycle.threading.Thread") as thread,
              mock.patch.object(self.manager, "_health") as health,
              mock.patch("subprocess.Popen") as spawn):
            self.assertIsNone(self.manager.start_background("http://127.0.0.1:17777"))
            self.assertEqual(self.manager.status, {"mode": "external", "status": "client_managed"})
            self.use_builtin_owner()
            self.assertIsNone(self.manager.start_background("http://192.0.2.1:17777"))
            thread.assert_not_called()
            health.assert_not_called()
            spawn.assert_not_called()

    def test_builtin_background_does_not_wait_for_negotiation_and_is_single_flight(self):
        self.use_builtin_owner()
        entered, release = threading.Event(), threading.Event()

        def delayed_ensure(base, token):
            entered.set()
            if not release.wait(3):
                raise AssertionError("test did not release Bridge negotiation")
            return {"mode": "builtin", "status": "reused"}

        with mock.patch.object(self.manager, "_ensure", side_effect=delayed_ensure) as ensure:
            thread = self.manager.start_background("http://127.0.0.1:17777")
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(thread.is_alive())
                self.assertEqual(self.manager.status["status"], "starting")
                self.assertIs(self.manager.start_background("http://127.0.0.1:17777"), thread)
                self.assertTrue(thread.daemon)
            finally:
                release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            ensure.assert_called_once()

    def test_background_failure_is_available_to_health_without_protocol_output(self):
        self.use_builtin_owner()
        with (mock.patch.object(self.manager, "_ensure", side_effect=BridgeStartError("bridge.busy: another startup")),
              mock.patch("sys.stdout") as stdout, mock.patch("sys.stderr") as stderr):
            thread = self.manager.start_background("http://127.0.0.1:17777")
            thread.join(3)
            self.assertEqual(self.manager.status, {
                "mode": "builtin", "status": "unavailable", "reason": "bridge.busy: another startup"})
            stdout.write.assert_not_called()
            stderr.write.assert_not_called()

    def test_external_request_does_not_wait_for_prior_builtin_warmup(self):
        self.use_builtin_owner()
        entered, release = threading.Event(), threading.Event()

        def delayed_owner(*args):
            entered.set()
            if not release.wait(3):
                raise AssertionError("test did not release Bridge negotiation")
            return {"mode": "builtin", "status": "reused"}

        with mock.patch.object(self.manager, "_ensure_builtin", side_effect=delayed_owner):
            thread = self.manager.start_background("http://127.0.0.1:17777")
            try:
                self.assertTrue(entered.wait(2))
                self.config.update("bridge", lambda bridge: bridge.update(mode="external"))
                self.assertEqual(self.manager.ensure("http://127.0.0.1:17777"), {
                    "mode": "external", "status": "client_managed"})
                self.assertTrue(thread.is_alive())
            finally:
                release.set()
                thread.join(3)

    def test_background_config_error_is_recorded_without_raw_details(self):
        with mock.patch.object(self.manager, "_settings", side_effect=ConfigError("secret-test-config")):
            self.assertIsNone(self.manager.start_background("http://127.0.0.1:17777"))
        self.assertEqual(self.manager.status["status"], "unavailable")
        self.assertNotIn("secret-test-config", self.manager.status["reason"])

    def test_unmanaged_external_environment_override(self):
        with mock.patch.dict(os.environ, {"TONERELAY_BRIDGE_MODE": "external"}):
            self.assertEqual(self.manager.ensure("http://127.0.0.1:17777")["mode"], "external")

    def test_refuses_nonloopback_bind_and_port_drift(self):
        self.use_builtin_owner()
        self.config.update("bridge", lambda b: b.update(host="0.0.0.0"))
        with self.assertRaisesRegex(BridgeStartError, "invalid_bind"):
            self.manager.ensure("http://127.0.0.1:17777")
        self.config.update("bridge", lambda b: b.update(host="127.0.0.1"))
        with self.assertRaisesRegex(BridgeStartError, "endpoint_mismatch"):
            self.manager.ensure("http://127.0.0.1:17778")

    def test_wrong_source_or_data_root_is_not_replaced(self):
        self.use_builtin_owner()
        for key in ("source_sha256", "data_root_sha256"):
            health = self.matching()
            health[key] = "0" * 64
            with mock.patch.object(self.manager, "_health", return_value=health), mock.patch("subprocess.Popen") as spawn:
                with self.assertRaisesRegex(BridgeStartError, "identity_mismatch"):
                    self.manager.ensure("http://127.0.0.1:17777")
                spawn.assert_not_called()
                self.assertEqual(self.manager.status["status"], "unavailable")

    def test_credentials_are_preserved_and_never_returned(self):
        self.use_builtin_owner()
        token = "local-test-credential"
        with mock.patch.object(self.manager, "_health", return_value=self.matching(True)), mock.patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = b'{"ok":true}'
            result = self.manager.ensure("http://127.0.0.1:17777", token)
            self.assertEqual(result, {"mode": "builtin", "status": "reused"})
            self.assertNotIn(token, json.dumps(result))
            req = opener.return_value.open.call_args.args[0]
            self.assertEqual(req.get_header("X-lr-xmp-token"), token)
        with mock.patch.object(self.manager, "_health", return_value=self.matching(True)):
            with self.assertRaisesRegex(BridgeStartError, "credential_mismatch"):
                self.manager.ensure("http://127.0.0.1:17777")

    def test_authenticated_identity_must_accept_configured_token(self):
        self.use_builtin_owner()
        with mock.patch.object(self.manager, "_health", return_value=self.matching(True)), mock.patch("urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = OSError("denied")
            with self.assertRaisesRegex(BridgeStartError, "credential_mismatch"):
                self.manager.ensure("http://127.0.0.1:17777", "test")

    def test_lock_refuses_a_second_owner_then_releases(self):
        path = self.root / "lr-ops" / ".owner.lock"
        with bridge_lock(path):
            with self.assertRaisesRegex(BridgeStartError, "busy"):
                with bridge_lock(path, timeout=0):
                    self.fail("two owners")
        with bridge_lock(path, timeout=0):
            pass

    def test_managed_environment_cannot_override_external_config(self):
        self.config.initialize()
        self.config.update("bridge", lambda b: b.update(mode="external"))
        with mock.patch.dict(os.environ, {"TONERELAY_CONFIG": str(self.config.path), "TONERELAY_BRIDGE_MODE": "builtin"}):
            manager = BundledBridge(self.root, self.config)
            self.assertEqual(manager.ensure("http://127.0.0.1:17777")["mode"], "external")

    def test_old_worker_cannot_resurrect_bridge_after_active_upgrade(self):
        self.use_builtin_owner()
        self.manager.installed_root = self.root
        with mock.patch.object(self.manager, "_health") as health, mock.patch("subprocess.Popen") as spawn:
            with self.assertRaisesRegex(BridgeStartError, "active_version_changed"):
                self.manager.ensure("http://127.0.0.1:17777")
            health.assert_not_called()
            spawn.assert_not_called()

    def test_bridge_installer_detection_receives_source_root_not_version_directory(self):
        with mock.patch("lr_ops.bridge_lifecycle.installed_runtime_root", return_value=None) as detect:
            BundledBridge(self.root, self.config)
            detect.assert_called_once_with(Path(__file__).resolve().parents[3])
