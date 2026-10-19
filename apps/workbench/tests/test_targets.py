"""LR targets: the registry, the router seam, and episode-to-target binding.

Three separate things are being pinned down here:

* the registry is *lazy* -- a single-target deployment that never adds a target
  must keep behaving exactly as it did before this module existed;
* the router really does re-point traffic when the selection changes, and hands
  file handles to whichever bridge is active;
* an episode created against one Lightroom refuses to be rendered against
  another, because a photo uuid is only meaningful inside one catalog and the
  failure mode is a wrong-photo write.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lr_ops.configuration import defaults
from workbench.studio.engine import Studio
from workbench.targets import (DEFAULT_TARGET_ID, LrOpsRouter, Target,
                               TargetError, TargetRegistry, is_loopback_base)


class RecordingClient:
    """Stands in for LrOpsClient, remembering which bridge it speaks for."""

    def __init__(self, base=None, token=None, **kwargs):
        self.base = base
        self.token = token
        self.calls = []
        self.probe_result = {
            "answered_by": self.base,
            "command": "probe_client",
            "access_mode": "full",
            "catalog_identity": "catalog-md5-v1:recording-client",
            "catalog_id": "test.lrcat",
            "sandbox": False,
            "sandbox_registered": False,
            "sandbox_matches_active": False,
            "catalog_authorized": True,
            "capabilities": {
                "develop_snapshot": True,
                "expected_access_mode": True,
                "expected_catalog_identity": True,
            },
        }

    def reachable(self):
        return not str(self.base).startswith("http://down")

    def run(self, command, params=None):
        self.calls.append((command, params))
        if command == "probe_client":
            return dict(self.probe_result)
        return {"answered_by": self.base, "command": command}

    def upload(self, filename, content):
        self.calls.append(("upload", filename))
        return f"{self.base}#stage"

    def fetch_output(self, ref, destination):
        self.calls.append(("fetch_output", ref))
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"staged-bytes")
        return destination

    def discard_stage(self, stage_id):
        self.calls.append(("discard", stage_id))


class RegistryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"

    def registry(self, seed_base=None, env=None):
        with mock.patch.dict(os.environ, env or {}, clear=False):
            for key in ("LROPS_BASE", "LR_XMP_MIMIC_LOCAL_TOKEN",
                        "LR_XMP_MIMIC_ACCESS_MODE"):
                if not (env or {}).get(key):
                    os.environ.pop(key, None)
            return TargetRegistry(self.path, seed_base=seed_base)


class TestTargetRegistrySeed(RegistryTestCase):
    def test_seeds_loopback_when_nothing_is_configured(self):
        registry = self.registry()
        self.assertEqual(registry.active_id, DEFAULT_TARGET_ID)
        self.assertEqual(registry.active.base, "http://127.0.0.1:17777")
        self.assertTrue(registry.active.is_default)
        self.assertEqual(registry.active.access_mode, "full")
        self.assertIsNone(registry.active.catalog_identity)

    def test_explicit_base_wins_over_environment(self):
        registry = self.registry(seed_base="http://192.168.1.50:17777",
                                 env={"LROPS_BASE": "http://ignored:17777"})
        self.assertEqual(registry.active.base, "http://192.168.1.50:17777")

    def test_environment_base_used_when_no_argument(self):
        registry = self.registry(env={"LROPS_BASE": "http://10.0.0.9:17777"})
        self.assertEqual(registry.active.base, "http://10.0.0.9:17777")
        # A remote seed is labelled by its URL, so the list is not a lie.
        self.assertEqual(registry.active.label, "http://10.0.0.9:17777")

    def test_token_seeded_from_environment(self):
        registry = self.registry(env={"LR_XMP_MIMIC_LOCAL_TOKEN": "s3cret"})
        self.assertEqual(registry.active.token, "s3cret")

    def test_access_mode_seeded_from_environment(self):
        registry = self.registry(env={"LR_XMP_MIMIC_ACCESS_MODE": "safe"})
        self.assertEqual(registry.active.access_mode, "safe")

    def test_first_run_persists_nothing(self):
        registry = self.registry()
        self.assertFalse(registry.persisted())
        self.assertFalse(self.path.exists())

    def test_corrupt_config_is_rejected_without_seed_fallback(self):
        self.path.write_text("{ this is not json", encoding="utf-8")
        with self.assertRaises(TargetError):
            self.registry(seed_base="http://fallback:17777")

    def test_invalid_target_rejects_the_complete_config(self):
        data = defaults()
        data["targets"]["targets"][0]["base"] = "ftp://nope"
        self.path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(TargetError):
            self.registry()

    def test_old_registry_file_is_not_imported(self):
        legacy = self.path.with_name("targets.json")
        legacy.write_text(json.dumps({"active": "old", "targets": [
            {"id": "old", "label": "old", "base": "http://old:17777"}]}))
        self.assertEqual(self.registry().active.id, "local")
        self.assertFalse(self.path.exists())

class TestTargetRegistryWrites(RegistryTestCase):
    def test_add_persists_and_survives_reload(self):
        fixture_token = "tok"
        registry = self.registry()
        registry.add(label="工作机", base="http://10.0.0.4:17777/", token=fixture_token,
                     catalog_id="SANDBOX.LRCAT", access_mode="safe",
                     catalog_identity="catalog-md5-v1:work")
        again = self.registry()
        ids = [t.id for t in again.list()]
        self.assertIn("工作机".lower().replace(" ", "-"), ids)
        added = again.get("工作机".lower().replace(" ", "-"))
        self.assertEqual(added.base, "http://10.0.0.4:17777")  # trailing slash dropped
        self.assertEqual(added.token, "tok")
        self.assertEqual(added.catalog_id, "sandbox.lrcat")
        self.assertEqual(added.access_mode, "safe")
        self.assertEqual(added.catalog_identity, "catalog-md5-v1:work")

    def test_invalid_access_mode_is_rejected(self):
        with self.assertRaises(TargetError):
            self.registry().add(label="bad", base="http://bad:17777",
                                access_mode="sandbox")

    def test_default_sorts_first(self):
        registry = self.registry()
        registry.add(label="remote", base="http://10.0.0.4:17777")
        self.assertTrue(registry.list()[0].is_default)

    def test_select_persists_active(self):
        registry = self.registry()
        added = registry.add(label="remote", base="http://10.0.0.4:17777")
        registry.select(added.id)
        self.assertEqual(registry.active_id, added.id)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["targets"]["active"], added.id)
        self.assertEqual(self.registry().active_id, added.id)

    def test_remove_refuses_the_default(self):
        registry = self.registry()
        with self.assertRaises(TargetError):
            registry.remove(DEFAULT_TARGET_ID)

    def test_remove_refuses_the_only_remaining_target(self):
        data = defaults()
        data["targets"] = {"active": "solo", "targets": [
            Target(id="solo", label="solo", base="http://solo:17777").to_json()]}
        self.path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(TargetError):
            self.registry().remove("solo")

    def test_removing_the_active_target_falls_back_to_another(self):
        registry = self.registry()
        added = registry.add(label="remote", base="http://10.0.0.4:17777")
        registry.select(added.id)
        registry.remove(added.id)
        self.assertEqual(registry.active_id, DEFAULT_TARGET_ID)

    def test_bad_base_rejected_on_add_and_update(self):
        registry = self.registry()
        with self.assertRaises(TargetError):
            registry.add(label="x", base="10.0.0.4:17777")
        with self.assertRaises(TargetError):
            registry.update(DEFAULT_TARGET_ID, base="not-a-url")

    def test_duplicate_id_rejected(self):
        registry = self.registry()
        registry.add(label="remote", base="http://10.0.0.4:17777")
        with self.assertRaises(TargetError):
            registry.add(label="other", base="http://10.0.0.5:17777", target_id="remote")

    def test_unknown_target_lookup_raises(self):
        with self.assertRaises(TargetError):
            self.registry().get("nope")


class TestRouter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"
        self.registry = TargetRegistry(self.path)
        self.router = LrOpsRouter(self.registry, client_factory=RecordingClient)

    def test_traffic_follows_the_selection(self):
        self.assertEqual(self.router.run("probe_client")["answered_by"],
                         "http://127.0.0.1:17777")
        added = self.registry.add(label="remote", base="http://10.0.0.4:17777")
        self.registry.select(added.id)
        self.assertEqual(self.router.run("probe_client")["answered_by"],
                         "http://10.0.0.4:17777")
        self.assertEqual(self.router.target_id, added.id)
        self.assertEqual(self.router.base, "http://10.0.0.4:17777")

    def test_clients_are_cached_per_target(self):
        first = self.router._current()
        self.registry.add(label="remote", base="http://10.0.0.4:17777")
        self.registry.select("remote")
        remote = self.router._current()
        self.registry.select(DEFAULT_TARGET_ID)
        self.assertIs(self.router._current(), first)   # not rebuilt
        self.assertIsNot(remote, first)

    def test_unconfigured_target_never_inherits_another_bridge_credential(self):
        fixture_token = "fixture-bridge-credential"
        with mock.patch.dict(os.environ, {"LR_XMP_MIMIC_LOCAL_TOKEN": fixture_token}):
            registry = TargetRegistry(self.path)
            remote = registry.add("remote", "http://remote.invalid:17777")
            registry.select(remote.id)
            router = LrOpsRouter(registry)
            first = router._current()
            self.assertIsNone(first.token)
            registry.update(remote.id, token=fixture_token)
            authenticated = router._current()
            self.assertIsNot(authenticated, first)
            self.assertEqual(authenticated.token, fixture_token)
            registry.update(remote.id, token="")
            self.assertIsNone(router._current().token)

    def test_persisted_config_wins_over_invalid_inherited_mode(self):
        self.registry.select(DEFAULT_TARGET_ID)
        with mock.patch.dict(os.environ, {"LR_XMP_MIMIC_ACCESS_MODE": "invalid"}):
            self.assertEqual(TargetRegistry(self.path).active.access_mode, "full")

    def test_request_validates_and_routes_the_same_target_snapshot(self):
        bound = self.registry.bind_active_catalog(
            access_mode="full", catalog_identity="catalog-md5-v1:bound", catalog_id="bound.lrcat")
        other = Target(id="other", label="other", base="http://other.invalid:17777")
        with mock.patch.object(TargetRegistry, "active", new_callable=mock.PropertyMock,
                               side_effect=[bound, other]) as active:
            result = self.router.run("get_develop", {
                "expected_catalog_identity": bound.catalog_identity,
                "expected_access_mode": bound.access_mode,
            })
        self.assertEqual(active.call_count, 1)
        self.assertEqual(result["answered_by"], bound.base)

    def test_pin_stays_on_target_after_selection_changes(self):
        pinned = self.router.pin()
        self.registry.add(label="remote", base="http://10.0.0.4:17777", catalog_id="sandbox.lrcat")
        self.registry.select("remote")
        self.assertEqual(pinned.target_id, DEFAULT_TARGET_ID)
        self.assertEqual(pinned.run("probe_client")["answered_by"], "http://127.0.0.1:17777")

    def test_pin_carries_the_identity_a_preview_is_recorded_under(self):
        """A pinned snapshot is what a fast preview attributes its bytes to."""
        remote = self.registry.add(label="remote", base="http://10.0.0.4:17777",
                                   catalog_id="SANDBOX.LRCAT", access_mode="safe",
                                   catalog_identity="catalog-md5-v1:sandbox")
        self.registry.select(remote.id)
        pinned = self.router.pin()
        self.registry.select(DEFAULT_TARGET_ID)
        self.assertEqual(pinned.target_id, "remote")
        self.assertEqual(pinned.catalog_id, "sandbox.lrcat")
        self.assertEqual(pinned.access_mode, "safe")
        self.assertEqual(pinned.catalog_identity, "catalog-md5-v1:sandbox")
        self.assertEqual(pinned.base, "http://10.0.0.4:17777")
        self.assertEqual(pinned.run("probe_client")["answered_by"], "http://10.0.0.4:17777")

    def test_pin_without_a_selected_target_is_the_local_one(self):
        pinned = self.router.pin()
        self.assertEqual(pinned.target_id, DEFAULT_TARGET_ID)
        self.assertIsNone(pinned.catalog_id)
        self.assertTrue(is_loopback_base(pinned.base))


    def test_rebuilt_when_the_base_url_is_edited(self):
        original = self.router._current()
        self.registry.update(DEFAULT_TARGET_ID, base="http://127.0.0.1:18000")
        self.assertIsNot(self.router._current(), original)
        self.assertEqual(self.router.base, "http://127.0.0.1:18000")

    def test_file_handles_go_to_the_active_bridge(self):
        local = self.router._current()          # instantiate the default target
        added = self.registry.add(label="remote", base="http://10.0.0.4:17777")
        self.registry.select(added.id)
        stage = self.router.upload("a.jpg", b"bytes")
        self.assertEqual(stage, "http://10.0.0.4:17777#stage")
        destination = Path(self.tmp.name) / "out" / "pulled.jpg"
        self.assertEqual(self.router.fetch_output("ref-1", destination), destination)
        self.assertEqual(destination.read_bytes(), b"staged-bytes")
        # The local bridge was never asked for a file: handles belong to the
        # bridge that minted them, and that is the active one.
        self.assertEqual([c[0] for c in local.calls], [])
        self.assertEqual([c[0] for c in self.router._current().calls],
                         ["upload", "fetch_output"])

class TestLoopbackClassification(unittest.TestCase):
    """Network-location classification, independent of Catalog identity."""

    def test_hosts_on_this_machine_are_loopback(self):
        for base in ("http://127.0.0.1:17777", "http://localhost:17777",
                     "http://[::1]:17777", "http://127.0.0.5:17777",
                     "HTTP://LOCALHOST:17777"):
            with self.subTest(base=base):
                self.assertTrue(is_loopback_base(base))

    def test_anything_else_is_not_loopback(self):
        for base in ("http://10.0.0.4:17777", "http://192.168.1.20:17777",
                     "http://lr.local:17777", "http://not-loopback", "", None):
            with self.subTest(base=base):
                self.assertFalse(is_loopback_base(base))


class TestEpisodeTargetBinding(unittest.TestCase):
    """The guard that keeps an episode pinned to the Lightroom it came from."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        registry = TargetRegistry(Path(self.tmp.name) / "config.json")
        self.registry = registry
        self.router = LrOpsRouter(registry, client_factory=RecordingClient)
        self.studio = Studio(Path(self.tmp.name), self.router)
        registry.add(label="remote", base="http://10.0.0.4:17777")

    def test_episode_from_another_target_is_refused(self):
        self.registry.select("remote")
        with self.assertRaises(ValueError) as caught:
            self.studio.require_target({"target_id": DEFAULT_TARGET_ID})
        self.assertIn("另一台 Lightroom", str(caught.exception))

    def test_episode_on_the_active_target_is_allowed(self):
        self.studio.require_target({"target_id": DEFAULT_TARGET_ID})  # no raise

    def test_legacy_episode_without_a_target_is_adopted(self):
        self.studio.require_target({})
        self.studio.require_target({"target_id": None})

    def test_bind_target_stamps_the_active_one(self):
        state = {}
        self.studio.bind_target(state)
        self.assertEqual(state["target_id"], DEFAULT_TARGET_ID)
        self.registry.select("remote")
        self.studio.bind_target(state)
        self.assertEqual(state["target_id"], "remote")

    def test_plain_client_disables_the_whole_check(self):
        """A single-target deployment must not be affected by any of this."""
        plain = Studio(Path(self.tmp.name), RecordingClient(base="http://solo:17777"))
        plain.require_target({"target_id": "something-else"})  # no raise
        state = {}
        plain.bind_target(state)
        self.assertEqual(state, {})

    def test_unbound_episode_adopts_on_bind(self):
        state = {"target_id": None}
        self.studio.bind_target(state)
        self.assertEqual(state["target_id"], DEFAULT_TARGET_ID)

    def test_mutating_entry_point_refuses_a_foreign_episode(self):
        """`_begin` is the single funnel for every write, so it must guard."""
        state = {"target_id": None, "revision": 0, "status": "ready",
                 "operations": {}, "error": None}
        self.studio.bind_target(state)          # created against local
        self.registry.select("remote")          # user switches Lightroom
        with self.assertRaises(ValueError):
            self.studio._begin(state, 0, "req-1", {"kind": "probe"})

    def test_remote_probe_uses_only_opaque_identity_for_binding(self):
        remote = self.registry.add(label="remote-id", base="http://10.0.0.8:17777",
                                   catalog_id="sandbox.lrcat", access_mode="safe",
                                   catalog_identity="catalog-md5-v1:sandbox")
        self.registry.select(remote.id)
        result = self.studio.run("probe_client")
        params = self.router._current().calls[-1][1]
        self.assertNotIn("expected_catalog_path", params)
        self.assertNotIn("expected_catalog_id", params)
        self.assertEqual(params["expected_access_mode"], "safe")
        self.assertEqual(params["expected_catalog_identity"],
                         "catalog-md5-v1:sandbox")

    def test_non_probe_run_requires_bound_catalog_identity(self):
        with self.assertRaisesRegex(ValueError, "expected_catalog_identity"):
            self.studio.run("list_photos", {"limit": 1})


if __name__ == "__main__":
    unittest.main()
