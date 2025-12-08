import re
import traceback
import asyncio
import base64
import tempfile
import os
import hashlib
from collections import defaultdict

from astrbot.api import logger, star
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Image, Plain

# 尝试导入 E2B
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
        # 格式: {session_id: last_code_hash}
        self.code_hashes = defaultdict(str)

    @filter.llm_tool(name="run_python_code")
    async def run_python_code(self, event: AstrMessageEvent, code: str = None, **kwargs):
        """在云沙箱中执行 Python 代码。
        
        【重要能力说明】
        1. **无状态环境**：每次调用都是全新的环境，**不支持**跨轮次变量记忆。
        2. **支持绘图**：支持 matplotlib/PIL。
        3. **绘图规范**：必须将图片保存为文件（如 'plot.png'），**严禁**使用 plt.show()。
        4. 系统会自动检测并发送生成的图片。
        
        Args:
            code (string): 要执行的 Python 代码
        """
        if code is None:
            code = kwargs.get('code')
        
        if not code:
            return "❌ System Error: No code received."

        # Markdown 清理
        match = re.search(r"```(?:python)?\s*(.*?)```", code, re.DOTALL | re.IGNORECASE)
        code_to_run = match.group(1).strip() if match else code.strip()

        # --- 基于 Session ID 的防重复调用 ---
        session_id = getattr(event, "session_id", event.get_sender_id())
        current_hash = hashlib.md5(code_to_run.encode('utf-8')).hexdigest()
        if self.code_hashes[session_id] == current_hash:
            logger.warning(f"[E2B] 拦截到会话 {session_id} 的重复代码调用")
            return (
                "⚠️ SYSTEM WARNING: You have already executed this exact code just now. \n"
                "Do NOT run it again. The image has already been generated and sent to the user.\n"
                "Please formulate your final response to the user based on the previous execution."
            )
        self.code_hashes[session_id] = current_hash

        # --- 强制设置 Matplotlib 后端 ---
        setup_code = "import matplotlib\nmatplotlib.use('Agg')\nimport matplotlib.pyplot as plt\n"
        full_code = setup_code + code_to_run

        api_key = self.config.get("e2b_api_key", "")
        if not api_key:
            return "❌ Error: E2B API Key is missing."
        if AsyncSandbox is None:
            return "❌ Error: AsyncSandbox class not found."

        # 获取配置的超时时间
        exec_timeout = self.config.get("timeout", 30)
        
        # 【安全修复1】设置沙箱存活时间
        # idle_timeout: 如果沙箱在 X 秒内没有新操作，E2B 云端会自动销毁它。
        # 我们设置为 执行超时 + 30秒缓冲，确保即使插件崩溃，沙箱也会在1分钟左右自动销毁。
        sandbox_idle_timeout = exec_timeout + 30

        sandbox = None 
        llm_feedback = []

        try:
            logger.info(f"[E2B] Session {session_id} creating sandbox (Auto-kill in {sandbox_idle_timeout}s)...")
            
            # 创建沙箱
            sandbox = await asyncio.wait_for(
                AsyncSandbox.create(
                    api_key=api_key,
                    idle_timeout=sandbox_idle_timeout # <--- 关键参数：服务端自动销毁
                ),
                timeout=15
            )
            
            # 执行代码
            execution = await asyncio.wait_for(
                sandbox.run_code(full_code),
                timeout=exec_timeout
            )
            logger.info(f"[E2B] Execution finished.")

            # --- 结果处理 ---
            
            # 图片处理 (后台异步发送)
            has_sent_image = False
            if execution.results:
                for res in execution.results:
                    if has_sent_image: break 

                    img_data = None
                    img_ext = ""

                    if hasattr(res, 'png') and res.png:
                        img_data = res.png; img_ext = ".png"
                    elif hasattr(res, 'jpeg') and res.jpeg:
                        img_data = res.jpeg; img_ext = ".jpg"
                    elif hasattr(res, 'formats'): 
                        if 'png' in res.formats: img_data = res.formats['png']; img_ext = ".png"
                        elif 'jpeg' in res.formats: img_data = res.formats['jpeg']; img_ext = ".jpg"

                    if img_data:
                        try:
                            img_bytes = base64.b64decode(img_data)
                            
                            async def send_image_task(data, ext, evt):
                                tmp_path = None
                                try:
                                    await asyncio.sleep(0.5)
                                    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp_file:
                                        tmp_file.write(data)
                                        tmp_path = tmp_file.name
                                    
                                    chain = [Image.fromFileSystem(tmp_path)]
                                    await evt.send(evt.chain_result(chain))
                                    logger.info("[E2B] Async image sent successfully.")
                                    
                                except Exception as inner_e:
                                    logger.error(f"[E2B] Async image send failed: {inner_e}")
                                finally:
                                    if tmp_path and os.path.exists(tmp_path):
                                        try: os.remove(tmp_path)
                                        except: pass

                            asyncio.create_task(send_image_task(img_bytes, img_ext, event))
                            
                            has_sent_image = True
                            llm_feedback.append("[System Notification: Image generated successfully and sent to user interface.]")
                            
                        except Exception as e:
                            logger.error(f"Image preparation failed: {e}")
                            llm_feedback.append(f"[System Error: Image generation failed: {e}]")

            # 文字日志
            if hasattr(execution, 'logs'):
                if execution.logs.stdout:
                    llm_feedback.append(f"📤 STDOUT:\n{''.join(execution.logs.stdout)}")
                if execution.logs.stderr:
                    llm_feedback.append(f"⚠️ STDERR:\n{''.join(execution.logs.stderr)}")
            
            result_text = "\n\n".join(llm_feedback)
            if not result_text:
                result_text = "✅ Code executed successfully (No visible output)."
            
            if len(result_text) > 3000:
                result_text = result_text[:3000] + "\n...(Output truncated)"

            final_return = (
                f"{result_text}\n\n"
                "--------------------------------------------------\n"
                "[SYSTEM COMMAND: Execution Complete. \n"
                "1. If an image was generated, it has been delivered.\n"
                "2. DO NOT retry or run the code again.\n"
                "3. Please explain the result to the user now.]"
            )

            return final_return

        except asyncio.TimeoutError:
            return f"❌ Execution timed out (>{exec_timeout}s)."
        except Exception as e:
            logger.error(f"[E2B] Execution Exception: {traceback.format_exc()}")
            return f"❌ Runtime Error: {str(e)}"
        finally:
            # 【安全修复2】更健壮的资源清理逻辑
            if sandbox:
                try:
                    logger.info("[E2B] Cleaning up sandbox...")
                    # 强制在 5 秒内完成关闭，防止 kill 本身卡死导致 finally 块无法结束
                    await asyncio.wait_for(sandbox.kill(), timeout=5)
                except asyncio.TimeoutError:
                    logger.warning("[E2B] Sandbox kill timed out (Server will auto-kill via idle_timeout).")
                except Exception as close_e:
                    logger.warning(f"[E2B] Failed to kill sandbox explicitly: {close_e}")
