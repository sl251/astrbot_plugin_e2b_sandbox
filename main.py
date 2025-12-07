import re
import traceback
import asyncio
import base64
import tempfile
import os

from astrbot.api import logger, star
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Image, Plain

# E2B 兼容性导入
try:
    from e2b_code_interpreter import AsyncSandbox
except ImportError:
    try:
        from e2b import AsyncSandbox
    except ImportError:
        AsyncSandbox = None

class Main(star.Star):
    """E2B 云沙箱执行 Python 代码插件"""

    def __init__(self, context: star.Context, config=None):
        super().__init__(context)
        self.config = config or {}

    @filter.llm_tool(name="execute_python_code")
    async def execute_python_code(self, event: AstrMessageEvent, code: str = None, **kwargs) -> str:
        """在云沙箱中执行 Python 代码。
        Args:
            code (str): 要执行的 Python 代码。
        """
        # --- 1. 参数防御 ---
        if code is None: code = kwargs.get('code')
        if code is None: return "❌ 系统错误：未接收到代码参数。"
        if AsyncSandbox is None: return "❌ 严重错误：未找到 AsyncSandbox 类。"

        match = re.search(r"```(?:python)?\s*(.*?)```", code, re.DOTALL | re.IGNORECASE)
        code_to_run = match.group(1).strip() if match else code.strip()
        
        api_key = self.config.get("e2b_api_key", "")
        if not api_key: return "❌ 错误：E2B API Key 未配置。"
        timeout = self.config.get("timeout", 30)
        
        sandbox = None
        
        try:
            # 💡 提示用户正在运行（消除等待焦虑）
            # await event.send(event.plain_result("🚀 正在云端执行代码..."))
            logger.info(f"[E2B] 开始连接沙箱...")
            
            # --- 2. 创建沙箱 & 执行 ---
            try:
                sandbox = await asyncio.wait_for(AsyncSandbox.create(api_key=api_key), timeout=15)
            except asyncio.TimeoutError:
                return "❌ 连接 E2B 服务器超时 (Check Network/API Key)."
            
            execution = None
            if hasattr(sandbox, 'run_code'):
                execution = await asyncio.wait_for(sandbox.run_code(code_to_run), timeout=timeout)
            elif hasattr(sandbox, 'notebook') and hasattr(sandbox.notebook, 'exec_cell'):
                execution = await asyncio.wait_for(sandbox.notebook.exec_cell(code_to_run), timeout=timeout)
            else:
                return "❌ SDK 错误：找不到执行方法"

            # --- 3. 插件直接接管输出 (不依赖 LLM) ---
            
            # 3.1 处理图片 (只发一张，避免重复)
            has_sent_image = False
            if execution.results:
                for res in execution.results:
                    if has_sent_image: break 

                    img_data = None
                    img_ext = ""
                    if hasattr(res, 'png') and res.png: img_data = res.png; img_ext = ".png"
                    elif hasattr(res, 'jpeg') and res.jpeg: img_data = res.jpeg; img_ext = ".jpg"
                    elif hasattr(res, 'formats'): 
                        if 'png' in res.formats: img_data = res.formats['png']; img_ext = ".png"
                        elif 'jpeg' in res.formats: img_data = res.formats['jpeg']; img_ext = ".jpg"

                    if img_data:
                        try:
                            img_bytes = base64.b64decode(img_data)
                            with tempfile.NamedTemporaryFile(suffix=img_ext, delete=False) as tmp_file:
                                tmp_file.write(img_bytes)
                                tmp_path = tmp_file.name
                            
                            # 直接发送图片
                            chain = [Image.fromFileSystem(tmp_path)]
                            await event.send(event.chain_result(chain))
                            
                            has_sent_image = True
                            if os.path.exists(tmp_path): os.remove(tmp_path)
                        except Exception as e:
                            logger.error(f"发图失败: {e}")

            # 3.2 处理文字日志 (插件自己发，防止 LLM 复读)
            logs_text = ""
            if hasattr(execution, 'logs'):
                parts = []
                if execution.logs.stdout: parts.append("".join(execution.logs.stdout))
                if execution.logs.stderr: parts.append("".join(execution.logs.stderr))
                logs_text = "\n".join(parts).strip()

            if logs_text:
                # 只有当日志不为空时才发
                if len(logs_text) > 1200:
                    logs_text = logs_text[:1200] + "\n...(Output Truncated)"
                try:
                    await event.send(event.plain_result(f"📝 运行输出:\n{logs_text}"))
                except: pass
            elif not has_sent_image:
                # 既没图也没字，发个提示
                await event.send(event.plain_result("✅ 代码执行完成 (无可见输出)"))

            # --- 4. 关键：给 LLM 一个闭嘴指令 ---
            # 我们不使用 stop_event (会卡UI)，也不返回 log (会重复)
            # 我们返回一个指令，强迫 LLM 结束对话。
            
            return (
                "SYSTEM: The code execution result (images/logs) has already been sent to the user directly by the plugin.\n"
                "SYSTEM: Your task is complete. DO NOT repeat the output.\n"
                "SYSTEM: Please reply with a single emoji '✅' to confirm completion."
            )

        except asyncio.TimeoutError:
            return f"❌ Execution timed out (>{timeout}s)."
        except Exception as e:
            return f"❌ System Error: {str(e)}"
        finally:
            if sandbox:
                try:
                    if hasattr(sandbox, 'kill'): await sandbox.kill()
                    elif hasattr(sandbox, 'close'): await sandbox.close()
                except Exception: pass
