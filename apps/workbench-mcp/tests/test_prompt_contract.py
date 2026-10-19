"""The MCP initialization prompt is built from the prompt-as-code manifest."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from workbench_mcp.prompt_loader import PromptContractError, load_runtime_operator_prompt
from workbench_mcp.server import McpServer, TOOLS


class PromptContractTests(unittest.TestCase):
    def test_compiled_prompt_is_the_mcp_initialization_prompt(self):
        expected = load_runtime_operator_prompt()
        response = McpServer(object()).handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
        })
        self.assertEqual(response["result"]["instructions"], expected)

    def test_every_tool_named_by_the_prompt_exists(self):
        prompt = load_runtime_operator_prompt()
        mentioned = set(re.findall(r"\btrlr_[a-z0-9_]+\b", prompt))
        available = {tool["name"] for tool in TOOLS}
        self.assertTrue(mentioned)
        self.assertEqual(set(), mentioned - available)
        self.assertNotRegex(prompt, r"\blrwb_[a-z0-9_]+\b")

    def test_prompt_avoids_known_stale_contracts(self):
        prompt = load_runtime_operator_prompt()
        for stale in ("Effect Stack Resolver", "Model Gateway", "编号最大的",
                      "/private/agent-home", "J:\\", "个 MCP 工具",
                      "trlr_style_library", "trlr_knowledge",
                      "trlr_style_cases", "trlr_reference_match",
                      "trlr_explore_episode", "trlr_probe_episode",
                      "trlr_knowledge_evidence", "trlr_image_stats",
                      "trlr_image_inspect", "trlr_search_episode",
                      "trlr_best_episode",
                      "knowledge-data.json",
                      "style-library.json"):
            self.assertNotIn(stale, prompt)

    def test_explicit_image_requests_require_user_visible_reply_body(self):
        prompt = load_runtime_operator_prompt()
        self.assertIn("工具结果中的图片只供 Agent 观察，不代表用户已经看见。", prompt)
        self.assertIn("用户明确要求看图时，必须把对应图片嵌入助手回复正文。", prompt)
        self.assertIn("MCP Server 本机路径不能直接作为客户端图片路径。", prompt)

    def test_configuration_requests_route_directly_to_live_state_and_tutorials(self):
        prompt = load_runtime_operator_prompt()
        self.assertIn("配置查询不要求 Catalog 已绑定或授权", prompt)
        self.assertIn('topic="access_mode"', prompt)
        self.assertIn("help.guide", prompt)
        self.assertIn("查看和教学本身不触发任何配置写入或 Catalog 切换", prompt)
        self.assertIn("没有读回前不得宣称设置完成", prompt)

    def test_read_only_configuration_does_not_expand_into_file_edits(self):
        prompt = load_runtime_operator_prompt()
        self.assertIn("只读配置或连接验证的结果直接返回对话", prompt)
        self.assertIn("不创建报告文件，不修改技能", prompt)
        self.assertIn("不要重复失败探针", prompt)
        self.assertIn("在回复中说明差异", prompt)

    def test_expected_mode_is_not_evidence_of_a_saved_setting(self):
        prompt = load_runtime_operator_prompt()
        self.assertIn("期望值不等于已保存", prompt)
        self.assertIn("当前接口未报告该值的确切来源与持久保存状态", prompt)
        self.assertIn("只读查询没有执行写入", prompt)
        tool = next(item for item in TOOLS
                    if item["name"] == "trlr_get_runtime_config")
        self.assertIn("does not prove it was saved", tool["description"])

    def test_manifest_metadata_is_part_of_the_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt_dir = root / "prompts"
            section = prompt_dir / "section.md"
            prompt_dir.mkdir()
            section.write_text("required fragment", encoding="utf-8")
            manifest = {
                "schema_version": "runtime-prompt-manifest-v1",
                "prompt_id": "wrong-prompt",
                "audience": "editing-agent",
                "entrypoint": "mcp.initialize.instructions",
                "sections": [{"id": "core", "path": "prompts/section.md"}],
                "required_fragments": ["required fragment"],
            }
            manifest_path = prompt_dir / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(PromptContractError):
                load_runtime_operator_prompt(manifest_path)

    def test_manifest_cannot_load_a_section_outside_its_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt_dir = root / "prompts"
            prompt_dir.mkdir()
            manifest = {
                "schema_version": "runtime-prompt-manifest-v1",
                "prompt_id": "runtime-operator",
                "audience": "editing-agent",
                "entrypoint": "mcp.initialize.instructions",
                "sections": [{"id": "core", "path": "../escape.md"}],
                "required_fragments": ["required fragment"],
            }
            manifest_path = prompt_dir / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(PromptContractError):
                load_runtime_operator_prompt(manifest_path)

    def test_manifest_rejects_duplicate_and_orphan_section_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt_dir = root / "prompts"
            source_dir = prompt_dir / "runtime-operator"
            source_dir.mkdir(parents=True)
            section = source_dir / "core.md"
            section.write_text("required fragment", encoding="utf-8")
            manifest = {
                "schema_version": "runtime-prompt-manifest-v1",
                "prompt_id": "runtime-operator",
                "audience": "editing-agent",
                "entrypoint": "mcp.initialize.instructions",
                "sections": [
                    {"id": "core", "path": "prompts/runtime-operator/core.md"},
                    {"id": "copy", "path": "prompts/runtime-operator/core.md"},
                ],
                "required_fragments": ["required fragment"],
            }
            manifest_path = prompt_dir / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(PromptContractError):
                load_runtime_operator_prompt(manifest_path)

            manifest["sections"] = manifest["sections"][:1]
            (source_dir / "orphan.md").write_text("not compiled", encoding="utf-8")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(PromptContractError):
                load_runtime_operator_prompt(manifest_path)


if __name__ == "__main__":
    unittest.main()
