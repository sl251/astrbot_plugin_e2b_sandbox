import asyncio
import json
import re
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .constants import (
    DEFAULT_DUPLICATE_EXEC_WINDOW_SECONDS,
    DEFAULT_SESSION_RETENTION_HOURS,
    DEFAULT_TEMPLATE,
    PLUGIN_NAME,
)

class SessionStoreMixin:
    def _get_plugin_data_dir(self):
        if self._host_data_path is not None:
            return Path(self._host_data_path()) / "plugin_data" / getattr(self, "name", PLUGIN_NAME)
        return Path(__file__).resolve().parent / "data"

    def _load_sandbox_sessions(self):
        self._plugin_data_dir.mkdir(parents=True, exist_ok=True)
        if not self._sandbox_state_path.exists():
            self.sandbox_sessions = {}
            return

        try:
            with open(self._sandbox_state_path, "r", encoding="utf-8") as file_obj:
                loaded = json.load(file_obj)
            self.sandbox_sessions = loaded if isinstance(loaded, dict) else {}
            # Pre-1.4 session files have no owner identifier. The old single
            # e2b_api_key is the only known owner; preserve that binding.
            if self._key_pool.legacy_id:
                changed = False
                for meta in self.sandbox_sessions.values():
                    if isinstance(meta, dict) and not meta.get("key_id"):
                        meta["key_id"] = self._key_pool.legacy_id
                        changed = True
                if changed:
                    self._save_sandbox_sessions()
        except Exception as exc:
            logger.warning(f"[E2B] Failed to load sandbox session state: {exc}")
            self.sandbox_sessions = {}

    def _save_sandbox_sessions(self):
        self._plugin_data_dir.mkdir(parents=True, exist_ok=True)
        temp_path = self._sandbox_state_path.with_suffix(".tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as file_obj:
                json.dump(self.sandbox_sessions, file_obj, ensure_ascii=False, indent=2)
            temp_path.replace(self._sandbox_state_path)
        except Exception as exc:
            logger.warning(f"[E2B] Failed to save sandbox session state: {exc}")

    def _get_session_lock(self, session_id: str):
        lock = self.session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self.session_locks[session_id] = lock
        return lock

    def _normalize_user_whitelist(self):
        raw_value = self.config.get("user_whitelist", [])
        if isinstance(raw_value, str):
            entries = re.split(r"[\r\n,]+", raw_value)
        elif isinstance(raw_value, list):
            entries = raw_value
        else:
            entries = []
        return {str(item).strip() for item in entries if str(item).strip()}

    def _get_user_id(self, event: AstrMessageEvent):
        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None) if message_obj is not None else None
        return (
            getattr(sender, "user_id", None)
            or getattr(sender, "qq", None)
            or getattr(event, "sender_id", None)
            or event.get_sender_id()
        )

    def _is_user_allowed(self, event: AstrMessageEvent):
        if self.config.get("enable_user_whitelist", True) is False:
            return True
        whitelist = self._normalize_user_whitelist()
        if not whitelist:
            return False
        return str(self._get_user_id(event)).strip() in whitelist

    def _get_user_access_denied_message(self, event: AstrMessageEvent):
        if self._is_user_allowed(event):
            return ""
        return (
            "Access denied: the current user is not in user_whitelist. "
            f"User ID: {self._get_user_id(event)}"
        )

    def _is_duplicate_execution(self, session_id: str, current_hash: str):
        if self.code_hashes.get(session_id) != current_hash:
            return False
        last_time = self.code_hash_timestamps.get(session_id, 0)
        return (time.time() - last_time) < DEFAULT_DUPLICATE_EXEC_WINDOW_SECONDS

    def _effective_template(self, template: str = ""):
        return str(template or self.config.get("default_template", DEFAULT_TEMPLATE) or "").strip()

    def _extract_sandbox_id(self, sandbox):
        for attr in ("sandbox_id", "id"):
            value = getattr(sandbox, attr, None)
            if value:
                return str(value)
        metadata = getattr(sandbox, "metadata", None)
        for attr in ("sandbox_id", "id"):
            value = getattr(metadata, attr, None)
            if value:
                return str(value)
        return ""

    def _update_sandbox_session(
        self,
        session_id: str,
        sandbox_id: str,
        template: str = "",
        status: str = "running",
        request_id: str = "",
        key_id: str = "",
    ):
        previous = self.sandbox_sessions.get(session_id, {})
        self.sandbox_sessions[session_id] = {
            "sandbox_id": str(sandbox_id),
            "template": str(template or ""),
            "status": status,
            "last_active": time.time(),
            "request_id": request_id or previous.get("request_id", ""),
            "key_id": key_id or previous.get("key_id", "") or self._key_pool.legacy_id,
        }
        self._save_sandbox_sessions()

    def _delete_sandbox_session(self, session_id: str):
        self.sandbox_sessions.pop(session_id, None)
        self._save_sandbox_sessions()

    def _cleanup_session_cache(self):
        now = time.time()
        cutoff = now - DEFAULT_SESSION_RETENTION_HOURS * 3600
        expired_session_ids = [
            session_id
            for session_id, last_access in self.session_last_access.items()
            if last_access < cutoff and session_id not in self.sandbox_sessions
            and not (self.session_locks.get(session_id) and self.session_locks[session_id].locked())
            and not self._has_pending_cleanup(session_id)
        ]

        for session_id in expired_session_ids:
            self.session_last_access.pop(session_id, None)
            self.code_hashes.pop(session_id, None)
            self.code_hash_timestamps.pop(session_id, None)
            self.session_files.pop(session_id, None)
            self._file_event_ids.pop(session_id, None)
            self.generated_files.pop(session_id, None)
            self.sent_file_signatures.pop(session_id, None)
            self.session_locks.pop(session_id, None)
            logger.info(f"[E2B] Cleaned expired session cache: {session_id}")

    def _mark_session_active(self, event: AstrMessageEvent):
        session_id = self._get_session_id(event)
        self.session_last_access[session_id] = time.time()
        if len(self.session_last_access) % 20 == 0:
            self._cleanup_session_cache()

    def _get_session_id(self, event: AstrMessageEvent):
        return (
            getattr(event, "unified_msg_origin", None)
            or getattr(event, "session_id", None)
            or event.get_sender_id()
        )
