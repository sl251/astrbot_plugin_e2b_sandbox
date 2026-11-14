"""v1.4.2 failure boundaries and the single-Key upgrade, without cloud RPCs."""

import asyncio
import json
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from helpers import Event, FakeSDK, Query, State, TestDirectory, load_main

main = load_main()
session_store = sys.modules[main.SessionStoreMixin.__module__]


class Version142Tests(unittest.IsolatedAsyncioTestCase):
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
        self.config = {"e2b_api_key": "old-account", "e2b_api_keys": ["new-account"],
                       "enable_user_whitelist": False, "allow_keep_running": True}
        self.plugin = self.load_plugin(self.config)
        self.event = Event("upgrade")
        self.session = self.event.unified_msg_origin
        self.plugin._stage_pending_files = AsyncMock(return_value=[])
        self.plugin._snapshot_sandbox_files = AsyncMock(return_value={})
        self.plugin._handle_images = AsyncMock(return_value=False)
        self.plugin._handle_generated_files = AsyncMock(return_value=[])

    def load_plugin(self, config):
        plugin = main.Main(self.context, config)
        plugin._start_maintenance = lambda: None
        return plugin

    async def asyncTearDown(self):
        await self.plugin.terminate()
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    async def create(self):
        self.plugin._next_creation_time = 0
        result = await self.plugin.create_session_sandbox(self.event)
        self.assertIn("Created sandbox", result)
        return FakeSDK.boxes[self.plugin.sandbox_sessions[self.session]["sandbox_id"]]

    async def test_corrupt_state_stops_load_without_overwriting_original(self):
        path = self.plugin._sandbox_state_path
        valid = {"sandbox_id": "legacy", "status": "paused", "last_active": time.time()}
        cases = ["{broken", "[]", json.dumps({self.session: []}),
                 json.dumps({self.session: {}})]
        for field, value in [("sandbox_id", 123), ("key_id", None),
                             ("last_active", "yesterday"), ("paused_at", -1),
                             ("created_at", True), ("last_active", float("nan")),
                             ("last_active", float("inf"))]:
            cases.append(json.dumps({self.session: {**valid, field: value}}))
        for raw in cases:
            with self.subTest(raw=raw):
                path.write_text(raw, encoding="utf-8")
                original = path.read_bytes()
                with self.assertRaisesRegex(RuntimeError, "could not be loaded safely"):
                    self.load_plugin(self.config)
                self.assertEqual(path.read_bytes(), original)
        self.assertEqual(FakeSDK.create_count, 0)
        self.assertEqual(FakeSDK.connect_count, 0)

    async def test_create_write_failures_do_not_send_rpc_or_leak_capacity(self):
        failures = [patch("builtins.open", side_effect=PermissionError("read only")),
                    patch.object(session_store.os, "fsync", side_effect=OSError("disk full")),
                    patch.object(Path, "replace", side_effect=PermissionError("replace denied"))]
        for failure in failures:
            with self.subTest(failure=failure), failure:
                self.plugin._next_creation_time = 0
                result = await self.plugin.create_session_sandbox(self.event)
            self.assertIn("activation was not sent", result)
            self.assertNotIn(self.session, self.plugin.sandbox_sessions)
            self.assertNotIn(self.session, self.plugin._capacity.occupied)
        self.assertEqual(FakeSDK.create_count, 0)
        self.assertEqual(FakeSDK.pause_count, 0)

    async def test_resume_write_failure_preserves_paused_record_and_original_file(self):
        await self.create()
        await self.plugin.pause_session_sandbox(self.event)
        before = dict(self.plugin.sandbox_sessions[self.session])
        original = self.plugin._sandbox_state_path.read_bytes()
        pauses = FakeSDK.pause_count
        with patch.object(Path, "replace", side_effect=PermissionError("read only")):
            result = await self.plugin.resume_session_sandbox(self.event)
        self.assertIn("activation was not sent", result)
        self.assertEqual(self.plugin.sandbox_sessions[self.session], before)
        self.assertEqual(self.plugin._sandbox_state_path.read_bytes(), original)
        self.assertEqual(FakeSDK.connect_count, 0)
        self.assertEqual(FakeSDK.pause_count, pauses)
        self.assertNotIn(self.session, self.plugin._capacity.occupied)

    async def test_running_reconnect_write_failure_keeps_occupied_slot(self):
        await self.create()
        before = dict(self.plugin.sandbox_sessions[self.session])
        with patch.object(self.plugin, "_save_sandbox_sessions", return_value=False):
            result = await self.plugin.resume_session_sandbox(self.event)
        self.assertIn("activation was not sent", result)
        self.assertEqual(self.plugin.sandbox_sessions[self.session], before)
        self.assertIn(self.session, self.plugin._capacity.occupied)
        self.assertEqual(FakeSDK.connect_count, 0)
        self.assertEqual(FakeSDK.pause_count, 0)

    async def test_owner_migration_write_failure_stops_load_and_preserves_file(self):
        path = self.plugin._sandbox_state_path
        path.write_text(json.dumps({self.session: {"sandbox_id": "legacy", "status": "paused"}}),
                        encoding="utf-8")
        original = path.read_bytes()
        with patch.object(Path, "replace", side_effect=PermissionError("read only")):
            with self.assertRaisesRegex(RuntimeError, "could not be loaded safely"):
                self.load_plugin(self.config)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(FakeSDK.connect_count, 0)

    async def test_single_key_upgrade_then_list_only_keeps_old_owner_and_rotates_new_sessions(self):
        box = await FakeSDK.create()
        box.state = State.PAUSED
        legacy = {"sandbox_id": box.sandbox_id, "status": "paused",
                  "last_active": time.time(), "paused_at": time.time()}
        self.plugin._sandbox_state_path.write_text(json.dumps({self.session: legacy}), encoding="utf-8")
        migrated = self.load_plugin(self.config)
        try:
            owner = migrated._key_pool.identifier("old-account")
            persisted = json.loads(migrated._sandbox_state_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted[self.session]["key_id"], owner)
            self.assertNotIn("old-account", json.dumps(persisted))
        finally:
            await migrated.terminate()
        # Once persisted, the original Key can move to the list and change order.
        reordered = self.load_plugin({"e2b_api_keys": ["new-account", "old-account"],
                                      "enable_user_whitelist": False})
        try:
            with patch.object(FakeSDK, "connect", wraps=FakeSDK.connect) as connect:
                result = await reordered.resume_session_sandbox(self.event)
            self.assertIn("Connected", result)
            self.assertEqual(connect.call_args.kwargs["api_key"], "old-account")
            self.assertEqual(reordered.sandbox_sessions[self.session]["key_id"], owner)
            self.assertEqual(reordered.sandbox_sessions[self.session]["created_at"], 0)
            with patch.object(FakeSDK, "create", wraps=FakeSDK.create) as create:
                for name in ("new-a", "new-b"):
                    reordered._next_creation_time = 0
                    self.assertIn("Created sandbox", await reordered.create_session_sandbox(Event(name)))
            self.assertEqual([call.kwargs["api_key"] for call in create.call_args_list],
                             ["new-account", "old-account"])
        finally:
            await reordered.terminate()

    async def test_cloud_resume_resets_pause_retention_cycle(self):
        box = await self.create()
        await self.plugin.pause_session_sandbox(self.event)
        meta = self.plugin.sandbox_sessions[self.session]
        meta["paused_at"] = time.time() - 86400
        box.state = State.RUNNING
        await self.plugin._refresh_session_state(self.session)
        self.assertNotIn("paused_at", meta)
        self.assertIn(self.session, self.plugin._capacity.occupied)
        box.state = State.PAUSED
        before = time.time()
        await self.plugin._refresh_session_state(self.session)
        self.assertGreaterEqual(meta["paused_at"], before)
        self.plugin._last_expired_cleanup = None
        await self.plugin._cleanup_expired_sessions()
        self.assertIn(self.session, self.plugin.sandbox_sessions)
        self.assertEqual(FakeSDK.kill_count, 0)

    async def test_unknown_cloud_state_is_not_treated_as_paused(self):
        box = await self.create()
        await self.plugin.pause_session_sandbox(self.event)
        box.state = "future-sdk-state"
        await self.plugin._refresh_session_state(self.session)
        meta = self.plugin.sandbox_sessions[self.session]
        self.assertEqual(meta["status"], "unknown")
        self.assertNotIn("paused_at", meta)
        self.assertIn(self.session, self.plugin._capacity.occupied)

    async def test_preparation_failure_allows_immediate_same_code_retry(self):
        for stage, code in [("_stage_pending_files", "print('upload')"),
                            ("_install_dependencies", "import numpy; print('install')")]:
            with self.subTest(stage=stage):
                with patch.object(self.plugin, stage, AsyncMock(side_effect=RuntimeError("prepare failed"))):
                    result = await self.plugin.run_python_code(self.event, code)
                self.assertIn("prepare failed", result)
                with patch.object(self.plugin, "_install_dependencies", AsyncMock()):
                    result = await self.plugin.run_python_code(self.event, code)
                self.assertIn("Execution Complete", result)

    async def test_kill_allows_same_code_on_replacement_sandbox(self):
        code = "print('same code')"
        self.assertIn("Execution Complete", await self.plugin.run_python_code(self.event, code))
        self.assertIn("Duplicate", await self.plugin.run_python_code(self.event, code))
        await self.plugin.kill_session_sandbox(self.event)
        self.plugin._next_creation_time = 0
        self.assertIn("Execution Complete", await self.plugin.run_python_code(self.event, code))
        self.assertEqual(FakeSDK.create_count, 2)

    async def test_shutdown_waits_for_active_execution_and_rejects_new_operations(self):
        box = await self.create()
        box.run_gate = asyncio.Event()
        run = asyncio.create_task(self.plugin.run_python_code(self.event, "print(42)", auto_pause=False))
        stop = None
        try:
            await asyncio.wait_for(box.run_started.wait(), 1)
            stop = asyncio.create_task(self.plugin.terminate())
            await asyncio.sleep(0)
            self.assertTrue(self.plugin._stopping)
            self.assertFalse(stop.done())
            rejected = await self.plugin.create_session_sandbox(Event("new-during-stop"))
            self.assertIn("shutting down", rejected)
            box.run_gate.set()
            self.assertIn("Execution Complete", await asyncio.wait_for(run, 1))
            await asyncio.wait_for(stop, 1)
            self.assertEqual(box.state, State.PAUSED)
            self.assertFalse(self.plugin._capacity.occupied)
            self.assertFalse(self.plugin._operation_completions)
        finally:
            box.run_gate.set()
            await asyncio.gather(run, *([stop] if stop else []), return_exceptions=True)

    async def test_duplicate_dashboard_targets_are_disabled_and_limits_match(self):
        await self.create()
        self.plugin.sandbox_sessions["duplicate"] = dict(self.plugin.sandbox_sessions[self.session])
        self.plugin.config["max_return_file_size_mb"] = 100
        snapshot = self.plugin._web_panel.snapshot()
        self.assertEqual(snapshot["capacity"]["return_file_limit_mb"], 50)
        self.assertEqual(len(snapshot["sessions"]), 2)
        for row in snapshot["sessions"]:
            self.assertFalse(row["can_pause"])
            self.assertFalse(row["can_kill"])
            self.assertEqual(row["blocked_reason"], "ambiguous_target")
        self.plugin.sandbox_sessions.pop("duplicate")

    async def test_invalid_key_types_are_ignored_and_overlapping_secrets_fully_redacted(self):
        pool = main.ApiKeyPool({"e2b_api_key": 123, "e2b_api_keys":
                                [None, 12, False, {}, "", " short-secret ", "short-secret-tail"]})
        self.assertEqual([value for _, value in await pool.candidates()],
                         ["short-secret", "short-secret-tail"])
        self.assertEqual(pool.redact("short-secret-tail short-secret"), "[REDACTED] [REDACTED]")
