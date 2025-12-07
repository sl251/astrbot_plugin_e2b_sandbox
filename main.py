import re
import traceback
import asyncio
import base64
import tempfile
import os

from astrbot.api import logger, star
from astrbot.api.event import filter, AstrMessageEvent
# 关键修复：图片组件必须从这里导入，否则会报 has no attribute 'Image'
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

    # 1. 增加默认值和 kwargs，防止参数报错
    @filter.llm_tool(name="run_python_code")
    async def run_python_code(self, event: AstrMessageEvent, code: str = None, **kwargs):
        """在云沙箱中执行 Python 代码

        Args:
            code (string): 要执行的 Python 代码
        """
        # 参数防御逻辑
        if code is None:
            code = kwargs.get('code')
        
        # 如果依然没有代码，报错并结束
        if not code:
            yield event.plain_result("❌ 系统错误：未接收到代码参数。")
            event.stop_event()
            return

        # Markdown 清理
        match = re.search(r"```(?:python)?\s*(.*?)```", code, re.DOTALL | re.IGNORECASE)
        code_to_run = match.group(1).strip() if match else code.strip()

        sender_id = event.get_sender_id()
        api_key = self.config.get("e2b_api_key", "")
        if not api_key:
            yield event.plain_result("❌ 错误：E2B API Key 未配置")
            event.stop_event()
            return

        if AsyncSandbox is None:
            yield event.plain_result("❌ 严重错误：未找到 AsyncSandbox 类。")
            event.stop_event()
            return

        timeout = self.config.get("timeout", 30)
        sandbox = None 

        try:
            logger.info(f"[E2B] 用户 {sender_id} 正在创建沙箱...")
            
            # 创建沙箱
            sandbox = await asyncio.wait_for(
                AsyncSandbox.create(api_key=api_key),
                timeout=15
            )
            
            # 执行代码
            execution = await asyncio.wait_for(
                sandbox.run_code(code_to_run),
                timeout=timeout
            )
            logger.info(f"[E2B] 执行完成")

            # --- 结果处理 (直接 yield 输出) ---
            
            # 1. 优先处理图片
            has_sent_image = False
            if execution.results:
                for res in execution.results:
                    if has_sent_image: break # 避免重复发图

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
                            # 解码并保存临时文件
                            img_bytes = base64.b64decode(img_data)
                            with tempfile.NamedTemporaryFile(suffix=img_ext, delete=False) as tmp_file:
                                tmp_file.write(img_bytes)
                                tmp_path = tmp_file.name
                            
                            # 构建图片消息链
                            # 使用 yield 直接推送给用户
                            chain = [Image.fromFileSystem(tmp_path)]
                            yield event.chain_result(chain)
                            
                            has_sent_image = True
                            logger.info("[E2B] 图片已直接 yield 给用户")
                            
                            if os.path.exists(tmp_path): os.remove(tmp_path)
                        except Exception as e:
                            logger.error(f"发图失败: {e}")
                            yield event.plain_result(f"⚠️ 图片处理失败: {e}")

            # 2. 处理文字日志 (Stdout/Stderr)
            logs_output = []
            if hasattr(execution, 'logs'):
                if execution.logs.stdout:
                    logs_output.append("📤 Output:\n" + "".join(execution.logs.stdout))
                if execution.logs.stderr:
                    logs_output.append("⚠️ Stderr:\n" + "".join(execution.logs.stderr))
            
            # 拼接文字结果
            result_text = "\n\n".join(logs_output)
            
            # 如果有文字结果，yield 文字
            if result_text:
                if len(result_text) > 2000:
                    result_text = result_text[:2000] + "\n...(输出过长截断)"
                yield event.plain_result(result_text)
            
            # 如果既没图也没字
            if not has_sent_image and not result_text:
                yield event.plain_result("✅ 代码执行成功 (无可见输出)")

        except asyncio.TimeoutError:
            yield event.plain_result(f"❌ 执行超时 (>{timeout}s)")
        except Exception as e:
            logger.error(f"[E2B] 执行异常: {traceback.format_exc()}")
            yield event.plain_result(f"❌ 系统错误: {str(e)}")
        finally:
            if sandbox:
                try:
                    if hasattr(sandbox, 'kill'): await sandbox.kill()
                    elif hasattr(sandbox, 'close'): await sandbox.close()
                except Exception: pass

        # 3. 核心：强制停止事件
        # 这会直接切断 LLM 的后续处理，前端收到这个信号后应该停止 loading
        event.stop_event()
