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
        # 记录每个会话的代码哈希，防止 LLM 短时间内重复调用同一段代码
        self.code_hashes = defaultdict(str)

    @filter.llm_tool(name="run_python_code")
    async def run_python_code(self, event: AstrMessageEvent, code: str = None, **kwargs):
        """在云沙箱中执行 Python 代码。
        
        【重要能力说明】
        1. **无状态环境**：每次调用都是全新的环境，不支持跨轮次变量记忆。
        2. **支持绘图**：支持 matplotlib/PIL。
        3. **绘图规范**：必须将图片保存为文件（如 'plot.png'），**严禁**使用 plt.show()。
        4. 系统会自动检测并发送生成的图片。
        
        Args:
            code (string): 要执行的 Python 代码
        """
        # 1. 参数获取与校验
        if code is None:
            code = kwargs.get('code')
        
        if not code:
            return "❌ System Error: No code received."

        # 2. Markdown 清理
        match = re.search(r"```(?:python)?\s*(.*?)```", code, re.DOTALL | re.IGNORECASE)
        code_to_run = match.group(1).strip() if match else code.strip()

        # 3. 防死循环机制 (Session 隔离)
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

        # 4. 配置检查
        api_key = self.config.get("e2b_api_key", "")
        if not api_key:
            return "❌ Error: E2B API Key is missing."
        if AsyncSandbox is None:
            return "❌ Error: AsyncSandbox class not found. Please pip install e2b-code-interpreter"

        # 5. 超时设置
        # exec_timeout: 客户端等待代码执行的最大时间 (默认 60s，给安装库留出时间)
        exec_timeout = self.config.get("timeout", 60)
        
        # sandbox_lifespan: 沙箱在服务端的最大存活时间
        # 设置为比执行超时稍长一点，确保即使插件崩溃，沙箱也会在约 90秒后自动销毁，而不是默认的 5分钟
        sandbox_lifespan = exec_timeout + 30 

        sandbox = None 
        llm_feedback = []

        try:
            logger.info(f"[E2B] Session {session_id} creating sandbox (Auto-kill in {sandbox_lifespan}s)...")
            
            # 创建沙箱
            sandbox = await asyncio.wait_for(
                AsyncSandbox.create(
                    api_key=api_key,
                    # 【关键修正】新版 SDK 使用 'timeout' 参数控制沙箱存活时间
                    # 这不是代码执行超时，而是沙箱本身的生命周期倒计时
                    timeout=sandbox_lifespan 
                ),
                timeout=15
            )

            # --- 自动检测并安装依赖 ---
            # E2B 基础环境很纯净，需要手动 pip install
            libs_to_install = []
            # 常见数据科学库检测
            common_libs = [
                'matplotlib', 'numpy', 'pandas', 'scipy', 'sklearn', 
                'requests', 'bs4', 'wordcloud', 'jieba', 'seaborn'
            ]
            for lib in common_libs:
                # 简单检测：如果代码里 import 了这个库
                if re.search(rf'\b{lib}\b', code_to_run):
                    libs_to_install.append(lib)
            
            # 特殊处理：plt -> matplotlib
            if re.search(r'\bplt\b', code_to_run) and 'matplotlib' not in libs_to_install:
                libs_to_install.append('matplotlib')

            if libs_to_install:
                install_cmd = f"pip install {' '.join(libs_to_install)}"
                logger.info(f"[E2B] Auto-installing dependencies: {libs_to_install}")
                # 安装库不计入代码执行结果，但需要给足时间
                await sandbox.commands.run(install_cmd, timeout=120)

            # --- 注入中文字体与后端配置 ---
            # 1. 强制 Agg 后端防止卡死
            # 2. 下载并配置 SimHei 字体防止中文乱码
            setup_code = """
import os
import matplotlib
matplotlib.use('Agg') # 强制非交互模式
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

def _configure_font():
    # 字体缓存路径
    font_path = '/tmp/SimHei.ttf'
    # 如果没有字体，从 GitHub 镜像下载
    if not os.path.exists(font_path):
        try:
            # 使用 curl 下载字体 (E2B 环境通常有 curl)
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

            # 6. 执行用户代码
            logger.info(f"[E2B] Running user code...")
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
                            
                            # 定义后台发送任务
                            async def send_image_task(data, ext, evt):
                                tmp_path = None
                                try:
                                    # 避让主流程，防止状态冲突
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
            
            # 截断防止 Token 溢出
            if len(result_text) > 3000:
                result_text = result_text[:3000] + "\n...(Output truncated)"

            # 构造最终 Prompt，强制停止工具循环
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
            return f"❌ Execution timed out (>{exec_timeout}s). Installing libraries might take time."
        except Exception as e:
            logger.error(f"[E2B] Execution Exception: {traceback.format_exc()}")
            return f"❌ Runtime Error: {str(e)}"
        finally:
            if sandbox:
                try:
                    # 强制在 5 秒内关闭，防止卡死
                    await asyncio.wait_for(sandbox.kill(), timeout=5)
                except: pass
