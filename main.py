from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent
from astrbot.api import llm_tool, logger
from e2b_code_interpreter import AsyncSandbox


@register("e2b_sandbox", "sl251", "使用 E2B 云沙箱执行 Python 代码", "1.0.0")
class E2BSandboxPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.config = context.get_config()
    
    @llm_tool(name="run_python_code")
    async def run_python_code(self, event: AstrMessageEvent, code: str):
        '''在安全的云沙箱环境中执行 Python 代码。当用户需要运行、测试或调试 Python 代码时使用此工具。

        Args:
            code(string): 要执行的 Python 代码
        '''
        api_key = self.config.get("e2b_api_key", "")
        if not api_key:
            return "错误：未配置 E2B API Key，请在插件配置中设置。"
        
        timeout = self.config.get("timeout", 30)
        max_output_length = self.config.get("max_output_length", 2000)
        
        try:
            async with AsyncSandbox.create(api_key=api_key, timeout=timeout) as sandbox:
                execution = await sandbox.run_code(code)
                
                result_parts = []
                
                # 处理标准输出
                if execution.logs and execution.logs.stdout:
                    stdout = "".join(execution.logs.stdout)
                    if stdout.strip():
                        result_parts.append(f"输出:\n{stdout}")
                
                # 处理标准错误
                if execution.logs and execution.logs.stderr:
                    stderr = "".join(execution.logs.stderr)
                    if stderr.strip():
                        result_parts.append(f"错误输出:\n{stderr}")
                
                # 处理返回值
                if execution.text:
                    result_parts.append(f"返回值: {execution.text}")
                
                # 处理执行错误
                if execution.error:
                    result_parts.append(f"执行错误: {execution.error.name}: {execution.error.value}")
                    if execution.error.traceback:
                        result_parts.append(f"错误追踪:\n{execution.error.traceback}")
                
                if not result_parts:
                    result = "代码执行成功，无输出。"
                else:
                    result = "\n\n".join(result_parts)
                
                # 限制输出长度
                if len(result) > max_output_length:
                    result = result[:max_output_length] + "\n\n... (输出过长，已截断)"
                
                return result
                
        except Exception as e:
            logger.error(f"E2B 执行错误: {e}")
            return f"代码执行失败: {str(e)}"
