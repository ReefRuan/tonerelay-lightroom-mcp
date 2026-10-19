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
        self.assertIn("工具提供图片与引用，由 Agent 构造正文", prompt)
        self.assertIn("不得猜地址", prompt)
        self.assertIn("普通看图只回复文件名与图片", prompt)
        self.assertIn("为每个可用项目分别输出对应文件名与一张正文图片", prompt)
        self.assertIn("不能仅列文件名后声称多张已显示", prompt)
        self.assertIn("display.image_index", prompt)
        self.assertIn("灰色占位不算正文显示成功", prompt)

    def test_initial_catalog_binding_asks_in_client_and_accepts_bind_reply(self):
        prompt = load_runtime_operator_prompt()
        self.assertIn("在当前客户端展示探测到的实际 Catalog 名称与访问模式", prompt)
        self.assertIn("用户回复“绑定”就是对刚展示目标和模式的确认", prompt)
        self.assertIn("未收到确认不自动绑定", prompt)
        self.assertIn("重新 health 核对身份与模式未改变", prompt)
        self.assertIn("只查配置、健康或明确只读验收时不绑定", prompt)
        self.assertIn("不静默覆盖绑定", prompt)

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
        self.assertIn("expected_source", prompt)
        self.assertIn("expected_persisted", prompt)
        self.assertIn("旧版本缺少这些字段", prompt)
        self.assertIn("只读查询没有执行写入", prompt)
        tool = next(item for item in TOOLS
                    if item["name"] == "trlr_get_runtime_config")
        self.assertIn("does not prove it was saved", tool["description"])

    def test_authorized_ui_fallback_is_an_agent_capability(self):
        prompt = load_runtime_operator_prompt()
        self.assertIn("MCP 不能覆盖任务步骤，或工具失败", prompt)
        self.assertIn("主动使用 Agent 自身的 Computer Use", prompt)
        self.assertIn("不要求 ToneRelay 提供 Computer Use 能力", prompt)
        self.assertIn("不为正常的目录浏览、模块切换或选片恢复额外询问", prompt)
        self.assertIn("不把 UI 完成的步骤归因于 ToneRelay", prompt)
        self.assertIn("轻量的一两步核验或恢复不增加确认流程", prompt)
        self.assertIn("在该段开始或扩大前简短告知", prompt)

    def test_ui_fallback_requires_observed_target_one_action_and_refresh(self):
        prompt = load_runtime_operator_prompt()
        self.assertIn("实际返回的应用和窗口中选定唯一目标", prompt)
        self.assertIn("先观察并停下检查，再做一个动作并立即刷新", prompt)
        self.assertIn("输入前核对最新窗口、目标照片或虚拟副本与焦点", prompt)
        self.assertIn("不复用旧索引和坐标", prompt)
        self.assertIn("结果未知时先核验，不重复输入或重放写入", prompt)
        self.assertIn("不能用 UI 绕过 Catalog guard", prompt)
        self.assertIn("身份或目标无法可靠核对时，只读观察并停止写入", prompt)

    def test_agent_can_launch_lightroom_through_an_identified_authorized_entry(self):
        prompt = load_runtime_operator_prompt()
        self.assertIn("用户要求打开或开始使用 Lightroom", prompt)
        self.assertIn("实际 UI 观察证实应用未运行", prompt)
        self.assertIn("Computer Use 技能已识别的正规应用入口启动", prompt)
        self.assertIn("不遍历磁盘或猜测安装路径", prompt)
        self.assertIn("启动后刷新实际应用与窗口，核对目标 Catalog", prompt)
        self.assertIn("不静默绑定或切换 Catalog", prompt)
        self.assertIn("不能从探针失败断言应用未打开", prompt)
        self.assertNotIn("普通修图入口由用户打开 Lightroom", prompt)
        self.assertNotIn("不用命令或进程工具启动/重启", prompt)

    def test_lightroom_restart_requires_checked_state_and_lifecycle_authorization(self):
        prompt = load_runtime_operator_prompt()
        self.assertIn("关闭或重启 Lightroom 前先核验未保存状态及未完成或未知作业", prompt)
        self.assertIn("沿用用户明确的生命周期授权", prompt)
        self.assertIn("存在未知作业时不自动关闭或重启 Lightroom/Bridge", prompt)
        self.assertIn("不把普通看图或版本查询当作重启授权", prompt)
        self.assertIn("不因 ToneRelay 工具未覆盖而屏蔽 Agent 自身能力", prompt)

    def test_ui_fallback_preserves_mandatory_denies_and_confirmations(self):
        prompt = load_runtime_operator_prompt()
        self.assertIn("强制禁令优先于操作确认，用户确认不能解除禁令", prompt)
        self.assertIn("Windows 不用 UI 执行终端命令", prompt)
        self.assertIn("不自动化身份认证、密码管理器、安全软件", prompt)
        self.assertIn("不使用 Windows 键快捷键或提交年龄验证", prompt)
        self.assertIn("已有任务授权不替代该策略要求的动作时确认", prompt)
        self.assertIn("UI 编辑仍保留原片与已有不可变版本", prompt)
        self.assertIn("UI 操作不自动获得 Episode 的 revision", prompt)

    def test_ui_fallback_does_not_repeat_unknown_mcp_writes(self):
        prompt = load_runtime_operator_prompt()
        self.assertIn("先读取已发生的副本、版本及作业元数据", prompt)
        self.assertIn("用 UI 只读核验", prompt)
        self.assertIn("未厘清结果前不能通过 UI 再执行同一写入", prompt)
        self.assertIn("停止实时照片操作", prompt)
        self.assertIn("未知写入结果不得自动重放", prompt)
        self.assertIn("实际观察到相应状态变化后，才做一次只读复验", prompt)
        self.assertIn("该观察不能自动放行 Catalog、客户端权限或安全批准", prompt)
        self.assertIn("同样失败且没有新证据就停止", prompt)

    def test_episode_ui_restore_is_an_attempt_with_observed_evidence(self):
        prompt = load_runtime_operator_prompt()
        self.assertIn("目标在当前来源中时不强制切换目录或模块", prompt)
        self.assertIn("跨来源请求可临时激活 All Photographs", prompt)
        self.assertIn("尝试恢复原目录、完整选片和模块", prompt)
        self.assertIn("不能承诺创建过程中完全不改变画面", prompt)
        self.assertIn("`ui_context` 的实际恢复状态与告警", prompt)
        self.assertIn("未核验时不能声称 UI 已恢复", prompt)
        self.assertIn("存在恢复 warning 时如实说明", prompt)
        self.assertIn("不把恢复失败当作重新创建 Episode 的理由", prompt)
        self.assertNotIn("当前不会恢复先前 UI 状态", prompt)

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
