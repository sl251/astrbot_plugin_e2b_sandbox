"""Authenticated, existing-session-only AstrBot dashboard adapter.

Integration (performed by the caller, not by this module):
    from .web_panel import WebPanel
    self._web_panel = WebPanel(self)
    self._web_panel.register()
    # Before the existing shutdown routine:
    await self._web_panel.close()

The inspected host discovers ``views/<name>/index.html`` (``pages`` is an
alias); it has no register_web_page(). Point ``views`` at the shipped ``webui``
directory, or package a copy as views. This module never creates that link or
writes a local file. register_web_api() takes route/handler/methods/description,
not an auth scope. The host authenticates extension calls and binds
astrbot.api.web.request. We additionally require a signed, unexpired dashboard
JWT; the scoped asset token from a view URL is NOT a management credential.

Snapshots read local tracking state only: no CREATE, CONNECT, cloud discovery,
refresh/recovery, capacity admission, maintenance, or configuration mutation.
No functions in this module are registered as LLM tools or message commands.
"""

import asyncio
import hashlib
import hmac
import json
import math
import re
import secrets
import time
from importlib import import_module
from pathlib import Path

from .constants import (
    DEFAULT_IDLE_PAUSE_SECONDS,
    DEFAULT_MAX_QUEUE_SIZE,
    DEFAULT_MAX_RETURN_FILE_SIZE_MB,
    DEFAULT_MAX_RUNNING_SANDBOXES,
    DEFAULT_MAX_UPLOAD_FILE_SIZE_MB,
    DEFAULT_PAUSED_RETENTION_HOURS,
    DEFAULT_QUEUE_WAIT_TIMEOUT,
    PLUGIN_NAME,
)
from .runtime_capacity import SandboxBusyError

SNAPSHOT_ROUTE = f"/{PLUGIN_NAME}/dashboard/snapshot"
ACTION_ROUTE = f"/{PLUGIN_NAME}/dashboard/action"
DASHBOARD_COOKIE = "astrbot_dashboard_jwt"
MAX_ACTION_BODY_BYTES = 4096
PUBLIC_HEADERS = {
    "Cache-Control": "no-store, private",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
_HEX_TOKEN = re.compile(r"^[0-9a-f]{64}$")
_SAFE_STATES = frozenset(("running", "paused", "starting", "unknown"))


class PanelError(Exception):
    """Only constant, public error messages may be sent to the browser."""

    def __init__(self, code, message, status_code=409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _number(value, default=0.0):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return default
    return float(value) if math.isfinite(value) else default


def _bounded_int(value, default, minimum, maximum):
    if isinstance(value, bool):
        return default
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        value = default
    return min(maximum, max(minimum, value))


class WebPanel:
    """One adapter per plugin instance; requests do not outlive its close()."""

    def __init__(self, plugin):
        self.plugin = plugin
        self.available = False
        self.unavailable_reason = "当前宿主不支持安全的插件页面 Web API，面板未启用。"
        self._web = None
        self._jwt = None
        self._salt = secrets.token_bytes(32)
        self._started_at = time.time()
        self._closed = False
        self._plugin_version_cache = None
        self._tasks = {}
        # Retain the exact handler objects: unregister must not remove a newly
        # reloaded instance's routes or any other plugin's API.
        self._routes = (
            (SNAPSHOT_ROUTE, self._snapshot_view, ["GET"], "E2B 沙箱管理：脱敏本地快照"),
            (ACTION_ROUTE, self._action_view, ["POST"], "E2B 沙箱管理：暂停或销毁已有会话"),
        )

    def register(self):
        """Fail closed on old hosts; do not add a Quart/public fallback."""
        context = getattr(self.plugin, "context", None)
        register = getattr(context, "register_web_api", None)
        if self._closed or not callable(register):
            return False
        if not isinstance(getattr(context, "registered_web_apis", None), list):
            return False
        try:
            web = import_module("astrbot.api.web")
            jwt = import_module("jwt")
        except (ImportError, AttributeError):
            return False
        if not callable(getattr(web, "json_response", None)):
            return False
        if "request" not in vars(web) or not callable(getattr(jwt, "decode", None)):
            return False
        self._web, self._jwt = web, jwt
        try:
            for route, handler, methods, description in self._routes:
                register(route, handler, list(methods), description)
        except Exception:
            self.unregister()
            return False
        self.available = True
        self.unavailable_reason = ""
        return True

    def unregister(self):
        apis = getattr(getattr(self.plugin, "context", None), "registered_web_apis", None)
        if isinstance(apis, list):
            handlers = {id(entry[1]) for entry in self._routes}
            apis[:] = [
                entry for entry in apis
                if not (isinstance(entry, (list, tuple)) and len(entry) >= 2
                        and id(entry[1]) in handlers)
            ]
        self.available = False

    async def close(self):
        """Stop admitting requests, detach only our routes, finish locked work."""
        self._closed = True
        self.unregister()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks.values()), return_exceptions=True)

    def _authorize(self, request):
        # username and plugin_name are injected by the authenticated host route,
        # never taken from a request body, query parameter, or client header.
        denied = PanelError("unauthorized", "请先登录 AstrBot WebUI。", 401)
        try:
            username = request.username
            if not isinstance(username, str) or not username.strip():
                raise denied
            if request.plugin_name != PLUGIN_NAME:
                raise denied
            authorization = request.headers.get("Authorization", "")
            if authorization:
                if not authorization.startswith("Bearer "):
                    raise denied
                token = authorization[7:]
            else:
                token = request.cookies.get(DASHBOARD_COOKIE, "")
            if not isinstance(token, str) or not token or len(token) > 16384:
                raise denied
            # The installed host itself uses this secret and the same JWT
            # algorithm. Never read cmd_config.json or expose the secret.
            # The host passes astrbot.api.web.request (a proxy) into handlers;
            # resolve the bound PluginRequest before reading the raw app state.
            raw_request = request
            get_current = getattr(request, "_get_current", None)
            if callable(get_current):
                raw_request = get_current()
            secret = getattr(raw_request, "_request").app.state.jwt_secret
            if not isinstance(secret, str) or not secret:
                raise denied
            payload = self._jwt.decode(
                token, secret, algorithms=["HS256"],
                options={"require": ["exp", "username"]},
            )
            if not isinstance(payload, dict) or payload.get("username") != username:
                raise denied
            # require_dashboard_user() on the inspected host checks username
            # but not token_type. Prevent a signed view asset token being reused
            # as a dashboard Bearer/cookie credential (confused deputy).
            if any(name in payload for name in
                   ("token_type", "purpose", "plugin_name", "page_name")):
                raise denied
            if any(request.query.get(name) for name in ("token", "api_key", "key")):
                raise denied
        except PanelError:
            raise
        except Exception:
            raise denied from None
        self._ensure_running()

    def _ensure_running(self):
        if self._closed or getattr(self.plugin, "_stopping", False):
            raise PanelError("stopping", "插件正在停止，请稍后刷新。", 503)

    def _reply(self, data=None, error=None):
        if error is not None:
            payload = {
                "status": "error", "message": error.message,
                "data": {"code": error.code},
            }
            status = error.status_code
        else:
            payload = {"status": "ok", "data": data}
            status = 200
        return self._web.json_response(payload, status_code=status, headers=PUBLIC_HEADERS)

    async def _snapshot_view(self):
        return await self.handle_snapshot(self._web.request)

    async def _action_view(self):
        return await self.handle_action(self._web.request)

    async def handle_snapshot(self, request):
        try:
            self._authorize(request)
            if request.method != "GET":
                raise PanelError("method_not_allowed", "此接口仅支持 GET。", 405)
            return self._reply(self.snapshot())
        except PanelError as exc:
            return self._reply(error=exc)
        except Exception:
            return self._reply(error=PanelError("snapshot_failed", "暂时无法读取本地快照。", 503))

    def _public_text(self, value, limit=240):
        if not isinstance(value, str):
            return ""
        pool = getattr(self.plugin, "_key_pool", None)
        raw_keys = getattr(pool, "_keys", {})
        # Even an accidental key in a session name/sandbox ID must be masked.
        # Do replacement BEFORE truncation so a prefix is not leaked.
        for key in sorted((v for v in raw_keys.values() if isinstance(v, str) and v),
                          key=len, reverse=True):
            value = value.replace(key, "[已脱敏]")
        value = re.sub(r"[\x00-\x1f\x7f]", " ", value)
        return value if len(value) <= limit else value[:limit] + "…"

    def _reference(self, session_id):
        return hmac.new(self._salt, session_id.encode("utf-8"), hashlib.sha256).hexdigest()

    def _version(self, session_id, meta):
        # Include lifecycle identity AND status/times. Reusing the same sandbox
        # ID after resume, changing ownership or replacing its record is stale.
        fields = [session_id]
        for name in ("sandbox_id", "key_id", "request_id", "status", "last_active", "paused_at"):
            value = meta.get(name)
            fields.append(value if isinstance(value, (str, int, float, type(None)))
                          and not isinstance(value, bool) else None)
        encoded = json.dumps(fields, ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
        return hmac.new(self._salt, encoded, hashlib.sha256).hexdigest()

    def _owner_known(self, meta):
        owner = meta.get("key_id")
        pool = getattr(self.plugin, "_key_pool", None)
        # Deliberately do NOT use legacy_id, resolve(None), or pool.candidates().
        return isinstance(owner, str) and bool(owner) and owner in getattr(pool, "_keys", {})

    def _busy(self, session_id):
        lock = getattr(self.plugin, "session_locks", {}).get(session_id)
        pending = getattr(self.plugin, "_has_pending_cleanup", None)
        return bool((lock is not None and lock.locked())
                    or (callable(pending) and pending(session_id))
                    or session_id in self._tasks)

    def _plugin_version(self):
        if self._plugin_version_cache is not None:
            return self._plugin_version_cache
        try:
            text = Path(__file__).with_name("metadata.yaml").read_text(encoding="utf-8")
            match = re.search(r'^version:\s*["\']?([^"\'\n]+)', text, re.MULTILINE)
            self._plugin_version_cache = match.group(1).strip() if match else ""
        except Exception:
            self._plugin_version_cache = ""
        return self._plugin_version_cache

    def snapshot(self):
        """Whitelisted DTO, not vars(plugin), configuration, or SDK metadata."""
        plugin = self.plugin
        pool = getattr(plugin, "_key_pool", None)
        order = list(getattr(pool, "_order", ()))
        labels = {identifier: f"Key #{index + 1}" for index, identifier in enumerate(order)}
        counts = {"total": 0, "running": 0, "paused": 0, "unknown": 0, "busy": 0}
        bound = {identifier: 0 for identifier in order}
        sessions = []
        records = list(getattr(plugin, "sandbox_sessions", {}).items())
        for session_id, meta in records:
            if not isinstance(session_id, str) or not isinstance(meta, dict):
                continue
            state = meta.get("status")
            state = state if isinstance(state, str) and state in _SAFE_STATES else "unknown"
            busy = self._busy(session_id)
            sandbox_id = meta.get("sandbox_id")
            public_id = self._public_text(sandbox_id, 256)
            owner_known = self._owner_known(meta)
            identity_safe = (isinstance(sandbox_id, str) and bool(sandbox_id)
                             and len(sandbox_id) <= 256 and sandbox_id == public_id)
            try:
                version = self._version(session_id, meta)
            except (TypeError, ValueError):
                version = ""
            allowed = bool(identity_safe and owner_known and version and not busy
                           and not self._closed and not getattr(plugin, "_stopping", False))
            owner = meta.get("key_id")
            if isinstance(owner, str) and owner in bound:
                bound[owner] += 1
            counts["total"] += 1
            counts[state if state in ("running", "paused") else "unknown"] += 1
            counts["busy"] += int(busy)
            blocked = ("busy" if busy else "owner_missing" if not owner_known
                       else "identity_unconfirmed" if not identity_safe or not version
                       else "stopping" if not allowed else "")
            sessions.append({
                "session_ref": self._reference(session_id),
                "session_label": self._public_text(session_id),
                "sandbox_id": public_id,
                "version": version,
                "status": state,
                "busy": busy,
                "key_label": labels.get(owner, "原所属 Key 未记录或未配置")
                             if isinstance(owner, str) else "原所属 Key 未记录或未配置",
                "owner_known": owner_known,
                "last_active": max(0.0, _number(meta.get("last_active"))),
                "paused_at": max(0.0, _number(meta.get("paused_at"))),
                "created_at": max(0.0, _number(meta.get("created_at"))),
                "template": self._public_text(meta.get("template"), 120),
                "pending_files": _bounded_int(len(getattr(plugin, "session_files", {}).get(session_id) or ()), 0, 0, 1000),
                "generated_files": _bounded_int(len(getattr(plugin, "generated_files", {}).get(session_id) or ()), 0, 0, 1000),
                "can_pause": allowed and state != "paused",
                "can_kill": allowed,
                "blocked_reason": blocked,
            })
        sessions.sort(key=lambda entry: (-entry["last_active"], entry["session_ref"]))
        now = time.monotonic()
        cooldowns = getattr(pool, "_cooldown_until", {})
        keys = [{
            "label": labels[identifier],
            "cooldown_seconds": math.ceil(max(0.0, _number(cooldowns.get(identifier)) - now)),
            "bound_sessions": bound[identifier],
            "available": math.ceil(max(0.0, _number(cooldowns.get(identifier)) - now)) <= 0,
        } for identifier in order]
        capacity = getattr(plugin, "_capacity", None)
        config = getattr(plugin, "config", None) or {}
        limit = _bounded_int(config.get("max_running_sandboxes"),
                             DEFAULT_MAX_RUNNING_SANDBOXES, 1, 20)
        occupied = len(getattr(capacity, "occupied", ()))
        return {
            "generated_at": time.time(),
            "source": "local_tracking_only",
            "counts": counts,
            "capacity": {
                "limit": limit,
                "occupied": occupied,
                "available": max(0, limit - occupied),
                "waiting": _bounded_int(getattr(capacity, "waiting", 0), 0, 0, 100),
                "max_queue": _bounded_int(config.get("max_queue_size"),
                                          DEFAULT_MAX_QUEUE_SIZE, 0, 100),
                "wait_timeout_seconds": _bounded_int(config.get("queue_wait_timeout"),
                                                     DEFAULT_QUEUE_WAIT_TIMEOUT, 1, 120),
                "runtime_limit": _bounded_int(getattr(capacity, "limit", limit), limit, 1, 20),
                "idle_pause_seconds": _bounded_int(config.get("idle_pause_seconds"),
                                                   DEFAULT_IDLE_PAUSE_SECONDS, 5, 3600),
                "paused_retention_hours": _bounded_int(config.get("paused_sandbox_retention_hours"),
                                                       DEFAULT_PAUSED_RETENTION_HOURS, 1, 720),
                "upload_limit_mb": _bounded_int(config.get("max_upload_file_size_mb"),
                                                DEFAULT_MAX_UPLOAD_FILE_SIZE_MB, 1, 100),
                "return_file_limit_mb": _bounded_int(config.get("max_return_file_size_mb"),
                                                     DEFAULT_MAX_RETURN_FILE_SIZE_MB, 1, 100),
            },
            "plugin": {
                "version": self._plugin_version(),
                "uptime_seconds": max(0, int(time.time() - self._started_at)),
            },
            "keys": keys,
            "sessions": sessions,
        }

    async def _action_body(self, request):
        if request.content_type is None or request.content_type.split(";", 1)[0].strip() != "application/json":
            raise PanelError("invalid_body", "操作请求必须使用 JSON。", 415)
        length = request.headers.get("Content-Length")
        if length is not None:
            try:
                if int(length) < 0 or int(length) > MAX_ACTION_BODY_BYTES:
                    raise ValueError
            except (TypeError, ValueError):
                raise PanelError("invalid_body", "操作请求体过大或无效。", 413) from None
        raw = await request.body()
        if not isinstance(raw, bytes) or len(raw) > MAX_ACTION_BODY_BYTES:
            raise PanelError("invalid_body", "操作请求体过大或无效。", 413)
        try:
            def unique_pairs(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate key")
                    result[key] = value
                return result
            data = json.loads(raw, object_pairs_hook=unique_pairs)
        except (ValueError, UnicodeError):
            raise PanelError("invalid_body", "操作请求格式无效。", 422) from None
        fields = {"action", "session_ref", "expected_sandbox_id", "expected_version", "confirm"}
        if not isinstance(data, dict) or set(data) != fields or data.get("confirm") is not True:
            raise PanelError("confirmation_required", "请明确确认目标沙箱后再操作。", 422)
        if data["action"] not in ("pause", "kill"):
            raise PanelError("unsupported_action", "面板仅支持暂停或销毁已有沙箱。", 422)
        for field in ("session_ref", "expected_version"):
            if not isinstance(data[field], str) or not _HEX_TOKEN.fullmatch(data[field]):
                raise PanelError("invalid_target", "目标版本无效，请刷新面板。", 422)
        sandbox_id = data["expected_sandbox_id"]
        if not isinstance(sandbox_id, str) or not sandbox_id or len(sandbox_id) > 256:
            raise PanelError("invalid_target", "目标沙箱 ID 无效。", 422)
        return data

    def _target(self, data):
        for session_id, meta in list(self.plugin.sandbox_sessions.items()):
            if not isinstance(session_id, str) or not isinstance(meta, dict):
                continue
            if not hmac.compare_digest(self._reference(session_id), data["session_ref"]):
                continue
            try:
                valid_version = hmac.compare_digest(self._version(session_id, meta), data["expected_version"])
            except (TypeError, ValueError):
                valid_version = False
            if meta.get("sandbox_id") != data["expected_sandbox_id"] or not valid_version:
                raise PanelError("stale_target", "该会话的沙箱或版本已变化，请刷新后重新确认。")
            if self._public_text(meta["sandbox_id"], 256) != meta["sandbox_id"]:
                raise PanelError("invalid_target", "目标标识无法安全展示，已拒绝操作。")
            if not self._owner_known(meta):
                raise PanelError("owner_missing", "原所属 API Key 未记录或已移除，不能换 Key 操作。")
            if sum(isinstance(other, dict) and other.get("sandbox_id") == meta["sandbox_id"]
                   for other in self.plugin.sandbox_sessions.values()) != 1:
                raise PanelError("ambiguous_target", "目标沙箱存在重复会话记录，已拒绝操作。")
            return session_id
        raise PanelError("target_missing", "目标会话已不存在，请刷新面板。", 404)

    async def _perform(self, session_id, data):
        # The complete lock scope is in a shielded task. Disconnecting an HTTP
        # caller must not release the lock while an SDK pause/kill is in flight.
        async with self.plugin._session_operation(session_id):
            self._ensure_running()
            if self._target(data) != session_id:
                raise PanelError("stale_target", "目标会话已变化，请刷新面板。")
            if data["action"] == "pause":
                if not await self.plugin._finish_pause(session_id):
                    raise PanelError("pause_unconfirmed", "暂停尚未确认；会话记录与容量保留，请刷新后检查。", 502)
                meta = self.plugin.sandbox_sessions.get(session_id)
                if meta is not None and (meta.get("sandbox_id") != data["expected_sandbox_id"]
                                         or meta.get("status") != "paused"):
                    raise PanelError("pause_unconfirmed", "暂停状态尚未确认，请刷新后检查。", 502)
                outcome = "paused" if meta is not None else "already_missing"
            else:
                await self.plugin._kill_sandbox_by_id(session_id)
                if session_id in self.plugin.sandbox_sessions:
                    raise PanelError("kill_unconfirmed", "销毁状态尚未确认；记录保留，请刷新后检查。", 502)
                outcome = "killed"
        return {"action": data["action"], "sandbox_id": data["expected_sandbox_id"], "outcome": outcome}

    async def handle_action(self, request):
        try:
            self._authorize(request)
            if request.method != "POST":
                raise PanelError("method_not_allowed", "此接口仅支持 POST。", 405)
            data = await self._action_body(request)
            # body() yields: close() may have started since authentication.
            self._ensure_running()
            session_id = self._target(data)
            if self._busy(session_id):
                raise PanelError("session_busy", "该会话正在执行或清理，请稍后刷新；操作不会排队。")
            task = asyncio.create_task(self._perform(session_id, data))
            self._tasks[session_id] = task

            def finished(completed):
                if self._tasks.get(session_id) is completed:
                    self._tasks.pop(session_id, None)
                # Retrieve exceptions even if the HTTP caller has disappeared.
                if not completed.cancelled():
                    completed.exception()

            task.add_done_callback(finished)
            return self._reply(await asyncio.shield(task))
        except PanelError as exc:
            return self._reply(error=exc)
        except SandboxBusyError:
            return self._reply(error=PanelError("session_busy", "该会话正在执行或清理，操作未排队。"))
        except Exception:
            # Never forward SDK exceptions, config values, local paths or stacks.
            return self._reply(error=PanelError("operation_failed", "云端操作未确认；请刷新面板检查，勿重复提交。", 502))
