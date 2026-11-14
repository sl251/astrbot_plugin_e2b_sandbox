import ast
import asyncio
import base64
import hashlib
import inspect
import json
import os
import posixpath
import re
import shutil
import tempfile
import time
import traceback
import uuid
import zipfile
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import wraps
from io import BytesIO
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import FunctionTool, logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.provider import ProviderRequest

from .runtime_capacity import RuntimeCapacity, SandboxBusyError
from .file_transfer import download_http, read_limited
from .sandbox_bootstrap import FONT_SETUP_CODE
from .execution_policy import apply_execution_policy
from .constants import *
from .tools import sandbox_action
from .utils import shlex_quote

class SandboxManagerMixin:
    async def initialize(self):
        self._start_maintenance()

    async def terminate(self):
        self._stopping = True
        if self._maintenance_task:
            self._maintenance_task.cancel()
            await asyncio.gather(self._maintenance_task, return_exceptions=True)
        if self._cleanup_tasks:
            await asyncio.gather(*self._cleanup_tasks, return_exceptions=True)
        # Keep durable records when cleanup fails, so reloads do not forget a running VM.
        for session_id in list(self.sandbox_sessions):
            lock = self._get_session_lock(session_id)
            if lock.locked():
                continue
            async with lock:
                await self._pause_session_safely(session_id)

    def _start_maintenance(self):
        if not self._stopping and (
            self._maintenance_task is None or self._maintenance_task.done()
        ):
            self._maintenance_task = asyncio.create_task(self._maintenance_loop())

    @asynccontextmanager
    async def _session_operation(self, session_id):
        if self._stopping:
            raise SandboxBusyError("Plugin is shutting down.")
        self._start_maintenance()
        lock = self._get_session_lock(session_id)
        if lock.locked() or self._has_pending_cleanup(session_id):
            raise SandboxBusyError("Another sandbox operation is active in this session.")
        async with lock:
            yield

    def _has_pending_cleanup(self, session_id):
        task = self._session_cleanup_tasks.get(session_id)
        return task is not None and not task.done()

    async def _configure_capacity(self):
        await self._capacity.configure(
            self._safe_int(self.config.get("max_running_sandboxes"), DEFAULT_MAX_RUNNING_SANDBOXES, minimum=1, maximum=20),
            self._safe_int(self.config.get("max_queue_size"), DEFAULT_MAX_QUEUE_SIZE, minimum=0, maximum=100),
            self._safe_int(self.config.get("queue_wait_timeout"), DEFAULT_QUEUE_WAIT_TIMEOUT, minimum=1, maximum=120),
        )

    async def _maintenance_loop(self):
        while True:
            try:
                await self._maintain_sessions()
            except Exception:
                logger.warning(f"[E2B] Maintenance failed: {traceback.format_exc()}")
            await asyncio.sleep(5)

    async def _maintain_sessions(self):
        idle_seconds = self._safe_int(
            self.config.get("idle_pause_seconds"), DEFAULT_IDLE_PAUSE_SECONDS,
            minimum=5, maximum=3600,
        )
        now = time.time()
        for session_id, meta in list(self.sandbox_sessions.items()):
            if meta.get("status") == "paused":
                continue
            lock = self._get_session_lock(session_id)
            if lock.locked() or self._has_pending_cleanup(session_id):
                continue
            if meta.get("status") == "running" and now - meta.get("last_active", now) < idle_seconds:
                continue
            async with lock:
                await self._pause_session_safely(session_id)
        await self._cleanup_expired_sessions()

    def _sdk_options(self, session_id=None, key_id=None):
        meta = self.sandbox_sessions.get(session_id, {}) if session_id else {}
        # Existing VM operations must use the original account, never
        # the round-robin key selected for the next new VM.
        owner = key_id or meta.get("key_id")
        return {
            "api_key": self._key_pool.resolve(owner),
            "proxy": str(self.config.get("proxy", DEFAULT_PROXY) or "").strip() or None,
        }

    def _is_sandbox_not_found(self, exc):
        return (
            self._api_error_status(exc) == 404
            or type(exc).__name__ in ("NotFoundException", "SandboxNotFoundException", "SandboxNotFoundError")
        )

    def _is_activation_rejected(self, exc):
        return (
            getattr(exc, "activation_rejected", False)
            or self._api_error_status(exc) in (400, 401, 402, 403, 404, 409, 422, 429)
            or type(exc).__name__ in (
                "AuthenticationException", "RateLimitException", "InvalidArgumentException",
            )
        )

    def _api_error_status(self, exc):
        status = getattr(exc, "status_code", None)
        if status is not None:
            return status
        match = re.match(r"^(\d{3}):", str(exc))
        return int(match.group(1)) if match else None

    async def _pause_session_safely(self, session_id, sandbox=None):
        meta = self.sandbox_sessions.get(session_id)
        if not meta:
            return True
        if meta.get("status") == "paused":
            await self._capacity.release(session_id)
            return True
        try:
            if not meta.get("sandbox_id"):
                await self._recover_uncertain_sandbox(session_id)
                meta = self.sandbox_sessions[session_id]
            if sandbox is None:
                pause_method = getattr(self._sandbox_sdk, "pause", None) or getattr(self._sandbox_sdk, "beta_pause", None)
                if pause_method is None:
                    raise RuntimeError("The E2B SDK does not support pausing a sandbox by ID.")
                await self._call_sandbox_entrypoint(
                    pause_method, {"sandbox_id": meta["sandbox_id"], **self._sdk_options(session_id=session_id)},
                    SANDBOX_API_TIMEOUT, "pause",
                )
            else:
                await asyncio.wait_for(self._pause_sandbox(sandbox), SANDBOX_API_TIMEOUT)
            meta["status"] = "paused"
            meta["paused_at"] = time.time()
            self._save_sandbox_sessions()
            await self._capacity.release(session_id)
            return True
        except Exception as exc:
            if self._is_sandbox_not_found(exc):
                self._delete_sandbox_session(session_id)
                await self._capacity.release(session_id)
                return True
            meta["status"] = "unknown"
            self._save_sandbox_sessions()
            logger.warning(f"[E2B] Pause not confirmed for {session_id}; slot retained: {exc}")
            return False

    async def _finish_pause(self, session_id, sandbox=None):
        if self._has_pending_cleanup(session_id):
            return await asyncio.shield(self._session_cleanup_tasks[session_id])
        task = asyncio.create_task(self._pause_session_safely(session_id, sandbox))
        self._session_cleanup_tasks[session_id] = task
        self._cleanup_tasks.add(task)
        def finished(completed):
            self._cleanup_tasks.discard(completed)
            if self._session_cleanup_tasks.get(session_id) is completed:
                self._session_cleanup_tasks.pop(session_id, None)
        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def _recover_uncertain_sandbox(self, session_id):
        """Find a create whose response was lost, without issuing another create."""
        meta = self.sandbox_sessions[session_id]
        request_id = meta.get("request_id")
        if not request_id or self._sandbox_sdk is None or self._sandbox_query is None or not hasattr(self._sandbox_sdk, "list"):
            raise RuntimeError("Sandbox activation is unconfirmed; its slot is retained for cleanup.")
        paginator = self._sandbox_sdk.list(
            query=self._sandbox_query(
                metadata={"plugin": PLUGIN_NAME, "request_id": request_id},
                state=[self._sandbox_state.RUNNING, self._sandbox_state.PAUSED],
            ),
            **{k: v for k, v in self._sdk_options(session_id=session_id).items() if v is not None},
        )
        if inspect.isawaitable(paginator):
            paginator = await asyncio.wait_for(paginator, SANDBOX_API_TIMEOUT)
        matches = []
        for _ in range(10):
            if not getattr(paginator, "has_next", False):
                break
            matches.extend(await asyncio.wait_for(paginator.next_items(), SANDBOX_API_TIMEOUT))
            if matches:
                break
        if not matches:
            raise RuntimeError("Sandbox activation is unconfirmed; retry cleanup later.")
        meta["sandbox_id"] = str(matches[0].sandbox_id)
        self._save_sandbox_sessions()

    async def _kill_sandbox_by_id(self, session_id):
        meta = self.sandbox_sessions.get(session_id)
        if not meta:
            await self._capacity.release(session_id)
            return
        if not meta.get("sandbox_id"):
            await self._recover_uncertain_sandbox(session_id)
        try:
            await self._call_sandbox_entrypoint(
                self._sandbox_sdk.kill, {"sandbox_id": meta["sandbox_id"], **self._sdk_options(session_id=session_id)},
                SANDBOX_API_TIMEOUT, "kill",
            )
        except Exception as exc:
            if not self._is_sandbox_not_found(exc):
                # A failed DELETE must not remove the only durable handle to this VM.
                raise
        self._delete_sandbox_session(session_id)
        await self._capacity.release(session_id)

    async def _refresh_session_state(self, session_id):
        meta = self.sandbox_sessions.get(session_id)
        if not meta:
            return
        if not meta.get("sandbox_id"):
            await self._recover_uncertain_sandbox(session_id)
        try:
            info = await self._call_sandbox_entrypoint(
                self._sandbox_sdk.get_info, {"sandbox_id": meta["sandbox_id"], **self._sdk_options(session_id=session_id)},
                SANDBOX_API_TIMEOUT, "get_info",
            )
        except Exception as exc:
            if self._is_sandbox_not_found(exc):
                self._delete_sandbox_session(session_id)
                await self._capacity.release(session_id)
                return
            raise
        state = getattr(info.state, "value", info.state)
        if state == "paused":
            meta["status"] = "paused"
            meta.setdefault("paused_at", time.time())
            await self._capacity.release(session_id)
        elif state == "running":
            meta["status"] = "running"
            self._capacity.occupied.add(session_id)
        self._save_sandbox_sessions()

    async def _cleanup_expired_sessions(self):
        if self._last_expired_cleanup is not None and time.monotonic() - self._last_expired_cleanup < 60:
            return
        self._last_expired_cleanup = time.monotonic()
        now = time.time()
        cutoff = now - DEFAULT_SESSION_RETENTION_HOURS * 3600
        paused_cutoff = now - self._safe_int(
            self.config.get("paused_sandbox_retention_hours"), DEFAULT_PAUSED_RETENTION_HOURS,
            minimum=1, maximum=720,
        ) * 3600
        expired_session_ids = {
            session_id for session_id in self.session_last_access.keys() | self.sandbox_sessions.keys()
            if (
                session_id not in self.sandbox_sessions
                and self.session_last_access.get(session_id, 0) < cutoff
            ) or (
                self.sandbox_sessions.get(session_id, {}).get("status") == "paused"
                and self.sandbox_sessions[session_id].get(
                    "paused_at", self.sandbox_sessions[session_id].get("last_active", 0)
                ) < paused_cutoff
            )
        }

        if not expired_session_ids:
            return

        for session_id in expired_session_ids:
            lock = self._get_session_lock(session_id)
            if lock.locked() or self._has_pending_cleanup(session_id):
                continue
            async with lock:
                try:
                    await self._kill_sandbox_by_id(session_id)
                except Exception as exc:
                    logger.warning(f"[E2B] Expired sandbox cleanup unconfirmed for {session_id}: {exc}")
                    continue
                self.session_last_access.pop(session_id, None)
                self.code_hashes.pop(session_id, None)
                self.code_hash_timestamps.pop(session_id, None)
                self.session_files.pop(session_id, None)
                self.generated_files.pop(session_id, None)
                self.sent_file_signatures.pop(session_id, None)
                logger.info(f"[E2B] Cleaned expired session cache: {session_id}")
            self.session_locks.pop(session_id, None)

        self._save_sandbox_sessions()

    async def _get_or_create_session_sandbox(
        self,
        event: AstrMessageEvent,
        template: str = "",
        timeout: int = DEFAULT_SANDBOX_TIMEOUT,
        create_if_missing: bool = True,
    ):
        session_id = self._get_session_id(event)
        sandbox_meta = self.sandbox_sessions.get(session_id, {})
        requested_template = self._effective_template(template)
        existing_template = str(sandbox_meta.get("template") or "")

        if not self._key_pool.has_keys or self._sandbox_sdk is None:
            raise RuntimeError("E2B API Key or AsyncSandbox SDK is missing.")
        await self._configure_capacity()
        if sandbox_meta and not sandbox_meta.get("sandbox_id"):
            await self._recover_uncertain_sandbox(session_id)

        if sandbox_meta.get("sandbox_id"):
            if requested_template and existing_template and requested_template != existing_template:
                raise RuntimeError(
                    "This session already has a sandbox with a different template. "
                    "Kill the current sandbox before switching templates."
                )

            await self._capacity.acquire(session_id)
            try:
                self._update_sandbox_session(
                    session_id, sandbox_meta["sandbox_id"], template=existing_template,
                    status="starting",
                )
                sandbox = await self._connect_to_existing_sandbox(
                    sandbox_meta["sandbox_id"], timeout=timeout,
                    key_id=sandbox_meta.get("key_id"),
                )
            except BaseException as exc:
                if self._is_sandbox_not_found(exc):
                    self._delete_sandbox_session(session_id)
                    await self._capacity.release(session_id)
                else:
                    self.sandbox_sessions[session_id]["status"] = "unknown"
                    self._save_sandbox_sessions()
                    await self._finish_pause(session_id)
                    raise
            else:
                self._update_sandbox_session(
                    session_id, sandbox_meta["sandbox_id"],
                    template=existing_template, status="running",
                )
                return (
                    sandbox, self.sandbox_sessions[session_id],
                    f"Connected to existing sandbox {sandbox_meta['sandbox_id']}.",
                )

        if not create_if_missing:
            return None, {}, ""

        candidates = await self._key_pool.candidates()
        proxy = str(self.config.get("proxy", DEFAULT_PROXY) or "").strip()
        await self._capacity.acquire(session_id)
        last_rejection = None
        for key_id, api_key in candidates:
            request_id = uuid.uuid4().hex

            def mark_starting():
                self._update_sandbox_session(
                    session_id, "", template=requested_template, status="starting",
                    request_id=request_id, key_id=key_id,
                )

            try:
                sandbox = await self._create_sandbox(
                    api_key=api_key, timeout=timeout, proxy=proxy, template=requested_template,
                    metadata={"plugin": PLUGIN_NAME, "request_id": request_id},
                    on_start=mark_starting,
                )
                sandbox_id = self._extract_sandbox_id(sandbox)
                if not sandbox_id:
                    raise RuntimeError("Sandbox created, but the SDK did not expose a sandbox ID.")
            except BaseException as exc:
                # Only a definitive account rejection can authorize another
                # CREATE. Timeouts, cancellations and 5xx may have created a VM:
                # keep the request_id + owner binding for later reconciliation.
                status = self._api_error_status(exc)
                if isinstance(exc, Exception) and self._key_pool.is_definitive_account_rejection(exc, status):
                    self._key_pool.reject(key_id, status)
                    last_rejection = exc
                    continue
                if session_id not in self.sandbox_sessions or self._is_activation_rejected(exc):
                    self._delete_sandbox_session(session_id)
                    await self._capacity.release(session_id)
                else:
                    self.sandbox_sessions[session_id]["status"] = "unknown"
                    self._save_sandbox_sessions()
                    await self._finish_pause(session_id)
                raise

            self._update_sandbox_session(
                session_id, sandbox_id, template=requested_template,
                status="running", key_id=key_id,
            )
            return (
                sandbox, self.sandbox_sessions[session_id],
                f"Created sandbox {sandbox_id} for this session.",
            )

        # Every attempted account returned a definitive rejection. No VM
        # was created, so releasing the reservation is safe.
        self._delete_sandbox_session(session_id)
        await self._capacity.release(session_id)
        raise RuntimeError(f"All configured E2B API Keys rejected sandbox creation. Last error: {last_rejection}")

    async def _connect_to_existing_sandbox(self, sandbox_id: str, timeout: int = DEFAULT_SANDBOX_TIMEOUT, key_id: str = None):
        if self._sandbox_sdk is None:
            raise RuntimeError("self._sandbox_sdk class not found.")

        connect_method = getattr(self._sandbox_sdk, "connect", None)
        if connect_method is None:
            raise RuntimeError(
                "Current E2B SDK does not support reconnecting sandboxes. Upgrade the E2B SDK first."
            )

        api_key = self._key_pool.resolve(key_id)
        proxy = str(self.config.get("proxy", DEFAULT_PROXY) or "").strip()
        connect_kwargs = {
            "sandbox_id": sandbox_id,
            "api_key": api_key,
            "proxy": proxy or None,
            "timeout": timeout,
        }
        return await self._call_sandbox_entrypoint(
            connect_method,
            connect_kwargs,
            call_timeout=20,
            action_name="connect",
        )

    async def _call_sandbox_entrypoint(self, method, kwargs, call_timeout: int, action_name: str):
        filtered_kwargs = {k: v for k, v in kwargs.items() if v is not None}
        try:
            signature = inspect.signature(method)
            if not any(
                param.kind == inspect.Parameter.VAR_KEYWORD
                for param in signature.parameters.values()
            ):
                filtered_kwargs = {
                    key: value
                    for key, value in filtered_kwargs.items()
                    if key in signature.parameters
                }
        except (TypeError, ValueError):
            pass

        async def invoke():
            deadline = time.monotonic() + call_timeout
            for attempt in range(3):
                try:
                    result = method(**filtered_kwargs)
                    return await result if inspect.isawaitable(result) else result
                except Exception as exc:
                    rate_limited = (
                        self._api_error_status(exc) == 429
                        or type(exc).__name__ == "RateLimitException"
                    )
                    if not rate_limited or attempt == 2:
                        raise
                    response = getattr(exc, "response", None)
                    headers = getattr(exc, "headers", None) or getattr(response, "headers", {})
                    try:
                        delay = max(float(headers.get("Retry-After", 1.1 * (attempt + 1))), 1.05)
                    except (TypeError, ValueError):
                        delay = 1.1 * (attempt + 1)
                    if delay >= deadline - time.monotonic():
                        raise
                    try:
                        await asyncio.sleep(delay)
                    except asyncio.CancelledError as cancelled:
                        # No RPC is in flight while waiting after an explicit rejection.
                        cancelled.activation_rejected = True
                        raise
            raise RuntimeError(f"E2B SDK {action_name} retry limit reached.")

        return await asyncio.wait_for(invoke(), timeout=call_timeout)

    async def _pause_sandbox(self, sandbox):
        for method_name in ("pause", "beta_pause"):
            pause_method = getattr(sandbox, method_name, None)
            if pause_method is None:
                continue
            result = pause_method()
            if inspect.isawaitable(result):
                await result
            return method_name

        raise RuntimeError(
            "Current E2B SDK does not support pause(). Upgrade to a newer E2B SDK with sandbox persistence support."
        )

    async def _create_sandbox(
        self, api_key: str, timeout: int, proxy: str, template: str = "",
        metadata=None, on_start=None,
    ):
        if self._sandbox_sdk is None:
            raise RuntimeError("self._sandbox_sdk class not found.")

        template = str(template or self.config.get("default_template", DEFAULT_TEMPLATE) or "").strip()
        create_kwargs = {
            "api_key": api_key,
            "timeout": timeout,
            "proxy": proxy or None,
            "template": template or None,
            "metadata": metadata,
        }
        create_method = getattr(self._sandbox_sdk, "create", None)
        beta_create = getattr(self._sandbox_sdk, "beta_create", None)
        supports_lifecycle = False
        if create_method:
            try:
                supports_lifecycle = "lifecycle" in inspect.signature(create_method).parameters
            except (TypeError, ValueError):
                pass
        if supports_lifecycle:
            create_kwargs["lifecycle"] = {"on_timeout": "pause", "auto_resume": False}
            method, action_name = create_method, "create"
        elif beta_create is not None:
            create_kwargs["auto_pause"] = True
            method, action_name = beta_create, "beta_create"
        elif create_method is not None:
            method, action_name = create_method, "create"
        else:
            raise RuntimeError("Current E2B SDK does not support sandbox creation.")

        # Limit initial creates to Hobby's 1/sec, including failed attempts.
        async with self._creation_lock:
            delay = self._next_creation_time - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_creation_time = time.monotonic() + 1.05
            if on_start:
                on_start()
            # Do not fallback after a timeout/5xx: the first RPC may have created a VM.
            return await self._call_sandbox_entrypoint(
                method, create_kwargs, call_timeout=SANDBOX_API_TIMEOUT, action_name=action_name,
            )
