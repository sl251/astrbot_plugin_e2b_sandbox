"""No E2B network: lifecycle fakes + strict host request/auth contract tests."""

import asyncio
import base64
import contextlib
import hashlib
import hmac
import importlib
import json
import shutil
import subprocess
import sys
import time
import types
import unittest
from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest.mock import AsyncMock, patch

from helpers import FakeSDK, FakeSandbox, Query, State, load_main

main = load_main()
panel_module = importlib.import_module("sandbox_plugin_test.web_panel")
WebPanel = panel_module.WebPanel
ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "views" / "dashboard"
if not DASHBOARD.is_dir():
    DASHBOARD = ROOT / "webui" / "dashboard"
SECRET = "only-a-local-test-dashboard-secret"
TOKEN_PAYLOAD = {"username": "dashboard-admin", "exp": 4102444800}


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def signed_token(payload=TOKEN_PAYLOAD, secret=SECRET):
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    signature = _b64(hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest())
    return f"{header}.{body}.{signature}"


class ContractJWT:
    """Small HS256 test double; production imports the host's PyJWT.

    It verifies the real signature/expiration rather than making any token
    'valid'. This tests our allowed credential types and decoder requirements,
    not PyJWT, the external HTTP router, or a real installed host end-to-end.
    """

    @staticmethod
    def decode(token, key, algorithms, options):
        header, payload, signature = token.split(".")
        decode = lambda part: json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        if algorithms != ["HS256"] or decode(header).get("alg") != "HS256":
            raise ValueError("unsupported algorithm")
        expected = _b64(hmac.new(key.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(expected, signature):
            raise ValueError("bad signature")
        data = decode(payload)
        if any(field not in data for field in options.get("require", ())):
            raise ValueError("missing required claim")
        if data["exp"] <= time.time():
            raise ValueError("expired")
        return data


class FakeResponse:
    def __init__(self, content, status_code=200, headers=None):
        self.content = content
        self.status_code = status_code
        self.headers = dict(headers or {})


class FakeRequest:
    def __init__(self, method="GET", payload=None, *, token=None, cookie=False,
                 username: str | None = "dashboard-admin", plugin_name=panel_module.PLUGIN_NAME):
        self.method = method
        self.username = username
        self.plugin_name = plugin_name
        self.query = {}
        self.cookies = {}
        self.headers = {}
        token = signed_token() if token is None else token
        if cookie:
            self.cookies[panel_module.DASHBOARD_COOKIE] = token
        else:
            self.headers["Authorization"] = f"Bearer {token}" if token else ""
        self.content_type = "application/json" if method == "POST" else None
        self.raw = json.dumps(payload).encode() if payload is not None else b""
        self.headers["Content-Length"] = str(len(self.raw))
        self.read_count = 0
        self.before_body: Callable[[], Awaitable[None]] | None = None
        self._request = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(jwt_secret=SECRET)))

    async def body(self):
        self.read_count += 1
        if self.before_body:
            await self.before_body()
        return self.raw


class WebPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeSDK.reset()
        self.context = types.SimpleNamespace(add_llm_tools=lambda *tools: None, registered_web_apis=[])

        def register(route, handler, methods, desc):
            apis = self.context.registered_web_apis
            apis[:] = [item for item in apis if item[0] != route]
            apis.append((route, handler, methods, desc))

        self.context.register_web_api = register
        self.patches = [
            patch.object(main, "AsyncSandbox", FakeSDK),
            patch.object(main, "SandboxQuery", Query),
            patch.object(main, "SandboxState", State),
            patch.object(main.SessionStoreMixin, "_get_plugin_data_dir", lambda plugin: ROOT / "webui"),
            patch.object(main.SessionStoreMixin, "_load_sandbox_sessions", lambda plugin: None),
            patch.object(main.SessionStoreMixin, "_save_sandbox_sessions", lambda plugin: None),
        ]
        for item in self.patches:
            item.start()
        self.plugin = main.Main(self.context, {
            "e2b_api_key": "e2b-secret-legacy-not-public",
            "e2b_api_keys": ["e2b-secret-second-not-public"],
            "enable_user_whitelist": False,
            "max_running_sandboxes": 2,
        })
        self.plugin._start_maintenance = lambda: None
        self.web = types.SimpleNamespace(request=FakeRequest(), json_response=FakeResponse)
        self.panel = WebPanel(self.plugin)
        self.panel._web, self.panel._jwt = self.web, ContractJWT

    async def asyncTearDown(self):
        await self.panel.close()
        await self.plugin.terminate()
        for item in reversed(self.patches):
            item.stop()

    async def existing(self, session_id="qq:GroupMessage:panel-session", status="running", owner=True):
        index = len(FakeSDK.boxes) + 1
        box = FakeSandbox(f"box-{index}", metadata={})
        box.state = State.RUNNING if status == "running" else State.PAUSED
        FakeSDK.boxes[box.sandbox_id] = box
        self.plugin._update_sandbox_session(session_id, box.sandbox_id, status=status)
        if not owner:
            self.plugin.sandbox_sessions[session_id].pop("key_id", None)
        if status == "running":
            self.plugin._capacity.occupied.add(session_id)
        return session_id, box

    def action_payload(self, session_id, action="pause"):
        meta = self.plugin.sandbox_sessions[session_id]
        return {
            "action": action,
            "session_ref": self.panel._reference(session_id),
            "expected_sandbox_id": meta["sandbox_id"],
            "expected_version": self.panel._version(session_id, meta),
            "confirm": True,
        }

    async def action(self, session_id, action="pause", **updates):
        payload = self.action_payload(session_id, action)
        payload.update(updates)
        return await self.panel.handle_action(FakeRequest("POST", payload))

    def code(self, response):
        return response.content.get("data", {}).get("code")

    async def test_registered_route_shape_and_authentication(self):
        with patch.object(panel_module, "import_module", side_effect=lambda name: self.web if name == "astrbot.api.web" else ContractJWT):
            self.assertTrue(self.panel.register())
            self.assertTrue(self.panel.register())
        apis = self.context.registered_web_apis
        self.assertEqual([entry[0] for entry in apis], [panel_module.SNAPSHOT_ROUTE, panel_module.ACTION_ROUTE])
        self.assertEqual([entry[2] for entry in apis], [["GET"], ["POST"]])
        self.web.request = FakeRequest(username=None)
        response = await apis[0][1]()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.code(response), "unauthorized")
        self.assertEqual(FakeSDK.create_count, 0)

    async def test_dashboard_bearer_and_dashboard_cookie_are_accepted(self):
        for cookie in (False, True):
            response = await self.panel.handle_snapshot(FakeRequest(cookie=cookie))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content["status"], "ok")
            self.assertIn("no-store", response.headers["Cache-Control"])

    async def test_no_user_invalid_expired_or_mismatched_credentials_denied(self):
        cases = [
            FakeRequest(username=None), FakeRequest(token=""), FakeRequest(token="made-up-token"),
            FakeRequest(token=signed_token(secret="wrong-secret")),
            FakeRequest(token=signed_token({"username": "dashboard-admin", "exp": 1})),
            FakeRequest(token=signed_token({"username": "dashboard-admin"})),
            FakeRequest(token=signed_token({"exp": 4102444800})),
            FakeRequest(token=signed_token({"username": "somebody-else", "exp": 4102444800})),
            FakeRequest(plugin_name="another-plugin"),
        ]
        for request in cases:
            with self.subTest(token=request.headers.get("Authorization")):
                response = await self.panel.handle_snapshot(request)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(self.code(response), "unauthorized")

    async def test_scoped_asset_token_and_apikey_never_authorize_management(self):
        view_payload = {**TOKEN_PAYLOAD, "token_type": "plugin_view_asset", "plugin_name": panel_module.PLUGIN_NAME, "page_name": "dashboard"}
        for cookie in (False, True):
            response = await self.panel.handle_snapshot(FakeRequest(token=signed_token(view_payload), cookie=cookie))
            self.assertEqual(response.status_code, 401)
        request = FakeRequest(token="")
        request.headers["Authorization"] = "ApiKey a-model-or-mobile-key"
        request.query["token"] = signed_token()
        response = await self.panel.handle_snapshot(request)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(FakeSDK.create_count, 0)

    async def test_snapshot_is_redacted_local_read_only_and_keeps_xss_as_text(self):
        raw_keys = list(self.plugin._key_pool._keys.values())
        dangerous_label = '<img src=x onerror="alert(1)"> ' + raw_keys[0]
        session, box = await self.existing(dangerous_label)
        self.plugin.sandbox_sessions[session].update({"debug": "/root/private/config", "api_key": raw_keys[1]})
        second, _ = await self.existing("second-session")
        self.plugin.sandbox_sessions[second]["sandbox_id"] = raw_keys[1]
        self.plugin._key_pool._cooldown_until[self.plugin._key_pool._order[0]] = time.monotonic() + 62
        before = json.dumps(self.plugin.sandbox_sessions, sort_keys=True)
        capacity_before = set(self.plugin._capacity.occupied)
        with patch.object(FakeSDK, "connect", AsyncMock(side_effect=AssertionError("must not connect"))), \
             patch.object(FakeSDK, "create", AsyncMock(side_effect=AssertionError("must not create"))), \
             patch.object(self.plugin, "_save_sandbox_sessions", side_effect=AssertionError("must not write")):
            response = await self.panel.handle_snapshot(FakeRequest())
        self.assertEqual(response.status_code, 200)
        data = response.content["data"]
        encoded = json.dumps(data, ensure_ascii=False)
        for key in raw_keys:
            self.assertNotIn(key, encoded)
        self.assertNotIn("/root/private", encoded)
        self.assertNotIn("debug", encoded)
        self.assertNotIn("api_key", encoded)
        self.assertEqual(data["keys"][0]["label"], "Key #1")
        self.assertGreaterEqual(data["keys"][0]["cooldown_seconds"], 61)
        self.assertEqual(data["counts"]["total"], 2)
        self.assertEqual(data["capacity"]["occupied"], 2)
        row = next(row for row in data["sessions"] if row["sandbox_id"] == box.sandbox_id)
        self.assertIn('<img src=x onerror="alert(1)">', row["session_label"])
        unsafe = next(row for row in data["sessions"] if row["sandbox_id"] != box.sandbox_id)
        self.assertFalse(unsafe["can_kill"])
        self.assertEqual(before, json.dumps(self.plugin.sandbox_sessions, sort_keys=True))
        self.assertEqual(capacity_before, self.plugin._capacity.occupied)

    async def test_auth_denied_before_action_body_is_read(self):
        request = FakeRequest("POST", {}, token="", username=None)
        response = await self.panel.handle_action(request)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(request.read_count, 0)

    async def test_pause_uses_existing_manager_releases_only_target_capacity(self):
        session, box = await self.existing()
        other, _ = await self.existing("untouched-session")
        payload = self.action_payload(session)
        other_before = dict(self.plugin.sandbox_sessions[other])
        with patch.object(self.plugin, "_finish_pause", wraps=self.plugin._finish_pause) as manager, \
             patch.object(FakeSDK, "connect", AsyncMock(side_effect=AssertionError("must not resume"))):
            response = await self.panel.handle_action(FakeRequest("POST", payload))
        manager.assert_awaited_once_with(session)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content["data"]["outcome"], "paused")
        self.assertEqual(box.state, State.PAUSED)
        self.assertEqual(self.plugin.sandbox_sessions[session]["status"], "paused")
        self.assertNotIn(session, self.plugin._capacity.occupied)
        self.assertIn(other, self.plugin._capacity.occupied)
        self.assertEqual(other_before, self.plugin.sandbox_sessions[other])
        self.assertEqual(FakeSDK.create_count, 0)
        self.assertEqual(self.plugin._capacity.waiting, 0)

    async def test_kill_uses_existing_manager_without_connect(self):
        session, _ = await self.existing()
        payload = self.action_payload(session, "kill")
        with patch.object(self.plugin, "_kill_sandbox_by_id", wraps=self.plugin._kill_sandbox_by_id) as manager, \
             patch.object(FakeSDK, "connect", AsyncMock(side_effect=AssertionError("must not resume"))):
            response = await self.panel.handle_action(FakeRequest("POST", payload))
        manager.assert_awaited_once_with(session)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content["data"]["outcome"], "killed")
        self.assertNotIn(session, self.plugin.sandbox_sessions)
        self.assertNotIn(session, self.plugin._capacity.occupied)
        self.assertEqual(FakeSDK.boxes, {})
        self.assertEqual(FakeSDK.create_count, 0)

    async def test_busy_and_pending_cleanup_rejected_without_queue(self):
        session, _ = await self.existing()
        lock = self.plugin._get_session_lock(session)
        await lock.acquire()
        try:
            response = await asyncio.wait_for(self.action(session), 0.5)
        finally:
            lock.release()
        self.assertEqual(self.code(response), "session_busy")
        self.plugin._has_pending_cleanup = lambda value: value == session
        response = await asyncio.wait_for(self.action(session, "kill"), 0.5)
        self.assertEqual(self.code(response), "session_busy")
        self.assertEqual(self.plugin._capacity.waiting, 0)
        self.assertEqual(self.panel._tasks, {})

    async def test_stale_id_version_missing_and_other_target_are_rejected(self):
        session, box = await self.existing()
        other, other_box = await self.existing("other-session")
        payload = self.action_payload(session, "kill")
        wrong_id = {**payload, "expected_sandbox_id": other_box.sandbox_id}
        response = await self.panel.handle_action(FakeRequest("POST", wrong_id))
        self.assertEqual(self.code(response), "stale_target")
        self.plugin.sandbox_sessions[session]["last_active"] += 1
        response = await self.panel.handle_action(FakeRequest("POST", payload))
        self.assertEqual(self.code(response), "stale_target")
        missing = {**payload, "session_ref": "f" * 64}
        response = await self.panel.handle_action(FakeRequest("POST", missing))
        self.assertEqual(self.code(response), "target_missing")
        self.assertIn(box.sandbox_id, FakeSDK.boxes)
        self.assertIn(other, self.plugin.sandbox_sessions)

    async def test_target_version_rechecked_inside_existing_session_lock(self):
        session, _ = await self.existing()
        payload = self.action_payload(session)
        original = self.plugin._session_operation

        @contextlib.asynccontextmanager
        async def changed_at_lock(session_id):
            async with original(session_id):
                self.plugin.sandbox_sessions[session_id]["sandbox_id"] = "replacement-box"
                yield

        with patch.object(self.plugin, "_session_operation", changed_at_lock), \
             patch.object(self.plugin, "_finish_pause", AsyncMock()) as pause:
            response = await self.panel.handle_action(FakeRequest("POST", payload))
        self.assertEqual(self.code(response), "stale_target")
        pause.assert_not_awaited()

    async def test_unknown_removed_owner_is_never_fallback_or_rotated(self):
        session, _ = await self.existing(owner=False)
        for action in ("pause", "kill"):
            with patch.object(self.plugin, "_finish_pause", AsyncMock()) as pause, \
                 patch.object(self.plugin, "_kill_sandbox_by_id", AsyncMock()) as kill:
                response = await self.action(session, action)
            self.assertEqual(self.code(response), "owner_missing")
            pause.assert_not_awaited()
            kill.assert_not_awaited()
        self.plugin.sandbox_sessions[session]["key_id"] = "key-removed-from-config"
        with patch.object(self.plugin, "_finish_pause", AsyncMock()) as pause, \
             patch.object(self.plugin, "_kill_sandbox_by_id", AsyncMock()) as kill:
            response = await self.action(session, "kill")
        self.assertEqual(self.code(response), "owner_missing")
        pause.assert_not_awaited()
        kill.assert_not_awaited()
        self.assertIn(session, self.plugin._capacity.occupied)
        self.assertEqual(self.plugin._key_pool._cursor, 0)
        self.assertEqual(FakeSDK.pause_count, 0)
        self.assertEqual(FakeSDK.kill_count, 0)
        row = self.panel.snapshot()["sessions"][0]
        self.assertFalse(row["can_pause"])
        self.assertFalse(row["can_kill"])

    async def test_duplicate_sandbox_mapping_rejected(self):
        session, box = await self.existing()
        self.plugin.sandbox_sessions["duplicate"] = dict(self.plugin.sandbox_sessions[session])
        response = await self.action(session, "kill")
        self.assertEqual(self.code(response), "ambiguous_target")
        self.assertIn(box.sandbox_id, FakeSDK.boxes)

    async def test_explicit_confirmation_and_exact_schema_required(self):
        session, _ = await self.existing()
        valid = self.action_payload(session)
        cases = [{**valid, "confirm": False}, {**valid, "confirm": 1}, {**valid, "key_id": "other"},
                 {**valid, "action": "resume"}, {**valid, "action": "create"},
                 {**valid, "action": "execute"}, {**valid, "action": "cleanup_all"},
                 {**valid, "session_ref": "malformed"}, {**valid, "expected_version": "0"},
                 {**valid, "expected_sandbox_id": ""}]
        for payload in cases:
            response = await self.panel.handle_action(FakeRequest("POST", payload))
            self.assertEqual(response.status_code, 422)
        self.assertIn(session, self.plugin._capacity.occupied)
        self.assertEqual(FakeSDK.create_count, 0)

    async def test_json_only_body_size_duplicate_fields_and_methods(self):
        session, _ = await self.existing()
        payload = self.action_payload(session)
        request = FakeRequest("POST", payload)
        request.content_type = "text/plain"
        self.assertEqual((await self.panel.handle_action(request)).status_code, 415)
        for raw in (b"x" * 5000, b'{"action":"pause","action":"kill"}'):
            request = FakeRequest("POST", payload)
            request.raw = raw
            request.headers.pop("Content-Length")
            self.assertIn((await self.panel.handle_action(request)).status_code, (413, 422))
        response = await self.panel.handle_action(FakeRequest("GET", payload))
        self.assertEqual(response.status_code, 405)
        response = await self.panel.handle_snapshot(FakeRequest("POST", payload))
        self.assertEqual(response.status_code, 405)

    async def test_pause_failure_retains_record_and_capacity(self):
        session, box = await self.existing()
        setattr(FakeSDK, "pause_error", RuntimeError("api_key=e2b-secret-legacy-not-public /root/private/path STACK"))
        with patch.object(self.plugin, "_finish_pause", wraps=self.plugin._finish_pause) as pause:
            response = await self.action(session)
        pause.assert_awaited_once_with(session)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.code(response), "pause_unconfirmed")
        self.assertIn(session, self.plugin.sandbox_sessions)
        self.assertEqual(self.plugin.sandbox_sessions[session]["status"], "unknown")
        self.assertIn(session, self.plugin._capacity.occupied)
        self.assertEqual(self.plugin._key_pool._cursor, 0)
        encoded = json.dumps(response.content)
        self.assertNotIn("e2b-secret", encoded)
        self.assertNotIn("/root/private", encoded)
        self.assertNotIn("STACK", encoded)

    async def test_kill_failure_retains_record_and_capacity_and_redacts_error(self):
        session, box = await self.existing()
        setattr(FakeSDK, "kill_error", RuntimeError("e2b-secret-second-not-public /root/secret Traceback"))
        response = await self.action(session, "kill")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.code(response), "operation_failed")
        self.assertIn(session, self.plugin.sandbox_sessions)
        self.assertIn(session, self.plugin._capacity.occupied)
        encoded = json.dumps(response.content)
        self.assertNotIn("e2b-secret", encoded)
        self.assertNotIn("/root/", encoded)
        self.assertNotIn("Traceback", encoded)

    async def test_disconnect_keeps_session_locked_until_management_finishes(self):
        session, _ = await self.existing()
        started, finish = asyncio.Event(), asyncio.Event()
        original = self.plugin._finish_pause

        async def slow_pause(session_id):
            self.assertTrue(self.plugin._get_session_lock(session_id).locked())
            started.set()
            await finish.wait()
            return await original(session_id)

        with patch.object(self.plugin, "_finish_pause", slow_pause):
            caller = asyncio.create_task(self.action(session))
            await asyncio.wait_for(started.wait(), 1)
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            self.assertTrue(self.plugin._get_session_lock(session).locked())
            response = await asyncio.wait_for(self.action(session, "kill"), 0.5)
            self.assertEqual(self.code(response), "session_busy")
            finish.set()
            await asyncio.gather(*tuple(self.panel._tasks.values()))
            await asyncio.sleep(0)
        self.assertFalse(self.plugin._get_session_lock(session).locked())
        self.assertNotIn(session, self.plugin._capacity.occupied)
        self.assertEqual(self.panel._tasks, {})

    async def test_close_rejects_new_requests_while_finishing_existing_action(self):
        session, _ = await self.existing()
        started, finish = asyncio.Event(), asyncio.Event()
        original = self.plugin._finish_pause

        async def slow_pause(session_id):
            started.set()
            await finish.wait()
            return await original(session_id)

        with patch.object(self.plugin, "_finish_pause", slow_pause):
            caller = asyncio.create_task(self.action(session))
            await asyncio.wait_for(started.wait(), 1)
            closer = asyncio.create_task(self.panel.close())
            await asyncio.sleep(0)
            self.assertFalse(closer.done())
            response = await self.panel.handle_snapshot(FakeRequest())
            self.assertEqual(self.code(response), "stopping")
            finish.set()
            self.assertEqual((await caller).status_code, 200)
            await closer

    async def test_shutdown_during_body_read_does_not_start_action(self):
        session, _ = await self.existing()
        request = FakeRequest("POST", self.action_payload(session))
        request.before_body = self.panel.close
        with patch.object(self.plugin, "_finish_pause", AsyncMock()) as pause:
            response = await self.panel.handle_action(request)
        self.assertEqual(self.code(response), "stopping")
        pause.assert_not_awaited()

    async def test_unregister_preserves_other_plugins_and_reloaded_handler(self):
        with patch.object(panel_module, "import_module", side_effect=lambda name: self.web if name == "astrbot.api.web" else ContractJWT):
            self.assertTrue(self.panel.register())
        other = ("/unrelated/route", lambda: None, ["GET"], "other")
        newer = (panel_module.SNAPSHOT_ROUTE, lambda: None, ["GET"], "new instance")
        self.context.registered_web_apis.append(other)
        self.context.registered_web_apis[0] = newer
        self.panel.unregister()
        self.assertEqual(self.context.registered_web_apis, [newer, other])

    async def test_old_hosts_and_partial_registration_fail_closed(self):
        old = WebPanel(types.SimpleNamespace(context=types.SimpleNamespace()))
        self.assertFalse(old.register())
        for missing in ("json_response", "request"):
            web = types.SimpleNamespace(**{name: value for name, value in vars(self.web).items() if name != missing})
            with patch.object(panel_module, "import_module", side_effect=lambda name: web if name == "astrbot.api.web" else ContractJWT):
                self.assertFalse(self.panel.register())
            self.assertEqual(self.context.registered_web_apis, [])
        with patch.object(panel_module, "import_module", side_effect=ImportError):
            self.assertFalse(self.panel.register())
        original = self.context.register_web_api

        def partial(route, handler, methods, desc):
            if route == panel_module.ACTION_ROUTE:
                raise RuntimeError("old host rejected second route")
            original(route, handler, methods, desc)

        self.context.register_web_api = partial
        with patch.object(panel_module, "import_module", side_effect=lambda name: self.web if name == "astrbot.api.web" else ContractJWT):
            self.assertFalse(self.panel.register())
        self.assertEqual(self.context.registered_web_apis, [])
        self.assertFalse(self.panel.available)


class FrontendTests(unittest.TestCase):
    def test_no_external_resources_token_reads_or_html_interpolation(self):
        html = (DASHBOARD / "index.html").read_text()
        css = (DASHBOARD / "panel.css").read_text()
        panel = (DASHBOARD / "panel.js").read_text()
        bridge = (DASHBOARD / "bridge-client.js").read_text()
        self.assertIn('src="/api/plugin/page/bridge-sdk.js"', html)
        self.assertIn("connect-src 'none'", html)
        self.assertIn('name="referrer" content="no-referrer"', html)
        for source in (html, css, panel, bridge):
            self.assertNotRegex(source, r"https?://|@import|url\(")
        for source in (panel, bridge):
            for dangerous in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "fetch(",
                              "localStorage", "sessionStorage", "location.search", "location.pathname", "document.cookie"):
                self.assertNotIn(dangerous, source)
        self.assertIn("textContent", panel)
        self.assertIn("document.hidden", panel)
        self.assertIn("controller.signal.aborted", panel)
        self.assertIn("expected_version: session.version", panel)
        self.assertIn("pendingTarget = Object.freeze", panel)
        self.assertIn("dom.killId.value !== pendingTarget.session.sandbox_id", panel)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable; bridge runtime check skipped")
    def test_bridge_wire_protocol_abort_source_origin_and_timeout(self):
        script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {randomUUID} = require('node:crypto');
const listeners = new Set();
const posted = [];
const parent = {postMessage: (value, origin) => posted.push({value, origin})};
const fake = {
  crypto: {randomUUID}, URL,
  location: {href: 'https://webui.example/api/plugin/view/assets/SCOPED-SECRET/x/dashboard/index.html'},
  parent, setTimeout, clearTimeout,
  addEventListener: (name, listener) => { if (name === 'message') listeners.add(listener); },
  removeEventListener: (name, listener) => { if (name === 'message') listeners.delete(listener); },
};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {window: fake});
const emit = (data, origin = 'https://webui.example', source = parent) => {
  for (const listener of [...listeners]) listener({data, origin, source});
};
const response = (postedRequest, extra = {}) => ({
  channel: 'astrbot-plugin-page', kind: 'response',
  requestId: postedRequest.value.requestId, ok: true, data: {status: 'ok', data: {sentinel: 42}}, ...extra,
});
(async () => {
  const client = fake.E2BPanelBridge.createClient(fake, 50);
  const controller = new AbortController();
  const read = client.getSnapshot(controller.signal);
  assert.equal(posted[0].origin, 'https://webui.example');
  assert.equal(posted[0].value.action, 'api:get');
  assert.equal(posted[0].value.endpoint, 'dashboard/snapshot');
  assert(!JSON.stringify(posted[0]).includes('SCOPED-SECRET'));
  emit(response(posted[0]), 'https://attacker.example');
  emit(response(posted[0]), 'https://webui.example', {});
  assert.equal(client.pendingCount(), 1);
  controller.abort();
  await assert.rejects(read, {name: 'AbortError'});
  assert.equal(client.pendingCount(), 0);
  emit(response(posted[0]));
  const second = client.getSnapshot();
  emit(response(posted[1]));
  assert.equal((await second).sentinel, 42);
  const body = {action: 'kill', expected_version: 'frozen'};
  const action = client.act(body);
  assert.equal(posted[2].value.endpoint, 'dashboard/action');
  assert.equal(posted[2].value.action, 'api:post');
  assert.equal(posted[2].value.body.expected_version, 'frozen');
  emit(response(posted[2], {ok: false, error: 'SECRET token=https://external/config STACK'}));
  try { await action; assert.fail('must reject'); } catch (error) {
    assert.equal(error.code, 'bridge_error');
    assert(!error.message.includes('SECRET'));
    assert(!error.message.includes('external'));
  }
  const inheritedCode = client.getSnapshot();
  emit(response(posted[3], {data: {status: 'error', message: 'secret', data: {code: '__proto__'}}}));
  try { await inheritedCode; assert.fail('must reject'); } catch (error) {
    assert.equal(error.code, 'request_failed');
    assert(!error.message.includes('secret'));
  }
  await assert.rejects(client.getSnapshot(), {code: 'timeout'});
  assert.equal(client.pendingCount(), 0);
  const cancelled = client.getSnapshot();
  client.destroy();
  await assert.rejects(cancelled, {name: 'AbortError'});
  assert.equal(listeners.size, 0);
  assert.equal(client.pendingCount(), 0);
  // Fail closed on null/file/mismatched origins and self-parent without posting or listening.
  for (const badHref of ['about:blank', 'about:srcdoc', 'file:///tmp/index.html']) {
    const badListeners = new Set();
    const badPosted = [];
    const badWindow = {
      ...fake,
      location: {href: badHref, origin: 'null'},
      parent: {postMessage: (value, origin) => badPosted.push({value, origin})},
      addEventListener: (_n, l) => badListeners.add(l),
      removeEventListener: (_n, l) => badListeners.delete(l),
    };
    assert.throws(() => fake.E2BPanelBridge.createClient(badWindow), {code: 'invalid_origin'});
    assert.equal(badListeners.size, 0);
    assert.equal(badPosted.length, 0);
  }
  const lanWindow = {
    ...fake,
    crypto: {getRandomValues: (arr) => arr.fill(7)},
    location: {href: 'http://192.168.1.10:6185/api/plugin/view/assets/t/p/dashboard/index.html', origin: 'null'},
  };
  const lanClient = fake.E2BPanelBridge.createClient(lanWindow, 50);
  const lanRead = lanClient.getSnapshot();
  assert.equal(posted.at(-1).origin, 'http://192.168.1.10:6185');
  emit(response(posted.at(-1)), 'http://192.168.1.10:6185');
  assert.equal((await lanRead).sentinel, 42);
  lanClient.destroy();
  console.log('bridge assertions passed: same-origin, fixed endpoints, no token, abort, timeout, redacted errors');
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run([node, "-e", script, str(DASHBOARD / "bridge-client.js")],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("bridge assertions passed", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable; panel DOM runtime check skipped")
    def test_panel_dom_theme_missing_fields_races_and_cleanup(self):
        script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

class FakeElement {
  constructor(tag, id = '') {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this.children = [];
    this.attributes = {};
    this.listeners = new Map();
    this.className = '';
    this.hidden = false;
    this.disabled = false;
    this.checked = true;
    this.value = '';
    this.open = false;
    this.isConnected = true;
    this.focused = false;
    this._text = '';
  }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return this.attributes[k]; }
  appendChild(child) {
    if (child && child._isFragment) this.children.push(...child.children);
    else this.children.push(child);
    return child;
  }
  replaceChildren(...nodes) {
    this.children = [];
    for (const node of nodes) this.appendChild(node);
  }
  addEventListener(name, fn) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(fn);
  }
  dispatch(name, event = {}) {
    const ev = {preventDefault: () => {}, ...event};
    for (const fn of (this.listeners.get(name) || [])) fn(ev);
  }
  focus() { this.focused = true; }
  showModal() { this.open = true; }
  close() { this.open = false; }
}

const ids = [
  'refresh-button', 'auto-refresh', 'poll-state', 'notice', 'session-list', 'session-search',
  'status-filter', 'previous-page', 'next-page', 'page-label', 'key-list', 'confirm-dialog',
  'confirm-action', 'cancel-action', 'pause-ack', 'kill-id', 'action-feedback', 'sessions-section',
  'count-total', 'count-running', 'count-paused', 'count-unknown', 'busy-count',
  'capacity-used', 'capacity-limit', 'capacity-free', 'capacity-queue', 'capacity-timeout',
  'capacity-progress', 'capacity-pending', 'last-updated', 'session-summary', 'key-count',
  'confirm-title', 'confirm-description', 'confirm-session', 'confirm-sandbox', 'confirm-key',
  'pause-ack-label', 'kill-input-label',
];
const elements = new Map(ids.map((id) => [id, new FakeElement('div', id)]));
elements.get('status-filter').value = 'all';
const docEl = new FakeElement('html');
const docListeners = new Map();
const winListeners = new Map();
const timers = new Set();
let nextTimer = 1;
const fakeDocument = {
  hidden: false,
  documentElement: docEl,
  activeElement: null,
  getElementById: (id) => elements.get(id) || null,
  createElement: (tag) => new FakeElement(tag),
  createDocumentFragment: () => ({_isFragment: true, children: [], appendChild(c) { this.children.push(c); }}),
  addEventListener: (name, fn) => {
    if (!docListeners.has(name)) docListeners.set(name, []);
    docListeners.get(name).push(fn);
  },
};

let contextCallback = null;
let unsubCalled = false;
let clientDestroyed = false;
let snapshotQueue = [];
class PanelClientError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}

const fakeWindow = {
  document: fakeDocument,
  HTMLElement: FakeElement,
  parent: {},
  AstrBotPluginView: {
    ready: async () => ({pluginName: 'astrbot_plugin_e2b_sandbox', isDark: true}),
    onContext: (cb) => { contextCallback = cb; return () => { unsubCalled = true; }; },
  },
  E2BPanelBridge: {
    PanelClientError,
    createClient: () => ({
      getSnapshot: () => new Promise((resolve, reject) => snapshotQueue.push({resolve, reject})),
      act: async (body) => ({action: body.action, sandbox_id: body.expected_sandbox_id, outcome: 'killed'}),
      destroy: () => { clientDestroyed = true; },
    }),
  },
  setTimeout: (fn) => { const id = nextTimer++; timers.add(id); return id; },
  clearTimeout: (id) => { timers.delete(id); },
  setInterval: (fn) => { const id = nextTimer++; timers.add(id); return id; },
  clearInterval: (id) => { timers.delete(id); },
  addEventListener: (name, fn) => {
    if (!winListeners.has(name)) winListeners.set(name, []);
    winListeners.get(name).push(fn);
  },
};

const code = fs.readFileSync(process.argv[1], 'utf8');
vm.runInNewContext(code, {
  window: fakeWindow,
  document: fakeDocument,
  HTMLElement: FakeElement,
  AbortController,
  Date,
  Math,
  Number,
  String,
  Array,
  Object,
  Promise,
  Error,
});

(async () => {
  await new Promise((r) => setImmediate(r));
  assert.equal(docEl.getAttribute('data-theme'), 'dark');
  contextCallback({isDark: false});
  assert.equal(docEl.getAttribute('data-theme'), 'light');

  // Trigger a second manual refresh before the first resolves: stale read must not win.
  elements.get('refresh-button').dispatch('click');
  assert.equal(snapshotQueue.length, 2);
  const firstRead = snapshotQueue[0];
  const secondRead = snapshotQueue[1];
  secondRead.resolve({
    source: 'local_tracking_only',
    generated_at: 'bad',
    counts: {total: 1, running: NaN},
    capacity: {occupied: null, limit: 2, available: undefined, waiting: 0, max_queue: 'x'},
    keys: [{label: 'Key #1', bound_sessions: undefined, cooldown_seconds: 'bad'}],
    sessions: [{
      session_ref: 'a'.repeat(64),
      session_label: 's1',
      sandbox_id: 'sbx-1',
      version: 'b'.repeat(64),
      status: 'running',
      key_label: 'Key #1',
      blocked_reason: 'toString',
      can_pause: true,
      can_kill: true,
    }],
  });
  await new Promise((r) => setImmediate(r));
  firstRead.resolve({
    source: 'local_tracking_only',
    generated_at: 1000,
    counts: {total: 99, running: 99, paused: 0, unknown: 0, busy: 0},
    capacity: {occupied: 99, limit: 99, available: 0, waiting: 0, max_queue: 0, wait_timeout_seconds: 10, runtime_limit: 99},
    keys: [],
    sessions: [],
  });
  await new Promise((r) => setImmediate(r));

  assert.equal(elements.get('count-total').textContent, '1');
  assert.equal(elements.get('count-running').textContent, '—');
  assert.equal(elements.get('capacity-used').textContent, '—');
  assert.equal(elements.get('capacity-queue').textContent, '0 / —');
  assert.equal(elements.get('capacity-timeout').textContent, '—');
  assert.equal(elements.get('last-updated').textContent, '—');
  const row = elements.get('session-list').children[0];
  assert.equal(row.children.length, 6);
  const actionBox = row.children[5].children[0];
  // Unknown blocked_reason values must not be echoed as UI text.
  assert.equal(actionBox.children.length, 1);
  assert.equal(actionBox.children[0].className, 'action-row');

  // pagehide cleans up timers, context listener, and bridge client.
  for (const fn of (winListeners.get('pagehide') || [])) fn();
  assert.equal(timers.size, 0);
  assert.equal(unsubCalled, true);
  assert.equal(clientDestroyed, true);
  console.log('panel DOM assertions passed: theme, missing/NaN fallback, stale-read guard, cleanup');
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run([node, "-e", script, str(DASHBOARD / "panel.js")],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("panel DOM assertions passed", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable; bridge error mapping check skipped")
    def test_bridge_maps_exact_public_errors_and_ignores_host_session(self):
        script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {randomUUID} = require('node:crypto');
const listeners = new Set();
const posted = [];
const parent = {postMessage: (value, origin) => posted.push({value, origin})};
const fake = {
  crypto: {randomUUID}, URL,
  location: {href: 'https://webui.example/api/v1/plugins/p/views/dashboard/_t/tok/'},
  parent, setTimeout, clearTimeout,
  addEventListener: (name, listener) => { if (name === 'message') listeners.add(listener); },
  removeEventListener: (name, listener) => { listeners.delete(listener); },
};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {window: fake});
const emit = (data) => {
  for (const listener of [...listeners]) listener({data, origin: 'https://webui.example', source: parent});
};
(async () => {
  const client = fake.E2BPanelBridge.createClient(fake, 50);
  // 宿主 plugin_page 协议不使用 session；即使收到带 session 的非 response 消息也不得缓存或伪造透传
  emit({channel: 'astrbot-plugin-page', kind: 'context', session: 'sess-xyz', context: {}});

  const stale = client.act({action: 'pause'});
  assert.equal('session' in posted[0].value, false);
  emit({
    channel: 'astrbot-plugin-page', kind: 'response', requestId: posted[0].value.requestId,
    ok: false, error: '该会话的沙箱或版本已变化，请刷新后重新确认。',
  });
  try { await stale; assert.fail('must reject'); } catch (error) {
    assert.equal(error.code, 'stale_target');
    assert.ok(error.message.includes('版本已变化'));
  }

  const busy = client.act({action: 'kill'});
  emit({
    channel: 'astrbot-plugin-page', kind: 'response', requestId: posted[1].value.requestId,
    ok: false, error: '该会话正在执行或清理，请稍后刷新；操作不会排队。',
  });
  try { await busy; assert.fail('must reject'); } catch (error) {
    assert.equal(error.code, 'session_busy');
  }

  // 非白名单未知错误（即使包含白名单前缀）也必须严格折叠脱敏
  const unknown = client.act({action: 'kill'});
  emit({
    channel: 'astrbot-plugin-page', kind: 'response', requestId: posted[2].value.requestId,
    ok: false, error: '请先登录 AstrBot WebUI。 Traceback /secret/key',
  });
  try { await unknown; assert.fail('must reject'); } catch (error) {
    assert.equal(error.code, 'bridge_error');
    assert(!error.message.includes('Traceback'));
    assert(!error.message.includes('/secret/key'));
  }
  client.destroy();
  console.log('bridge public error allowlist assertions passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run([node, "-e", script, str(DASHBOARD / "bridge-client.js")],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("bridge public error allowlist assertions passed", result.stdout)

    def test_dashboard_strict_csp_and_no_inline_scripts(self):
        from html.parser import HTMLParser

        class SecurityHTMLParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.csp_values = []
                self.scripts = []
                self._in_script = False
                self._script_attrs = {}
                self._script_chunks = []

            def handle_starttag(self, tag, attrs):
                attr_map = {k.lower(): (v or "") for k, v in attrs}
                if tag.lower() == "meta" and attr_map.get("http-equiv", "").lower() == "content-security-policy":
                    self.csp_values.append(attr_map.get("content", ""))
                elif tag.lower() == "script":
                    self._in_script = True
                    self._script_attrs = attr_map
                    self._script_chunks = []

            def handle_data(self, data):
                if self._in_script:
                    self._script_chunks.append(data)

            def handle_endtag(self, tag):
                if tag.lower() == "script" and self._in_script:
                    self.scripts.append((self._script_attrs, "".join(self._script_chunks)))
                    self._in_script = False

        html = (DASHBOARD / "index.html").read_text(encoding="utf-8")
        parser = SecurityHTMLParser()
        parser.feed(html)
        parser.close()

        self.assertEqual(len(parser.csp_values), 1)
        csp = parser.csp_values[0]
        csp_lower = csp.lower()
        self.assertNotIn("http:", csp_lower)
        self.assertNotIn("https:", csp_lower)
        self.assertNotIn("unsafe-inline", csp_lower)
        self.assertIn("script-src 'self'", csp)
        self.assertIn("style-src 'self'", csp)
        self.assertIn("connect-src 'none'", csp)

        directives = {}
        for part in csp.split(";"):
            part = part.strip()
            if not part:
                continue
            tokens = part.split()
            directives[tokens[0].lower()] = tokens[1:]
        self.assertEqual(directives.get("script-src"), ["'self'"])
        self.assertEqual(directives.get("style-src"), ["'self'"])
        self.assertEqual(directives.get("connect-src"), ["'none'"])

        self.assertGreaterEqual(len(parser.scripts), 3)
        for attrs, body in parser.scripts:
            self.assertTrue(attrs.get("src", "").strip(), "inline <script> without src is forbidden")
            self.assertEqual(body.strip(), "", "script element with src must not contain inline code")

    @unittest.skipUnless(shutil.which("node"), "Node unavailable; bridge session regression check skipped")
    def test_bridge_correlates_by_request_id_across_varying_host_sessions(self):
        script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {randomUUID} = require('node:crypto');
const listeners = new Set();
const posted = [];
const parent = {postMessage: (value, origin) => posted.push({value, origin})};
const fake = {
  crypto: {randomUUID}, URL,
  location: {href: 'https://webui.example/api/plugin/view/assets/tok/p/dashboard/index.html'},
  parent, setTimeout, clearTimeout,
  addEventListener: (name, listener) => { if (name === 'message') listeners.add(listener); },
  removeEventListener: (name, listener) => { listeners.delete(listener); },
};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {window: fake});
const emit = (data, origin = 'https://webui.example', source = parent) => {
  for (const listener of [...listeners]) listener({data, origin, source});
};
(async () => {
  const client = fake.E2BPanelBridge.createClient(fake, 100);

  // 场景1：顺序请求，模拟宿主先后下发不同 session ('abc' -> 'def')，均应按 requestId 正常关联且不抛异常
  const firstReq = client.getSnapshot();
  assert.equal('session' in posted[0].value, false);
  emit({
    channel: 'astrbot-plugin-page', kind: 'response', requestId: posted[0].value.requestId,
    session: 'abc', ok: true, data: {status: 'ok', data: {tag: 'first'}},
  });
  assert.deepEqual(await firstReq, {tag: 'first'});
  assert.equal(client.pendingCount(), 0);

  const secondReq = client.act({action: 'pause', expected_version: 'v1'});
  assert.equal('session' in posted[1].value, false);
  emit({
    channel: 'astrbot-plugin-page', kind: 'response', requestId: posted[1].value.requestId,
    session: 'def', ok: true, data: {status: 'ok', data: {tag: 'second'}},
  });
  assert.deepEqual(await secondReq, {tag: 'second'});
  assert.equal(client.pendingCount(), 0);

  // 场景2：并发交错请求，先回 session:'def' 后回 session:'abc'，各自按 requestId 精确匹配对应 pending
  const reqA = client.getSnapshot();
  const reqB = client.act({action: 'kill', expected_version: 'v2'});
  assert.equal(client.pendingCount(), 2);
  emit({
    channel: 'astrbot-plugin-page', kind: 'response', requestId: posted[3].value.requestId,
    session: 'def', ok: true, data: {status: 'ok', data: {which: 'B'}},
  });
  emit({
    channel: 'astrbot-plugin-page', kind: 'response', requestId: posted[2].value.requestId,
    session: 'abc', ok: true, data: {status: 'ok', data: {which: 'A'}},
  });
  assert.deepEqual(await reqB, {which: 'B'});
  assert.deepEqual(await reqA, {which: 'A'});
  assert.equal(client.pendingCount(), 0);

  // 认证边界与白名单 endpoint 不降级
  await assert.rejects(client.act({action: 'pause'}), {code: 'timeout'});
  client.destroy();
  console.log('bridge varying-session correlation assertions passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run([node, "-e", script, str(DASHBOARD / "bridge-client.js")],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("bridge varying-session correlation assertions passed", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable; redesign regression check skipped")
    def test_panel_tri_state_theme_tabs_cooldown_peak_and_file_counts(self):
        script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

class FakeEl {
  constructor(tag, id = '') {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this.children = [];
    this.attributes = {};
    this.listeners = new Map();
    this.className = '';
    this.hidden = false;
    this.disabled = false;
    this.checked = true;
    this.value = '';
    this.open = false;
    this.isConnected = true;
    this.style = {};
    this._text = '';
  }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return this.attributes[k]; }
  appendChild(c) { if (c && c._frag) this.children.push(...c.children); else this.children.push(c); return c; }
  replaceChildren(...nodes) { this.children = []; nodes.forEach((n) => this.appendChild(n)); }
  addEventListener(n, fn) { if (!this.listeners.has(n)) this.listeners.set(n, []); this.listeners.get(n).push(fn); }
  dispatch(n, ev = {}) { const e = {preventDefault: () => {}, ...ev}; (this.listeners.get(n) || []).forEach((fn) => fn(e)); }
  focus() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
}

const ids = [
  'refresh-button', 'auto-refresh', 'poll-state', 'theme-toggle', 'theme-label',
  'notice-bar', 'notice', 'retry-button', 'tab-sessions', 'tab-keys', 'tab-keys-dot',
  'tabpanel-sessions', 'tabpanel-keys', 'view-switch-wrap', 'sessions-region',
  'session-list', 'session-search', 'status-filter', 'previous-page', 'next-page',
  'page-label', 'key-list', 'kpi-running-card', 'kpi-keys-card', 'confirm-dialog',
  'confirm-action', 'cancel-action', 'pause-ack', 'kill-id', 'action-feedback',
];
const els = new Map(ids.map((id) => [id, new FakeEl('div', id)]));
els.get('status-filter').value = 'all';
els.get('tabpanel-keys').hidden = true;
const docEl = new FakeEl('html');
const metaScheme = new FakeEl('meta');
let contextCb = null;
let snapCall = 0;

const fakeDoc = {
  hidden: false,
  documentElement: docEl,
  activeElement: null,
  getElementById: (id) => els.get(id) || null,
  querySelector: (sel) => (sel === 'meta[name="color-scheme"]' ? metaScheme : null),
  createElement: (t) => new FakeEl(t),
  createDocumentFragment: () => ({_frag: true, children: [], appendChild(c) { this.children.push(c); }}),
  addEventListener: () => {},
};
class PanelClientError extends Error {}
const snapshots = [
  {
    source: 'local_tracking_only',
    generated_at: 1700000000,
    counts: {total: 1, running: 1, paused: 0, unknown: 0, busy: 0},
    capacity: {limit: 3, occupied: 1, available: 2, waiting: 0, max_queue: 10},
    keys: [{label: 'Key #1', alias: '主力', bound_sessions: 1, cooldown_seconds: 240, available: false}],
    sessions: [{
      session_ref: 'a'.repeat(64), session_label: 'aiocqhttp:GroupMessage:123',
      sandbox_id: 'sbx-1', version: 'b'.repeat(64), status: 'running',
      key_label: 'Key #1', pending_files: null, uploaded_files: 2, generated_files: 1,
      owner_known: true, can_pause: true, can_kill: true,
    }],
  },
  {
    source: 'local_tracking_only',
    generated_at: 1700000060,
    counts: {total: 1, running: 1, paused: 0, unknown: 0, busy: 0},
    capacity: {limit: 3, occupied: 1, available: 2, waiting: 0, max_queue: 10},
    keys: [{label: 'Key #1', alias: '主力', bound_sessions: 1, cooldown_seconds: 180, available: false}],
    sessions: [],
  },
];
const fakeWin = {
  document: fakeDoc,
  HTMLElement: FakeEl,
  parent: {},
  AstrBotPluginView: {
    ready: async () => ({pluginName: 'astrbot_plugin_e2b_sandbox', isDark: true}),
    onContext: (cb) => { contextCb = cb; return () => {}; },
  },
  E2BPanelBridge: {
    PanelClientError,
    createClient: () => ({
      getSnapshot: async () => snapshots[Math.min(snapCall++, snapshots.length - 1)],
      act: async () => ({}),
      destroy: () => {},
    }),
  },
  setTimeout: () => 1, clearTimeout: () => {}, setInterval: () => 2, clearInterval: () => {},
  addEventListener: () => {},
};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
  window: fakeWin, document: fakeDoc, HTMLElement: FakeEl, AbortController,
  Date, Math, Number, String, Array, Object, Promise, Error, Set,
});
(async () => {
  await new Promise((r) => setTimeout(r, 20));
  // 1. 默认跟随宿主深色，点击三态切换：auto -> light -> dark -> auto
  assert.equal(docEl.getAttribute('data-theme-mode'), 'auto');
  assert.equal(docEl.getAttribute('data-theme'), 'dark');
  assert.equal(els.get('theme-label').textContent, '跟随宿主');

  els.get('theme-toggle').dispatch('click');
  assert.equal(docEl.getAttribute('data-theme-mode'), 'light');
  assert.equal(docEl.getAttribute('data-theme'), 'light');
  assert.equal(els.get('theme-label').textContent, '浅色');
  // 手动覆盖期间，宿主发来 isDark:true 不得冲掉手动浅色
  contextCb({isDark: true});
  assert.equal(docEl.getAttribute('data-theme'), 'light');

  els.get('theme-toggle').dispatch('click');
  assert.equal(docEl.getAttribute('data-theme-mode'), 'dark');
  assert.equal(els.get('theme-label').textContent, '深色');

  els.get('theme-toggle').dispatch('click');
  assert.equal(docEl.getAttribute('data-theme-mode'), 'auto');
  assert.equal(docEl.getAttribute('data-theme'), 'dark');
  contextCb({isDark: false});
  assert.equal(docEl.getAttribute('data-theme'), 'light');

  // 2. 附件缺失边界：pending_files 为 null 时 uploaded_files=2 仍正确展示
  const row = els.get('session-list').children[0];
  const inPill = row.children[4].children[0].children[0];
  assert.equal(inPill.textContent, '↑ 入箱 2');

  // 3. 冷却峰值记忆：首次 240s（峰值 300s），二次刷新 180s 应为 60% 而非跳回 100%
  assert.equal(els.get('tab-keys-dot').hidden, false);
  els.get('refresh-button').dispatch('click');
  await new Promise((r) => setTimeout(r, 20));
  const keyCard = els.get('key-list').children[0];
  const cooldownBar = keyCard.children[2].children[0];
  assert.equal(cooldownBar.style.width, '60%');

  // 4. Tab 切换与视图切换器显隐联动
  els.get('tab-keys').dispatch('click');
  assert.equal(els.get('tabpanel-sessions').hidden, true);
  assert.equal(els.get('tabpanel-keys').hidden, false);
  assert.equal(els.get('view-switch-wrap').hidden, true);
  els.get('kpi-running-card').dispatch('click');
  assert.equal(els.get('tabpanel-sessions').hidden, false);
  assert.equal(els.get('status-filter').value, 'running');
  console.log('redesign regression assertions passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run([node, "-e", script, str(DASHBOARD / "panel.js")],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("redesign regression assertions passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
