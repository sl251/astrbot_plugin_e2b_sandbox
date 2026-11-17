"""沙箱会话持久化存储、用户准入白名单、群聊隔离作用域与内存缓存清理。"""

import asyncio
import json
import math
import os
import re
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .constants import (
    DEFAULT_DUPLICATE_EXEC_WINDOW_SECONDS,
    DEFAULT_SANDBOX_SCOPE,
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
            # 丢失状态记录等同于丢失账户归属与运行槽位占用；损坏的状态文件绝不能静默当作空安装覆盖。
            if not isinstance(loaded, dict):
                raise ValueError("Session state must be an object.")
            for session_id, meta in loaded.items():
                if not isinstance(session_id, str) or not isinstance(meta, dict):
                    raise ValueError("Invalid session record.")
                for name in ("sandbox_id", "key_id", "request_id", "template", "status"):
                    if name in meta and not isinstance(meta[name], str):
                        raise ValueError("Invalid session identity or status.")
                if not (meta.get("sandbox_id") or meta.get("request_id")):
                    raise ValueError("Session has no recoverable identity.")
                for name in ("last_active", "paused_at", "created_at"):
                    value = meta.get(name)
                    if name in meta and (isinstance(value, bool)
                            or not isinstance(value, (int, float))
                            or not math.isfinite(value) or value < 0):
                        raise ValueError("Invalid session timestamp.")
            self.sandbox_sessions = loaded
            # 1.4 之前的旧状态文件没有记录账户指纹，使用旧版单 e2b_api_key 自动补写归属并持久化
            if self._key_pool.legacy_id:
                changed = False
                for meta in self.sandbox_sessions.values():
                    if isinstance(meta, dict) and not meta.get("key_id"):
                        meta["key_id"] = self._key_pool.legacy_id
                        changed = True
                if changed:
                    if self._save_sandbox_sessions() is False:
                        raise OSError("Session owner migration could not be saved.")
        except Exception as exc:
            logger.error(f"[E2B] Session state could not be loaded: {type(exc).__name__}")
            raise RuntimeError(
                "E2B session state could not be loaded safely. Preserve and repair "
                "sandbox_sessions.json (or restore a backup), then reload the plugin."
            ) from None

    def _save_sandbox_sessions(self):
        temp_path = self._sandbox_state_path.with_suffix(".tmp")
        try:
            self._plugin_data_dir.mkdir(parents=True, exist_ok=True)
            with open(temp_path, "w", encoding="utf-8") as file_obj:
                json.dump(self.sandbox_sessions, file_obj, ensure_ascii=False, indent=2, allow_nan=False)
                file_obj.flush()
                os.fsync(file_obj.fileno())
            temp_path.replace(self._sandbox_state_path)
            return True
        except Exception as exc:
            logger.warning(f"[E2B] Failed to save sandbox session state: {self._key_pool.redact(exc)}")
            return False

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
        require_persist: bool = False,
    ):
        previous = self.sandbox_sessions.get(session_id, {})
        self.sandbox_sessions[session_id] = {
            "sandbox_id": str(sandbox_id),
            "template": str(template or ""),
            "status": status,
            "last_active": time.time(),
            "request_id": request_id or previous.get("request_id", ""),
            "key_id": key_id or previous.get("key_id", "") or self._key_pool.legacy_id,
            # 旧版记录未保存创建时间；恢复重连不是新建，保持为 0（未知）而不伪造最近时间
            "created_at": previous.get("created_at", 0) if previous else time.time(),
        }
        if self._save_sandbox_sessions() is False and require_persist:
            # 此时尚未向云端发出激活请求，回滚内存记录并保持磁盘原文件不变
            if previous:
                self.sandbox_sessions[session_id] = previous
            else:
                self.sandbox_sessions.pop(session_id, None)
            raise RuntimeError("Cannot persist E2B activation intent; activation was not sent.")

    def _delete_sandbox_session(self, session_id: str):
        removed = self.sandbox_sessions.pop(session_id, None)
        if isinstance(removed, dict) and removed.get("sandbox_id"):
            verified = getattr(self, "_verified_sandbox_packages", None)
            if isinstance(verified, dict):
                verified.pop(removed["sandbox_id"], None)
        # 销毁后的新虚拟机不再包含旧虚拟机的变量、已上传文件或执行副作用
        self.code_hashes.pop(session_id, None)
        self.code_hash_timestamps.pop(session_id, None)
        uploaded = getattr(self, "uploaded_session_files", None)
        if isinstance(uploaded, dict):
            uploaded.pop(session_id, None)
        self._save_sandbox_sessions()

    def _evict_session_memory_cache(self, session_id: str):
        """统一释放单个已无活跃沙箱会话的内存缓存。"""
        self.session_last_access.pop(session_id, None)
        self.code_hashes.pop(session_id, None)
        self.code_hash_timestamps.pop(session_id, None)
        self.session_files.pop(session_id, None)
        self._file_event_ids.pop(session_id, None)
        self.generated_files.pop(session_id, None)
        self.sent_file_signatures.pop(session_id, None)
        uploaded = getattr(self, "uploaded_session_files", None)
        if isinstance(uploaded, dict):
            uploaded.pop(session_id, None)

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
            self._evict_session_memory_cache(session_id)
            self.session_locks.pop(session_id, None)
            logger.info(f"[E2B] Cleaned expired session cache: {session_id}")

    def _mark_session_active(self, event: AstrMessageEvent):
        session_id = self._get_session_id(event)
        self.session_last_access[session_id] = time.time()
        if len(self.session_last_access) % 20 == 0:
            self._cleanup_session_cache()

    def _is_group_event(self, event: AstrMessageEvent, base_origin: str) -> bool:
        message_obj = getattr(event, "message_obj", None)
        if message_obj is not None and getattr(message_obj, "group_id", None):
            return True
        raw_message = getattr(message_obj, "raw_message", None) if message_obj is not None else None
        if isinstance(raw_message, dict) and raw_message.get("group_id"):
            return True
        return ":GroupMessage:" in str(base_origin or "")

    def _get_session_id(self, event: AstrMessageEvent):
        base = (
            getattr(event, "unified_msg_origin", None)
            or getattr(event, "session_id", None)
            or event.get_sender_id()
        )
        scope = str(getattr(self, "config", {}).get("sandbox_scope", DEFAULT_SANDBOX_SCOPE) or DEFAULT_SANDBOX_SCOPE).strip()
        if scope == "group_member" and self._is_group_event(event, base):
            user_id = str(self._get_user_id(event) or "").strip()
            if user_id:
                return f"{base}#user:{user_id}"
        return base
