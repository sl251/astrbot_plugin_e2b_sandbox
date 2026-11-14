"""Account-aware E2B rotation and persistent-sandbox regression tests."""

import asyncio
import json
import types
import unittest
from unittest.mock import patch

from helpers import Event, FakeSDK, Query, State, TestDirectory, load_main


main = load_main()


class MultiKeyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeSDK.reset()
        self.directory = TestDirectory()
        self.config = {
            "e2b_api_key": "account-one",
            "e2b_api_keys": ["account-two", "account-two"],
            "enable_user_whitelist": False,
        }
        self.calls = []
        self.rejected = set()
        self.created_then_failed = False

        create = FakeSDK.create
        connect = FakeSDK.connect
        pause = FakeSDK.pause
        kill = FakeSDK.kill
        get_info = FakeSDK.get_info
        listing = FakeSDK.list

        async def keyed_create(**kwargs):
            owner = kwargs.get("api_key")
            self.calls.append(("create", owner))
            if owner in self.rejected:
                raise RuntimeError("402: Credits exhausted")
            box = await create(**kwargs)
            box.owner = owner
            if self.created_then_failed:
                raise RuntimeError("503: response lost after create")
            return box

        async def keyed_connect(sandbox_id, **kwargs):
            self.calls.append(("connect", kwargs.get("api_key")))
            if FakeSDK.boxes[sandbox_id].owner != kwargs.get("api_key"):
                raise RuntimeError("403: wrong account")
            return await connect(sandbox_id, **kwargs)

        async def keyed_pause(sandbox_id, **kwargs):
            self.calls.append(("pause", kwargs.get("api_key")))
            if kwargs.get("api_key") and FakeSDK.boxes[sandbox_id].owner != kwargs.get("api_key"):
                raise RuntimeError("403: wrong account")
            return await pause(sandbox_id, **kwargs)

        async def keyed_kill(sandbox_id, **kwargs):
            self.calls.append(("kill", kwargs.get("api_key")))
            if FakeSDK.boxes[sandbox_id].owner != kwargs.get("api_key"):
                raise RuntimeError("403: wrong account")
            return await kill(sandbox_id, **kwargs)

        async def keyed_info(sandbox_id, **kwargs):
            self.calls.append(("info", kwargs.get("api_key")))
            if FakeSDK.boxes[sandbox_id].owner != kwargs.get("api_key"):
                raise RuntimeError("403: wrong account")
            return await get_info(sandbox_id, **kwargs)

        def keyed_list(query, **kwargs):
            owner = kwargs.get("api_key")
            self.calls.append(("list", owner))
            original = listing(query, **kwargs)
            class Page:
                @property
                def has_next(self):
                    return original.has_next
                async def next_items(self):
                    items = await original.next_items()
                    return [box for box in items if getattr(box, "owner", None) == owner]
            return Page()

        self.patches = [
            patch.object(main, "get_astrbot_data_path", lambda: self.directory.name),
            patch.object(main, "AsyncSandbox", FakeSDK),
            patch.object(main, "SandboxQuery", Query),
            patch.object(main, "SandboxState", State),
            patch.object(FakeSDK, "create", keyed_create),
            patch.object(FakeSDK, "connect", keyed_connect),
            patch.object(FakeSDK, "pause", keyed_pause),
            patch.object(FakeSDK, "kill", keyed_kill),
            patch.object(FakeSDK, "get_info", keyed_info),
            patch.object(FakeSDK, "list", keyed_list),
        ]
        for item in self.patches:
            item.start()
        self.context = types.SimpleNamespace(add_llm_tools=lambda *args: None)
        self.plugin = main.Main(self.context, self.config)
        self.plugin._start_maintenance = lambda: None

    async def asyncTearDown(self):
        await self.plugin.terminate()
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    async def create(self, name):
        self.plugin._next_creation_time = 0
        result = await self.plugin.create_session_sandbox(Event(name))
        self.assertIn("Created sandbox", result)
        return result

    async def test_new_sandboxes_rotate_but_existing_sessions_keep_owner(self):
        for name in ("a", "b", "c"):
            await self.create(name)
        self.assertEqual(
            [v for op, v in self.calls if op == "create"],
            ["account-one", "account-two", "account-one"],
        )
        session = "test:friend:b"
        self.assertEqual(
            self.plugin.sandbox_sessions[session]["key_id"],
            self.plugin._key_pool.identifier("account-two"),
        )
        await self.plugin.pause_session_sandbox(Event("b"))
        result = await self.plugin.resume_session_sandbox(Event("b"))
        self.assertIn("Connected to existing", result)
        self.assertIn(("pause", "account-two"), self.calls)
        self.assertIn(("connect", "account-two"), self.calls)
        await self.plugin.kill_session_sandbox(Event("b"))
        self.assertIn(("kill", "account-two"), self.calls)

    async def test_bound_key_survives_reload_and_config_order_changes(self):
        await self.create("a")
        await self.create("b")
        await self.plugin.pause_session_sandbox(Event("b"))
        changed = {"e2b_api_key": "account-one", "e2b_api_keys": ["account-two", "account-three"],
                   "enable_user_whitelist": False}
        other = main.Main(self.context, changed)
        other._start_maintenance = lambda: None
        try:
            self.assertEqual(other.sandbox_sessions["test:friend:b"]["key_id"],
                             self.plugin._key_pool.identifier("account-two"))
            result = await other.resume_session_sandbox(Event("b"))
            self.assertIn("Connected", result)
            self.assertIn(("connect", "account-two"), self.calls)
        finally:
            await other.terminate()

    async def test_definitive_402_fails_over_without_extra_sandbox(self):
        self.rejected.add("account-one")
        await self.create("a")
        self.assertEqual([v for op, v in self.calls if op == "create"],
                         ["account-one", "account-two"])
        self.assertEqual(FakeSDK.create_count, 1)
        meta = self.plugin.sandbox_sessions["test:friend:a"]
        self.assertEqual(meta["key_id"], self.plugin._key_pool.identifier("account-two"))
        await self.create("b")
        self.assertEqual([v for op, v in self.calls if op == "create"][-1], "account-two")

    async def test_all_accounts_rejected_releases_capacity(self):
        self.rejected.update(("account-one", "account-two"))
        self.plugin._next_creation_time = 0
        error = await self.plugin.create_session_sandbox(Event("a"))
        self.assertIn("All configured E2B API Keys rejected", error)
        self.assertFalse(self.plugin.sandbox_sessions)
        self.assertFalse(self.plugin._capacity.occupied)

    async def test_uncertain_create_never_rotates_to_second_key(self):
        self.created_then_failed = True
        self.plugin._next_creation_time = 0
        result = await self.plugin.create_session_sandbox(Event("a"))
        self.assertIn("503", result)
        self.assertEqual([v for op, v in self.calls if op == "create"], ["account-one"])
        self.assertEqual(FakeSDK.create_count, 1)
        meta = self.plugin.sandbox_sessions["test:friend:a"]
        self.assertEqual(meta["key_id"], self.plugin._key_pool.identifier("account-one"))
        self.assertIn(("list", "account-one"), self.calls)

    async def test_legacy_state_migrates_only_with_original_key(self):
        await self.create("a")
        session = "test:friend:a"
        self.plugin.sandbox_sessions[session].pop("key_id")
        self.plugin._save_sandbox_sessions()
        other = main.Main(self.context, self.config)
        other._start_maintenance = lambda: None
        try:
            self.assertEqual(other.sandbox_sessions[session]["key_id"],
                             other._key_pool.identifier("account-one"))
            persisted = json.loads(other._sandbox_state_path.read_text())
            self.assertEqual(persisted[session]["key_id"], other._key_pool.legacy_id)
            self.assertNotIn("account-one", str(persisted))
        finally:
            await other.terminate()

    async def test_removed_bound_key_never_falls_back_to_other_account(self):
        await self.create("a")
        other_config = {"e2b_api_keys": ["account-two"], "enable_user_whitelist": False}
        other = main.Main(self.context, other_config)
        other._start_maintenance = lambda: None
        try:
            result = await other.resume_session_sandbox(Event("a"))
            self.assertIn("no longer configured", result)
            self.assertNotIn(("connect", "account-two"), self.calls)
        finally:
            await other.terminate()

    async def test_config_deduplicates_legacy_and_list_entries(self):
        from sandbox_plugin_test.api_keys import ApiKeyPool
        pool = ApiKeyPool({"e2b_api_key": "one", "e2b_api_keys": ["one", "two", "two"]})
        chosen = []
        for _ in range(4):
            chosen.append((await pool.candidates())[0][1])
        self.assertEqual(chosen, ["one", "two", "one", "two"])

    async def test_legacy_without_owner_is_not_assigned_to_arbitrary_new_account(self):
        await self.create("a")
        session = "test:friend:a"
        self.plugin.sandbox_sessions[session].pop("key_id")
        self.plugin._save_sandbox_sessions()
        other = main.Main(self.context, {"e2b_api_keys": ["account-two", "account-three"],
                                         "enable_user_whitelist": False})
        other._start_maintenance = lambda: None
        try:
            result = await other.resume_session_sandbox(Event("a"))
            self.assertIn("no recorded API Key owner", result)
            self.assertNotIn(("connect", "account-two"), self.calls)
        finally:
            await other.terminate()
