"""云端 Python 代码执行器：按需绘图字体引导、预装库毫秒级检测缓存、流式输出聚合与结果投递。"""

import ast
import asyncio
import hashlib
import json
import re
import time
import traceback

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .constants import (
    DEFAULT_EXEC_TIMEOUT,
    DEFAULT_OUTPUT_LIMIT,
    DEFAULT_SANDBOX_TIMEOUT,
    IMPORT_PACKAGE_MAP,
    MAX_RESULT_LIMIT,
    PLOT_IDENTIFIERS,
    PLOT_MODULES,
)
from .runtime_capacity import SandboxBusyError
from .sandbox_bootstrap import FONT_SETUP_CODE
from .tools import sandbox_action
from .utils import shlex_quote

# 反向映射：用于远端快速调用 importlib.util.find_spec 检测包是否已预装，避免每次无谓启动 pip
_PACKAGE_TO_MODULE = {pkg: mod for mod, pkg in IMPORT_PACKAGE_MAP.items()}
_PLOT_ATTR_METHODS = frozenset({"plot", "hist", "boxplot", "scatter", "bar", "barh", "pie", "savefig"})


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
        3. 支持绘图，图片会自动发送给用户（单次最多自动发送多张图表）。
        4. 如果当前会话里有用户刚发送的文件，插件会自动上传到沙箱。
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
            stage = "sandbox activation"
            try:
                pending_files = self._get_pending_files(event)
                # 仅以关键文件身份参与去重哈希，避免内部状态时间戳字段导致重复请求绕过去重
                hash_files = [
                    {
                        "name": item.get("name"),
                        "file_id": item.get("file_id"),
                        "file": item.get("file"),
                        "url": item.get("url"),
                        "path": item.get("path"),
                    }
                    for item in pending_files
                ]
                hash_source = json.dumps(
                    {"code": code_to_run, "files": hash_files},
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
                self.generated_files[session_id] = []
                if sandbox_notice:
                    llm_feedback.append(f"[System Notification] {sandbox_notice}")

                stage = "file upload"
                uploaded_paths = await self._stage_pending_files(event, sandbox, pending_files)
                if uploaded_paths:
                    llm_feedback.append(
                        "[System Notification] Uploaded files: " + ", ".join(uploaded_paths)
                    )

                stage = "dependency preparation"
                packages = self._detect_packages(code_to_run)
                if packages:
                    await self._install_dependencies(sandbox, packages)

                before_snapshot = await self._snapshot_sandbox_files(sandbox)
                full_code = self._build_execution_code(code_to_run)

                stage = "code execution"
                # 附件上传与依赖安装失败时尚未运行用户代码，应允许立即修正重试；
                # 一旦进入代码执行阶段，即便超时也需保留指纹，因为远端可能已产生副作用。
                self.code_hashes[session_id] = current_hash
                self.code_hash_timestamps[session_id] = time.time()
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

                stage = "result delivery"
                sent_images = await self._handle_images(event, execution, streamed_results)
                if sent_images:
                    count = int(sent_images) if isinstance(sent_images, int) and not isinstance(sent_images, bool) else 1
                    image_note = (
                        "Image generated successfully and sent to user interface."
                        if count == 1
                        else f"{count} images generated successfully and sent to user interface."
                    )
                    llm_feedback.append(f"[System Notification] {image_note}")

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
                directives = ["[SYSTEM COMMAND: Execution Complete."]
                if sent_images:
                    directives.append("Generated chart(s) have already been delivered to the user.")
                if sent_files:
                    directives.append(
                        "Generated files are cached locally; call e2b_sandbox_list_files and "
                        "e2b_sandbox_send_file to deliver the chosen file."
                    )
                directives.append(pause_summary)
                directives.append(
                    "DO NOT pass /home/user/... sandbox paths to send_message_to_user. "
                    "Explain the result to the user now.]"
                )
                return (
                    f"{result_text}\n\n"
                    "--------------------------------------------------\n"
                    + "\n".join(directives)
                )
            except asyncio.CancelledError:
                logger.warning("[E2B] Task cancelled; attempting to pause the sandbox.")
                raise
            except SandboxBusyError:
                raise
            except asyncio.TimeoutError:
                if stage == "code execution":
                    return f"Error: Execution timed out (>{exec_timeout}s)."
                return f"E2B {stage} timed out. Sandbox state will be reconciled before retry."
            except Exception as exc:
                logger.error(f"[E2B] Execution Exception: {self._key_pool.redact(traceback.format_exc())}")
                return f"Runtime Error: {self._key_pool.redact(exc)}"
            finally:
                if sandbox is not None and not completed:
                    await self._finish_pause(session_id, sandbox)

    async def _install_dependencies(self, sandbox, packages):
        """先在沙箱内快速检查缺失模块，仅对未预装的包执行 pip 安装，并按沙箱 ID 缓存已验证结果。"""
        sandbox_id = self._extract_sandbox_id(sandbox)
        verified_map = getattr(self, "_verified_sandbox_packages", None)
        verified = verified_map.setdefault(sandbox_id, set()) if (verified_map is not None and sandbox_id) else set()
        pending_packages = [pkg for pkg in packages if pkg not in verified]
        if not pending_packages:
            return

        # 构造包名与模块名映射，在沙箱内通过 find_spec 毫秒级筛除默认镜像已预装的库
        pairs = [(pkg, _PACKAGE_TO_MODULE.get(pkg, pkg)) for pkg in pending_packages]
        pairs_json = json.dumps(pairs)
        probe_py = (
            "import importlib.util, json, subprocess, sys; "
            f"pairs = json.loads({shlex_quote(pairs_json)}); "
            "missing = [pkg for pkg, mod in pairs if importlib.util.find_spec(mod) is None]; "
            "sys.exit(0 if not missing else subprocess.call("
            "[sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', *missing]))"
        )
        install_cmd = f"python -c {shlex_quote(probe_py)}"
        logger.info(f"[E2B] Checking/installing dependencies: {pending_packages}")
        install_result = await sandbox.commands.run(install_cmd, timeout=180)

        exit_code = getattr(install_result, "exit_code", 0)
        if exit_code not in (0, None):
            stderr_text = getattr(install_result, "stderr", "") or getattr(install_result, "stdout", "")
            raise RuntimeError(f"Dependency installation failed: {stderr_text}".strip())

        verified.update(pending_packages)

    def _needs_plot_setup(self, code: str) -> bool:
        """基于 AST 判断代码是否可能涉及绘图操作，避免纯计算或表格任务无条件加载 matplotlib。"""
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return any(token in code for token in ("matplotlib", "plt", "seaborn", "wordcloud", ".plot"))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in PLOT_MODULES:
                        return True
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in PLOT_MODULES:
                    return True
            elif isinstance(node, ast.Name) and node.id in PLOT_IDENTIFIERS:
                return True
            elif isinstance(node, ast.Attribute) and node.attr in _PLOT_ATTR_METHODS:
                return True
        return False

    def _build_execution_code(self, code_to_run: str, include_font_setup=None) -> str:
        """按需拼装中文字体与无头绘图展示引导代码。"""
        should_inject = self._needs_plot_setup(code_to_run) if include_font_setup is None else bool(include_font_setup)
        if not should_inject:
            return code_to_run
        return FONT_SETUP_CODE + "\n" + code_to_run

    def _detect_packages(self, code: str):
        """基于 AST 提取顶层导入的第三方包，仅对真正的标识符引用 `plt` 补充 matplotlib。"""
        packages = set()
        has_plt_identifier = False
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
            elif isinstance(node, ast.Name) and node.id == "plt":
                has_plt_identifier = True

        if has_plt_identifier and "matplotlib" not in packages:
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
        except (TypeError, ValueError, OverflowError):
            parsed = default

        if minimum is not None:
            parsed = max(minimum, parsed)
        if maximum is not None:
            parsed = min(maximum, parsed)
        return parsed
