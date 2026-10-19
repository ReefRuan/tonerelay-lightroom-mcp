import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lr_ops import cli


IDENTITY = "catalog-md5-v1:0123456789abcdef0123456789abcdef"
CAPABILITIES = {
    "expected_access_mode": True,
    "expected_catalog_identity": True,
}


class CatalogAwareCliTests(unittest.TestCase):
    def test_bound_submitter_probes_once_and_injects_the_envelope(self):
        probe = {"access_mode": "full", "catalog_identity": IDENTITY,
                 "capabilities": CAPABILITIES, "catalog_authorized": True}
        with mock.patch.object(cli, "submit_and_wait",
                               side_effect=[probe, {"target": None}]) as submit:
            bound = cli.bound_submitter(timeout=17)
            result = bound("get_selection", {})
        self.assertEqual(result, {"target": None})
        self.assertEqual(submit.call_args_list, [
            mock.call("probe_client", {}, timeout=17),
            mock.call("get_selection", {
                "expected_access_mode": "full",
                "expected_catalog_identity": IDENTITY,
            }, timeout=17),
        ])

    def test_probe_command_remains_unbound(self):
        probe = {"access_mode": "safe", "catalog_identity": IDENTITY,
                 "capabilities": CAPABILITIES, "catalog_authorized": True}
        stdout = io.StringIO()
        with mock.patch.object(cli, "submit_and_wait", return_value=probe) as submit, \
                mock.patch("sys.stdout", stdout), \
                mock.patch.object(cli, "bound_submitter") as bind:
            cli.main(["probe"])
        submit.assert_called_once_with("probe_client", {})
        bind.assert_not_called()
        self.assertEqual(json.loads(stdout.getvalue()), probe)

    def test_import_binds_before_staging_and_uses_one_bound_submitter(self):
        events = []

        def submit(command, params):
            events.append(("submit", command, params))
            return {"photo": {"uuid": "imported"}}

        def bind():
            events.append(("bind",))
            return submit

        def upload(path):
            events.append(("upload", path.name))
            return "stage-1"

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "photo.jpg"
            source.write_bytes(b"jpeg")
            with mock.patch.object(cli, "bound_submitter", side_effect=bind), \
                    mock.patch.object(cli, "upload_file", side_effect=upload), \
                    mock.patch("sys.stdout", io.StringIO()):
                cli.main(["import", str(source)])
        self.assertEqual(events, [
            ("bind",),
            ("upload", "photo.jpg"),
            ("submit", "import_photo", {"stage_id": "stage-1"}),
        ])

    def test_invalid_probe_contract_stops_before_the_command(self):
        with mock.patch.object(cli, "submit_and_wait",
                               return_value={"catalog_identity": IDENTITY}) as submit:
            with self.assertRaisesRegex(SystemExit, "Cannot bind"):
                cli.bound_submitter()
        submit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
