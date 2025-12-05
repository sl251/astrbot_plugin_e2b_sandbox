import traceback
import time
from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api import llm_tool, logger
from e2b_code_interpreter import AsyncSandbox


@register("e2b_sandbox", "sl251", "使用 E2B 云沙箱执行 Python 代码", "1.0.0")
class E2BSandboxPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config
        self._last_call = {"code": None, "time": 0, "result": ""}

    @llm_tool(name="run_python_code")
    async def run_python_code(self, event: AstrMessageEvent, code: str, silent: bool = None):
        '''在云沙箱中执行Python代码。

        Args:
            code (string): 要执行的 Python 代码。
            silent (bool): 模式选择 (可选)。此参数可覆盖插件的默认设置。
                         - `False`: 强制直接输出结果给用户，确保不发生循环。
                         - `True`: 强制将结果返回给模型进行下一步分析，用于连续对话。
        '''
        current_time = time.time()

        is_silent = silent
        if is_silent is None:
            is_silent = self.config.get("default_silent_mode", False) if self.config else False

        # 智能熔断器：只有在30秒内，且 code 内容与上一次完全相同时，才触发
        if (code.strip() == self._last_call["code"] and 
            current_time - self._last_call["time"] < 30):
            
            # 根据当前模式决定熔断行为
            if is_silent:
                logger.warning(f"[E2B] Silent 模式下检测到重复调用，将返回缓存结果给 LLM。")
                return self._last_call["result"]
            else:
                logger.warning(f"[E2B] 检测到重复调用，强制中断循环！")
                event.set_result(MessageEventResult().message(f"检测到重复执行，返回缓存结果：\n{self._last_call['result']}"))
                return

        logger.info(f"[E2B] 开始执行代码: {code[:100]}")

        api_key = self.config.get("e2b_api_key", "") if self.config else ""
        if not api_key:
            logger.error("[E2B] API Key 未配置")
            result = "错误：未配置 E2B API Key，请在插件配置中设置。"
            event.set_result(MessageEventResult().message(result))
            return

        timeout = self.config.get("timeout", 30) if self.config else 30
        max_output_length = self.config.get("max_output_length", 2000) if self.config else 2000

        sandbox = None
        result = ""
        try:
            logger.info("[E2B] 正在创建沙箱...")
            sandbox = await AsyncSandbox.create(api_key=api_key)
            logger.info("[E2B] 沙箱创建成功，开始执行代码...")
            execution = await sandbox.run_code(code, timeout=timeout)
            logger.info("[E2B] 代码执行完成")

            result_parts = []
            if execution.logs and execution.logs.stdout:
                stdout = "".join(execution.logs.stdout).strip()
                if stdout:
                    result_parts.append(f"输出:\n{stdout}")

            if execution.logs and execution.logs.stderr:
                stderr = "".join(execution.logs.stderr).strip()
                if stderr:
                    result_parts.append(f"错误:\n{stderr}")

            if execution.text:
                result_parts.append(f"返回值: {execution.text}")

            if execution.error:
                result_parts.append(f"执行错误: {execution.error.name}: {execution.error.value}")

            if not result_parts:
                result = "代码执行成功，无输出。"
            else:
                result = "\n\n".join(result_parts)

            if len(result) > max_output_length:
                result = result[:max_output_length] + "\n...(已截断)"

        except Exception as e:
            logger.error(f"[E2B] 执行错误: {e}")
            logger.error(f"[E2B] 错误堆栈:\n{traceback.format_exc()}")
            result = f"代码执行失败: {str(e)}"
        finally:
            if sandbox:
                try:
                    await sandbox.kill()
                    logger.info("[E2B] 沙箱已关闭")
                except Exception:
                    pass

        self._last_call = {"code": code.strip(), "time": current_time, "result": result}

        if is_silent:
            logger.info(f"[E2B] Silent 模式：将结果返回给 LLM。")
            return result
        else:
            logger.info(f"[E2B] 默认稳定模式：直接输出结果并结束回合。")
            event.set_result(MessageEventResult().message(result))
            return
