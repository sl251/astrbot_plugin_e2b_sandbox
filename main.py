import traceback
import time
from astrbot.api.star import Context, Star, register
from astrbot. api.event import AstrMessageEvent, MessageEventResult
from astrbot.api import llm_tool, logger
from e2b_code_interpreter import AsyncSandbox


@register("e2b_sandbox", "sl251", "使用 E2B 云沙箱执行 Python 代码", "1.0. 0")
class E2BSandboxPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config
        self._last_execution = {}
    
    @llm_tool(name="run_python_code")
    async def run_python_code(self, event: AstrMessageEvent, code: str):
        '''在云沙箱中执行Python代码。执行完成后，请直接将结果告诉用户，不要再次调用此工具。

        Args:
            code(string): 要执行的 Python 代码
        '''
        # 防重复调用
        cache_key = code. strip()
        current_time = time. time()
        if cache_key in self._last_execution:
            last_time, last_result = self._last_execution[cache_key]
            if current_time - last_time < 30:
                logger.info(f"[E2B] 检测到重复调用，终止循环")
                event.set_result(MessageEventResult(). message(f"代码执行结果：\n{last_result}"))
                return
        
        logger.info(f"[E2B] 开始执行代码: {code[:100]}")
        
        api_key = ""
        if self. config:
            api_key = self. config.get("e2b_api_key", "")
        
        if not api_key:
            logger.warning("[E2B] API Key 未配置")
            return "错误：未配置 E2B API Key，请在插件配置中设置。"
        
        logger.info(f"[E2B] API Key 已配置，长度: {len(api_key)}")
        
        timeout = self.config.get("timeout", 30) if self.config else 30
        max_output_length = self.config. get("max_output_length", 2000) if self.config else 2000
        
        sandbox = None
        try:
            logger.info("[E2B] 正在创建沙箱...")
            sandbox = await AsyncSandbox. create(api_key=api_key)
            logger.info("[E2B] 沙箱创建成功，开始执行代码...")
            execution = await sandbox.run_code(code, timeout=timeout)
            logger.info("[E2B] 代码执行完成")
            
            result_parts = []
            
            if execution.logs and execution.logs. stdout:
                stdout = "".join(execution. logs.stdout). strip()
                if stdout:
                    result_parts.append(f"输出:\n{stdout}")
            
            if execution.logs and execution. logs.stderr:
                stderr = "". join(execution.logs.stderr).strip()
                if stderr:
                    result_parts.append(f"错误:\n{stderr}")
            
            if execution. text:
                result_parts.append(f"返回值: {execution.text}")
            
            if execution.error:
                result_parts.append(f"执行错误: {execution.error. name}: {execution.error.value}")
            
            if not result_parts:
                result = "代码执行成功，无输出。"
            else:
                result = "\n\n".join(result_parts)
            
            if len(result) > max_output_length:
                result = result[:max_output_length] + "\n.. .(已截断)"
            
            logger.info(f"[E2B] 返回结果: {result[:100]}")
            self._last_execution[cache_key] = (current_time, result)
            
            # 返回结果给 LLM，让 LLM 生成自然语言回复
            return f"代码已成功执行，结果如下：\n{result}\n\n请将以上结果用自然语言告诉用户。"
                
        except Exception as e:
            logger.error(f"[E2B] 执行错误: {e}")
            logger.error(f"[E2B] 错误堆栈:\n{traceback. format_exc()}")
            result = f"代码执行失败: {str(e)}"
            self._last_execution[cache_key] = (current_time, result)
            return result
        finally:
            if sandbox:
                try:
                    await sandbox.kill()
                    logger.info("[E2B] 沙箱已关闭")
                except:
                    pass
