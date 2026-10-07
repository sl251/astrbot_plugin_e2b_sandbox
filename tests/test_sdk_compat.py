"""Exercise real SDK descriptors with network entry points mocked."""

import inspect
import types
import unittest
from unittest.mock import AsyncMock, patch

from helpers import TestDirectory, load_main


main = load_main()


@unittest.skipIf(main.AsyncSandbox is None, "Install requirements.txt to check the real SDK")
class SDKCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TestDirectory()
        self.data_patch = patch.object(main, "get_astrbot_data_path", lambda: self.directory.name)
        self.data_patch.start()
        self.plugin = main.Main(types.SimpleNamespace(add_llm_tools=lambda *tools: None), {})

    async def asyncTearDown(self):
        self.data_patch.stop()
        self.directory.cleanup()

    async def test_create_selects_supported_server_auto_pause_arguments(self):
        captured = []
        async def capture(method, kwargs, **opts):
            # Binding checks the actual installed SDK, without making a request.
            inspect.signature(method).bind(**{k: v for k, v in kwargs.items() if v is not None})
            captured.append(kwargs)
            return object()
        self.plugin._call_sandbox_entrypoint = capture
        await self.plugin._create_sandbox("mock", 600, "", metadata={"request_id": "test"})
        if "lifecycle" in inspect.signature(main.AsyncSandbox.create).parameters:
            self.assertEqual(captured[0]["lifecycle"], {"on_timeout": "pause", "auto_resume": False})
        else:
            self.assertTrue(captured[0]["auto_pause"])
        self.assertEqual(captured[0]["metadata"], {"request_id": "test"})

    async def test_real_class_control_descriptors_accept_id_without_connect(self):
        from e2b.sandbox_async.sandbox_api import SandboxApi
        sdk = main.AsyncSandbox
        pause = getattr(sdk, "pause", None) or sdk.beta_pause
        for method, internal in [(sdk.kill, "_cls_kill"), (pause, "_cls_pause"),
                                 (sdk.get_info, "_cls_get_info")]:
            with self.subTest(method=internal):
                with patch.object(SandboxApi, internal, AsyncMock(return_value=True)) as rpc:
                    await self.plugin._call_sandbox_entrypoint(
                        method, {"sandbox_id": "mock-id", "api_key": "mock", "proxy": None}, 1, internal,
                    )
                rpc.assert_awaited_once_with(sandbox_id="mock-id", api_key="mock")

    async def test_real_metadata_query_builds_paginator_without_resuming(self):
        query = main.SandboxQuery(
            metadata={"plugin": main.PLUGIN_NAME, "request_id": "test"},
            state=[main.SandboxState.RUNNING, main.SandboxState.PAUSED],
        )
        paginator = main.AsyncSandbox.list(query=query, api_key="mock")
        self.assertTrue(paginator.has_next)
        self.assertTrue(callable(paginator.next_items))

    async def test_real_filesystem_supports_binary_read_with_timeout(self):
        from e2b.sandbox_async.filesystem.filesystem import Filesystem
        from e2b.sandbox_async.filesystem.filesystem import ENVD_DEFAULT_USER
        # Exercise the installed implementation while mocking only HTTP I/O.
        fs = Filesystem.__new__(Filesystem)
        fs._envd_version = ENVD_DEFAULT_USER
        fs._connection_config = types.SimpleNamespace(get_request_timeout=lambda timeout: timeout)
        import httpx
        payload = bytes(range(256))
        fs._envd_api = types.SimpleNamespace(get=AsyncMock(return_value=httpx.Response(200, content=payload)))
        result = await self.plugin._read_sandbox_file_bytes(types.SimpleNamespace(files=fs), "/home/user/test.bin")
        self.assertEqual(result, payload)
        self.assertEqual(fs._envd_api.get.call_args.kwargs["timeout"], 30)
