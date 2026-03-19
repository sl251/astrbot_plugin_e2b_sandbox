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
import urllib.request
import base64 as py_base64
import zipfile
from collections import defaultdict
from io import BytesIO
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.provider import ProviderRequest

try:
    from e2b_code_interpreter import AsyncSandbox
except ImportError:
    try:
        from e2b import AsyncSandbox
    except ImportError:
        AsyncSandbox = None


DEFAULT_EXEC_TIMEOUT = 60
DEFAULT_OUTPUT_LIMIT = 2000
DEFAULT_PROXY = ""
DEFAULT_TEMPLATE = ""
DEFAULT_UPLOAD_DIR = "/home/user/uploads"
DEFAULT_WORK_DIR = "/home/user"
DEFAULT_EXPORT_DIRNAME = "exports"
MAX_RESULT_LIMIT = 20000
MAX_SESSION_FILE_COUNT = 5
MAX_GENERATED_FILE_CANDIDATES = 3
DEFAULT_MAX_RETURN_FILE_SIZE_MB = 5
DEFAULT_FILE_RETENTION_HOURS = 24
DEFAULT_SESSION_RETENTION_HOURS = 12
SANDBOX_PATH_PATTERN = re.compile(r"(/home/user(?:/[\w\-. \u4e00-\u9fff]+)+)")

IMPORT_PACKAGE_MAP = {
    "PIL": "Pillow",
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "dotenv": "python-dotenv",
    "jieba": "jieba",
    "matplotlib": "matplotlib",
    "numpy": "numpy",
    "openpyxl": "openpyxl",
    "pandas": "pandas",
    "requests": "requests",
    "seaborn": "seaborn",
    "sklearn": "scikit-learn",
    "wordcloud": "wordcloud",
}


class Main(star.Star):
    """Use E2B cloud sandboxes to execute Python code safely."""

    def __init__(self, context: star.Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.code_hashes = defaultdict(str)
        self.session_files = defaultdict(list)
        self.generated_files = defaultdict(list)
        self.sent_file_signatures = defaultdict(set)
        self.session_last_access = {}

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def remember_session_files(self, event: AstrMessageEvent):
        self._mark_session_active(event)
        files = self._extract_event_files(event)
        if not files:
            return

        files = await self._hydrate_component_files(event, files)

        session_id = self._get_session_id(event)
        self.session_files[session_id] = files[-MAX_SESSION_FILE_COUNT:]
        logger.info(
            f"[E2B] Cached {len(self.session_files[session_id])} file(s) for session {session_id}"
        )
        logger.info(f"[E2B] Cached file metadata: {self.session_files[session_id]}")

    @filter.llm_tool(name="run_python_code")
    async def run_python_code(
        self,
        event: AstrMessageEvent,
        code: str = "",
        template: str = "",
    ):
        """在 E2B 云沙箱中执行 Python 代码。

        重要说明：
        1. 每次调用都会创建一个全新的沙箱环境。
        2. 支持常见 Python 库与联网请求。
        3. 支持绘图，图片会自动发送给用户。
        4. 如果当前会话里有用户刚发送的文件，插件会尝试自动上传到沙箱。
        5. 需要处理附件时，请优先从 /home/user/uploads/ 读取文件。
        6. 如果要把生成的文件自动发回用户，请把文件保存到 /home/user/uploads/。
        7. 不要在顶层脚本里使用 return，直接 print 结果即可。

        Args:
            code (string): 要执行的 Python 代码。
        """
        if not code:
            return "Error: No code received."

        match = re.search(r"```(?:python)?\s*(.*?)```", code, re.DOTALL | re.IGNORECASE)
        code_to_run = match.group(1).strip() if match else code.strip()

        session_id = self._get_session_id(event)
        self._mark_session_active(event)
        pending_files = self._get_pending_files(event)
        hash_source = json.dumps(
            {"code": code_to_run, "files": pending_files},
            ensure_ascii=False,
            sort_keys=True,
        )
        current_hash = hashlib.md5(hash_source.encode("utf-8")).hexdigest()

        if self.code_hashes[session_id] == current_hash:
            logger.warning(f"[E2B] Duplicate execution intercepted for session {session_id}")
            return "SYSTEM WARNING: Duplicate code execution intercepted."
        self.code_hashes[session_id] = current_hash
        self.generated_files[session_id] = []

        api_key = self.config.get("e2b_api_key", "")
        if not api_key:
            return "Error: E2B API Key is missing."
        if AsyncSandbox is None:
            return "Error: AsyncSandbox class not found."

        exec_timeout = self._safe_int(self.config.get("timeout"), DEFAULT_EXEC_TIMEOUT, minimum=5)
        output_limit = self._safe_int(
            self.config.get("max_output_length"),
            DEFAULT_OUTPUT_LIMIT,
            minimum=200,
            maximum=MAX_RESULT_LIMIT,
        )
        proxy = str(self.config.get("proxy", DEFAULT_PROXY) or "").strip()
        sandbox_lifespan = exec_timeout + 30

        sandbox = None
        llm_feedback = []
        streamed_stdout = []
        streamed_stderr = []
        streamed_results = []
        before_snapshot = {}

        try:
            logger.info(f"[E2B] Session {session_id} creating sandbox...")
            sandbox = await self._create_sandbox(
                api_key=api_key,
                timeout=sandbox_lifespan,
                proxy=proxy,
                template=template,
            )

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
                llm_feedback.append(f"EXECUTION ERROR:\n{self._stringify_output(execution_error)}")

            has_sent_image = await self._handle_images(event, execution, streamed_results)
            if has_sent_image:
                llm_feedback.append(
                    "[System Notification] Image generated successfully and sent to user interface."
                )

            sent_files = await self._handle_generated_files(
                event,
                sandbox,
                pending_files,
                [stdout_text, stderr_text, text_result, self._stringify_output(execution_error)],
                before_snapshot,
            )
            if sent_files:
                llm_feedback.append(
                    "[System Notification] Generated files sent to user interface: "
                    + ", ".join(sent_files)
                )

            result_text = "\n\n".join(part for part in llm_feedback if part).strip()
            if not result_text:
                result_text = "Code executed successfully (no visible output)."
            result_text = self._truncate(result_text, output_limit)

            return (
                f"{result_text}\n\n"
                "--------------------------------------------------\n"
                "[SYSTEM COMMAND: Execution Complete.\n"
                "1. If an image was generated, it has been delivered.\n"
                "2. If a file was generated, the plugin has already attempted to deliver it automatically.\n"
                "3. DO NOT retry or run the code again.\n"
                "4. DO NOT call send_message_to_user with /home/user/... sandbox file paths.\n"
                "5. Explain the result to the user now.]"
            )

        except asyncio.CancelledError:
            logger.warning("[E2B] Task cancelled by AstrBot Core. Cleaning up sandbox...")
            raise
        except asyncio.TimeoutError:
            return f"Error: Execution timed out (>{exec_timeout}s)."
        except Exception as exc:
            logger.error(f"[E2B] Execution Exception: {traceback.format_exc()}")
            return f"Runtime Error: {exc}"
        finally:
            if sandbox:
                try:
                    await asyncio.wait_for(sandbox.kill(), timeout=5)
                except Exception:
                    pass

    async def e2b_run_python_code(
        self,
        event: AstrMessageEvent,
        code: str = "",
        template: str = "",
    ):
        """Run Python code in an E2B sandbox with an optional template."""
        return await self.run_python_code(event, code=code, template=template)

    async def e2b_list_files(self, event: AstrMessageEvent, query: str = ""):
        """List generated files cached from the latest E2B execution in this session."""
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
        """Send one cached generated file to the user by name or 1-based index."""
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

        if selected is None and file_index:
            idx = self._safe_int(file_index, 0, minimum=1, maximum=len(generated_files))
            if idx:
                selected = generated_files[idx - 1]

        if selected is None:
            selected = generated_files[0]

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
        req.system_prompt += (
            "\n\n[System Notice] The E2B sandbox is ephemeral. Each tool call starts a fresh sandbox, "
            "and files or variables created in one call will not exist in the next call unless recreated or re-uploaded. "
            "Do not rely on cross-call state. Do not use top-level return in Python scripts. "
            "Do not use send_message_to_user to send sandbox file paths such as /home/user/... . "
            "This plugin will automatically try to return generated files to the user."
        )

        pending_files = self._get_pending_files(event)
        if not pending_files:
            return

        file_list = []
        for file_meta in pending_files:
            try:
                remote_path = self._resolve_remote_path(file_meta["name"])
            except Exception:
                continue
            file_list.append(f"- {file_meta['name']} -> {remote_path}")

        if not file_list:
            return

        req.system_prompt += (
            "\n\n[System Notice] The current session has cached user files that will be uploaded "
            "to the E2B sandbox before code execution. Prefer reading them from these paths:\n"
            + "\n".join(file_list)
            + "\n[System Notice] If you want generated files to be sent back to the user, save them under /home/user/uploads/. "
            "The plugin will also try to detect printed /home/user/... file paths automatically. "
            "After generating a file, just explain the result to the user. "
            "Do not call send_message_to_user with a file attachment that points to a sandbox path."
        )

    async def _create_sandbox(self, api_key: str, timeout: int, proxy: str, template: str = ""):
        create_kwargs = {
            "api_key": api_key,
            "timeout": timeout,
        }
        if proxy:
            create_kwargs["proxy"] = proxy
        template = str(template or self.config.get("default_template", DEFAULT_TEMPLATE) or "").strip()
        if template:
            create_kwargs["template"] = template

        unsupported_options = []
        if proxy:
            unsupported_options.append(("proxy", "[E2B] Current SDK does not accept proxy on create(); retrying without it."))
        if template:
            unsupported_options.append(("template", "[E2B] Current SDK does not accept template on create(); retrying without it."))

        while True:
            try:
                return await asyncio.wait_for(AsyncSandbox.create(**create_kwargs), timeout=20)
            except TypeError:
                if not unsupported_options:
                    raise
                option_key, warning_text = unsupported_options.pop(0)
                if option_key in create_kwargs:
                    logger.warning(warning_text)
                    create_kwargs.pop(option_key, None)

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

    async def _stage_pending_files(self, event: AstrMessageEvent, sandbox, pending_files):
        uploaded_paths = []
        await sandbox.commands.run(f"mkdir -p {DEFAULT_UPLOAD_DIR}", timeout=30)
        for file_meta in pending_files:
            file_payload = await self._resolve_file_payload(event, file_meta)
            if not file_payload:
                continue

            remote_path = self._resolve_remote_path(file_payload["name"])
            await sandbox.files.write(remote_path, file_payload["content"])
            uploaded_paths.append(remote_path)

        return uploaded_paths

    async def _resolve_file_payload(self, event: AstrMessageEvent, file_meta):
        source = self._extract_local_source(file_meta)
        if source and os.path.exists(source):
            content = await asyncio.to_thread(self._read_local_file, source)
            return {"name": file_meta.get("name") or os.path.basename(source), "content": content}

        file_url = self._extract_remote_url(file_meta)
        if file_url:
            content = await asyncio.to_thread(self._download_url, file_url)
            if content is not None:
                return {"name": file_meta.get("name") or "attachment.bin", "content": content}

        fallback_url = await self._get_file_url_from_bot(event, file_meta)
        if fallback_url:
            if os.path.exists(fallback_url):
                content = await asyncio.to_thread(self._read_local_file, fallback_url)
            else:
                content = await asyncio.to_thread(self._download_url, fallback_url)
            if content is not None:
                return {"name": file_meta.get("name") or "attachment.bin", "content": content}

        logger.warning(
            f"[E2B] Failed to resolve file payload for {file_meta.get('name') or file_meta}"
        )
        return None

    async def _get_file_url_from_bot(self, event: AstrMessageEvent, file_meta):
        file_id = file_meta.get("file_id")
        file_token = file_meta.get("file")

        if file_meta.get("group_id") and file_id and file_meta.get("busid") is not None:
            response = await self._call_bot_api(
                event,
                "get_group_file_url",
                {
                    "group_id": file_meta["group_id"],
                    "file_id": file_id,
                    "busid": file_meta["busid"],
                },
            )
            extracted = self._extract_url_or_path(response)
            if extracted:
                return extracted

        if file_meta.get("user_id") and (file_id or file_token):
            response = await self._call_bot_api(
                event,
                "get_private_file_url",
                {
                    "user_id": file_meta["user_id"],
                    "file_id": file_id,
                    "file": file_token,
                },
            )
            extracted = self._extract_url_or_path(response)
            if extracted:
                return extracted

        if file_token or file_id:
            response = await self._call_bot_api(
                event,
                "get_file",
                {
                    "file": file_token or file_id,
                    "type": "file",
                },
            )
            extracted = self._extract_url_or_path(response)
            if extracted:
                return extracted

        return None

    async def _call_bot_api(self, event: AstrMessageEvent, action: str, params: dict):
        bot = getattr(event, "bot", None)
        if bot is None:
            logger.warning(f"[E2B] event.bot is unavailable; cannot call {action}")
            return None

        candidates = [
            getattr(bot, "call_action", None),
            getattr(bot, "call_api", None),
        ]

        api = getattr(bot, "api", None)
        if api is not None:
            candidates.extend(
                [
                    getattr(api, "call_action", None),
                    getattr(api, "call_api", None),
                ]
            )

        for method in candidates:
            if not callable(method):
                continue

            for args, kwargs in (
                ((action,), params),
                ((action, params), {}),
            ):
                try:
                    result = method(*args, **{k: v for k, v in kwargs.items() if v is not None})
                    if inspect.isawaitable(result):
                        result = await result
                    return result
                except TypeError:
                    continue
                except Exception as exc:
                    logger.warning(f"[E2B] Bot API call {action} failed: {exc}")
                    break

        logger.warning(f"[E2B] No compatible bot API caller found for {action}")
        return None

    async def _handle_images(self, event: AstrMessageEvent, execution, streamed_results):
        has_sent_image = False
        results = list(getattr(execution, "results", []) or [])
        if not results and streamed_results:
            results = streamed_results

        for res in results:
            img_data, img_ext = self._extract_image_data(res)
            if not img_data:
                continue

            try:
                img_bytes = base64.b64decode(img_data)
            except Exception as exc:
                logger.error(f"[E2B] Image preparation failed: {exc}")
                continue

            async def send_image_task(data, ext, evt):
                tmp_path = None
                try:
                    await asyncio.sleep(0.3)
                    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp_file:
                        tmp_file.write(data)
                        tmp_path = tmp_file.name
                    await evt.send(evt.chain_result([Image.fromFileSystem(tmp_path)]))
                    logger.info("[E2B] Async image sent successfully.")
                except Exception as exc:
                    logger.error(f"[E2B] Async image send failed: {exc}")
                finally:
                    if tmp_path and os.path.exists(tmp_path):
                        try:
                            os.remove(tmp_path)
                        except Exception:
                            pass

            asyncio.create_task(send_image_task(img_bytes, img_ext, event))
            has_sent_image = True
            break

        return has_sent_image

    async def _handle_generated_files(
        self,
        event: AstrMessageEvent,
        sandbox,
        pending_files,
        hint_texts,
        before_snapshot,
    ):
        self._cleanup_export_cache()
        self._cleanup_session_cache()

        session_id = self._get_session_id(event)
        generated_files = await self._collect_generated_files(
            sandbox,
            pending_files,
            hint_texts,
            session_id,
            before_snapshot,
        )
        if not generated_files:
            self.generated_files[session_id] = []
            return []

        cached_files = []
        for file_name, file_bytes, remote_path, file_size, signature in generated_files:
            local_path = self._write_export_file(file_name, file_bytes)
            if not local_path:
                continue

            cached_files.append(
                {
                    "name": local_path.name,
                    "local_path": str(local_path.resolve()),
                    "remote_path": remote_path,
                    "size": file_size,
                    "signature": signature,
                }
            )

        self.generated_files[session_id] = cached_files
        if not cached_files:
            return []

        first_file = cached_files[0]
        local_path = Path(first_file["local_path"])
        signature = first_file.get("signature")
        if signature not in self.sent_file_signatures[session_id]:
            await self._send_local_file(event, local_path)
            if signature:
                self.sent_file_signatures[session_id].add(signature)

        return [first_file["name"]]

    def _build_execution_code(self, code_to_run: str) -> str:
        setup_code = """
import os
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

def _configure_font():
    font_path = '/tmp/SimHei.ttf'
    if not os.path.exists(font_path):
        try:
            os.system('curl -Ls -o /tmp/SimHei.ttf https://github.com/StellarCN/scp_zh/raw/master/fonts/SimHei.ttf > /dev/null 2>&1')
        except Exception:
            pass

    if os.path.exists(font_path):
        try:
            fm.fontManager.addfont(font_path)
            plt.rcParams['font.sans-serif'] = ['SimHei']
            plt.rcParams['axes.unicode_minus'] = False
        except Exception:
            pass

try:
    _configure_font()
except Exception:
    pass
"""
        return setup_code + "\n" + code_to_run

    def _extract_event_files(self, event: AstrMessageEvent):
        files = []
        message_obj = getattr(event, "message_obj", None)
        if message_obj is None:
            return files

        group_id = getattr(message_obj, "group_id", "") or ""
        sender = getattr(message_obj, "sender", None)
        user_id = getattr(sender, "user_id", None) or getattr(sender, "qq", None)

        for component in getattr(message_obj, "message", []) or []:
            if not self._is_file_component(component):
                continue

            files.append(
                self._normalize_file_meta(
                    {
                        "name": getattr(component, "name", None) or getattr(component, "file", None),
                        "file": getattr(component, "file", None),
                        "file_id": getattr(component, "file_id", None),
                        "file_size": getattr(component, "file_size", None),
                        "path": getattr(component, "path", None),
                        "url": getattr(component, "url", None),
                        "file_path": getattr(component, "file_path", None),
                        "local_path": getattr(component, "local_path", None),
                        "temp_path": getattr(component, "temp_path", None),
                        "uri": getattr(component, "uri", None),
                        "busid": getattr(component, "busid", None),
                        "group_id": group_id,
                        "user_id": user_id,
                    }
                )
            )

        raw_message = getattr(message_obj, "raw_message", None)
        if isinstance(raw_message, dict):
            for segment in raw_message.get("message", []) or []:
                if isinstance(segment, dict) and segment.get("type") == "file":
                    data = segment.get("data", {}) or {}
                    files.append(
                        self._normalize_file_meta(
                            {
                                "name": data.get("name") or data.get("file"),
                                "file": data.get("file"),
                                "file_id": data.get("file_id"),
                                "file_size": data.get("file_size"),
                                "path": data.get("path"),
                                "url": data.get("url"),
                                "file_path": data.get("file_path"),
                                "local_path": data.get("local_path"),
                                "temp_path": data.get("temp_path"),
                                "uri": data.get("uri"),
                                "busid": data.get("busid"),
                                "group_id": raw_message.get("group_id") or group_id,
                                "user_id": raw_message.get("user_id") or user_id,
                            }
                        )
                    )

            if raw_message.get("notice_type") == "group_upload":
                upload_file = raw_message.get("file", {}) or {}
                files.append(
                    self._normalize_file_meta(
                        {
                            "name": upload_file.get("name"),
                            "file": upload_file.get("id"),
                            "file_id": upload_file.get("id"),
                            "file_size": upload_file.get("size"),
                            "busid": upload_file.get("busid"),
                            "group_id": raw_message.get("group_id") or group_id,
                            "user_id": raw_message.get("user_id") or user_id,
                        }
                    )
                )

        deduped = []
        seen = set()
        for file_meta in files:
            if not file_meta:
                continue
            signature = (
                file_meta.get("file_id"),
                file_meta.get("name"),
                file_meta.get("group_id"),
                file_meta.get("user_id"),
            )
            if signature in seen:
                continue
            seen.add(signature)
            deduped.append(file_meta)

        return deduped

    async def _hydrate_component_files(self, event: AstrMessageEvent, files):
        message_obj = getattr(event, "message_obj", None)
        if message_obj is None:
            return files

        local_paths = {}
        for component in getattr(message_obj, "message", []) or []:
            if not self._is_file_component(component):
                continue

            getter = getattr(component, "get_file", None)
            if not callable(getter):
                continue

            try:
                local_path = getter()
                if inspect.isawaitable(local_path):
                    local_path = await local_path
            except Exception as exc:
                logger.warning(f"[E2B] component.get_file() failed: {exc}")
                continue

            if not local_path:
                continue

            local_path = str(local_path)
            name = getattr(component, "name", None) or getattr(component, "file", None)
            if name:
                logger.info(f"[E2B] Resolved component file {name} -> {local_path}")
                local_paths[str(name)] = local_path
            else:
                logger.info(f"[E2B] Resolved unnamed component file -> {local_path}")

        if not local_paths:
            for component in getattr(message_obj, "message", []) or []:
                if not self._is_file_component(component):
                    continue
                logger.info(
                    f"[E2B] File component snapshot: class={type(component).__name__}, attrs={self._safe_component_attrs(component)}"
                )

        if not local_paths:
            return files

        hydrated = []
        for file_meta in files:
            merged = dict(file_meta)
            if not merged.get("path"):
                local_path = local_paths.get(merged.get("name", ""))
                if local_path:
                    merged["path"] = local_path
            hydrated.append(merged)

        return hydrated

    def _is_file_component(self, component) -> bool:
        if isinstance(component, Comp.File):
            return True

        if callable(getattr(component, "get_file", None)):
            return True

        component_type = getattr(component, "type", None)
        if isinstance(component_type, str) and component_type.lower() == "file":
            return True

        class_name = type(component).__name__.lower()
        if class_name == "file":
            return True

        return False

    def _get_pending_files(self, event: AstrMessageEvent):
        current_files = self._extract_event_files(event)
        session_id = self._get_session_id(event)
        if current_files:
            self.session_files[session_id] = current_files[-MAX_SESSION_FILE_COUNT:]
            return list(self.session_files[session_id])
        return list(self.session_files.get(session_id, []))

    def _normalize_file_meta(self, file_meta):
        if not isinstance(file_meta, dict):
            return None

        name = file_meta.get("name") or file_meta.get("file")
        if not name and not file_meta.get("file_id"):
            return None

        return {
            "name": str(name or file_meta.get("file_id")),
            "file": file_meta.get("file"),
            "file_id": file_meta.get("file_id"),
            "file_size": file_meta.get("file_size"),
            "path": file_meta.get("path"),
            "url": file_meta.get("url"),
            "file_path": file_meta.get("file_path"),
            "local_path": file_meta.get("local_path"),
            "temp_path": file_meta.get("temp_path"),
            "uri": file_meta.get("uri"),
            "busid": file_meta.get("busid"),
            "group_id": file_meta.get("group_id"),
            "user_id": file_meta.get("user_id"),
        }

    def _resolve_remote_path(self, name: str):
        raw_path = str(name).strip().replace("\\", "/")
        if raw_path.startswith("/"):
            normalized = posixpath.normpath(raw_path)
        else:
            normalized = posixpath.normpath(posixpath.join(DEFAULT_UPLOAD_DIR, raw_path))

        if normalized in (".", "/"):
            raise ValueError("Invalid remote file path")
        return normalized

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

    async def _collect_generated_files(
        self,
        sandbox,
        pending_files,
        hint_texts,
        session_id,
        before_snapshot,
    ):
        input_names = {self._basename(meta.get("name", "")) for meta in pending_files}
        after_snapshot = await self._snapshot_sandbox_files(sandbox)
        generated_paths = []
        seen_paths = set()

        for remote_path, meta in after_snapshot.items():
            if self._basename(remote_path) in input_names:
                continue
            before_meta = before_snapshot.get(remote_path)
            if before_meta is None:
                generated_paths.append(remote_path)
                seen_paths.add(remote_path)
                continue
            if meta["size"] != before_meta["size"] or meta["mtime"] != before_meta["mtime"]:
                generated_paths.append(remote_path)
                seen_paths.add(remote_path)

        if not generated_paths:
            for remote_path in self._extract_paths_from_texts(hint_texts):
                if remote_path in seen_paths:
                    continue
                if self._basename(remote_path) in input_names:
                    continue
                if remote_path not in after_snapshot:
                    continue
                seen_paths.add(remote_path)
                generated_paths.append(remote_path)

        if not generated_paths:
            return []

        max_bytes = self._safe_int(
            self.config.get("max_return_file_size_mb"),
            DEFAULT_MAX_RETURN_FILE_SIZE_MB,
            minimum=1,
            maximum=50,
        ) * 1024 * 1024

        candidates = []
        for remote_path in generated_paths:
            file_name = self._basename(remote_path)
            stat_result = await sandbox.commands.run(
                f"stat -c %s {shlex_quote(remote_path)}",
                timeout=15,
            )
            file_size = self._parse_int_output(stat_result)
            if file_size is None:
                logger.warning(f"[E2B] Failed to stat generated file: {remote_path}")
                continue
            if file_size <= 0:
                logger.info(f"[E2B] Skip generated file {file_name}: empty file")
                continue
            if file_size > max_bytes:
                logger.warning(
                    f"[E2B] Skip generated file {file_name}: size {file_size} exceeds limit {max_bytes}"
                )
                continue

            try:
                content = await self._read_sandbox_file_bytes(sandbox, remote_path)
            except Exception as exc:
                logger.warning(f"[E2B] Failed to download generated file {remote_path}: {exc}")
                continue

            if not content:
                logger.info(f"[E2B] Skip generated file {file_name}: downloaded content is empty")
                continue
            if not self._is_valid_generated_file(file_name, content):
                logger.warning(f"[E2B] Skip generated file {file_name}: integrity validation failed")
                continue

            signature = self._build_file_signature(file_name, content)
            if signature in self.sent_file_signatures[session_id]:
                logger.info(f"[E2B] Skip generated file {file_name}: duplicate in current session")
                continue

            score = self._score_generated_file(
                remote_path,
                file_name,
                file_size,
                hint_texts,
                before_snapshot,
                after_snapshot,
            )
            candidates.append((score, file_name, content, remote_path, file_size, signature))

        if not candidates:
            return []

        candidates.sort(key=lambda item: item[0], reverse=True)
        selected_candidates = candidates[:MAX_GENERATED_FILE_CANDIDATES]
        logger.info(
            "[E2B] Cached generated file candidates: "
            + ", ".join(f"{item[1]} (score={item[0]})" for item in selected_candidates)
        )
        return [
            (file_name, content, remote_path, file_size, signature)
            for _score, file_name, content, remote_path, file_size, signature in selected_candidates
        ]

    def _score_generated_file(self, remote_path, file_name, file_size, hint_texts, before_snapshot, after_snapshot):
        score = 0
        lower_name = file_name.lower()
        lower_path = remote_path.lower()
        before_meta = before_snapshot.get(remote_path)
        after_meta = after_snapshot.get(remote_path, {})

        if lower_path.startswith(f"{DEFAULT_UPLOAD_DIR}/"):
            score += 100
        elif lower_path.startswith(f"{DEFAULT_WORK_DIR}/"):
            score += 40

        if before_meta is None:
            score += 120
        elif after_meta.get("mtime") != before_meta.get("mtime") or after_meta.get("size") != before_meta.get("size"):
            score += 70

        if any(keyword in lower_name for keyword in ("修改", "结果", "output", "final", "report", "export")):
            score += 30

        if "." in file_name:
            score += 10

        if file_size > 0:
            score += min(file_size, 4096) // 256

        joined_hints = "\n".join(str(text) for text in hint_texts if text)
        if remote_path in joined_hints or file_name in joined_hints:
            score += 120

        return score

    async def _snapshot_sandbox_files(self, sandbox):
        snapshot = {}
        search_dirs = [DEFAULT_UPLOAD_DIR, DEFAULT_WORK_DIR]

        for base_dir in search_dirs:
            listing = await sandbox.commands.run(
                f"find {shlex_quote(base_dir)} -maxdepth 1 -type f -printf '%p\\t%s\\t%T@\\n'",
                timeout=30,
            )
            stdout = getattr(listing, "stdout", "") or ""
            if isinstance(stdout, list):
                stdout = "".join(stdout)

            for line in stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split("\t")
                if len(parts) != 3:
                    continue
                remote_path, size_text, mtime_text = parts
                try:
                    snapshot[remote_path] = {
                        "size": int(float(size_text)),
                        "mtime": float(mtime_text),
                    }
                except (TypeError, ValueError):
                    continue

        return snapshot

    def _build_file_signature(self, file_name, content: bytes):
        digest = hashlib.md5(content).hexdigest()
        return f"{file_name}:{digest}"

    def _extract_paths_from_texts(self, texts):
        paths = []
        for text in texts:
            if not text:
                continue
            for match in SANDBOX_PATH_PATTERN.findall(str(text)):
                normalized = match.strip().rstrip(".,:;)]}\"'")
                if normalized.startswith("/home/user/"):
                    paths.append(normalized)
        return paths

    def _extract_image_data(self, result):
        if hasattr(result, "png") and result.png:
            return result.png, ".png"
        if hasattr(result, "jpeg") and result.jpeg:
            return result.jpeg, ".jpg"
        if hasattr(result, "svg") and result.svg:
            encoded = base64.b64encode(str(result.svg).encode("utf-8")).decode("utf-8")
            return encoded, ".svg"

        if hasattr(result, "formats"):
            formats_data = result.formats() if callable(result.formats) else result.formats
            if isinstance(formats_data, dict):
                if formats_data.get("png"):
                    return formats_data["png"], ".png"
                if formats_data.get("jpeg"):
                    return formats_data["jpeg"], ".jpg"
        return None, ""

    def _extract_url_or_path(self, payload):
        if payload is None:
            return None

        if isinstance(payload, str):
            return payload

        if isinstance(payload, dict):
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            for key in ("url", "file_url", "download_url", "path", "file"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    def _extract_local_source(self, file_meta):
        candidates = [
            file_meta.get("path"),
            file_meta.get("file_path"),
            file_meta.get("local_path"),
            file_meta.get("temp_path"),
            file_meta.get("file"),
        ]
        for value in candidates:
            if isinstance(value, str) and value and os.path.exists(value):
                return value
        return None

    def _extract_remote_url(self, file_meta):
        candidates = [
            file_meta.get("url"),
            file_meta.get("uri"),
            file_meta.get("file"),
            file_meta.get("path"),
        ]
        for value in candidates:
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
        return None

    def _safe_component_attrs(self, component):
        attrs = {}
        for key in dir(component):
            if key.startswith("_"):
                continue
            if key in {"get_file"}:
                attrs[key] = "<callable>"
                continue
            try:
                value = getattr(component, key)
            except Exception:
                continue
            if callable(value):
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                attrs[key] = value
        return attrs

    def _download_url(self, url: str):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                return response.read()
        except Exception as exc:
            logger.warning(f"[E2B] Failed to download {url}: {exc}")
            return None

    async def _read_sandbox_file_bytes(self, sandbox, remote_path: str):
        command = (
            "python - <<'PY'\n"
            "import base64\n"
            "from pathlib import Path\n"
            f"path = Path({json.dumps(remote_path, ensure_ascii=False)})\n"
            "print(base64.b64encode(path.read_bytes()).decode('ascii'))\n"
            "PY"
        )
        result = await sandbox.commands.run(command, timeout=30)
        exit_code = getattr(result, "exit_code", 0)
        if exit_code not in (0, None):
            stderr_text = getattr(result, "stderr", "") or getattr(result, "stdout", "")
            raise RuntimeError(f"Failed to read sandbox file: {stderr_text}".strip())

        stdout = getattr(result, "stdout", "") or ""
        if isinstance(stdout, list):
            stdout = "".join(stdout)
        encoded = str(stdout).strip()
        if not encoded:
            return b""
        return py_base64.b64decode(encoded)

    def _read_local_file(self, path: str):
        with open(path, "rb") as file_obj:
            return file_obj.read()

    def _is_valid_generated_file(self, file_name: str, content: bytes) -> bool:
        lower_name = file_name.lower()
        zip_like_suffixes = (
            ".xlsx",
            ".xlsm",
            ".xltx",
            ".xltm",
            ".docx",
            ".pptx",
            ".zip",
        )
        if not lower_name.endswith(zip_like_suffixes):
            return True

        try:
            with zipfile.ZipFile(BytesIO(content)) as zip_file:
                bad_member = zip_file.testzip()
                if bad_member is not None:
                    logger.warning(
                        f"[E2B] Zip validation failed for {file_name}: corrupted member {bad_member}"
                    )
                    return False
                return True
        except zipfile.BadZipFile:
            logger.warning(f"[E2B] Zip validation failed for {file_name}: bad zip container")
            return False
        except Exception as exc:
            logger.warning(f"[E2B] Zip validation failed for {file_name}: {exc}")
            return False

    async def _send_local_file(self, event: AstrMessageEvent, path_obj: Path):
        await asyncio.sleep(0.2)
        await event.send(
            event.chain_result([Comp.File(file=str(path_obj.resolve()), name=path_obj.name)])
        )
        logger.info(f"[E2B] Exported file sent successfully: {path_obj.name}")

    def _write_export_file(self, file_name: str, content: bytes):
        export_dir = self._get_export_dir()
        export_dir.mkdir(parents=True, exist_ok=True)

        safe_name = self._sanitize_filename(file_name)
        target = export_dir / safe_name
        stem = target.stem
        suffix = target.suffix
        index = 1
        while target.exists():
            target = export_dir / f"{stem}_{index}{suffix}"
            index += 1

        try:
            with open(target, "wb") as file_obj:
                file_obj.write(content)
            return target
        except Exception as exc:
            logger.error(f"[E2B] Failed to write export file {safe_name}: {exc}")
            return None

    def _cleanup_export_cache(self):
        export_dir = self._get_export_dir()
        if not export_dir.exists():
            return

        retention_hours = self._safe_int(
            self.config.get("file_retention_hours"),
            DEFAULT_FILE_RETENTION_HOURS,
            minimum=1,
            maximum=168,
        )
        cutoff = time.time() - retention_hours * 3600

        for path in export_dir.iterdir():
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                    continue
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except Exception as exc:
                logger.warning(f"[E2B] Failed to cleanup export cache {path}: {exc}")

    def _cleanup_session_cache(self):
        now = time.time()
        cutoff = now - DEFAULT_SESSION_RETENTION_HOURS * 3600
        expired_session_ids = [
            session_id
            for session_id, last_access in self.session_last_access.items()
            if last_access < cutoff
        ]

        for session_id in expired_session_ids:
            self.session_last_access.pop(session_id, None)
            self.code_hashes.pop(session_id, None)
            self.session_files.pop(session_id, None)
            self.generated_files.pop(session_id, None)
            self.sent_file_signatures.pop(session_id, None)
            logger.info(f"[E2B] Cleaned expired session cache: {session_id}")

    def _mark_session_active(self, event: AstrMessageEvent):
        session_id = self._get_session_id(event)
        self.session_last_access[session_id] = time.time()
        if len(self.session_last_access) % 20 == 0:
            self._cleanup_session_cache()

    def _get_export_dir(self):
        return Path(__file__).resolve().parent / DEFAULT_EXPORT_DIRNAME

    def _sanitize_filename(self, file_name: str):
        name = os.path.basename(file_name).strip() or "exported_file"
        return re.sub(r'[<>:"/\\\\|?*]', "_", name)

    def _basename(self, file_name: str):
        return os.path.basename(file_name.replace("\\", "/"))

    def _parse_int_output(self, command_result):
        stdout = getattr(command_result, "stdout", "") or ""
        if isinstance(stdout, list):
            stdout = "".join(stdout)
        try:
            return int(str(stdout).strip())
        except (TypeError, ValueError):
            return None

    def _get_session_id(self, event: AstrMessageEvent):
        return getattr(event, "session_id", event.get_sender_id())

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


def shlex_quote(value: str):
    return "'" + str(value).replace("'", "'\"'\"'") + "'"
