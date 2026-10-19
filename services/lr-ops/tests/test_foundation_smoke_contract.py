import unittest
from unittest import mock

import real_foundation_smoke as smoke


IDENTITY = "catalog-md5-v1:0123456789abcdef0123456789abcdef"


class FoundationSmokeContractTests(unittest.TestCase):
    def test_jobs_require_probe_then_reuse_one_catalog_binding(self):
        evidence = []
        jobs = smoke.Jobs(evidence)
        with self.assertRaisesRegex(RuntimeError, "probe_client"):
            jobs.run("list_photos", {"limit": 1})

        probe = {"access_mode": "safe", "catalog_identity": IDENTITY,
                 "capabilities": {"expected_access_mode": True,
                                  "expected_catalog_identity": True},
                 "catalog_authorized": True}
        with mock.patch.object(smoke, "submit_job",
                               side_effect=[probe, {"photos": []}]) as submit:
            self.assertEqual(jobs.probe(), probe)
            result = jobs.run("list_photos", {"limit": 1})
        self.assertEqual(result, {"photos": []})
        self.assertEqual(submit.call_args_list, [
            mock.call("probe_client", {}, evidence),
            mock.call("list_photos", {
                "limit": 1,
                "expected_access_mode": "safe",
                "expected_catalog_identity": IDENTITY,
            }, evidence),
        ])

    def test_probe_without_identity_never_enables_commands(self):
        jobs = smoke.Jobs([])
        with mock.patch.object(smoke, "submit_job",
                               return_value={"access_mode": "full"}):
            with self.assertRaises(ValueError):
                jobs.probe()
        with self.assertRaises(RuntimeError):
            jobs.run("list_photos", {})


if __name__ == "__main__":
    unittest.main()
