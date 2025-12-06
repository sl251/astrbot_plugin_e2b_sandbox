import traceback
import asyncio
from astrbot.api.all import *
from astrbot.api.event import filter 
from e2b_code_interpreter import AsyncSandbox

class Main(Star):
    """E2B 云沙箱执行 Python 代码插件"""

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}

    @filter.llm_tool(name="run_python_code")
    async def run_python_code(self, event: AstrMessageEvent, code: str):
        """在云沙箱中执行 Python 代码

        Args:
            code (string): 要执行的 Python 代码
        """
        code_stripped = code.strip()
        sender_id = event.get_sender_id()
        result = ""

        # 1. 检查 API Key
        api_key = self.config.get("e2b_api_key", "")
        if not api_key:
            yield event.plain_result("❌ 错误：E2B API Key 未配置")
            event.stop_event()
            return

        timeout = self.config.get("timeout", 30)
        max_output_length = self.config.get("max_output_length", 2000)

        sandbox = None # 初始化变量，确保 finally 能访问
        stdout_output = []
        stderr_output = []
        
        # 2. 内存安全机制：实时统计长度
        current_len = 0
        is_truncated = False

        def on_stdout(msg):
            nonlocal current_len, is_truncated
            msg_str = str(msg)
            if current_len < max_output_length:
                stdout_output.append(msg_str)
                current_len += len(msg_str)
            else:
                is_truncated = True

        def on_stderr(msg):
            nonlocal current_len, is_truncated
            msg_str = str(msg)
            if current_len < max_output_length:
                stderr_output.append(msg_str)
                current_len += len(msg_str)
            else:
                is_truncated = True

        try:
            logger.info(f"[E2B] 用户 {sender_id} 正在创建沙箱...")
            
            # 3. 恢复使用 create() 方法，但在外层包裹 try...finally 确保 kill
            sandbox = await asyncio.wait_for(
                AsyncSandbox.create(api_key=api_key),
                timeout=10
            )
            
            logger.info(f"[E2B] 沙箱创建成功，开始执行...")

            # 执行代码，设置超时
            execution = await asyncio.wait_for(
                sandbox.run_code(code_stripped, on_stdout=on_stdout, on_stderr=on_stderr),
                timeout=timeout
            )
            logger.info(f"[E2B] 执行完成")

            # 4. 结果处理
            result_parts = []
            if stdout_output:
                result_parts.append("📤 输出:\n" + "".join(stdout_output))

            if execution.error:
                error_name = getattr(execution.error, 'name', '未知错误')
                error_value = getattr(execution.error, 'value', '')
                result_parts.append("❌ 执行错误: " + str(error_name) + ": " + str(error_value))

            if stderr_output:
                result_parts.append("⚠️ 警告输出:\n" + "".join(stderr_output))

            if not result_parts:
                result = "✅ 代码执行成功，无输出。"
            else:
                result = "\n\n".join(result_parts)

            if is_truncated:
                result += f"\n\n... (输出过长，已截断)"

        except asyncio.TimeoutError:
            result = f"❌ 代码执行超时（超过 {timeout} 秒）"
        except Exception as e:
            logger.error(f"[E2B] 执行异常: {traceback.format_exc()}")
            result = f"❌ 执行出错: {str(e)}"
        finally:
            # 5. 资源清理：手动 kill 沙箱
            if sandbox:
                try:
                    await sandbox.kill()
                    logger.info(f"[E2B] 沙箱已清理")
                except Exception as cleanup_err:
                    logger.warning(f"[E2B] 沙箱清理失败: {cleanup_err}")

        # 6. 返回结果
        yield event.plain_result(result)
        event.stop_event()
