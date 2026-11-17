"""注册给大语言模型（LLM）调用的 E2B 云沙箱 FunctionTool 工具定义与异常脱敏包装器。"""

import traceback
from dataclasses import dataclass, field
from functools import wraps
from typing import Any

from astrbot.api import FunctionTool, logger
from astrbot.api.event import AstrMessageEvent

from .runtime_capacity import SandboxBusyError


def sandbox_action(method):
    """捕获并发繁忙与未预期异常，并在日志与返回文本中统一脱敏 API Key。"""
    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        try:
            return await method(self, *args, **kwargs)
        except SandboxBusyError as exc:
            return f"Busy: {exc} Do not retry immediately in this turn."
        except Exception as exc:
            logger.error(f"[E2B] {method.__name__} failed: {self._key_pool.redact(traceback.format_exc())}")
            return f"Error: {self._key_pool.redact(exc)}"
    return wrapped


@dataclass
class RunPythonCodeTool(FunctionTool):
    plugin: Any = field(repr=False, default=None)
    name: str = "e2b_sandbox_run_python_code"
    description: str = (
        "Execute Python in an isolated, session-scoped E2B cloud sandbox. "
        "Prefer this tool for general code execution, calculations, plotting, and user-provided files. "
        "Access follows the E2B plugin's user policy, independently of AstrBot admin status. "
        "Automatically creates or reuses the session sandbox. "
        "For cloud tasks, Python subprocess can execute shell commands inside E2B. "
        "This environment is not the AstrBot host or another user-specified server."
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute inside the sandbox.",
                },
                "template": {
                    "type": "string",
                    "description": "Optional E2B template ID used only when creating a new sandbox.",
                },
                "auto_pause": {
                    "type": "boolean",
                    "description": "Whether to automatically pause the sandbox after execution. Default is true.",
                },
            },
            "required": ["code"],
        }
    )

    async def run(
        self,
        event: AstrMessageEvent,
        code: str,
        template: str = "",
        auto_pause: bool = True,
    ):
        return await self.plugin.run_python_code(
            event,
            code=code,
            template=template,
            auto_pause=auto_pause,
        )


@dataclass
class CreateSandboxTool(FunctionTool):
    plugin: Any = field(repr=False, default=None)
    name: str = "e2b_sandbox_create"
    description: str = (
        "Explicitly pre-warm a sandbox for the current session without running code. "
        "Not needed before e2b_sandbox_run_python_code, which creates or reuses one automatically."
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "template": {
                    "type": "string",
                    "description": "Optional E2B template ID for the sandbox to create.",
                },
            },
        }
    )

    async def run(self, event: AstrMessageEvent, template: str = ""):
        return await self.plugin.create_session_sandbox(event, template=template)


@dataclass
class ResumeSandboxTool(FunctionTool):
    plugin: Any = field(repr=False, default=None)
    name: str = "e2b_sandbox_resume"
    description: str = "Resume or reconnect to the current session's existing sandbox."
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "_": {
                    "type": "string",
                    "description": "Optional placeholder parameter for provider compatibility. Ignore this field.",
                },
            },
        }
    )

    async def run(self, event: AstrMessageEvent, _: str = ""):
        return await self.plugin.resume_session_sandbox(event)


@dataclass
class PauseSandboxTool(FunctionTool):
    plugin: Any = field(repr=False, default=None)
    name: str = "e2b_sandbox_pause"
    description: str = "Pause the current session's sandbox and preserve its filesystem and memory state."
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "_": {
                    "type": "string",
                    "description": "Optional placeholder parameter for provider compatibility. Ignore this field.",
                },
            },
        }
    )

    async def run(self, event: AstrMessageEvent, _: str = ""):
        return await self.plugin.pause_session_sandbox(event)


@dataclass
class KillSandboxTool(FunctionTool):
    plugin: Any = field(repr=False, default=None)
    name: str = "e2b_sandbox_kill"
    description: str = "Kill the current session's sandbox and delete its state permanently."
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "_": {
                    "type": "string",
                    "description": "Optional placeholder parameter for provider compatibility. Ignore this field.",
                },
            },
        }
    )

    async def run(self, event: AstrMessageEvent, _: str = ""):
        return await self.plugin.kill_session_sandbox(event)


@dataclass
class SandboxStatusTool(FunctionTool):
    plugin: Any = field(repr=False, default=None)
    name: str = "e2b_sandbox_status"
    description: str = "Get the current session sandbox ID, status, template, and last active time."
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "_": {
                    "type": "string",
                    "description": "Optional placeholder parameter for provider compatibility. Ignore this field.",
                },
            },
        }
    )

    async def run(self, event: AstrMessageEvent, _: str = ""):
        return await self.plugin.get_session_sandbox_status(event)


@dataclass
class ListFilesTool(FunctionTool):
    plugin: Any = field(repr=False, default=None)
    name: str = "e2b_sandbox_list_files"
    description: str = "List generated files cached from the latest sandbox execution in the current session."
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "_": {
                    "type": "string",
                    "description": "Optional placeholder parameter for provider compatibility. Ignore this field.",
                },
            },
        }
    )

    async def run(self, event: AstrMessageEvent, _: str = ""):
        return await self.plugin.e2b_list_files(event)


@dataclass
class SendFileTool(FunctionTool):
    plugin: Any = field(repr=False, default=None)
    name: str = "e2b_sandbox_send_file"
    description: str = "Send one cached generated file to the user by file name or 1-based index."
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "file_name": {
                    "type": "string",
                    "description": "Exact cached file name to send.",
                },
                "file_index": {
                    "type": "integer",
                    "description": "1-based index from e2b_sandbox_list_files when the file name is unknown.",
                },
            },
        }
    )

    async def run(self, event: AstrMessageEvent, file_name: str = "", file_index: int = 0):
        return await self.plugin.e2b_send_file(event, file_name=file_name, file_index=file_index)
