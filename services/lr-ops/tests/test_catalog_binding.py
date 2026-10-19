import unittest

from lr_ops.catalog_binding import CatalogBinding


IDENTITY = "catalog-md5-v1:0123456789abcdef0123456789abcdef"
CAPABILITIES = {
    "expected_access_mode": True,
    "expected_catalog_identity": True,
}


class CatalogBindingTests(unittest.TestCase):
    def test_probe_values_are_applied_without_mutating_command_params(self):
        binding = CatalogBinding.from_probe({
            "access_mode": "full", "catalog_identity": IDENTITY,
            "capabilities": CAPABILITIES, "catalog_authorized": True,
        })
        params = {"photo_uuid": "photo-1"}
        bound = binding.apply(params)
        self.assertEqual(params, {"photo_uuid": "photo-1"})
        self.assertEqual(bound, {
            "photo_uuid": "photo-1",
            "expected_access_mode": "full",
            "expected_catalog_identity": IDENTITY,
        })

    def test_safe_mode_is_preserved(self):
        binding = CatalogBinding.from_probe({
            "access_mode": "safe", "catalog_identity": IDENTITY,
            "capabilities": CAPABILITIES, "catalog_authorized": True,
        })
        self.assertEqual(binding.apply({})["expected_access_mode"], "safe")

    def test_invalid_probe_is_rejected(self):
        for probe in (
            {},
            {"access_mode": "other", "catalog_identity": IDENTITY},
            {"access_mode": "full", "catalog_identity": None},
            {"access_mode": "full", "catalog_identity": ""},
            {"access_mode": "full", "catalog_identity": IDENTITY},
            {"access_mode": "full", "catalog_identity": IDENTITY,
             "capabilities": {"expected_access_mode": True}},
            {"access_mode": "full", "catalog_identity": IDENTITY,
             "capabilities": CAPABILITIES, "catalog_authorized": False},
        ):
            with self.subTest(probe=probe), self.assertRaises(ValueError):
                CatalogBinding.from_probe(probe)

    def test_conflicting_caller_envelope_is_rejected(self):
        binding = CatalogBinding("full", IDENTITY)
        for params in (
            {"expected_access_mode": "safe"},
            {"expected_catalog_identity": "catalog-md5-v1:other"},
        ):
            with self.subTest(params=params), self.assertRaises(ValueError):
                binding.apply(params)


if __name__ == "__main__":
    unittest.main()
