import ast
import asyncio
import hashlib
import json
import re
import time
import traceback

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .runtime_capacity import SandboxBusyError
from .sandbox_bootstrap import FONT_SETUP_CODE
from .constants import (
    DEFAULT_EXEC_TIMEOUT,
    DEFAULT_OUTPUT_LIMIT,
    DEFAULT_SANDBOX_TIMEOUT,
    IMPORT_PACKAGE_MAP,
    MAX_RESULT_LIMIT,
)
from .tools import sandbox_action

class ExecutorMixin:
    @sandbox_action
    async def run_python_code(
        self,
        event: AstrMessageEvent,
        code: str = "",
        template: str = "",
        auto_pause: bool = True,
    ):
        """在 E2B 云沙箱中执行 Python 代码。

        重要说明：
        1. 同一个会话复用持久沙箱，需要时才创建或恢复。
        2. 支持常见 Python 库与联网请求。
        3. 支持绘图，图片会自动发送给用户。
        4. 如果当前会话里有用户刚发送的文件，插件会尝试自动上传到沙箱。
        5. 需要处理附件时，请优先从 /home/user/uploads/ 读取文件。
        6. 生成的文件会缓存，使用 list_files / send_file 选择并发送。
        7. 不要在顶层脚本里使用 return，直接 print 结果即可。

        Args:
            code (string): 要执行的 Python 代码。
        """
        if not code:
            return "Error: No code received."

        denied_message = self._get_user_access_denied_message(event)
        if denied_message:
            return denied_message

        match = re.search(r"```(?:python)?\s*(.*?)```", code, re.DOTALL | re.IGNORECASE)
        code_to_run = match.group(1).strip() if match else code.strip()

        session_id = self._get_session_id(event)
        await self._cleanup_expired_sessions()
        self._mark_session_active(event)

        if not self._key_pool.has_keys:
            return "Error: E2B API Key is missing."
        if self._sandbox_sdk is None:
            return "Error: self._sandbox_sdk class not found."

        exec_timeout = self._safe_int(self.config.get("timeout"), DEFAULT_EXEC_TIMEOUT, minimum=5)
        output_limit = self._safe_int(
            self.config.get("max_output_length"),
            DEFAULT_OUTPUT_LIMIT,
            minimum=200,
            maximum=MAX_RESULT_LIMIT,
        )
        sandbox_lifespan = max(exec_timeout + 30, DEFAULT_SANDBOX_TIMEOUT)

        sandbox = None
        llm_feedback = []
        streamed_stdout = []
        streamed_stderr = []
        streamed_results = []
        before_snapshot = {}
        completed = False
        auto_pause = auto_pause is not False or self.config.get("allow_keep_running", False) is not True

        async with self._session_operation(session_id):
            try:
                pending_files = self._get_pending_files(event)
                hash_source = json.dumps(
                    {"code": code_to_run, "files": pending_files},
                    ensure_ascii=False, sort_keys=True,
                )
                current_hash = hashlib.md5(hash_source.encode("utf-8")).hexdigest()
                if self._is_duplicate_execution(session_id, current_hash):
                    return "SYSTEM WARNING: Duplicate code execution intercepted."
                sandbox, sandbox_meta, sandbox_notice = await self._get_or_create_session_sandbox(
                    event=event,
                    template=template,
                    timeout=sandbox_lifespan,
                    create_if_missing=True,
                )
                self.code_hashes[session_id] = current_hash
                self.code_hash_timestamps[session_id] = time.time()
                self.generated_files[session_id] = []
                if sandbox_notice:
                    llm_feedback.append(f"[System Notification] {sandbox_notice}")

                uploaded_paths = await self._stage_pending_files(event, sandbox, pending_files)
                if uploaded_paths:
                    llm_feedback.append(
                        "[System Notification] Uploaded files: " + ", ".join(uploaded_paths)
                    )

                packages = self._detect_packages(code_to_run)
                if packages:
                    await self._install_dependencies(sandbox, packages)

                before_snapshot = await self._snapshot_sandbox_files(sandbox)
                full_code = self._build_execution_code(code_to_run)

                logger.info("[E2B] Running user code...")
                execution = await asyncio.wait_for(
                    sandbox.run_code(
                        full_code,
                        on_stdout=lambda msg: streamed_stdout.append(self._stringify_output(msg)),
                        on_stderr=lambda msg: streamed_stderr.append(self._stringify_output(msg)),
                        on_result=lambda result: streamed_results.append(result),
                        timeout=exec_timeout,
                    ),
                    timeout=exec_timeout + 5,
                )
                logger.info("[E2B] Execution finished.")

                stdout_text = self._merge_chunks(streamed_stdout)
                stderr_text = self._merge_chunks(streamed_stderr)

                if hasattr(execution, "logs"):
                    if not stdout_text and getattr(execution.logs, "stdout", None):
                        stdout_text = "".join(execution.logs.stdout)
                    if not stderr_text and getattr(execution.logs, "stderr", None):
                        stderr_text = "".join(execution.logs.stderr)

                if stdout_text:
                    llm_feedback.append(f"STDOUT:\n{stdout_text}")
                if stderr_text:
                    llm_feedback.append(f"STDERR:\n{stderr_text}")

                text_result = self._extract_text_result(execution, streamed_results)
                if text_result:
                    llm_feedback.append(f"RESULT:\n{text_result}")

                execution_error = getattr(execution, "error", None)
                if execution_error:
                    llm_feedback.append(
                        f"EXECUTION ERROR:\n{self._stringify_output(execution_error)}"
                    )

                has_sent_image = await self._handle_images(event, execution, streamed_results)
                if has_sent_image:
                    llm_feedback.append(
                        "[System Notification] Image generated successfully and sent to user interface."
                    )

                sent_files = await self._handle_generated_files(
                    event,
                    sandbox,
                    pending_files,
                    [
                        stdout_text,
                        stderr_text,
                        text_result,
                        self._stringify_output(execution_error),
                    ],
                    before_snapshot,
                )
                if sent_files:
                    llm_feedback.append(
                        "[System Notification] Generated files cached for manual delivery: "
                        + ", ".join(sent_files)
                    )

                self._update_sandbox_session(
                    session_id,
                    sandbox_meta["sandbox_id"],
                    template=sandbox_meta.get("template", ""),
                    status="running",
                )

                pause_summary = "Sandbox kept running."
                if auto_pause:
                    paused = await self._finish_pause(session_id, sandbox)
                    pause_summary = (
                        "Sandbox auto-paused."
                        if paused else "Pause unconfirmed; the running slot is retained and cleanup will retry."
                    )

                result_text = "\n\n".join(part for part in llm_feedback if part).strip()
                if not result_text:
                    result_text = "Code executed successfully (no visible output)."
                result_text = self._truncate(result_text, output_limit)

                completed = True
                return (
                    f"{result_text}\n\n"
                    "--------------------------------------------------\n"
                    "[SYSTEM COMMAND: Execution Complete.\n"
                    "1. If an image was generated, it has been delivered.\n"
                    "2. If files were generated, they are cached locally in this session.\n"
                    "3. Call e2b_sandbox_list_files to inspect candidates and e2b_sandbox_send_file to deliver the chosen file.\n"
                    f"4. {pause_summary}\n"
                    "5. Keeping a sandbox running requires allow_keep_running=true in plugin settings; idle sandboxes are still auto-paused.\n"
                    "6. Use e2b_sandbox_status, e2b_sandbox_pause, e2b_sandbox_resume, or e2b_sandbox_kill to control lifecycle explicitly.\n"
                    "7. DO NOT call send_message_to_user with /home/user/... sandbox file paths.\n"
                    "8. Explain the result to the user now.]"
                )
            except asyncio.CancelledError:
                logger.warning("[E2B] Task cancelled; attempting to pause the sandbox.")
                raise
            except SandboxBusyError:
                raise
            except asyncio.TimeoutError:
                return f"Error: Execution timed out (>{exec_timeout}s)."
            except Exception as exc:
                logger.error(f"[E2B] Execution Exception: {traceback.format_exc()}")
                return f"Runtime Error: {exc}"
            finally:
                if sandbox is not None and not completed:
                    await self._finish_pause(session_id, sandbox)

    async def _install_dependencies(self, sandbox, packages):
        install_cmd = (
            "python -m pip install --disable-pip-version-check --no-input "
            + " ".join(packages)
        )
        logger.info(f"[E2B] Auto-installing dependencies: {packages}")
        install_result = await sandbox.commands.run(install_cmd, timeout=180)

        exit_code = getattr(install_result, "exit_code", 0)
        if exit_code not in (0, None):
            stderr_text = getattr(install_result, "stderr", "") or getattr(install_result, "stdout", "")
            raise RuntimeError(f"Dependency installation failed: {stderr_text}".strip())

    def _build_execution_code(self, code_to_run: str) -> str:
        return FONT_SETUP_CODE + "\n" + code_to_run

    def _detect_packages(self, code: str):
        packages = set()
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_level = alias.name.split(".")[0]
                    package = IMPORT_PACKAGE_MAP.get(top_level)
                    if package:
                        packages.add(package)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level = node.module.split(".")[0]
                package = IMPORT_PACKAGE_MAP.get(top_level)
                if package:
                    packages.add(package)

        if "plt" in code and "matplotlib" not in packages:
            packages.add("matplotlib")

        return sorted(packages)

    def _extract_text_result(self, execution, streamed_results):
        text = getattr(execution, "text", None)
        if text:
            return str(text)

        result_texts = []
        results = list(getattr(execution, "results", []) or []) or list(streamed_results)
        for result in results:
            text_value = getattr(result, "text", None)
            if text_value:
                result_texts.append(str(text_value))

        return "\n".join(result_texts).strip()

    def _parse_int_output(self, command_result):
        stdout = getattr(command_result, "stdout", "") or ""
        if isinstance(stdout, list):
            stdout = "".join(stdout)
        try:
            return int(str(stdout).strip())
        except (TypeError, ValueError):
            return None

    def _stringify_output(self, value):
        if value is None:
            return ""

        for attr in ("line", "message", "text", "name", "value"):
            attr_value = getattr(value, attr, None)
            if attr_value not in (None, ""):
                return str(attr_value)

        return str(value)

    def _merge_chunks(self, chunks):
        return "".join(chunk for chunk in chunks if chunk).strip()

    def _truncate(self, text: str, limit: int):
        if len(text) <= limit:
            return text
        return text[:limit] + "\n...(Output truncated)"

    def _safe_int(self, value, default, minimum=None, maximum=None):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default

        if minimum is not None:
            parsed = max(minimum, parsed)
        if maximum is not None:
            parsed = min(maximum, parsed)
        return parsed
