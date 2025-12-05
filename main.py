import traceback
import base64
import tempfile
import os
from typing import Optional

from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api import llm_tool, logger
from e2b_code_interpreter import AsyncSandbox

@register("e2b_sandbox", "sl251", "E2B 云沙箱 Python 执行器", "1.0.2")
class E2BSandboxPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

    @llm_tool(name="run_python_code")
    async def run_python_code(self, event: AstrMessageEvent, code: str, silent: Optional[bool] = None):
        '''在 E2B 云沙箱中执行 Python 代码。支持绘图、联网。

        Args:
            code (string): Python 代码。
            silent (bool): 
                - False (默认): 将运行结果(文本+图)直接发给用户，并结束 LLM 对话 (防循环)。
                - True: 将文本结果返回给 LLM 进行分析 (如让 AI 总结数据)。
        '''
        # 1. 确定模式 (优先使用参数，其次使用配置)
        is_silent = silent
        if is_silent is None:
            is_silent = self.config.get("default_silent_mode", False)

        logger.info(f"[E2B] Executing code: {code[:50]}...")

        # 2. 检查配置
        api_key = self.config.get("e2b_api_key", "")
        if not api_key:
            err_msg = "❌ 配置错误: 未找到 E2B API Key，请在插件配置中填写。"
            # 如果是 Silent 模式，返回文本给 LLM；否则直接回复用户
            if is_silent: return err_msg 
            event.set_result(MessageEventResult().message(err_msg)) 
            return

        timeout = self.config.get("timeout", 30)
        
        sandbox = None
        text_output = ""
        image_files = [] # 存储临时图片路径

        try:
            # 3. 创建沙箱与执行 (局部变量，线程安全)
            sandbox = await AsyncSandbox.create(api_key=api_key)
            execution = await sandbox.run_code(code, timeout=timeout)
            
            # 4. 解析文本结果
            result_parts = []
            if execution.logs.stdout:
                result_parts.append(f"📄 标准输出:\n{''.join(execution.logs.stdout).strip()}")
            if execution.logs.stderr:
                result_parts.append(f"⚠️ 错误输出:\n{''.join(execution.logs.stderr).strip()}")
            if execution.text:
                result_parts.append(f"↩️ 返回值: {execution.text}")
            if execution.error:
                result_parts.append(f"❌ 执行报错: {execution.error.name}: {execution.error.value}")
                
            text_output = "\n\n".join(result_parts) if result_parts else "✅ 执行成功，无文本输出。"

            # 5. 解析图片结果 (Base64 -> TempFile)
            if execution.results:
                for res in execution.results:
                    img_data = None
                    if hasattr(res, 'png') and res.png:
                        img_data = base64.b64decode(res.png)
                    elif hasattr(res, 'jpeg') and res.jpeg:
                        img_data = base64.b64decode(res.jpeg)
                    
                    if img_data:
                        # 创建临时文件
                        fd, path = tempfile.mkstemp(suffix=".png", prefix="e2b_plot_")
                        with os.fdopen(fd, 'wb') as f:
                            f.write(img_data)
                        image_files.append(path)

        except Exception as e:
            logger.error(f"[E2B] Runtime Error: {traceback.format_exc()}")
            text_output = f"❌ 沙箱运行异常: {str(e)}"
        finally:
            if sandbox:
                await sandbox.kill()

        # 6. 结果处理逻辑

        # 截断过长文本
        max_len = self.config.get("max_output_length", 2000)
        display_text = text_output
        if len(display_text) > max_len:
            display_text = display_text[:max_len] + f"\n...(已截断剩余 {len(display_text)-max_len} 字符)"

        # === 分支 A: 默认交互模式 (Silent=False) ===
        # 策略：插件接管回复，强制结束 LLM 流程 (物理防死循环)
        if not is_silent:
            # 构建消息链
            chain = MessageEventResult().message(display_text)
            
            # 追加图片
            for img_path in image_files:
                try:
                    chain = chain.file(img_path)
                except Exception as e:
                    chain = chain.message(f"\n[图片发送失败: {e}]")
            
            event.set_result(chain)
            return

        # === 分支 B: 沉浸分析模式 (Silent=True) ===
        # 策略：返回文本给 LLM，附带 System Prompt 指令禁止复读
        else:
            system_instruction = (
                "\n\n[SYSTEM MESSAGE: Code executed successfully. "
                "The output is provided above. "
                "DO NOT execute the same code again. "
                "Please analyze the result or answer the user's question now based on the output.]"
            )
            
            if image_files:
                return f"{text_output}\n[System: {len(image_files)} images generated but hidden in silent mode.]{system_instruction}"
            
            return f"{text_output}{system_instruction}"
