"""AstrBot E2B 云沙箱插件主入口：工具注册、消息附件记忆、系统提示词注入与生命周期路由。"""

import asyncio
import time
from collections import defaultdict
from pathlib import Path

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest

from .api_keys import ApiKeyPool
from .constants import (
    DEFAULT_SANDBOX_TIMEOUT,
    MAX_SESSION_FILE_COUNT,
    PLUGIN_NAME as PLUGIN_NAME,
)
from .execution_policy import apply_execution_policy
from .executor import ExecutorMixin
from .media_handler import MediaMixin
from .runtime_capacity import RuntimeCapacity
# 保留现有集成辅助与单测依赖的公开符号导出
from .sandbox_bootstrap import FONT_SETUP_CODE as FONT_SETUP_CODE
from .sandbox_manager import SandboxManagerMixin
from .session_store import SessionStoreMixin
from .tools import (
    CreateSandboxTool,
    KillSandboxTool,
    ListFilesTool,
    PauseSandboxTool,
    ResumeSandboxTool,
    RunPythonCodeTool,
    SandboxStatusTool,
    SendFileTool,
    sandbox_action,
)
from .web_panel import WebPanel

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except ImportError:
    get_astrbot_data_path = None

try:
    from e2b_code_interpreter import AsyncSandbox
except ImportError:
    try:
        from e2b import AsyncSandbox
    except ImportError:
        AsyncSandbox = None

try:
    from e2b import SandboxQuery, SandboxState
except ImportError:
    SandboxQuery = SandboxState = None


class Main(SandboxManagerMixin, SessionStoreMixin, ExecutorMixin, MediaMixin, star.Star):
    def __init__(self, context: star.Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self._key_pool = ApiKeyPool(self.config)
        self.code_hashes = defaultdict(str)
        self.code_hash_timestamps = {}
        self.session_files = defaultdict(list)
        self.uploaded_session_files = defaultdict(list)
        self._verified_sandbox_packages = {}
        self._file_event_ids = {}
        self.generated_files = defaultdict(list)
        self.sent_file_signatures = defaultdict(set)
        self.session_last_access = {}
        self.session_locks = {}
        self.sandbox_sessions = {}
        self._plugin_data_dir = self._get_plugin_data_dir()
        self._sandbox_state_path = self._plugin_data_dir / "sandbox_sessions.json"
        self._load_sandbox_sessions()
        self._capacity = RuntimeCapacity(
            occupied=(
                session_id for session_id, meta in self.sandbox_sessions.items()
                if meta.get("status") != "paused"
            )
        )
        self._creation_lock = asyncio.Lock()
        self._next_creation_time = 0.0
        self._maintenance_task = None
        self._cleanup_tasks = set()
        self._inflight_tasks = set()
        self._operation_completions = set()
        self._session_cleanup_tasks = {}
        self._last_expired_cleanup = None
        self._stopping = False
        self._register_llm_tools()
        # 可选 WebUI 管理面板：旧版宿主缺少鉴权插件页面 API 时自动安全降级，不影响聊天功能
        self._web_panel = WebPanel(self)

    async def initialize(self):
        await super().initialize()
        self._web_panel.register()

    async def terminate(self):
        # 先停止接收面板新请求，再等待沙箱管理器排空进行中的锁内操作并收尾暂停
        self._stopping = True
        await self._web_panel.close()
        await super().terminate()

    def _track_task(self, task, bucket):
        bucket.add(task)
        task.add_done_callback(bucket.discard)
        return task

    @property
    def _sandbox_sdk(self):
        return AsyncSandbox

    @property
    def _sandbox_query(self):
        return SandboxQuery

    @property
    def _sandbox_state(self):
        return SandboxState

    @property
    def _host_data_path(self):
        return get_astrbot_data_path

    def _register_llm_tools(self):
        tools = [
            RunPythonCodeTool(plugin=self),
            CreateSandboxTool(plugin=self),
            ResumeSandboxTool(plugin=self),
            PauseSandboxTool(plugin=self),
            KillSandboxTool(plugin=self),
            SandboxStatusTool(plugin=self),
            ListFilesTool(plugin=self),
            SendFileTool(plugin=self),
        ]
        add_tools = getattr(self.context, "add_llm_tools", None)
        if add_tools is None:
            raise RuntimeError("Current AstrBot version does not support context.add_llm_tools().")
        add_tools(*tools)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def remember_session_files(self, event: AstrMessageEvent):
        if not self._is_user_allowed(event):
            return

        await self._cleanup_expired_sessions()
        self._mark_session_active(event)
        files = self._extract_event_files(event)
        if not files:
            return

        files = await self._hydrate_component_files(event, files)
        now = time.time()
        for item in files:
            item["awaiting_upload"] = True
            item["cached_at"] = now

        session_id = self._get_session_id(event)
        # 保留此前消息尚未上传且未同名冲突的待传附件，支持连续发送多份文档后再发文字提问
        existing_pending = [
            prev for prev in self.session_files.get(session_id, ())
            if prev.get("awaiting_upload") is True
            and prev.get("name") not in {f.get("name") for f in files}
        ]
        self.session_files[session_id] = (existing_pending + files)[-MAX_SESSION_FILE_COUNT:]
        self._file_event_ids[session_id] = self._event_token(event)
        logger.info(
            f"[E2B] Cached {len(self.session_files[session_id])} file(s) for session {session_id}"
        )
        logger.debug(f"[E2B] Cached file names: {[meta.get('name') for meta in self.session_files[session_id]]}")

    @sandbox_action
    async def create_session_sandbox(self, event: AstrMessageEvent, template: str = ""):
        denied_message = self._get_user_access_denied_message(event)
        if denied_message:
            return denied_message

        session_id = self._get_session_id(event)
        await self._cleanup_expired_sessions()
        self._mark_session_active(event)

        async with self._session_operation(session_id):
            _, sandbox_meta, notice = await self._get_or_create_session_sandbox(
                event=event,
                template=template,
                timeout=DEFAULT_SANDBOX_TIMEOUT,
                create_if_missing=True,
            )
            message = notice or "Sandbox is ready."
            return (
                f"{message}\n"
                f"Sandbox ID: {sandbox_meta['sandbox_id']}\n"
                f"Status: {sandbox_meta.get('status', 'running')}\n"
                f"Template: {sandbox_meta.get('template') or '(default)'}"
            )

    @sandbox_action
    async def resume_session_sandbox(self, event: AstrMessageEvent):
        denied_message = self._get_user_access_denied_message(event)
        if denied_message:
            return denied_message

        session_id = self._get_session_id(event)
        await self._cleanup_expired_sessions()
        self._mark_session_active(event)

        async with self._session_operation(session_id):
            sandbox, sandbox_meta, notice = await self._get_or_create_session_sandbox(
                event=event,
                timeout=DEFAULT_SANDBOX_TIMEOUT,
                create_if_missing=False,
            )
            if sandbox is None:
                return "No sandbox exists for this session. Call e2b_sandbox_create or e2b_sandbox_run_python_code first."
            return (
                f"{notice or 'Sandbox resumed.'}\n"
                f"Sandbox ID: {sandbox_meta['sandbox_id']}\n"
                "Status: running"
            )

    @sandbox_action
    async def pause_session_sandbox(self, event: AstrMessageEvent):
        denied_message = self._get_user_access_denied_message(event)
        if denied_message:
            return denied_message

        session_id = self._get_session_id(event)
        await self._cleanup_expired_sessions()
        self._mark_session_active(event)

        async with self._session_operation(session_id):
            sandbox_meta = self.sandbox_sessions.get(session_id)
            if not sandbox_meta:
                return "No sandbox exists for this session."
            if sandbox_meta.get("status") == "paused":
                return f"Sandbox is already paused.\nSandbox ID: {sandbox_meta['sandbox_id']}"

            if not await self._finish_pause(session_id):
                return "Error: Pause could not be confirmed. The slot is retained and cleanup will retry."
            if session_id not in self.sandbox_sessions:
                return "Sandbox no longer exists; its local record was removed."
            return (
                "Sandbox paused.\n"
                f"Sandbox ID: {sandbox_meta['sandbox_id']}\n"
                "State is preserved and can be resumed later."
            )

    @sandbox_action
    async def kill_session_sandbox(self, event: AstrMessageEvent):
        denied_message = self._get_user_access_denied_message(event)
        if denied_message:
            return denied_message

        session_id = self._get_session_id(event)
        await self._cleanup_expired_sessions()
        self._mark_session_active(event)

        async with self._session_operation(session_id):
            sandbox_meta = self.sandbox_sessions.get(session_id)
            if not sandbox_meta:
                return "No sandbox exists for this session."

            await self._kill_sandbox_by_id(session_id)
            return f"Sandbox killed.\nSandbox ID: {sandbox_meta['sandbox_id']}"

    @sandbox_action
    async def get_session_sandbox_status(self, event: AstrMessageEvent):
        denied_message = self._get_user_access_denied_message(event)
        if denied_message:
            return denied_message

        session_id = self._get_session_id(event)
        await self._cleanup_expired_sessions()
        self._mark_session_active(event)

        async with self._session_operation(session_id):
            await self._refresh_session_state(session_id)
        sandbox_meta = self.sandbox_sessions.get(session_id)
        if not sandbox_meta or not sandbox_meta.get("sandbox_id"):
            return "No sandbox exists for this session."

        last_active = sandbox_meta.get("last_active", 0)
        last_active_text = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_active))
            if last_active
            else "unknown"
        )
        return (
            f"Sandbox ID: {sandbox_meta['sandbox_id']}\n"
            f"Status: {sandbox_meta.get('status', 'unknown')}\n"
            f"Template: {sandbox_meta.get('template') or '(default)'}\n"
            f"Last active: {last_active_text}"
        )

    async def e2b_list_files(self, event: AstrMessageEvent, query: str = ""):
        """列出当前会话最近一次云沙箱执行缓存的待发送结果文件。"""
        denied_message = self._get_user_access_denied_message(event)
        if denied_message:
            return denied_message
        session_id = self._get_session_id(event)
        self._mark_session_active(event)
        generated_files = self.generated_files.get(session_id, [])
        if not generated_files:
            return "No generated files are currently cached for this session."

        lines = []
        for index, file_meta in enumerate(generated_files, start=1):
            lines.append(
                f"{index}. {file_meta['name']} ({file_meta['size']} bytes, source: {file_meta['remote_path']})"
            )
        return "Cached generated files:\n" + "\n".join(lines)

    async def e2b_send_file(
        self,
        event: AstrMessageEvent,
        file_name: str = "",
        file_index: int = 0,
        name: str = "",
    ):
        """按文件名或从 1 开始的序号向当前聊天发送一份已缓存的生成文件。"""
        denied_message = self._get_user_access_denied_message(event)
        if denied_message:
            return denied_message
        session_id = self._get_session_id(event)
        self._mark_session_active(event)
        generated_files = self.generated_files.get(session_id, [])
        if not generated_files:
            return "No generated files are currently cached for this session."

        if not file_name and name:
            file_name = name

        selected = None
        normalized_name = self._basename(file_name).lower().strip() if file_name else ""
        if normalized_name:
            for file_meta in generated_files:
                if file_meta["name"].lower() == normalized_name:
                    selected = file_meta
                    break

        if normalized_name and selected is None:
            return "Error: Requested file name was not found. Call e2b_sandbox_list_files for available names."
        if not normalized_name and file_index != 0:
            try:
                idx = int(file_index)
                if isinstance(file_index, bool) or idx != float(file_index) or not 1 <= idx <= len(generated_files):
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                return "Error: Invalid file index. Call e2b_sandbox_list_files for valid indices."
            selected = generated_files[idx - 1]

        if selected is None and len(generated_files) == 1:
            selected = generated_files[0]
        if selected is None:
            return "Error: Multiple files are available. Specify a file name or index from e2b_sandbox_list_files."

        local_path = Path(selected["local_path"])
        if not local_path.exists():
            return f"Cached file not found on disk: {selected['name']}"

        signature = selected.get("signature")
        if signature in self.sent_file_signatures[session_id]:
            return f"File already sent in this session: {selected['name']}"

        await self._send_local_file(event, local_path)
        if signature:
            self.sent_file_signatures[session_id].add(signature)
        return f"Sent file to user: {selected['name']}"

    @filter.on_llm_request()
    async def inject_file_hint(self, event: AstrMessageEvent, req: ProviderRequest):
        req.system_prompt = req.system_prompt or ""
        denied_message = self._get_user_access_denied_message(event)
        if denied_message:
            req.system_prompt += (
                "\n\n[System Notice] The E2B sandbox tools are disabled for this user by user_whitelist. "
                "Do not call any E2B tools in this conversation."
            )
            return

        if not apply_execution_policy(self.context, event, req, self.config, logger):
            # 工具已被当前人格禁用或交由子代理单独持有时，不注入直接调用说明
            return

        req.system_prompt += (
            "\n\n[System Notice] The E2B sandbox is session-scoped. Reuse the current session sandbox when follow-up work depends on files or variables created earlier. "
            "Run code directly with e2b_sandbox_run_python_code (it automatically creates or reuses the sandbox; call e2b_sandbox_create only to pre-warm an empty sandbox), "
            "and manage lifecycle with e2b_sandbox_status, e2b_sandbox_pause, e2b_sandbox_resume, and e2b_sandbox_kill when explicitly needed. "
            "Code executions auto-pause unless allow_keep_running is enabled and you pass auto_pause=false. Idle sandboxes still auto-pause. "
            "Do not change template mid-session without killing the old sandbox first. Do not use top-level return in Python scripts. "
            "Do not pass /home/user/... sandbox paths to send_message_to_user. "
            "When code generates files, call e2b_sandbox_list_files and e2b_sandbox_send_file to deliver the chosen file."
        )

        session_id = self._get_session_id(event)
        pending_files = self._get_pending_files(event)
        file_list = []
        for file_meta in pending_files:
            try:
                remote_path = self._resolve_remote_path(file_meta["name"])
            except Exception:
                continue
            file_list.append(f"- {file_meta['name']} -> {remote_path}")

        uploaded_map = getattr(self, "uploaded_session_files", None)
        sessions_map = getattr(self, "sandbox_sessions", None)
        already_uploaded = (
            list(uploaded_map.get(session_id, ()))
            if isinstance(uploaded_map, dict) and isinstance(sessions_map, dict) and session_id in sessions_map
            else []
        )

        if not file_list and not already_uploaded:
            return

        sections = []
        if file_list:
            sections.append(
                "[System Notice] The current session has cached user files that will be uploaded "
                "to the E2B sandbox before code execution. Prefer reading them from these paths:\n"
                + "\n".join(file_list)
            )
        if already_uploaded:
            sections.append(
                "[System Notice] Files previously uploaded and still available in this session's sandbox:\n"
                + "\n".join(f"- {path}" for path in already_uploaded)
            )
        sections.append(
            "[System Notice] Save generated deliverable files under /home/user/uploads/ (or print their /home/user/... path), "
            "then call e2b_sandbox_list_files and e2b_sandbox_send_file to deliver them. "
            "Do not call send_message_to_user with a sandbox path."
        )
        req.system_prompt += "\n\n" + "\n".join(sections)
