import asyncio
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from helpers import Event, FakeSDK, FakeSandbox, Query, State, TestDirectory, load_main


main = load_main()


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeSDK.reset()
        self.directory = TestDirectory()
        self.patches = [
            patch.object(main, "get_astrbot_data_path", lambda: self.directory.name),
            patch.object(main, "AsyncSandbox", FakeSDK),
            patch.object(main, "SandboxQuery", Query),
            patch.object(main, "SandboxState", State),
        ]
        for item in self.patches:
            item.start()
        self.context = types.SimpleNamespace(add_llm_tools=lambda *tools: None)
        self.plugin = main.Main(self.context, {"e2b_api_key": "mock", "enable_user_whitelist": False})
        # Invoke maintenance explicitly for deterministic tests instead of relying on timers.
        self.plugin._start_maintenance = lambda: None
        self.event = Event()
        self.session = self.event.unified_msg_origin

    async def asyncTearDown(self):
        await self.plugin.terminate()
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    async def make_box(self, event=None, state=State.PAUSED):
        event = event or self.event
        box = await FakeSDK.create(metadata={})
        box.state = state
        session = event.unified_msg_origin
        self.plugin._update_sandbox_session(session, box.sandbox_id, status=state.value)
        if state == State.RUNNING:
            self.plugin._capacity.occupied.add(session)
        return box

    async def test_whitelist_defaults_preserve_previous_access(self):
        for config, allowed in [({}, False), ({"user_whitelist": ["user"]}, True),
                                ({"user_whitelist": ["other"]}, False),
                                ({"enable_user_whitelist": False}, True),
                                ({"enable_user_whitelist": "false"}, False)]:
            with self.subTest(config=config):
                self.plugin.config = config
                self.assertEqual(self.plugin._is_user_allowed(self.event), allowed)

    async def test_every_tool_is_denied_when_whitelist_is_empty(self):
        self.plugin.config = {"e2b_api_key": "mock"}
        for method in ["create_session_sandbox", "resume_session_sandbox", "pause_session_sandbox",
                       "kill_session_sandbox", "get_session_sandbox_status", "e2b_list_files", "e2b_send_file"]:
            with self.subTest(tool=method):
                result = await getattr(self.plugin, method)(self.event)
                self.assertIn("Access denied", result)
        self.assertIn("Access denied", await self.plugin.run_python_code(self.event, "print(1)"))
        self.assertEqual(FakeSDK.create_count, 0)

    async def test_successful_run_forces_pause_by_default(self):
        result = await self.plugin.run_python_code(self.event, "print(1)", auto_pause=False)
        self.assertIn("auto-paused", result)
        self.assertEqual(FakeSDK.count(), 0)
        self.assertFalse(self.plugin._capacity.occupied)

    async def test_upload_failure_pauses_sandbox_and_releases_slot(self):
        self.plugin._stage_pending_files = AsyncMock(side_effect=RuntimeError("upload failed"))
        result = await self.plugin.run_python_code(self.event, "print(1)")
        self.assertIn("upload failed", result)
        self.assertEqual(FakeSDK.count(), 0)
        self.assertFalse(self.plugin._capacity.occupied)

    async def test_cancelled_execution_pauses_before_propagating_cancellation(self):
        box = await self.make_box()
        box.run_gate = asyncio.Event()
        task = asyncio.create_task(self.plugin.run_python_code(self.event, "print(1)"))
        await box.run_started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(box.state, State.PAUSED)
        self.assertFalse(self.plugin._capacity.occupied)

    async def test_execution_timeout_pauses_and_releases_slot(self):
        box = await self.make_box()
        box.run_error = asyncio.TimeoutError()
        result = await self.plugin.run_python_code(self.event, "print(1)")
        self.assertIn("timed out", result)
        self.assertEqual(box.state, State.PAUSED)
        self.assertFalse(self.plugin._capacity.occupied)

    async def test_second_cancellation_keeps_session_busy_until_pause_finishes(self):
        box = await self.make_box()
        box.run_gate = asyncio.Event()
        pause_started, pause_gate = asyncio.Event(), asyncio.Event()
        original_pause = self.plugin._pause_session_safely
        async def slow_pause(*args):
            pause_started.set()
            await pause_gate.wait()
            return await original_pause(*args)
        self.plugin._pause_session_safely = slow_pause
        task = asyncio.create_task(self.plugin.run_python_code(self.event, "print(1)"))
        await box.run_started.wait()
        task.cancel()
        await pause_started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIn("Busy:", await self.plugin.resume_session_sandbox(self.event))
        pause_gate.set()
        await asyncio.gather(*self.plugin._cleanup_tasks)
        self.assertEqual(box.state, State.PAUSED)
        self.assertFalse(self.plugin._capacity.occupied)

    async def test_post_create_sdk_validation_error_recovers_remote_sandbox(self):
        class TemplateException(Exception):
            pass
        original_create = FakeSDK.create
        async def created_then_failed(**kwargs):
            await original_create(**kwargs)
            raise TemplateException("SDK rejected the returned envd version")
        with patch.object(FakeSDK, "create", created_then_failed):
            result = await self.plugin.create_session_sandbox(self.event)
        self.assertIn("envd version", result)
        self.assertEqual(FakeSDK.create_count, 1)
        self.assertEqual(FakeSDK.count(), 0)
        self.assertEqual(self.plugin.sandbox_sessions[self.session]["status"], "paused")
        self.assertFalse(self.plugin._capacity.occupied)

    async def test_pause_failure_keeps_slot_then_maintenance_recovers(self):
        await self.make_box()
        FakeSDK.pause_error = RuntimeError("503: busy")
        result = await self.plugin.run_python_code(self.event, "print(1)")
        self.assertIn("Pause unconfirmed", result)
        self.assertEqual(self.plugin._capacity.occupied, {self.session})
        self.assertEqual(self.plugin.sandbox_sessions[self.session]["status"], "unknown")
        FakeSDK.pause_error = None
        await self.plugin._maintain_sessions()
        self.assertFalse(self.plugin._capacity.occupied)

    async def test_create_resume_and_run_share_one_capacity_limit(self):
        self.plugin.config.update(max_running_sandboxes=1, max_queue_size=0)
        first = await self.plugin.create_session_sandbox(self.event)
        self.assertIn("Created", first)
        other = Event("other")
        box = await self.make_box(other)
        for call in [self.plugin.resume_session_sandbox(other),
                     self.plugin.run_python_code(other, "print(1)"),
                     self.plugin.create_session_sandbox(Event("third"))]:
            self.assertIn("Busy:", await call)
        self.assertEqual(box.state, State.PAUSED)
        self.assertEqual(FakeSDK.count(), 1)

    async def test_concurrent_executions_do_not_exceed_global_limit(self):
        self.plugin.config["max_running_sandboxes"] = 2
        events = [Event(str(i)) for i in range(6)]
        boxes = [await self.make_box(e) for e in events]
        FakeSDK.peak = 0
        gate = asyncio.Event()
        for box in boxes:
            box.run_gate = gate
        tasks = [asyncio.create_task(self.plugin.run_python_code(e, "print(1)")) for e in events]
        for _ in range(100):
            if FakeSDK.count() == 2 and self.plugin._capacity.waiting == 4:
                break
            await asyncio.sleep(0)
        self.assertEqual(FakeSDK.count(), 2)
        self.assertEqual(self.plugin._capacity.waiting, 4)
        gate.set()
        results = await asyncio.gather(*tasks)
        self.assertTrue(all("auto-paused" in result for result in results))
        self.assertLessEqual(FakeSDK.peak, 2)
        self.assertFalse(self.plugin._capacity.occupied)

    async def test_same_session_does_not_build_an_unbounded_lock_queue(self):
        box = await self.make_box()
        box.run_gate = asyncio.Event()
        first = asyncio.create_task(self.plugin.run_python_code(self.event, "print(1)"))
        await box.run_started.wait()
        result = await self.plugin.create_session_sandbox(self.event)
        self.assertIn("Another sandbox operation", result)
        box.run_gate.set()
        await first

    async def test_cancelled_create_is_discovered_and_paused_without_duplicate(self):
        FakeSDK.create_gate = asyncio.Event()
        task = asyncio.create_task(self.plugin.create_session_sandbox(self.event))
        for _ in range(100):
            if FakeSDK.boxes:
                break
            await asyncio.sleep(0)
        self.assertTrue(FakeSDK.boxes)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(FakeSDK.create_count, 1)
        self.assertEqual(FakeSDK.count(), 0)
        self.assertFalse(self.plugin._capacity.occupied)

    async def test_unknown_create_retains_slot_without_starting_a_second_box(self):
        FakeSDK.create_error = asyncio.TimeoutError()
        result = await self.plugin.create_session_sandbox(self.event)
        self.assertIn("Error:", result)
        self.assertEqual(FakeSDK.create_count, 1)
        self.assertEqual(self.plugin._capacity.occupied, {self.session})
        metadata = self.plugin.sandbox_sessions[self.session]
        box = FakeSandbox("delayed", {"plugin": main.PLUGIN_NAME, "request_id": metadata["request_id"]})
        FakeSDK.boxes[box.sandbox_id] = box
        await self.plugin._maintain_sessions()
        self.assertEqual(box.state, State.PAUSED)
        self.assertFalse(self.plugin._capacity.occupied)

    async def test_rejected_create_does_not_leak_a_reservation(self):
        FakeSDK.create_error = RuntimeError("402: Credits exhausted")
        result = await self.plugin.create_session_sandbox(self.event)
        self.assertIn("Credits exhausted", result)
        self.assertFalse(self.plugin._capacity.occupied)
        self.assertFalse(self.plugin.sandbox_sessions)

    async def test_missing_remote_sandbox_is_replaced_only_for_run_or_create(self):
        self.plugin._update_sandbox_session(self.session, "gone", status="paused")
        self.assertIn("No sandbox", await self.plugin.resume_session_sandbox(self.event))
        self.assertEqual(FakeSDK.create_count, 0)
        result = await self.plugin.run_python_code(self.event, "print(1)")
        self.assertIn("Created", result)
        self.assertEqual(FakeSDK.create_count, 1)

    async def test_kill_by_id_never_connects_and_preserves_record_on_failure(self):
        await self.make_box(state=State.RUNNING)
        FakeSDK.kill_error = RuntimeError("delete failed")
        result = await self.plugin.kill_session_sandbox(self.event)
        self.assertIn("Error:", result)
        self.assertIn(self.session, self.plugin.sandbox_sessions)
        self.assertIn(self.session, self.plugin._capacity.occupied)
        FakeSDK.kill_error = None
        self.assertIn("Sandbox killed", await self.plugin.kill_session_sandbox(self.event))
        self.assertEqual(FakeSDK.connect_count, 0)
        self.assertFalse(self.plugin._capacity.occupied)

    async def test_idle_pause_does_not_interrupt_locked_execution(self):
        box = await self.make_box(state=State.RUNNING)
        self.plugin.sandbox_sessions[self.session]["last_active"] = time.time() - 100
        async with self.plugin._get_session_lock(self.session):
            await self.plugin._maintain_sessions()
        self.assertEqual(box.state, State.RUNNING)
        await self.plugin._maintain_sessions()
        self.assertEqual(box.state, State.PAUSED)

    async def test_keep_running_requires_operator_opt_in(self):
        self.plugin.config["allow_keep_running"] = True
        await self.make_box()
        result = await self.plugin.run_python_code(self.event, "print(1)", auto_pause=False)
        self.assertIn("kept running", result)
        self.assertEqual(self.plugin._capacity.occupied, {self.session})

    async def test_paused_sandbox_expires_without_messages_and_chat_does_not_extend_ttl(self):
        box = await self.make_box()
        self.plugin.config["paused_sandbox_retention_hours"] = 1
        self.plugin.sandbox_sessions[self.session]["paused_at"] = time.time() - 3601
        self.plugin._mark_session_active(self.event)
        await self.plugin._maintain_sessions()
        self.assertNotIn(box.sandbox_id, FakeSDK.boxes)
        self.assertNotIn(self.session, self.plugin.sandbox_sessions)
        self.assertEqual(FakeSDK.connect_count, 0)

    async def test_paused_retention_is_configurable_and_legacy_records_are_supported(self):
        box = await self.make_box()
        self.plugin.config["paused_sandbox_retention_hours"] = 24
        self.plugin.sandbox_sessions[self.session]["last_active"] = time.time() - 13 * 3600
        await self.plugin._maintain_sessions()
        self.assertIn(box.sandbox_id, FakeSDK.boxes)
        self.plugin.config["paused_sandbox_retention_hours"] = 12
        self.plugin._last_expired_cleanup = None
        await self.plugin._maintain_sessions()
        self.assertNotIn(box.sandbox_id, FakeSDK.boxes)

    async def test_failed_expiry_delete_keeps_record_and_retries(self):
        box = await self.make_box()
        self.plugin.sandbox_sessions[self.session]["paused_at"] = time.time() - 13 * 3600
        FakeSDK.kill_error = RuntimeError("offline")
        await self.plugin._maintain_sessions()
        self.assertIn(self.session, self.plugin.sandbox_sessions)
        FakeSDK.kill_error = None
        self.plugin._last_expired_cleanup = None
        await self.plugin._maintain_sessions()
        self.assertNotIn(box.sandbox_id, FakeSDK.boxes)

    async def test_expiry_skips_a_busy_session(self):
        box = await self.make_box()
        self.plugin.sandbox_sessions[self.session]["paused_at"] = time.time() - 13 * 3600
        async with self.plugin._get_session_lock(self.session):
            await self.plugin._maintain_sessions()
        self.assertIn(box.sandbox_id, FakeSDK.boxes)

    async def test_first_expiry_check_runs_even_when_monotonic_clock_is_near_zero(self):
        box = await self.make_box()
        self.plugin.sandbox_sessions[self.session]["paused_at"] = time.time() - 13 * 3600
        with patch.object(main.time, "monotonic", return_value=12):
            await self.plugin._cleanup_expired_sessions()
            self.assertNotIn(box.sandbox_id, FakeSDK.boxes)
            self.assertEqual(self.plugin._last_expired_cleanup, 12)
            second = await self.make_box(Event("second"))
            self.plugin.sandbox_sessions["test:friend:second"]["paused_at"] = time.time() - 13 * 3600
            await self.plugin._cleanup_expired_sessions()
            self.assertIn(second.sandbox_id, FakeSDK.boxes)

    async def test_pause_timestamp_survives_reload(self):
        await self.make_box(state=State.RUNNING)
        await self.plugin._pause_session_safely(self.session)
        paused_at = self.plugin.sandbox_sessions[self.session]["paused_at"]
        other = main.Main(self.context, self.plugin.config)
        self.assertEqual(other.sandbox_sessions[self.session]["paused_at"], paused_at)
        self.assertFalse(other._capacity.occupied)

    async def test_status_queries_cloud_without_resuming(self):
        box = await self.make_box(state=State.RUNNING)
        box.state = State.PAUSED
        result = await self.plugin.get_session_sandbox_status(self.event)
        self.assertIn("Status: paused", result)
        self.assertEqual(FakeSDK.connect_count, 0)
        self.assertFalse(self.plugin._capacity.occupied)

    async def test_file_selection_rejects_unknown_name_invalid_index_and_ambiguity(self):
        files = []
        for name in ["first.xlsx", "second.xlsx"]:
            path = Path(self.directory.name) / name
            path.write_bytes(b"mock")
            files.append({"name": name, "local_path": str(path), "signature": name})
        self.plugin.generated_files[self.session] = files
        for kwargs in [{"file_name": "missing.xlsx"}, {"file_index": 99}, {"file_index": -1},
                       {"file_index": 1.5}, {"file_index": "oops"}, {}]:
            with self.subTest(kwargs=kwargs):
                result = await self.plugin.e2b_send_file(self.event, **kwargs)
                self.assertIn("Error:", result)
                self.assertFalse(self.event.sent)
        result = await self.plugin.e2b_send_file(self.event, file_index=2)
        self.assertIn("second.xlsx", result)
        self.assertEqual(len(self.event.sent), 1)

    async def test_create_rate_limit_and_server_auto_pause(self):
        captured = []
        async def capture(method, kwargs, **opts):
            captured.append((time.monotonic(), kwargs))
            return object()
        self.plugin._call_sandbox_entrypoint = capture
        await self.plugin._create_sandbox("mock", 600, "", metadata={"plugin": "test"})
        await self.plugin._create_sandbox("mock", 600, "", metadata={"plugin": "test"})
        self.assertGreaterEqual(captured[1][0] - captured[0][0], 1)
        self.assertEqual(captured[0][1]["lifecycle"], {"on_timeout": "pause", "auto_resume": False})

    async def test_429_uses_bounded_retries_but_network_errors_do_not(self):
        attempts = 0
        async def rejected():
            nonlocal attempts
            attempts += 1
            raise RuntimeError("429: Too many sandboxes")
        with patch.object(main.asyncio, "sleep", AsyncMock()):
            with self.assertRaises(RuntimeError):
                await self.plugin._call_sandbox_entrypoint(rejected, {}, 10, "create")
        self.assertEqual(attempts, 3)
        attempts = 0
        async def offline():
            nonlocal attempts
            attempts += 1
            raise ConnectionError("offline")
        with self.assertRaises(ConnectionError):
            await self.plugin._call_sandbox_entrypoint(offline, {}, 1, "create")
        self.assertEqual(attempts, 1)

    async def test_long_retry_after_preserves_definitive_rejection(self):
        class RateLimitError(Exception):
            status_code = 429
            headers = {"Retry-After": "60"}
        async def rejected():
            raise RateLimitError("quota full")
        with self.assertRaises(RateLimitError):
            await self.plugin._call_sandbox_entrypoint(rejected, {}, 1, "create")

    async def test_initialize_runs_paused_cleanup_without_an_incoming_message(self):
        box = await self.make_box()
        self.plugin.sandbox_sessions[self.session]["paused_at"] = time.time() - 13 * 3600
        self.plugin._start_maintenance = main.Main._start_maintenance.__get__(self.plugin)
        await self.plugin.initialize()
        for _ in range(100):
            if box.sandbox_id not in FakeSDK.boxes:
                break
            await asyncio.sleep(0)
        self.assertNotIn(box.sandbox_id, FakeSDK.boxes)
