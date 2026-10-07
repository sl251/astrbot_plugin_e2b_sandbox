import types
import unittest
from unittest.mock import Mock

from helpers import Event, load_main

main = load_main()
from sandbox_plugin_test.execution_policy import (
    E2B_DEFAULT_PROMPT,
    E2B_EXECUTION_TOOL,
    LOCAL_EXECUTION_TOOLS,
    LOCAL_RESTRICTED_PROMPT,
)


class ToolSet:
    def __init__(self, names):
        self.tools = [types.SimpleNamespace(name=name, active=True) for name in names]

    def get_tool(self, name):
        return next((tool for tool in self.tools if tool.name == name), None)

    def remove_tool(self, name):
        # Mutate in place to verify isolation even with this host implementation.
        self.tools[:] = [tool for tool in self.tools if tool.name != name]


class ExecutionPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = main.Main.__new__(main.Main)
        self.plugin.config = {"enable_user_whitelist": False}
        self.settings = {"computer_use_runtime": "local", "computer_use_require_admin": True}
        self.plugin.context = types.SimpleNamespace(
            get_config=Mock(return_value={"provider_settings": self.settings}),
        )
        self.plugin._get_pending_files = Mock(return_value=[])
        self.event = Event()
        self.event.is_admin = Mock(return_value=False)
        self.shared = ToolSet([
            E2B_EXECUTION_TOOL, *LOCAL_EXECUTION_TOOLS,
            "astrbot_file_read_tool", "astrbot_file_write_tool",
            "astrbot_file_edit_tool", "astrbot_grep_tool", "transfer_to_ops_engineer",
        ])
        self.request = types.SimpleNamespace(func_tool=self.shared, system_prompt="Host preamble")

    async def apply(self):
        await self.plugin.inject_file_hint(self.event, self.request)

    async def test_non_admin_filters_only_execution_tools_on_request_copy(self):
        await self.apply()
        self.assertIsNot(self.request.func_tool, self.shared)
        self.assertIsNot(self.request.func_tool.tools, self.shared.tools)
        for name in LOCAL_EXECUTION_TOOLS:
            self.assertIsNone(self.request.func_tool.get_tool(name))
            self.assertIsNotNone(self.shared.get_tool(name))
        for name in (E2B_EXECUTION_TOOL, "astrbot_file_read_tool", "astrbot_file_write_tool",
                     "astrbot_file_edit_tool", "astrbot_grep_tool", "transfer_to_ops_engineer"):
            self.assertIs(self.request.func_tool.get_tool(name), self.shared.get_tool(name))
        self.assertTrue(all(tool.active for tool in self.shared.tools))
        self.assertTrue(self.request.system_prompt.startswith("Host preamble"))
        self.assertIn(E2B_DEFAULT_PROMPT, self.request.system_prompt)
        self.assertIn(LOCAL_RESTRICTED_PROMPT, self.request.system_prompt)

    async def test_single_agent_without_orchestration_or_local_tools_uses_e2b(self):
        # No orchestrator, handoff, router prompt, or other built-in tools.
        self.request.func_tool = ToolSet([E2B_EXECUTION_TOOL])
        self.settings["computer_use_runtime"] = "none"
        self.plugin._get_pending_files.return_value = [{"name": "input.csv"}]
        await self.apply()
        self.assertIn(E2B_DEFAULT_PROMPT, self.request.system_prompt)
        self.assertIn("/home/user/uploads/input.csv", self.request.system_prompt)
        self.assertEqual(len(self.request.func_tool.tools), 1)

    async def test_host_without_optional_config_api_still_uses_e2b(self):
        self.plugin.context = types.SimpleNamespace()
        await self.apply()
        self.assertIs(self.request.func_tool, self.shared)
        self.assertIn(E2B_DEFAULT_PROMPT, self.request.system_prompt)
        self.assertIn("session-scoped", self.request.system_prompt)

    async def test_admin_request_keeps_local_tools_even_after_non_admin_request(self):
        await self.apply()
        self.event.is_admin.return_value = True
        self.request = types.SimpleNamespace(func_tool=self.shared, system_prompt="")
        await self.apply()
        self.assertIs(self.request.func_tool, self.shared)
        self.assertNotIn(LOCAL_RESTRICTED_PROMPT, self.request.system_prompt)

    async def test_per_conversation_configuration_is_used(self):
        def get_config(*, umo=None):
            self.assertEqual(umo, self.event.unified_msg_origin)
            return {"provider_settings": self.settings}
        self.plugin.context.get_config.side_effect = get_config
        await self.apply()
        self.assertIsNone(self.request.func_tool.get_tool(LOCAL_EXECUTION_TOOLS[0]))

    async def test_local_execution_allowed_by_operator_is_not_hidden(self):
        self.settings["computer_use_require_admin"] = False
        await self.apply()
        self.assertIs(self.request.func_tool, self.shared)
        self.assertNotIn(LOCAL_RESTRICTED_PROMPT, self.request.system_prompt)

    async def test_default_admin_requirement_is_respected(self):
        del self.settings["computer_use_require_admin"]
        await self.apply()
        self.assertIsNone(self.request.func_tool.get_tool(LOCAL_EXECUTION_TOOLS[0]))

    async def test_other_runtimes_do_not_hide_similarly_named_tools(self):
        for runtime in ("sandbox", "none"):
            self.settings["computer_use_runtime"] = runtime
            await self.apply()
            self.assertIs(self.request.func_tool, self.shared)
            self.assertNotIn(LOCAL_RESTRICTED_PROMPT, self.request.system_prompt)

    async def test_no_e2b_tool_does_not_advertise_direct_execution_or_files(self):
        self.shared.remove_tool(E2B_EXECUTION_TOOL)
        await self.apply()
        self.assertNotIn(E2B_DEFAULT_PROMPT, self.request.system_prompt)
        self.assertNotIn("session-scoped", self.request.system_prompt)
        self.plugin._get_pending_files.assert_not_called()
        self.assertIsNotNone(self.request.func_tool.get_tool("transfer_to_ops_engineer"))

    async def test_inactive_e2b_tool_does_not_advertise_direct_execution(self):
        self.shared.get_tool(E2B_EXECUTION_TOOL).active = False
        await self.apply()
        self.assertNotIn(E2B_DEFAULT_PROMPT, self.request.system_prompt)

    async def test_empty_prompt_and_no_tool_set_are_supported(self):
        self.request.func_tool = None
        self.request.system_prompt = None
        await self.apply()
        self.assertIsNone(self.request.func_tool)
        self.assertNotIn(E2B_DEFAULT_PROMPT, self.request.system_prompt)

    async def test_denied_e2b_user_does_not_receive_cloud_routing(self):
        self.plugin.config = {"enable_user_whitelist": True, "user_whitelist": []}
        self.request.system_prompt = None
        await self.apply()
        self.assertIn("disabled for this user", self.request.system_prompt)
        self.assertNotIn(E2B_DEFAULT_PROMPT, self.request.system_prompt)
        self.assertIs(self.request.func_tool, self.shared)
        self.plugin.context.get_config.assert_not_called()

    async def test_operator_can_disable_prompt_preference_independently(self):
        self.plugin.config["prefer_e2b_for_code"] = False
        await self.apply()
        self.assertNotIn(E2B_DEFAULT_PROMPT, self.request.system_prompt)
        self.assertIsNone(self.request.func_tool.get_tool(LOCAL_EXECUTION_TOOLS[0]))
        self.assertIn("session-scoped", self.request.system_prompt)

    async def test_operator_can_disable_filter_without_claiming_host_permission(self):
        self.plugin.config["hide_restricted_local_tools"] = False
        await self.apply()
        self.assertIs(self.request.func_tool, self.shared)
        self.assertIn(LOCAL_RESTRICTED_PROMPT, self.request.system_prompt)
        self.assertIn(E2B_DEFAULT_PROMPT, self.request.system_prompt)

    async def test_lifecycle_and_attachment_hints_remain(self):
        self.plugin._get_pending_files.return_value = [{"name": "report.csv"}]
        await self.apply()
        for hint in ("auto-pause", "Reuse", "/home/user/uploads/report.csv", "e2b_sandbox_send_file"):
            self.assertIn(hint, self.request.system_prompt)

    async def test_repeated_hook_does_not_duplicate_routing_policy(self):
        await self.apply()
        await self.apply()
        self.assertEqual(self.request.system_prompt.count(E2B_DEFAULT_PROMPT), 1)
        self.assertEqual(self.request.system_prompt.count(LOCAL_RESTRICTED_PROMPT), 1)

    async def test_config_read_failure_preserves_tools_and_e2b_guidance(self):
        self.plugin.context.get_config.side_effect = RuntimeError("unknown host version")
        await self.apply()
        self.assertIs(self.request.func_tool, self.shared)
        self.assertIn(E2B_DEFAULT_PROMPT, self.request.system_prompt)
        self.assertNotIn(LOCAL_RESTRICTED_PROMPT, self.request.system_prompt)

    async def test_missing_origin_does_not_fall_back_to_global_config(self):
        self.event.unified_msg_origin = None
        await self.apply()
        self.plugin.context.get_config.assert_not_called()
        self.assertIs(self.request.func_tool, self.shared)

    def test_model_facing_description_distinguishes_cloud_and_host(self):
        description = main.RunPythonCodeTool(plugin=self.plugin).description
        self.assertIn("AstrBot admin status", description)
        self.assertIn("not the AstrBot host", description)
