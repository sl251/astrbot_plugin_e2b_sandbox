import traceback
import asyncio
# 修正导入：
# 1. logger 直接从 api 导入
# 2. star 模块从 api 导入，通过 star.Star 使用
from astrbot.api import logger, star
from astrbot.api.event import filter, AstrMessageEvent
from e2b_code_interpreter import AsyncSandbox

# 使用 star.Star
class Main(star.Star):
    """E2B 云沙箱执行 Python 代码插件"""

    # 使用 star.Context
    def __init__(self, context: star.Context, config=None):
        super().__init__(context)
        self.config = config or {}

    @filter.llm_tool(name="run_python_code")
    async def run_python_code(self, event: AstrMessageEvent, code: str):
        """在云沙箱中执行 Python 代码

        Args:
            code (string): 要执行的 Python 代码
        """
        # 1. 初始化 result，防止 UnboundLocalError
        result = "初始化中..."
        
        code_stripped = code.strip()
        sender_id = event.get_sender_id()

        # 2. 检查 API Key
        api_key = self.config.get("e2b_api_key", "")
        if not api_key:
            yield event.plain_result("❌ 错误：E2B API Key 未配置")
            event.stop_event()
            return

        timeout = self.config.get("timeout", 30)
        max_output_length = self.config.get("max_output_length", 2000)

        # 3. 初始化资源变量
        sandbox = None
        stdout_output = []
        stderr_output = []
        
        # 4. 共享状态
        current_len = 0
        is_truncated = False

        # 5. 通用日志处理函数 (满足 DRY 原则)
        def append_log(msg, target_list):
            nonlocal current_len, is_truncated
            msg_str = str(msg)
            if current_len < max_output_length:
                target_list.append(msg_str)
                current_len += len(msg_str)
            else:
                is_truncated = True

        try:
            logger.info(f"[E2B] 用户 {sender_id} 正在创建沙箱...")
            
            # 创建沙箱
            sandbox = await asyncio.wait_for(
                AsyncSandbox.create(api_key=api_key),
                timeout=10
            )
            
            logger.info(f"[E2B] 沙箱创建成功，开始执行...")

            # 执行代码
            execution = await asyncio.wait_for(
                sandbox.run_code(
                    code_stripped, 
                    on_stdout=lambda m: append_log(m, stdout_output), 
                    on_stderr=lambda m: append_log(m, stderr_output)
                ),
                timeout=timeout
            )
            logger.info(f"[E2B] 执行完成")

            # 结果处理
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
                result += f"\n\n... (输出过长，已在 {max_output_length} 字符处截断)"

        except asyncio.TimeoutError:
            result = f"❌ 代码执行超时（超过 {timeout} 秒）"
        except Exception as e:
            logger.error(f"[E2B] 执行异常: {traceback.format_exc()}")
            result = f"❌ 执行出错: {str(e)}"
        finally:
            # 6. 资源清理
            if sandbox:
                try:
                    await sandbox.kill()
                    logger.info(f"[E2B] 沙箱已清理")
                except Exception as cleanup_err:
                    logger.warning(f"[E2B] 沙箱清理失败: {cleanup_err}")

        yield event.plain_result(result)
        event.stop_event()
