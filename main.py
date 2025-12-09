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
        self.code_hashes = defaultdict(str)

    @filter.llm_tool(name="run_python_code")
    async def run_python_code(self, event: AstrMessageEvent, code: str = None, **kwargs):
        """在云沙箱中执行 Python 代码。
        
        【重要能力说明】
        1. **无状态环境**：每次调用都是全新的环境。
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

        # 防重复调用
        session_id = getattr(event, "session_id", event.get_sender_id())
        current_hash = hashlib.md5(code_to_run.encode('utf-8')).hexdigest()
        
        if self.code_hashes[session_id] == current_hash:
            logger.warning(f"[E2B] 拦截到会话 {session_id} 的重复代码调用")
            return "⚠️ SYSTEM WARNING: Duplicate code execution intercepted."
        self.code_hashes[session_id] = current_hash

        api_key = self.config.get("e2b_api_key", "")
        if not api_key:
            return "❌ Error: E2B API Key is missing."
        if AsyncSandbox is None:
            return "❌ Error: AsyncSandbox class not found."

        # 客户端等待时间
        exec_timeout = self.config.get("timeout", 60)
        sandbox_lifespan = exec_timeout + 30 

        sandbox = None 
        llm_feedback = []

        try:
            logger.info(f"[E2B] Session {session_id} creating sandbox...")
            
            sandbox = await asyncio.wait_for(
                AsyncSandbox.create(
                    api_key=api_key,
                    timeout=sandbox_lifespan 
                ),
                timeout=15
            )

            # --- 自动检测并安装依赖 ---
            libs_to_install = []
            # 移除了巨型库，只保留核心库
            common_libs = [
                'matplotlib', 'numpy', 'pandas', 
                'requests', 'bs4', 'wordcloud', 'jieba', 'seaborn'
            ]
            for lib in common_libs:
                if re.search(rf'\b{lib}\b', code_to_run):
                    libs_to_install.append(lib)
            
            if re.search(r'\bplt\b', code_to_run) and 'matplotlib' not in libs_to_install:
                libs_to_install.append('matplotlib')

            if libs_to_install:
                install_cmd = f"pip install {' '.join(libs_to_install)}"
                logger.info(f"[E2B] Auto-installing dependencies: {libs_to_install}")
                await sandbox.commands.run(install_cmd, timeout=120)

            # --- 注入中文字体与后端配置 ---
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
            os.system('curl -L -o /tmp/SimHei.ttf https://github.com/StellarCN/scp_zh/raw/master/fonts/SimHei.ttf')
        except: pass
            
    if os.path.exists(font_path):
        try:
            fm.fontManager.addfont(font_path)
            plt.rcParams['font.sans-serif'] = ['SimHei']
            plt.rcParams['axes.unicode_minus'] = False
        except: pass

try:
    _configure_font()
except: pass
"""
            full_code = setup_code + "\n" + code_to_run

            logger.info(f"[E2B] Running user code...")
            execution = await asyncio.wait_for(
                sandbox.run_code(full_code),
                timeout=exec_timeout
            )
            logger.info(f"[E2B] Execution finished.")

            # --- 结果处理 (修复 TypeError) ---
            has_sent_image = False
            if execution.results:
                for res in execution.results:
                    if has_sent_image: break 
                    img_data = None
                    img_ext = ""

                    # 1. 优先检查直接属性 (E2B SDK v1.x 标准)
                    if hasattr(res, 'png') and res.png:
                        img_data = res.png; img_ext = ".png"
                    elif hasattr(res, 'jpeg') and res.jpeg:
                        img_data = res.jpeg; img_ext = ".jpg"
                    
                    # 2. 兼容性检查 formats (修复报错的关键点)
                    elif hasattr(res, 'formats'): 
                        # 如果是方法则调用，如果是属性则直接用
                        formats_data = res.formats() if callable(res.formats) else res.formats
                        
                        # 确保拿到的是字典再操作
                        if isinstance(formats_data, dict):
                            if 'png' in formats_data: 
                                img_data = formats_data['png']; img_ext = ".png"
                            elif 'jpeg' in formats_data: 
                                img_data = formats_data['jpeg']; img_ext = ".jpg"

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

            if hasattr(execution, 'logs'):
                if execution.logs.stdout:
                    llm_feedback.append(f"📤 STDOUT:\n{''.join(execution.logs.stdout)}")
                if execution.logs.stderr:
                    llm_feedback.append(f"⚠️ STDERR:\n{''.join(execution.logs.stderr)}")
            
            result_text = "\n\n".join(llm_feedback)
            if not result_text: result_text = "✅ Code executed successfully (No visible output)."
            if len(result_text) > 3000: result_text = result_text[:3000] + "\n...(Output truncated)"

            final_return = (
                f"{result_text}\n\n"
                "--------------------------------------------------\n"
                "[SYSTEM COMMAND: Execution Complete. \n"
                "1. If an image was generated, it has been delivered.\n"
                "2. DO NOT retry or run the code again.\n"
                "3. Please explain the result to the user now.]"
            )
            return final_return

        except asyncio.CancelledError:
            logger.warning("[E2B] Task cancelled by AstrBot Core (Timeout). Cleaning up sandbox...")
            raise 
        except asyncio.TimeoutError:
            return f"❌ Execution timed out (>{exec_timeout}s). Installing libraries might take time."
        except Exception as e:
            logger.error(f"[E2B] Execution Exception: {traceback.format_exc()}")
            return f"❌ Runtime Error: {str(e)}"
        finally:
            if sandbox:
                try:
                    await asyncio.wait_for(sandbox.kill(), timeout=5)
                except: pass
