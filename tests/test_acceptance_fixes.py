import asyncio
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

from helpers import Event, FakeSDK, Query, State, TestDirectory, load_main

main = load_main()


class AcceptanceFixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeSDK.reset()
        self.directory = TestDirectory()
        self.patchers = [patch.object(main, 'get_astrbot_data_path', lambda: self.directory.name),
                         patch.object(main, 'AsyncSandbox', FakeSDK),
                         patch.object(main, 'SandboxQuery', Query),
                         patch.object(main, 'SandboxState', State)]
        for item in self.patchers:
            item.start()
        self.plugin = main.Main(types.SimpleNamespace(add_llm_tools=lambda *args: None),
                                {'e2b_api_key': 'mock', 'enable_user_whitelist': False})
        self.plugin._start_maintenance = lambda: None
        self.event = Event()
        self.session = self.event.unified_msg_origin

    async def asyncTearDown(self):
        await self.plugin.terminate()
        for item in reversed(self.patchers):
            item.stop()
        self.directory.cleanup()

    async def test_hydrated_attachment_survives_pending_file_refresh(self):
        original = {'name': 'data.csv', 'file_id': 'same-id', 'path': None}
        self.plugin.session_files[self.session] = [{**original, 'path': '/test/resolved.csv'}]
        with patch.object(self.plugin, '_extract_event_files', return_value=[dict(original)]):
            self.assertEqual(self.plugin._get_pending_files(self.event)[0]['path'], '/test/resolved.csv')

    async def test_same_filename_new_attachment_does_not_reuse_old_path(self):
        self.plugin.session_files[self.session] = [
            {'name': 'data.csv', 'file_id': 'old-id', 'path': '/test/old.csv'}]
        with patch.object(self.plugin, '_extract_event_files', return_value=[
                {'name': 'data.csv', 'file_id': 'new-id', 'path': None}]):
            self.assertIsNone(self.plugin._get_pending_files(self.event)[0]['path'])

    async def test_locked_cache_is_not_evicted(self):
        self.plugin.session_last_access[self.session] = time.time() - 86400
        self.plugin.session_files[self.session] = [{'name': 'busy.csv'}]
        lock = self.plugin._get_session_lock(self.session)
        async with lock:
            self.plugin._cleanup_session_cache()
            self.assertIs(self.plugin.session_locks.get(self.session), lock)
            self.assertIn(self.session, self.plugin.session_files)
        self.plugin._cleanup_session_cache()
        self.assertNotIn(self.session, self.plugin.session_locks)

    async def test_setup_timeout_not_misreported_as_code_execution_timeout(self):
        self.plugin._get_or_create_session_sandbox = AsyncMock(side_effect=asyncio.TimeoutError())
        result = await self.plugin.run_python_code(self.event, 'print(1)')
        self.assertNotIn('Execution timed out', result)
        self.assertIn('sandbox activation', result.lower())

    async def test_new_text_message_does_not_repeat_old_upload(self):
        original = self.event
        self.plugin.session_files[self.session] = [{'name': 'old.csv', 'path': '/test/old.csv'}]
        self.plugin._file_event_ids[self.session] = ('event', original)
        self.assertEqual(self.plugin._get_pending_files(Event()), [])

    async def test_image_delivery_is_tracked_and_failed_send_not_success(self):
        import base64
        execution = types.SimpleNamespace(results=[types.SimpleNamespace(png=base64.b64encode(b'data').decode())])
        self.event.send = AsyncMock(side_effect=RuntimeError('delivery failed'))
        self.assertFalse(await self.plugin._handle_images(self.event, execution, []))
        await asyncio.sleep(0)
        self.assertEqual(len(self.plugin._inflight_tasks), 0)

    async def test_sdk_exception_does_not_leak_key_to_log_or_tool_result(self):
        key = self.plugin._key_pool.resolve(self.plugin._key_pool.legacy_id)
        self.plugin._get_or_create_session_sandbox = AsyncMock(side_effect=RuntimeError('SDK echoed ' + key))
        with self.assertLogs(main.logger, level='ERROR') as captured:
            result = await self.plugin.run_python_code(self.event, 'print(2)')
        self.assertNotIn(key, result)
        self.assertNotIn(key, '\n'.join(captured.output))
        self.assertIn('[REDACTED]', result)

    async def test_key_round_robin_skips_cooled_key_fairly(self):
        pool = main.ApiKeyPool({'e2b_api_keys': ['a', 'b', 'c']})
        pool.reject(pool.identifier('a'), 401)
        selected = [(await pool.candidates())[0][1] for _ in range(4)]
        self.assertEqual(selected, ['b', 'c', 'b', 'c'])
