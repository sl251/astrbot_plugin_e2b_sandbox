import traceback
import asyncio
from astrbot.api import llm_tool, logger, star
from astrbot.api.event import AstrMessageEvent
from e2b_code_interpreter import AsyncSandbox


@star.register("e2b_sandbox", "sl251", "使用 E2B 云沙箱安全执行 Python 代码", "1.0.3", "https://github.com/sl251/astrbot_plugin_e2b_sandbox")
class Main(star.Star):
    """E2B 云沙箱执行 Python 代码插件"""

    def __init__(self, context: star.Context, config=None):
        super().__init__(context)
        self.config = config or {}

    @llm_tool(name="run_python_code")
    async def run_python_code(self, event: AstrMessageEvent, code: str):
        """在云沙箱中执行 Python 代码

        Args:
            code (string): 要执行的 Python 代码
        """
        code_stripped = code.strip()
        sender_id = event.get_sender_id()
        result = ""

        logger.info(f"[E2B] 用户 {sender_id} 开始执行代码（长度: {len(code)} 字符）")

        api_key = self.config.get("e2b_api_key", "")
        if not api_key:
            logger.error("[E2B] API Key 未配置")
            result = "❌ 错误：E2B API Key 未配置，请在插件配置中设置"
            yield event.plain_result(result)
            event.stop_event()
            return

        timeout = self.config.get("timeout", 30)
        max_output_length = self.config.get("max_output_length", 2000)

        sandbox = None
        stdout_output = []
        stderr_output = []

        def on_stdout(msg):
            stdout_output.append(str(msg))

        def on_stderr(msg):
            stderr_output.append(str(msg))

        try:
            logger.info(f"[E2B] 用户 {sender_id} 正在创建沙箱...")
            sandbox = await asyncio.wait_for(
                AsyncSandbox.create(api_key=api_key),
                timeout=10
            )
            logger.info(f"[E2B] 用户 {sender_id} 沙箱创建成功，开始执行代码...")

            execution = await asyncio.wait_for(
                sandbox.run_code(code_stripped, on_stdout=on_stdout, on_stderr=on_stderr),
                timeout=timeout + 5
            )
            logger.info(f"[E2B] 用户 {sender_id} 代码执行完成")

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

            if len(result) > max_output_length:
                result = result[:max_output_length] + "\n...   （已截断）"

        except asyncio.TimeoutError:
            logger.error(f"[E2B] 用户 {sender_id} 代码执行超时（超过 {timeout} 秒）")
            result = "❌ 代码执行超时（" + str(timeout) + "秒）"
        except Exception as e:
            logger.error(f"[E2B] 用户 {sender_id} 执行错误: {type(e).__name__}: {e}")
            logger.error(f"[E2B] 错误堆栈:\n{traceback.format_exc()}")
            result = "❌ 代码执行失败: " + str(e)
        finally:
            if sandbox:
                try:
                    await sandbox.kill()
                    logger.info(f"[E2B] 用户 {sender_id} 沙箱已关闭")
                except Exception as cleanup_error:
                    logger.warning(f"[E2B] 用户 {sender_id} 沙箱关闭异常: {str(cleanup_error)}")

        logger.info(f"[E2B] 用户 {sender_id} 返回执行结果给用户，终止事件传播")
        yield event.plain_result(result)
        event.stop_event()
