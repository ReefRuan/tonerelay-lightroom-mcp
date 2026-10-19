import unittest

from workbench.presets import PresetTools


class PresetToolsTests(unittest.TestCase):
    def test_each_request_calls_the_runner_exactly_once(self):
        calls = []

        def runner(command, params):
            calls.append((command, params))
            return {"presets": [], "count": 0, "offset": 0, "limit": 100}

        tools = PresetTools(runner)
        tools.list()
        tools.get({"preset_uuid": "u1"})
        tools.compare({"preset_name": "a"}, {"preset_uuid": "b"})
        self.assertEqual([command for command, _ in calls],
                         ["list_develop_presets", "get_develop_preset",
                          "compare_develop_presets"])
        self.assertEqual(calls[0][1], {"query": "", "limit": 100, "offset": 0})

    def test_validation_happens_before_the_runner_and_runner_errors_pass_through(self):
        calls = []

        def runner(command, params):
            calls.append(command)
            raise RuntimeError("sdk exploded")

        tools = PresetTools(runner)
        bad_calls = [
            lambda: tools.list(query=3),
            lambda: tools.list(limit=True),
            lambda: tools.list(limit=0),
            lambda: tools.list(limit=101),
            lambda: tools.list(limit=1.5),
            lambda: tools.list(offset=-1),
            lambda: tools.get({}),
            lambda: tools.get({"preset_name": "x", "extra": True}),
            lambda: tools.get({"preset_uuid": 3}),
            lambda: tools.get({"preset_name": "x", "preset_scope": "all"}),
            lambda: tools.compare({"preset_name": "x"}, {"preset_folder": "f"}),
            lambda: tools.compare({"preset_name": "x"},
                                  {"preset_uuid": "y", "extra": 1}),
        ]
        for call in bad_calls:
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(calls, [])
        with self.assertRaises(RuntimeError):
            tools.list()
        self.assertEqual(calls, ["list_develop_presets"])

    def test_legacy_empty_arrays_are_normalized_without_touching_objects(self):
        empty_list = PresetTools(lambda command, params: {
            "presets": {}, "count": 0, "offset": 0, "limit": 100})
        self.assertEqual(empty_list.list()["presets"], [])
        empty_changes = PresetTools(lambda command, params: {"changes": {}})
        self.assertEqual(empty_changes.compare(
            {"preset_name": "x"}, {"preset_uuid": "y"})["changes"], [])
        # Empty tables that are not the known array fields are real JSON objects.
        nested = PresetTools(lambda command, params: {
            "settings": {"Curve": {}, "Nested": {}}, "uuid": "u"})
        self.assertEqual(nested.get({"preset_uuid": "u"})["settings"],
                         {"Curve": {}, "Nested": {}})
        with self.assertRaises(ValueError):
            PresetTools(lambda command, params: {"presets": {"0": {}}}).list()
        with self.assertRaises(ValueError):
            PresetTools(lambda command, params: {"changes": "bad"}).compare(
                {"preset_name": "x"}, {"preset_uuid": "y"})
        with self.assertRaises(ValueError):
            PresetTools(lambda command, params: []).list()


if __name__ == "__main__":
    unittest.main()
