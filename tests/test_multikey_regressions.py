"""Persistent adversarial regressions for E2B multi-Key rotation (v1.4.0).

Every case maps back to the multi-Key acceptance handoff and pins the
library-level contract (no simulated cloud acceptance, FakeSDK only):

* M (multi-Key attribution / migration)
  - M1: a bound Key that disappeared from the config must never be silently
    re-attributed to another account (paused and running records alike), and
    a failed resume must not release the running slot it never owned.
  - M2: an account-level rejection while resuming a paused sandbox keeps the
    paused record, paused_at and the capacity slot untouched (no compensating
    pause RPC); a 404 is the only status that may discard the record.
* C (cancellation)
  - C1: cancelling before the create RPC is sent leaves no record, no slot
    and no RPC (capacity queue wait, creation-lock wait, rate-limit wait).
  - C2: cancelling while the next Key waits out the create rate limit, after a
    definitive 401 rotation, sends no second RPC and leaves no ghost record.
  - C3: a create-time 401 rotates only the NEW creation; an existing sandbox
    keeps being managed by its original Key even while that Key is in cooldown.
  - C5: cancelling during a 429 Retry-After sleep cleans the in-flight record
    without leaving an unknown/ghost state (create and paused-resume paths).
* L (legacy records)
  - L1: list-only configs never guess, migrate or clean an unattributed
    legacy record (resume/run/status/kill/pause and background expiry all
    refuse), while brand-new sessions still record their Key fingerprint.
  - L2: a legacy record migrates exactly once to the legacy Key fingerprint
    and survives config reordering without re-attribution or recreation.
* Q (cooldown vs concurrency)
  - Q1: concurrent creators holding stale candidate lists see the cooldown
    published under the creation lock and skip the rejected Key without a
    duplicate 401, a leaked KeyInCooldownError or a wrong-Key sandbox.
  - Q2: cooldown blocks only NEW creation; existing sandboxes keep being
    managed by their cooled Key, admission fails fast once every Key cools,
    and expired cooldowns rotate the original Key back in.
"""

import asyncio
import json
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

from helpers import Event, FakeSDK, Query, State, TestDirectory, load_main

main = load_main()
from sandbox_plugin_test.api_keys import KeyInCooldownError  # noqa: E402


class RejectedError(RuntimeError):
    """Simulates an E2B account-level HTTP rejection with a status code."""

    def __init__(self, status, retry_after=None):
        super().__init__(f"{status}: rejected by account")
        self.status_code = status
        if retry_after is not None:
            self.headers = {"Retry-After": retry_after}


class MultiKeyRegressionTests(unittest.IsolatedAsyncioTestCase):
    """M/C/L/Q acceptance regressions exercised against the live plugin."""

    async def asyncSetUp(self):
        FakeSDK.reset()
        self.directory = TestDirectory()
        self.patchers = [
            patch.object(main, "get_astrbot_data_path", lambda: self.directory.name),
            patch.object(main, "AsyncSandbox", FakeSDK),
            patch.object(main, "SandboxQuery", Query),
            patch.object(main, "SandboxState", State),
        ]
        for item in self.patchers:
            item.start()
        self.context = types.SimpleNamespace(add_llm_tools=lambda *a: None)
        self.config = {
            "e2b_api_key": "account-one",
            "e2b_api_keys": ["account-two"],
            "enable_user_whitelist": False,
        }
        self.calls = []
        self.create_rejections = {}
        self.connect_rejections = {}
        create = FakeSDK.create
        connect = FakeSDK.connect
        pause = FakeSDK.pause
        kill = FakeSDK.kill
        get_info = FakeSDK.get_info
        sdk_list = FakeSDK.list

        async def keyed_create(**kwargs):
            owner = kwargs.get("api_key")
            self.calls.append(("create", owner))
            if owner in self.create_rejections:
                status, retry_after = self.create_rejections[owner]
                raise RejectedError(status, retry_after)
            box = await create(**kwargs)
            box.owner = owner
            return box

        async def keyed_connect(sandbox_id, **kwargs):
            self.calls.append(("connect", kwargs.get("api_key")))
            box = FakeSDK.boxes.get(sandbox_id)
            if box is None:
                return await connect(sandbox_id, **kwargs)
            owner = kwargs.get("api_key")
            if owner in self.connect_rejections:
                status, retry_after = self.connect_rejections[owner]
                raise RejectedError(status, retry_after)
            if box.owner != owner:
                raise RuntimeError(f"403: sandbox {sandbox_id} does not belong to {owner}")
            return await connect(sandbox_id, **kwargs)

        async def keyed_pause(sandbox_id, **kwargs):
            self.calls.append(("pause", kwargs.get("api_key")))
            box = FakeSDK.boxes.get(sandbox_id)
            owner = kwargs.get("api_key")
            if box is not None and owner is not None and box.owner != owner:
                raise RuntimeError(f"403: sandbox {sandbox_id} does not belong to {owner}")
            return await pause(sandbox_id, **kwargs)

        async def keyed_kill(sandbox_id, **kwargs):
            self.calls.append(("kill", kwargs.get("api_key")))
            box = FakeSDK.boxes.get(sandbox_id)
            owner = kwargs.get("api_key")
            if box is not None and owner is not None and box.owner != owner:
                raise RuntimeError(f"403: sandbox {sandbox_id} does not belong to {owner}")
            return await kill(sandbox_id, **kwargs)

        async def keyed_info(sandbox_id, **kwargs):
            self.calls.append(("info", kwargs.get("api_key")))
            box = FakeSDK.boxes.get(sandbox_id)
            owner = kwargs.get("api_key")
            if box is None or box.owner != owner:
                raise RuntimeError(f"403: sandbox {sandbox_id} does not belong to {owner}")
            return await get_info(sandbox_id, **kwargs)

        async def keyed_list(query, **kwargs):
            self.calls.append(("list", kwargs.get("api_key")))
            return await sdk_list(query, **kwargs)

        for name, wrapper in (
            ("create", keyed_create),
            ("connect", keyed_connect),
            ("pause", keyed_pause),
            ("kill", keyed_kill),
            ("get_info", keyed_info),
            ("list", keyed_list),
        ):
            item = patch.object(FakeSDK, name, wrapper)
            item.start()
            self.patchers.append(item)

        self.plugin = main.Main(self.context, self.config)
        self.plugin._start_maintenance = lambda: None

    async def asyncTearDown(self):
        try:
            await self.plugin.terminate()
        finally:
            for item in reversed(self.patchers):
                item.stop()
            self.directory.cleanup()

    async def create(self, name):
        event = Event(name)
        self.plugin._next_creation_time = 0
        result = await self.plugin.create_session_sandbox(event)
        self.assertIn("Created sandbox", result)
        return result

    # ------------------------------------------------------------------
    # M1: removed bound Key never re-attributed
    # ------------------------------------------------------------------
    async def test_M1_paused_key_removed_resume_refuses_without_reassignment(self):
        await self.create("a")
        session = "test:friend:a"
        await self.plugin.pause_session_sandbox(Event("a"))
        paused_at = self.plugin.sandbox_sessions[session]["paused_at"]
        original_key = self.plugin.sandbox_sessions[session]["key_id"]

        other = main.Main(self.context, {
            "e2b_api_keys": ["account-two"],
            "enable_user_whitelist": False,
            "max_running_sandboxes": 1,
        })
        other._start_maintenance = lambda: None
        try:
            calls_before = list(self.calls)
            result = await other.resume_session_sandbox(Event("a"))
            self.assertIn("no longer configured", result)
            self.assertEqual(self.calls, calls_before)
            meta = other.sandbox_sessions[session]
            self.assertEqual(meta["status"], "paused")
            self.assertEqual(meta["paused_at"], paused_at)
            self.assertEqual(meta["key_id"], original_key)
            self.assertNotIn(session, other._capacity.occupied)

            # Repeating the failure still has zero side effects.
            result = await other.resume_session_sandbox(Event("a"))
            self.assertIn("no longer configured", result)
            self.assertEqual(self.calls, calls_before)

            # The failed resume occupied nothing: the single slot stays free.
            other._next_creation_time = 0
            result = await other.create_session_sandbox(Event("new"))
            self.assertIn("Created sandbox", result)
        finally:
            await other.terminate()

        # Reload once more: still refused, record still intact on disk.
        third = main.Main(self.context, {
            "e2b_api_keys": ["account-two"],
            "enable_user_whitelist": False,
        })
        third._start_maintenance = lambda: None
        try:
            result = await third.resume_session_sandbox(Event("a"))
            self.assertIn("no longer configured", result)
            self.assertEqual(third.sandbox_sessions[session]["status"], "paused")
            self.assertEqual(third.sandbox_sessions[session]["key_id"], original_key)
        finally:
            await third.terminate()

    async def test_M1_running_key_removed_resume_fails_but_slot_is_retained(self):
        await self.create("a")
        session = "test:friend:a"

        other = main.Main(self.context, {
            "e2b_api_keys": ["account-two"],
            "enable_user_whitelist": False,
            "max_running_sandboxes": 1,
            "max_queue_size": 0,
        })
        other._start_maintenance = lambda: None
        try:
            self.assertIn(session, other._capacity.occupied)
            calls_before = list(self.calls)
            result = await other.resume_session_sandbox(Event("a"))
            self.assertIn("no longer configured", result)
            self.assertEqual(self.calls, calls_before)
            # The failure must NOT release the slot it never acquired.
            self.assertIn(session, other._capacity.occupied)

            # Capacity is genuinely full because the ghost slot is retained.
            result = await other.create_session_sandbox(Event("new"))
            self.assertIn("Sandbox queue is full", result)
            self.assertNotIn("test:friend:new", other.sandbox_sessions)
            self.assertEqual(self.calls, calls_before)
        finally:
            await other.terminate()

        # Contrast: restoring the original Key manages the very same sandbox.
        restore = main.Main(self.context, {
            "e2b_api_key": "account-one",
            "e2b_api_keys": ["account-two"],
            "enable_user_whitelist": False,
        })
        restore._start_maintenance = lambda: None
        try:
            result = await restore.resume_session_sandbox(Event("a"))
            self.assertIn("Connected to existing", result)
            self.assertIn(("connect", "account-one"), self.calls)
        finally:
            await restore.terminate()

    # ------------------------------------------------------------------
    # M2: account rejection on paused resume keeps the paused record
    # ------------------------------------------------------------------
    async def test_M2_paused_resume_account_rejection_keeps_paused_record(self):
        await self.create("a")
        session = "test:friend:a"
        await self.plugin.pause_session_sandbox(Event("a"))
        paused_at = self.plugin.sandbox_sessions[session]["paused_at"]

        for status in (401, 402, 403, 409, 422, 429):
            with self.subTest(status=status):
                calls_before = list(self.calls)
                pause_count_before = FakeSDK.pause_count
                self.connect_rejections["account-one"] = (
                    status,
                    "60" if status == 429 else None,
                )
                result = await self.plugin.resume_session_sandbox(Event("a"))
                self.assertIn(str(status), result)
                meta = self.plugin.sandbox_sessions[session]
                self.assertEqual(meta["status"], "paused")
                self.assertEqual(meta["paused_at"], paused_at)
                self.assertNotIn(session, self.plugin._capacity.occupied)
                # No compensating pause: the VM was never resumed.
                self.assertEqual(FakeSDK.pause_count, pause_count_before)
                # Exactly one connect attempt, with the bound account.
                self.assertEqual(
                    self.calls[len(calls_before):], [("connect", "account-one")]
                )
        self.connect_rejections.pop("account-one", None)

        # An SDK without connect() refuses before any RPC or state change.
        class SdkWithoutConnect:
            @staticmethod
            async def create(**kwargs):
                raise AssertionError("create must not be called")

        calls_before = list(self.calls)
        with patch.object(main, "AsyncSandbox", SdkWithoutConnect):
            result = await self.plugin.resume_session_sandbox(Event("a"))
        self.assertIn("does not support reconnecting sandboxes", result)
        self.assertEqual(self.calls, calls_before)
        self.assertEqual(self.plugin.sandbox_sessions[session]["status"], "paused")
        self.assertNotIn(session, self.plugin._capacity.occupied)

        # Contrast: 404 is the only status allowed to discard the record.
        sandbox_id = self.plugin.sandbox_sessions[session]["sandbox_id"]
        FakeSDK.boxes.pop(sandbox_id)
        result = await self.plugin.resume_session_sandbox(Event("a"))
        self.assertIn("No sandbox exists for this session", result)
        self.assertNotIn(session, self.plugin.sandbox_sessions)
        self.assertNotIn(session, self.plugin._capacity.occupied)
        result = await self.plugin.resume_session_sandbox(Event("a"))
        self.assertIn("No sandbox exists for this session", result)

    # ------------------------------------------------------------------
    # C1: cancel before the request is sent leaves no trace
    # ------------------------------------------------------------------
    async def test_C1_cancel_before_request_sent_leaves_no_trace(self):
        await self.create("full")
        creates_before = [c for c in self.calls if c[0] == "create"]
        self.assertEqual(creates_before, [("create", "account-one")])

        # Window 1: cancelled while queued for capacity.
        self.plugin.config.update({"max_running_sandboxes": 1})
        task = asyncio.create_task(
            self.plugin.create_session_sandbox(Event("queued"))
        )
        for _ in range(1000):
            if self.plugin._capacity.waiting:
                break
            await asyncio.sleep(0)
        self.assertTrue(self.plugin._capacity.waiting)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.plugin._capacity.waiting)
        self.assertNotIn("test:friend:queued", self.plugin.sandbox_sessions)
        self.assertNotIn("test:friend:queued", self.plugin._capacity.occupied)
        self.assertEqual([c for c in self.calls if c[0] == "create"], creates_before)
        await self.plugin.pause_session_sandbox(Event("full"))
        await self.create("queued")
        self.plugin.config.update({"max_running_sandboxes": 3})

        # Window 2: cancelled while waiting for the creation lock.
        creates_snapshot = [c for c in self.calls if c[0] == "create"]
        async with self.plugin._creation_lock:
            task = asyncio.create_task(
                self.plugin.create_session_sandbox(Event("lockwait"))
            )
            for _ in range(1000):
                if "test:friend:lockwait" in self.plugin._capacity.occupied:
                    break
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertNotIn("test:friend:lockwait", self.plugin.sandbox_sessions)
        self.assertNotIn("test:friend:lockwait", self.plugin._capacity.occupied)
        self.assertEqual([c for c in self.calls if c[0] == "create"], creates_snapshot)
        await self.create("lockwait")

        # Window 3: cancelled during the pre-send rate-limit wait.
        creates_snapshot = [c for c in self.calls if c[0] == "create"]
        self.plugin._next_creation_time = time.monotonic() + 60
        task = asyncio.create_task(
            self.plugin.create_session_sandbox(Event("ratewait"))
        )
        for _ in range(1000):
            if "test:friend:ratewait" in self.plugin._capacity.occupied:
                break
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn("test:friend:ratewait", self.plugin.sandbox_sessions)
        self.assertNotIn("test:friend:ratewait", self.plugin._capacity.occupied)
        self.assertEqual([c for c in self.calls if c[0] == "create"], creates_snapshot)
        await self.create("ratewait")

    # ------------------------------------------------------------------
    # C2: cancel while the rotated Key waits out the create rate limit
    # ------------------------------------------------------------------
    async def test_C2_cancel_during_rate_wait_after_401_rotation(self):
        self.create_rejections["account-one"] = (401, None)
        session = "test:friend:c2"
        sleep_delays = []
        real_sleep = asyncio.sleep

        async def tracking_sleep(delay, *args, **kwargs):
            sleep_delays.append(delay)
            return await real_sleep(delay, *args, **kwargs)

        with patch.object(main.asyncio, "sleep", tracking_sleep):
            self.plugin._next_creation_time = 0
            task = asyncio.create_task(
                self.plugin.create_session_sandbox(Event("c2"))
            )
            for _ in range(2000):
                if any(delay >= 0.5 for delay in sleep_delays):
                    break
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        # Only A's 401 attempt ever reached the network; B never sent a RPC.
        self.assertEqual([c for c in self.calls if c[0] == "create"],
                         [("create", "account-one")])
        self.assertNotIn(("list", "account-one"), self.calls)
        self.assertNotIn(("pause", "account-one"), self.calls)
        self.assertNotIn(session, self.plugin.sandbox_sessions)
        self.assertNotIn(session, self.plugin._capacity.occupied)
        # A's cooldown was published before B's wait started.
        with self.assertRaises(KeyInCooldownError):
            self.plugin._key_pool.ensure_available(
                self.plugin._key_pool.identifier("account-one")
            )

        # No ghost record after a cold reload either.
        other = main.Main(self.context, self.config)
        other._start_maintenance = lambda: None
        try:
            self.assertNotIn(session, other.sandbox_sessions)
            self.assertNotIn(session, other._capacity.occupied)
            persisted = json.loads(other._sandbox_state_path.read_text())
            self.assertNotIn(session, persisted)
        finally:
            await other.terminate()

        # A follow-up creation still works and skips the cooled Key.
        result = await self.create("followup")
        self.assertIn("Created sandbox", result)
        self.assertEqual(
            self.plugin.sandbox_sessions["test:friend:followup"]["key_id"],
            self.plugin._key_pool.identifier("account-two"),
        )

    # ------------------------------------------------------------------
    # C3: create-time 401 rotates only the new creation
    # ------------------------------------------------------------------
    async def test_C3_account_401_rotates_only_new_creation(self):
        await self.create("old")
        session_old = "test:friend:old"
        await self.plugin.pause_session_sandbox(Event("old"))

        second = main.Main(self.context, self.config)
        second._start_maintenance = lambda: None
        try:
            self.create_rejections["account-one"] = (401, None)
            second._next_creation_time = 0
            result = await second.create_session_sandbox(Event("new"))
            self.assertIn("Created sandbox", result)
            session_new = "test:friend:new"
            meta_new = second.sandbox_sessions[session_new]
            self.assertEqual(
                meta_new["key_id"], second._key_pool.identifier("account-two")
            )
            box = FakeSDK.boxes[meta_new["sandbox_id"]]
            self.assertEqual(box.owner, "account-two")
            self.assertEqual(meta_new["request_id"], box.metadata["request_id"])
            self.assertEqual(
                [c for c in self.calls if c[0] == "create"][-2:],
                [("create", "account-one"), ("create", "account-two")],
            )

            # The new sandbox is managed with the rotated Key.
            await second.pause_session_sandbox(Event("new"))
            self.assertIn(("pause", "account-two"), self.calls)
            result = await second.resume_session_sandbox(Event("new"))
            self.assertIn("Connected to existing", result)
            self.assertEqual(self.calls[-1], ("connect", "account-two"))

            # The existing sandbox keeps its original Key even though that
            # Key is now in cooldown for new creations.
            with self.assertRaises(KeyInCooldownError):
                second._key_pool.ensure_available(
                    second._key_pool.identifier("account-one")
                )
            result = await second.resume_session_sandbox(Event("old"))
            self.assertIn("Connected to existing", result)
            self.assertEqual(self.calls[-1], ("connect", "account-one"))
        finally:
            await second.terminate()

        # State file keeps fingerprints, never plaintext keys.
        text = second._sandbox_state_path.read_text()
        self.assertNotIn("account-one", text)
        self.assertNotIn("account-two", text)
        self.assertIn(second._key_pool.identifier("account-two"), text)

    # ------------------------------------------------------------------
    # C5: cancel during a 429 Retry-After sleep cleans the record
    # ------------------------------------------------------------------
    async def test_C5_cancel_during_429_retry_sleep_on_create(self):
        self.create_rejections["account-one"] = (429, "1.1")
        session = "test:friend:c5"
        sleep_delays = []
        real_sleep = asyncio.sleep

        async def tracking_sleep(delay, *args, **kwargs):
            sleep_delays.append(delay)
            return await real_sleep(delay, *args, **kwargs)

        with patch.object(main.asyncio, "sleep", tracking_sleep):
            self.plugin._next_creation_time = 0
            task = asyncio.create_task(
                self.plugin.create_session_sandbox(Event("c5"))
            )
            for _ in range(2000):
                if any(delay >= 1.0 for delay in sleep_delays):
                    break
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        # The request WAS sent (record existed) but cancellation cleans it:
        # no unknown record, no occupied slot, no recovery attempt.
        self.assertEqual([c for c in self.calls if c[0] == "create"],
                         [("create", "account-one")])
        self.assertNotIn(("list", "account-one"), self.calls)
        self.assertNotIn(session, self.plugin.sandbox_sessions)
        self.assertNotIn(session, self.plugin._capacity.occupied)

        other = main.Main(self.context, self.config)
        other._start_maintenance = lambda: None
        try:
            self.assertNotIn(session, other.sandbox_sessions)
            persisted = json.loads(other._sandbox_state_path.read_text())
            self.assertNotIn(session, persisted)
        finally:
            await other.terminate()

        # A follow-up creation works right away.
        await self.create("c5")
        self.assertEqual(
            self.plugin.sandbox_sessions[session]["key_id"],
            self.plugin._key_pool.identifier("account-two"),
        )

    async def test_C5_cancel_during_429_retry_sleep_on_paused_resume(self):
        await self.create("a")
        session = "test:friend:a"
        await self.plugin.pause_session_sandbox(Event("a"))
        paused_at = self.plugin.sandbox_sessions[session]["paused_at"]
        self.connect_rejections["account-one"] = (429, "1.1")
        calls_before = list(self.calls)
        pause_count_before = FakeSDK.pause_count
        sleep_delays = []
        real_sleep = asyncio.sleep

        async def tracking_sleep(delay, *args, **kwargs):
            sleep_delays.append(delay)
            return await real_sleep(delay, *args, **kwargs)

        with patch.object(main.asyncio, "sleep", tracking_sleep):
            task = asyncio.create_task(
                self.plugin.resume_session_sandbox(Event("a"))
            )
            for _ in range(2000):
                if any(delay >= 1.0 for delay in sleep_delays):
                    break
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        # The paused record is restored exactly, never pause-compensated.
        meta = self.plugin.sandbox_sessions[session]
        self.assertEqual(meta["status"], "paused")
        self.assertEqual(meta["paused_at"], paused_at)
        self.assertNotIn(session, self.plugin._capacity.occupied)
        # Exactly one connect attempt with the bound account, no pause RPC.
        self.assertEqual(self.calls[len(calls_before):], [("connect", "account-one")])
        self.assertEqual(FakeSDK.pause_count, pause_count_before)

    # ------------------------------------------------------------------
    # L1: list-only configs never guess a legacy owner
    # ------------------------------------------------------------------
    async def test_L1_list_only_config_never_guesses_legacy_owner(self):
        await self.create("a")
        session = "test:friend:a"
        await self.plugin.pause_session_sandbox(Event("a"))
        paused_at = self.plugin.sandbox_sessions[session]["paused_at"]
        sandbox_id = self.plugin.sandbox_sessions[session]["sandbox_id"]
        self.plugin.sandbox_sessions[session].pop("key_id")
        self.plugin._save_sandbox_sessions()

        for keys in (["account-two"], ["account-three"],
                     ["account-two", "account-three"]):
            with self.subTest(keys=keys):
                other = main.Main(self.context, {
                    "e2b_api_keys": list(keys),
                    "enable_user_whitelist": False,
                })
                other._start_maintenance = lambda: None
                try:
                    calls_before = list(self.calls)
                    result = await other.resume_session_sandbox(Event("a"))
                    self.assertIn("no recorded API Key owner", result)
                    self.assertEqual(self.calls, calls_before)
                    meta = other.sandbox_sessions[session]
                    self.assertNotIn("key_id", meta)
                    self.assertEqual(meta["status"], "paused")
                    self.assertEqual(meta["paused_at"], paused_at)
                    self.assertEqual(meta["sandbox_id"], sandbox_id)
                    self.assertNotIn(session, other._capacity.occupied)

                    # run / status / kill / pause all refuse the same way and
                    # never mutate or drop the record.
                    result = await other.run_python_code(Event("a"), "print(1)")
                    self.assertIn("no recorded API Key owner", result)
                    result = await other.get_session_sandbox_status(Event("a"))
                    self.assertIn("no recorded API Key owner", result)
                    result = await other.kill_session_sandbox(Event("a"))
                    self.assertIn("no recorded API Key owner", result)
                    self.assertIn(session, other.sandbox_sessions)
                    result = await other.pause_session_sandbox(Event("a"))
                    self.assertIn("already paused", result)
                    self.assertEqual(self.calls, calls_before)
                    persisted = json.loads(other._sandbox_state_path.read_text())
                    self.assertNotIn("key_id", persisted[session])
                finally:
                    await other.terminate()

        # New sessions still get a fingerprint; the legacy record is not
        # migrated, and background expiry cannot clean what it cannot kill.
        other = main.Main(self.context, {
            "e2b_api_keys": ["account-two", "account-three"],
            "enable_user_whitelist": False,
        })
        other._start_maintenance = lambda: None
        try:
            calls_before = list(self.calls)
            result = await other.resume_session_sandbox(Event("a"))
            self.assertIn("no recorded API Key owner", result)
            self.assertEqual(self.calls, calls_before)

            other._next_creation_time = 0
            result = await other.create_session_sandbox(Event("fresh"))
            self.assertIn("Created sandbox", result)
            fresh = "test:friend:fresh"
            self.assertEqual(
                other.sandbox_sessions[fresh]["key_id"],
                other._key_pool.identifier("account-two"),
            )
            persisted = json.loads(other._sandbox_state_path.read_text())
            self.assertNotIn("key_id", persisted[session])
            self.assertIn("key_id", persisted[fresh])

            other.sandbox_sessions[session]["paused_at"] = time.time() - 720 * 3600
            other._last_expired_cleanup = None
            await other._cleanup_expired_sessions()
            self.assertIn(session, other.sandbox_sessions)
            self.assertEqual([c for c in self.calls if c[0] == "kill"], [])
        finally:
            await other.terminate()

    # ------------------------------------------------------------------
    # L2: legacy migration is explicit and reorder-safe
    # ------------------------------------------------------------------
    async def test_L2_legacy_migration_and_reorder_keep_original_key(self):
        await self.create("a")
        session = "test:friend:a"
        await self.plugin.pause_session_sandbox(Event("a"))
        self.plugin.sandbox_sessions[session].pop("key_id")
        self.plugin._save_sandbox_sessions()

        # 1) A legacy config migrates the record to the legacy fingerprint.
        migrated = main.Main(self.context, {
            "e2b_api_key": "account-one",
            "e2b_api_keys": ["account-two"],
            "enable_user_whitelist": False,
        })
        migrated._start_maintenance = lambda: None
        try:
            self.assertEqual(
                migrated.sandbox_sessions[session]["key_id"],
                migrated._key_pool.identifier("account-one"),
            )
            persisted = json.loads(migrated._sandbox_state_path.read_text())
            self.assertEqual(
                persisted[session]["key_id"],
                migrated._key_pool.legacy_id,
            )
            self.assertNotIn("account-one", migrated._sandbox_state_path.read_text())
        finally:
            await migrated.terminate()

        # 2) Legacy field cleared and A reordered behind B: still account-one.
        reordered = main.Main(self.context, {
            "e2b_api_keys": ["account-two", "account-one"],
            "enable_user_whitelist": False,
        })
        reordered._start_maintenance = lambda: None
        try:
            calls_before = list(self.calls)
            result = await reordered.resume_session_sandbox(Event("a"))
            self.assertIn("Connected to existing", result)
            self.assertEqual(self.calls[-1], ("connect", "account-one"))
            self.assertNotIn(("connect", "account-two"), self.calls)
            self.assertEqual(FakeSDK.create_count, 1)
        finally:
            await reordered.terminate()

        # 3) Removing A entirely: explicit refusal, never a fallback to B.
        removed = main.Main(self.context, {
            "e2b_api_keys": ["account-two"],
            "enable_user_whitelist": False,
        })
        removed._start_maintenance = lambda: None
        try:
            calls_before = list(self.calls)
            result = await removed.resume_session_sandbox(Event("a"))
            self.assertIn("no longer configured", result)
            self.assertEqual(self.calls, calls_before)
            self.assertEqual(removed.sandbox_sessions[session]["status"], "paused")
            self.assertEqual(
                removed.sandbox_sessions[session]["key_id"],
                removed._key_pool.identifier("account-one"),
            )
        finally:
            await removed.terminate()

    # ------------------------------------------------------------------
    # Q1: cooldown published under the creation lock is seen by waiters
    # ------------------------------------------------------------------
    async def test_Q1_concurrent_creates_publish_cooldown_exactly_once(self):
        self.create_rejections["account-one"] = (401, None)
        self.plugin.config.update({"max_running_sandboxes": 3})
        names = ("q1", "q2", "q3")
        sessions = ["test:friend:" + n for n in names]

        async with self.plugin._creation_lock:
            tasks = [
                asyncio.create_task(self.plugin.create_session_sandbox(Event(n)))
                for n in names
            ]
            for _ in range(5000):
                if all(s in self.plugin._capacity.occupied for s in sessions):
                    break
                await asyncio.sleep(0)
            # All three hold candidate lists (two include A); none sent a RPC.
            self.assertTrue(all(s in self.plugin._capacity.occupied for s in sessions))
            self.assertEqual([c for c in self.calls if c[0] == "create"], [])

        with patch.object(main.asyncio, "sleep", AsyncMock()):
            results = await asyncio.gather(*tasks)

        self.assertTrue(all("Created sandbox" in r for r in results))
        creates = [c for c in self.calls if c[0] == "create"]
        # Exactly ONE 401 for A; the stale-list waiter skipped it without
        # issuing a second RPC.
        self.assertEqual(creates.count(("create", "account-one")), 1)
        self.assertEqual(creates.count(("create", "account-two")), 3)
        for s in sessions:
            self.assertEqual(
                self.plugin.sandbox_sessions[s]["key_id"],
                self.plugin._key_pool.identifier("account-two"),
            )
        # A is in cooldown and no KeyInCooldownError leaked into any result.
        with self.assertRaises(KeyInCooldownError):
            self.plugin._key_pool.ensure_available(
                self.plugin._key_pool.identifier("account-one")
            )

        # A later creation still skips the cooled Key.
        self.plugin.config.update({"max_running_sandboxes": 5})
        await self.create("later")
        self.assertEqual(
            self.plugin.sandbox_sessions["test:friend:later"]["key_id"],
            self.plugin._key_pool.identifier("account-two"),
        )

    # ------------------------------------------------------------------
    # Q2: cooldown gates new creation only; existing ops keep the Key
    # ------------------------------------------------------------------
    async def test_Q2_cooldown_blocks_new_creation_only(self):
        pool = self.plugin._key_pool
        await self.create("w")
        session = "test:friend:w"
        self.assertEqual(
            self.plugin.sandbox_sessions[session]["key_id"],
            pool.identifier("account-one"),
        )
        pool.reject(pool.identifier("account-one"), 401)

        # Existing sandbox is still managed with its cooled Key.
        result = await self.plugin.get_session_sandbox_status(Event("w"))
        self.assertIn("Sandbox ID", result)
        self.assertIn(("info", "account-one"), self.calls)
        await self.plugin.pause_session_sandbox(Event("w"))
        self.assertIn(("pause", "account-one"), self.calls)

        # New creation skips the cooled Key.
        await self.create("n1")
        self.assertEqual(
            self.plugin.sandbox_sessions["test:friend:n1"]["key_id"],
            pool.identifier("account-two"),
        )

        # All keys cooled: admission refuses before capacity or any RPC.
        pool.reject(pool.identifier("account-two"), 429)
        create_count = FakeSDK.create_count
        calls_before = list(self.calls)
        result = await self.plugin.create_session_sandbox(Event("n2"))
        self.assertIn("All configured E2B API Keys are in cooldown", result)
        n2 = "test:friend:n2"
        self.assertNotIn(n2, self.plugin.sandbox_sessions)
        self.assertNotIn(n2, self.plugin._capacity.occupied)
        self.assertEqual(FakeSDK.create_count, create_count)
        self.assertEqual(self.calls, calls_before)

        # After cooldown expiry the original Key is selected again.
        for key_id in (pool.identifier("account-one"),
                       pool.identifier("account-two")):
            pool._cooldown_until[key_id] = time.monotonic() - 1
        self.plugin._next_creation_time = 0
        result = await self.plugin.create_session_sandbox(Event("n3"))
        self.assertIn("Created sandbox", result)
        self.plugin._next_creation_time = 0
        n3 = "test:friend:n3"
        self.assertEqual(
            self.plugin.sandbox_sessions[n3]["key_id"],
            pool.identifier("account-one"),
        )
        self.assertEqual(self.calls[-1], ("create", "account-one"))


if __name__ == "__main__":
    unittest.main()
