"""参数操作文档：契约与执行一致、边界行为、以及独立可加载。

这一组是**外部行为**验证：断言的是 validate() 接受或拒绝什么、查询入口返回什么，
不是「新模块返回了它自己定义的常量」。文档里写的范围、单位、曲线约束必须和执行
校验是同一份数字，所以下面直接从参数记录里取值去喂 validate()。
"""

import copy
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from workbench.studio import parameter_specs
from workbench.studio.parameter_specs import parameter_records
from workbench.studio.settings import BOUNDS, CURVES, FAMILIES, validate

REPO = Path(__file__).resolve().parents[3]
WB_APPS = Path(__file__).resolve().parents[1]


class ContractConsistencyTests(unittest.TestCase):
    """文档与执行契约同源：无遗漏、无重复、分组一致。"""

    def test_every_structured_field_documented_once(self):
        records = parameter_records()
        ids = [row["id"] for row in records]
        self.assertEqual(len(ids), len(set(ids)), "参数记录必须无重复")
        self.assertEqual(set(ids), set(BOUNDS) | set(CURVES),
                         "参数操作文档必须覆盖全部结构化字段，且不多不少")

    def test_numeric_and_curve_split_matches_execution(self):
        records = {row["id"]: row for row in parameter_records()}
        numeric = [k for k, v in records.items() if v["value_type"] != "curve"]
        curves = [k for k, v in records.items() if v["value_type"] == "curve"]
        self.assertEqual(sorted(numeric), sorted(BOUNDS))
        self.assertEqual(sorted(curves), sorted(CURVES))
        for key in CURVES:
            self.assertEqual(records[key]["bounds"],
                             [parameter_specs.CURVE_COORD_MIN,
                              parameter_specs.CURVE_COORD_MAX])

    def test_documented_bounds_are_the_enforced_bounds(self):
        for row in parameter_records():
            if row["value_type"] == "curve":
                continue
            lo, hi = row["bounds"]
            with self.subTest(row["id"]):
                self.assertEqual(validate({row["id"]: lo}), {row["id"]: lo})
                self.assertEqual(validate({row["id"]: hi}), {row["id"]: hi})

    def test_groups_cover_every_field_exactly_once(self):
        seen = [key for _, _, keys in FAMILIES for key in keys]
        self.assertEqual(len(seen), len(set(seen)), "一个字段只能属于一个分组")
        self.assertEqual(set(seen), set(BOUNDS) | set(CURVES))
        self.assertEqual(
            tuple(fid for fid, _, _ in FAMILIES),
            tuple(fid for fid, _ in parameter_specs.GROUPS),
        )
        for family_id, label, keys in FAMILIES:
            with self.subTest(family_id):
                self.assertTrue(label)
                for key in keys:
                    row = next(r for r in parameter_records() if r["id"] == key)
                    self.assertEqual(row["family"], family_id)

    def test_curve_point_rule_is_shared_with_validation(self):
        rule = parameter_specs.curve_point_rule()
        self.assertEqual(rule["min_points"], parameter_specs.CURVE_MIN_POINTS)
        self.assertEqual(rule["max_points"], parameter_specs.CURVE_MAX_POINTS)
        key = CURVES[0]
        # 说明里的点数范围就是校验接受的范围，两边不能各写一套。
        points = [[x, x] for x in range(0, 255, max(1, 254 // (rule["min_points"] - 1)))]
        points[-1] = [255, 255]
        self.assertEqual(len(points), rule["min_points"])
        self.assertEqual(validate({key: [v for p in points for v in p]}),
                         {key: [v for p in points for v in p]})


class BoundaryBehaviourTests(unittest.TestCase):
    """数值边界、错误类型、RAW/JPEG 互斥、曲线形状。"""

    def test_out_of_range_rejected_for_every_documented_field(self):
        for row in parameter_records():
            if row["value_type"] == "curve":
                continue
            lo, hi = row["bounds"]
            with self.subTest(row["id"]):
                with self.assertRaises(ValueError):
                    validate({row["id"]: lo - 1})
                with self.assertRaises(ValueError):
                    validate({row["id"]: hi + 1})

    def test_non_finite_and_wrong_types_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf"), "0", None, [1], {}):
            with self.subTest(repr(bad)):
                with self.assertRaises(ValueError):
                    validate({"Exposure2012": bad})

    def test_bool_is_not_accepted_as_a_number(self):
        # 布尔在透传通道是合法开关，但结构化数值字段不接受它冒充数字。
        with self.assertRaises(ValueError):
            validate({"Exposure2012": True})

    def test_raw_and_rendered_white_balance_cannot_mix(self):
        with self.assertRaises(ValueError):
            validate({"Temperature": 5500, "IncrementalTemperature": 10})
        with self.assertRaises(ValueError):
            validate({"Tint": 5, "IncrementalTint": 5})
        # 各自单独用是合法的。
        self.assertEqual(validate({"Temperature": 5500, "Tint": 5}),
                         {"Temperature": 5500, "Tint": 5})
        self.assertEqual(validate({"IncrementalTemperature": 10, "IncrementalTint": 5}),
                         {"IncrementalTemperature": 10, "IncrementalTint": 5})

    def test_documented_input_type_matches_which_group_a_key_is_in(self):
        raw = {r["id"] for r in parameter_records() if r["input_type"] == "raw"}
        rendered = {r["id"] for r in parameter_records() if r["input_type"] == "rendered"}
        self.assertEqual(raw, set(parameter_specs.RAW_ONLY_KEYS))
        self.assertEqual(rendered, set(parameter_specs.RENDERED_ONLY_KEYS))
        self.assertEqual(raw | rendered, set(parameter_specs.WB_KEYS))

    def test_curve_shape_rules(self):
        key = CURVES[0]
        good = [0, 0, 128, 135, 255, 255]
        self.assertEqual(validate({key: good}), {key: good})
        bad = {
            "too few points": [0, 0, 255, 255][:2],
            "odd length": [0, 0, 128, 135, 255],
            "x not starting at 0": [1, 0, 255, 255],
            "x not ending at 255": [0, 0, 254, 255],
            "x not increasing": [0, 0, 128, 135, 128, 200, 255, 255],
            "coordinate above 255": [0, 0, 300, 135, 255, 255],
            "non integer coordinate": [0, 0, 128.5, 135, 255, 255],
            "not a list": "0,0,255,255",
        }
        for label, value in bad.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    validate({key: value})
        # 点数上限：2..16 组，17 组要拒。
        too_many = []
        for index in range(17):
            too_many += [index * 15, index * 15]
        too_many[-2] = 255
        with self.assertRaises(ValueError):
            validate({key: too_many})

    def test_vignette_style_keeps_numeric_range_and_does_not_claim_the_enum(self):
        """本地证据只证明数值范围与回读；不因文案写着「枚举」就收紧执行行为。"""
        row = next(r for r in parameter_records() if r["id"] == "PostCropVignetteStyle")
        self.assertEqual(row["value_type"], "number")
        self.assertEqual(row["bounds"], [1, 3])
        # 证据里的写法（XMP roundtrip 解析为 2.0）必须仍然被接受。
        self.assertEqual(validate({"PostCropVignetteStyle": 2}),
                         {"PostCropVignetteStyle": 2})
        self.assertEqual(validate({"PostCropVignetteStyle": 2.0}),
                         {"PostCropVignetteStyle": 2.0})
        with self.assertRaises(ValueError):
            validate({"PostCropVignetteStyle": 0})
        with self.assertRaises(ValueError):
            validate({"PostCropVignetteStyle": 4})
        # 档位语义单列为未核实，不能悄悄写成结论。
        joined = " ".join(row["limitations"])
        self.assertIn("未核实", joined)
        self.assertNotIn("高光优先", joined)
        self.assertNotIn("颜色优先", joined)

    def test_passthrough_rules_unchanged(self):
        """没有参数说明不等于不允许写；说明存在也不等于已验证支持。"""
        self.assertEqual(validate({"GrainAmount": 20}), {"GrainAmount": 20})
        self.assertEqual(validate({"ConvertToGrayscale": True}),
                         {"ConvertToGrayscale": True})
        with self.assertRaises(ValueError):
            validate({"ToneCurveName2012": "Custom"})
        with self.assertRaises(ValueError):
            validate({"Not A Field": 1})

class StandaloneParameterDocsTests(unittest.TestCase):
    """参数操作文档不依赖已删除的内置知识或风格子系统。"""

    def test_module_imports_without_knowledge(self):
        """干净解释器里 import 参数模块，不应把知识库一起拉进来。"""
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(WB_APPS), str(REPO / "services" / "lr-ops"), env.get("PYTHONPATH", "")])
        code = ("import sys, workbench.studio.parameter_specs as ps;"
                "print(len(ps.parameter_records()));"
                "loaded={'workbench.studio.knowledge','workbench.studio.styles'} & set(sys.modules);"
                "print('legacy' if loaded else 'clean')")
        out = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(REPO),
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        count, status = out.stdout.split()
        self.assertEqual(count, "60")
        self.assertEqual(status, "clean", "参数模块不应 import 已删除的内容子系统")

    def test_reference_works_without_creating_content_indexes(self):
        from test_studio import FakeLR
        from workbench.studio.engine import Studio
        with tempfile.TemporaryDirectory() as tmp:
            studio = Studio(Path(tmp), FakeLR())
            reference = studio.parameter_reference()
            self.assertEqual(reference["count"], 60)
            self.assertEqual({row["id"] for row in reference["items"]},
                             set(BOUNDS) | set(CURVES))
            for removed in ("knowledge_library", "library", "user_library"):
                self.assertFalse(hasattr(studio, removed))
            self.assertFalse((Path(tmp) / "knowledge").exists())
            self.assertFalse((Path(tmp) / "user-styles.json").exists())
            for row in reference["items"]:
                self.assertNotIn("review_status", row)

    def test_existing_runtime_content_files_are_left_untouched(self):
        """Removing loaders must not delete or rewrite a user's runtime data."""
        from test_studio import FakeLR
        from workbench.studio.engine import Studio
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_styles = root / "user-styles.json"
            user_styles.write_bytes(b"legacy user data")
            index = root / "knowledge" / "index.sqlite"
            index.parent.mkdir()
            index.write_bytes(b"legacy index data")
            Studio(root, FakeLR())
            self.assertEqual(user_styles.read_bytes(), b"legacy user data")
            self.assertEqual(index.read_bytes(), b"legacy index data")

    def test_retired_runtime_opinion_subsystems_are_not_exposed(self):
        from test_studio import FakeLR
        from workbench.studio.engine import Studio
        with tempfile.TemporaryDirectory() as tmp:
            studio = Studio(Path(tmp), FakeLR())
            for name in ("styles", "recipe", "knowledge", "cases",
                         "knowledge_evidence", "start_exploration", "start_probe",
                         "start_search"):
                self.assertFalse(hasattr(studio, name), name)

    def test_review_status_never_gates_parameter_documents(self):
        """主入口不把审核状态作为参数可读条件。"""
        from test_studio import FakeLR
        from workbench.studio.engine import Studio
        with tempfile.TemporaryDirectory() as tmp:
            studio = Studio(Path(tmp), FakeLR())
            self.assertEqual(studio.parameter_reference()["count"], 60)
            self.assertEqual(studio.parameter_reference(identifier="Exposure2012")["count"], 1)
            # 参数文档记录里没有可被审核的状态字段。
            record = studio.parameter_reference(identifier="Exposure2012")["items"][0]
            self.assertEqual(record["evidence_status"], "project_contract")
            self.assertNotIn("review", record)


class ParameterRecordShapeTests(unittest.TestCase):
    def test_records_carry_contract_and_checked_explanation(self):
        for row in parameter_records():
            with self.subTest(row["id"]):
                self.assertEqual(row["kind"], "parameter")
                self.assertEqual(row["evidence_status"], "project_contract")
                self.assertTrue(row["title"])
                self.assertTrue(row["unit"])
                self.assertTrue(row["details"])
                self.assertTrue(row["limitations"])
                self.assertIn(row["support"]["native_xmp_import"], ("unverified", "verified"))
                self.assertEqual(row["support"]["source_availability"], "must_check_snapshot")

    def test_four_states_are_reported_separately(self):
        """项目校验 / 快照可用性 / LR 生效 / 原生导入，不能互相替代。"""
        row = next(r for r in parameter_records() if r["id"] == "Exposure2012")
        self.assertEqual(row["support"]["execution"], "enabled")
        self.assertEqual(row["support"]["source_availability"], "must_check_snapshot")
        self.assertEqual(row["support"]["native_xmp_import"], "unverified")
        self.assertIn("回读", " ".join(row["limitations"]))

    def test_query_only_matches_real_fields(self):
        self.assertEqual([r["id"] for r in parameter_specs.search(identifier="Exposure2012")],
                         ["Exposure2012"])
        with self.assertRaises(ValueError):
            parameter_specs.search(identifier="NotARealField")
        hits = {r["id"] for r in parameter_specs.search("Temperature kelvin")}
        self.assertTrue(hits <= set(BOUNDS) | set(CURVES))
        self.assertIn("Temperature", hits)


if __name__ == "__main__":
    unittest.main()
