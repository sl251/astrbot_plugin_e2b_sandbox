import re  # 新增：用于正则提取代码
import traceback
import asyncio
# 保持正确的导入路径
from astrbot.api import logger, star
from astrbot.api.event import filter, AstrMessageEvent
from e2b_code_interpreter import AsyncSandbox

class Main(star.Star):
    """E2B 云沙箱执行 Python 代码插件"""

    def __init__(self, context: star.Context, config=None):
        super().__init__(context)
        self.config = config or {}

    @filter.llm_tool(name="run_python_code")
    async def run_python_code(self, event: AstrMessageEvent, code: str):
        """在云沙箱中执行 Python 代码

        Args:
            code (string): 要执行的 Python 代码
        """
        # 1. 初始化 result
        result = "执行初始化中..."
        
        # 2. 增强版 Markdown 清理逻辑 (使用正则)
        # 无论代码在回复的中间、开头还是结尾，都能提取出来
        # 匹配 ```python ... ``` 或 ``` ... ```，re.DOTALL 让 . 能匹配换行符
        match = re.search(r"```(?:python)?\s*(.*?)```", code, re.DOTALL | re.IGNORECASE)
        
        if match:
            # 如果匹配到了代码块，提取中间的内容
            code_to_run = match.group(1).strip()
        else:
            # 如果没匹配到，假设整个输入就是代码 (或者 LLM 没用 markdown)
            code_to_run = code.strip()

        sender_id = event.get_sender_id()

        # 3. 检查 API Key
        api_key = self.config.get("e2b_api_key", "")
        if not api_key:
            yield event.plain_result("❌ 错误：E2B API Key 未配置")
            event.stop_event()
            return

        timeout = self.config.get("timeout", 30)
        max_output_length = self.config.get("max_output_length", 2000)

        # 资源变量初始化
        sandbox = None 
        stdout_output = []
        stderr_output = []
        
        # 长度控制
        current_len = 0
        is_truncated = False

        # 日志收集辅助函数
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
                    code_to_run,  # 使用处理后的代码
                    on_stdout=lambda m: append_log(m, stdout_output), 
                    on_stderr=lambda m: append_log(m, stderr_output)
                ),
                timeout=timeout
            )
            logger.info(f"[E2B] 执行完成")

            # 结果拼接
            result_parts = []
            if stdout_output:
                result_parts.append("📤 Output:\n" + "".join(stdout_output))

            if execution.error:
                error_name = getattr(execution.error, 'name', '未知错误')
                error_value = getattr(execution.error, 'value', '')
                result_parts.append("❌ Error: " + str(error_name) + ": " + str(error_value))

            if stderr_output:
                result_parts.append("⚠️ Stderr:\n" + "".join(stderr_output))
            
            if execution.results:
                 result_parts.append(f"📈 Results: {str(execution.results)}")

            if not result_parts:
                result = "✅ Code executed successfully (No output)."
            else:
                result = "\n\n".join(result_parts)

            if is_truncated:
                result += f"\n\n... (Output truncated at {max_output_length} chars)"

        except asyncio.TimeoutError:
            result = f"❌ Execution timed out (>{timeout}s)."
        except Exception as e:
            logger.error(f"[E2B] 执行异常: {traceback.format_exc()}")
            result = f"❌ System Error: {str(e)}"
        finally:
            # 资源清理
            if sandbox:
                try:
                    await sandbox.kill()
                    logger.info(f"[E2B] 沙箱已清理")
                except Exception as cleanup_err:
                    logger.warning(f"[E2B] 沙箱清理失败: {cleanup_err}")

        # 4. 直接 yield 给用户
        yield event.plain_result(result)
        event.stop_event()
