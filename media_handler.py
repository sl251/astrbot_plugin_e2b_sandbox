import asyncio
import base64
import hashlib
import inspect
import os
import posixpath
import re
import shutil
import tempfile
import time
import zipfile
from io import BytesIO
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image

from .file_transfer import download_http, read_limited
from .constants import (
    DEFAULT_EXPORT_DIRNAME,
    DEFAULT_FILE_RETENTION_HOURS,
    DEFAULT_MAX_RETURN_FILE_SIZE_MB,
    DEFAULT_MAX_UPLOAD_FILE_SIZE_MB,
    DEFAULT_UPLOAD_DIR,
    DEFAULT_WORK_DIR,
    MAX_GENERATED_FILE_CANDIDATES,
    MAX_SESSION_FILE_COUNT,
    SANDBOX_PATH_PATTERN,
)
from .utils import shlex_quote

class MediaMixin:
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
                    await asyncio.wait_for(
                        evt.send(evt.chain_result([Image.fromFileSystem(tmp_path)])), timeout=20,
                    )
                    logger.info("[E2B] Async image sent successfully.")
                    return True
                except Exception as exc:
                    logger.error(f"[E2B] Async image send failed: {type(exc).__name__}")
                    return False
                finally:
                    if tmp_path and os.path.exists(tmp_path):
                        try:
                            os.remove(tmp_path)
                        except Exception:
                            pass

            task = self._track_task(
                asyncio.create_task(send_image_task(img_bytes, img_ext, event)),
                self._inflight_tasks,
            )
            has_sent_image = await asyncio.shield(task)
            if has_sent_image:
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

        return [file_meta["name"] for file_meta in cached_files]

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
        message_id = getattr(getattr(event, "message_obj", None), "message_id", None)
        token = ("message", str(message_id)) if message_id is not None else ("event", event)
        same_event = self._file_event_ids.get(session_id) == token
        cached_files = self.session_files.get(session_id, [])
        cached_by_identity = {}
        cached_by_name = {}
        for item in cached_files:
            identity = item.get("file_id") or item.get("file") or item.get("url")
            if identity:
                cached_by_identity[(item.get("name"), str(identity))] = item
            cached_by_name[item.get("name")] = item
        merged = []
        for item in current_files:
            item = dict(item)
            identity = item.get("file_id") or item.get("file") or item.get("url")
            previous = cached_by_identity.get((item.get("name"), str(identity))) if identity else None
            if previous is None and same_event and not identity:
                previous = cached_by_name.get(item.get("name"))
            if previous and not item.get("path"):
                item["path"] = previous.get("path")
            merged.append(item)
        if not current_files and same_event:
            merged = list(cached_files)
        self.session_files[session_id] = merged[-MAX_SESSION_FILE_COUNT:]
        self._file_event_ids[session_id] = token
        return list(self.session_files[session_id])

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
        filename = posixpath.basename(raw_path)
        if filename in ("", ".", "..") or "\x00" in filename:
            raise ValueError("Invalid attachment filename")
        return posixpath.join(DEFAULT_UPLOAD_DIR, filename)

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
            if len(content) > max_bytes or len(content) != file_size:
                logger.warning(f"[E2B] Skip generated file {file_name}: size changed during download")
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
        return download_http(url, self._max_upload_bytes())

    def _max_upload_bytes(self):
        return self._safe_int(
            self.config.get("max_upload_file_size_mb"), DEFAULT_MAX_UPLOAD_FILE_SIZE_MB,
            minimum=1, maximum=100,
        ) * 1024 * 1024

    async def _read_sandbox_file_bytes(self, sandbox, remote_path: str):
        content = await asyncio.wait_for(
            sandbox.files.read(remote_path, format="bytes", request_timeout=30),
            timeout=30,
        )
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise TypeError("E2B files.read(format='bytes') returned non-binary content")
        return bytes(content)

    def _read_local_file(self, path: str):
        if not Path(path).is_file():
            raise ValueError("Attachment must be a regular file")
        with open(path, "rb") as file_obj:
            max_bytes = self._max_upload_bytes()
            if os.fstat(file_obj.fileno()).st_size > max_bytes:
                raise ValueError(f"Attachment exceeds the upload limit ({max_bytes} bytes).")
            return read_limited(file_obj, max_bytes)

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

    def _get_export_dir(self):
        return self._plugin_data_dir / DEFAULT_EXPORT_DIRNAME

    def _sanitize_filename(self, file_name: str):
        name = os.path.basename(file_name).strip() or "exported_file"
        return re.sub(r'[<>:"/\\\\|?*]', "_", name)

    def _basename(self, file_name: str):
        return os.path.basename(file_name.replace("\\", "/"))
