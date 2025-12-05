```python
import traceback
from typing import Optional

from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api import llm_tool, logger
from e2b_code_interpreter import AsyncSandbox

@register("e2b_sandbox", "sl251", "E2B 云沙箱 Python 执行器", "1.0.0")
class E2BSandboxPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        # 保持无状态设计，符合 AI 评审要求

    @llm_tool(name="run_python_code")
    async def run_python_code(self, event: AstrMessageEvent, code: str, silent: Optional[bool] = None):
        '''在 E2B 云沙箱中执行 Python 代码。

        Args:
            code (string): 要执行的 Python 代码。
            silent (bool): 
                - True (默认): 结果返回给 LLM，用于上下文记忆和分析。
                - False: 结果直接发送给用户，并强制结束本次 LLM 对话。
        '''
        # 1. 确定模式
        is_silent = silent
        if is_silent is None:
            is_silent = self.config.get("default_silent_mode", True)

        # ✅ 日志提示：任务开始
        logger.info(f"[E2B] 收到代码执行请求，正在处理...")
        logger.info(f"[E2B] 代码片段: {code[:50]}...")

        # 2. 检查配置
        api_key = self.config.get("e2b_api_key", "")
        if not api_key:
            err_msg = "❌ 配置错误: 未找到 E2B API Key，请在插件配置中填写。"
            logger.error("[E2B] 执行失败：未配置 API Key")
            if is_silent: return err_msg
            event.set_result(MessageEventResult().message(err_msg))
            return

        timeout = self.config.get("timeout", 30)
        sandbox = None
        text_output = ""

        try:
            # ✅ 日志提示：开始创建沙箱
            logger.info("[E2B] 正在请求 E2B 创建云沙箱实例...")
            sandbox = await AsyncSandbox.create(api_key=api_key)
            
            # ✅ 日志提示：沙箱就绪，开始跑代码
            logger.info(f"[E2B] 沙箱创建成功 (ID: {sandbox.sandbox_id})，开始执行代码...")
            execution = await sandbox.run_code(code, timeout=timeout)
            
            # ✅ 日志提示：执行完成
            logger.info("[E2B] 代码执行完毕，正在解析输出结果...")
            
            # 4. 解析结果
            result_parts = []
            if execution.logs.stdout:
                result_parts.append(f"Standard Output:\n{''.join(execution.logs.stdout).strip()}")
            if execution.logs.stderr:
                result_parts.append(f"Error Output:\n{''.join(execution.logs.stderr).strip()}")
            if execution.text:
                result_parts.append(f"Return Value: {execution.text}")
            if execution.error:
                result_parts.append(f"Execution Error: {execution.error.name}: {execution.error.value}")
                
            text_output = "\n\n".join(result_parts) if result_parts else "Code executed successfully (No text output)."

        except Exception as e:
            logger.error(f"[E2B] 运行时发生异常: {e}")
            logger.error(traceback.format_exc())
            text_output = f"Sandbox Runtime Error: {str(e)}"
        finally:
            if sandbox:
                try:
                    await sandbox.kill()
                    # ✅ 日志提示：资源回收
                    logger.info("[E2B] 沙箱已销毁，资源已释放。")
                except Exception as e:
                    logger.warning(f"[E2B] 沙箱销毁失败 (可能是网络原因): {e}")

        # 5. 结果截断
        max_len = self.config.get("max_output_length", 2000)
        if len(text_output) > max_len:
            text_output = text_output[:max_len] + f"\n...(Output truncated, remaining {len(text_output)-max_len} chars omitted)"

        # === 6. 返回逻辑 ===

        if not is_silent:
            logger.info("[E2B] 模式: 直接回复用户 (Silent=False)")
            event.set_result(MessageEventResult().message(text_output))
            return
        else:
            logger.info("[E2B] 模式: 返回结果给 LLM 分析 (Silent=True)")
            system_hint = (
                "\n\n<SYSTEM_NOTE>\n"
                "1. Code execution COMPLETED. The output is provided above.\n"
                "2. DO NOT execute the code again.\n"
                "3. Please answer the user's question based on the output.\n"
                "</SYSTEM_NOTE>"
            )
            return f"{text_output}{system_hint}"
```
