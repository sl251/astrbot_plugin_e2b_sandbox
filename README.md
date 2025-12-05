# AstrBot E2B 云沙箱插件 (astrbot_plugin_e2b_sandbox)

![Platform](https://img.shields.io/badge/Platform-Windows%20Tested-blue)
![License](https://img.shields.io/badge/License-AGPL--3.0-green)

使用 E2B 云沙箱安全执行 Python 代码，支持通过 LLM 自然语言调用。

这是 AstrBot 的一个增强插件，它替换了本地不安全的 Python 执行环境，将代码运行托管在隔离的云端沙箱中，且无需担心破坏本地宿主机环境。

## ✨ 功能特性

- 🔒 **安全隔离**：代码在 E2B 云端沙箱执行，完全隔离，无法访问宿主机文件。
- 🌐 **联网能力**：沙箱支持访问互联网，可以进行爬虫、API 调用等操作。
- 📦 **动态装库**：支持在代码中 import 或 `pip install` 任意第三方库。
- 🤖 **自动调用**：LLM 自动识别意图并调用代码执行工具。
- ⚡ **异步非阻塞**：执行耗时代码时不影响机器人响应其他消息。
- ⚙️ **双模式运行**：支持稳定的“单次执行”模式和灵活的“连续追问”模式，可通过配置切换，并内置智能防循环机制。

## ⚠️ 重要前置要求 (必读)

在安装本插件前，请务必注意以下四点：

1.  **🤖 LLM 能力要求**：
    您选用的语言模型必须支持**工具调用 (Tool Calling / Function Calling)** 功能。不支持此功能的模型将无法识别和使用本插件。（例如: `GPT-4`, `Gemini Pro` 系列模型均支持）

2.  **💻 环境兼容性警告**：
    **本插件目前仅在 Windows 非docker环境下进行过测试和验证。**
    *   Linux 或 Docker 部署用户：请注意网络代理配置可能存在差异（例如 Docker 容器内访问宿主机代理需配置特殊 Host），请自行测试连通性。

3.  **🚫 必须禁用系统自带插件**：
    由于 AstrBot 自带的 `astrbot_plugin_python_interpreter` (本地解释器) 功能与本插件重叠，**请务必在“插件管理” -> “系统插件”中将其禁用**，否则会导致 LLM 混淆、重复执行或报错。

    
    <img width="317" alt="禁用自带插件" src="https://github.com/user-attachments/assets/3bb8999b-fcfb-4400-80dd-923702f1f337" />

    

5.  **🌐 网络环境配置**：
    E2B 的服务器位于海外。在国内网络环境下，**必须在 AstrBot 配置中设置 HTTP 代理**，否则无法连接沙箱。
    *   *Windows 用户提示：如果使用 v2rayN，建议开启独立的 HTTP 端口（如 10809），尽量避免使用 10808 混合端口，以防出现 `Server disconnected` 握手错误。*

## 🛠️ 安装

### 1. 安装 Python 依赖
本插件依赖 `e2b_code_interpreter` 库，请确保在 AstrBot 的 Python 环境中安装：

```bash
pip install e2b-code-interpreter
```

### 2. 安装插件
- **方法 A**：在 AstrBot 插件市场 中搜索 `astrbot_plugin_e2b_sandbox` 并安装。
- **方法 B**：使用仓库地址安装：`https://github.com/sl251/astrbot_plugin_e2b_sandbox`

## ⚙️ 配置

1. 前往 [E2B Dashboard](https://e2b.dev) 注册账号并获取 API Key。
2. 在 AstrBot 插件配置页面填入以下信息：

| 配置项 | 说明 | 推荐值 |
|--------|------|--------|
| `e2b_api_key` | E2B API Key (以 `e2b_` 开头) | 必填 |
| `timeout` | 单次代码执行超时时间（秒） | 60 |
| `max_output_length` | 最大文本输出长度（防止刷屏） | 2000 |

## 💰 关于 E2B 额度（几乎无限！）

很多用户担心云服务收费问题，但对于个人开发者和 AstrBot 聊天场景，**E2B 几乎是免费的**。

- **赠送额度**：注册即送 **$100** 一次性抵扣金。
- **计费极低**：E2B 按**沙箱存活秒数**计费。基础沙箱约为 **$0.05 / 小时**。
- **实际算账**：$100 额度 ≈ **2000 小时** 的运行时间。对于聊天机器人这种“用完即焚”的场景，这笔额度可以用上好几年。

## 🚀 使用示例

直接用自然语言与机器人对话：

- **基础计算**：`"帮我计算 1 到 100 的质数之和"`
- **联网获取**：`"帮我获取 Google 的 robots.txt，只打印前 10 行"`
- **安装并使用第三方库**：`"帮我安装并使用 faker 库生成 5 个中文名字"`
- **系统命令**：`"执行代码看看沙箱的操作系统是什么版本"`

### 运行效果演示

<img width="685" height="407" alt="image" src="https://github.com/user-attachments/assets/cc4528c3-e30e-4fd5-ad59-6b03afe31e49" />



<img width="1369" height="814" alt="屏幕截图 2025-12-05 221054" src="https://github.com/user-attachments/assets/0cd59ebe-2117-4092-afd6-071a3e9d2d77" />



<img width="1368" height="432" alt="屏幕截图 2025-12-05 221038" src="https://github.com/user-attachments/assets/c744b296-9982-4b07-96bc-1449fadc6785" />

## 📋 待办清单 (To-Do List)

- [ ] **支持图片输出**：支持返回 Matplotlib/PIL 生成的图表或图片。
- [ ] **文件上传支持**：允许用户上传文件供沙箱读取处理。
- [ ] **更丰富的库预设**：优化沙箱环境初始化速度。

## 常见问题 (FAQ)

**Q: 报错 `Server disconnected without sending a response`?**
A: 这是典型的代理配置问题。请检查 AstrBot 的 HTTP 代理设置。请确保填写的代理端口是开启的，且协议匹配（推荐使用纯 HTTP 端口），节点不稳定也会导致该报错 。

**Q: 代码能绘图吗？**
A: 暂时不支持直接显示图片。
**Q: “结果返回模型”开关有什么用？我应该打开吗？**
A: 这个开关控制着插件的两种核心工作模式，您可以根据需求选择：
关闭（默认） ：代码执行后，结果会直接输出给用户，LLM 看不到执行结果。这是最安全稳定的模式，杜绝 LLM 陷入循环调用的问题。适用于绝大多数“一步到位”的计算或查询任务。
开启 ：代码执行后，结果会返回给 LLM 进行分析。这使得 LLM 拥有了“记忆”，可以执行需要连续追问的复杂任务（例如“第一步获取数据，第二步分析数据”）。
风险提示：此模式在长对话中，有小概率导致 LLM“迷失”而陷入循环。
安全保障：插件内置了智能熔断器。即使发生循环，它也只拦截完全相同的重复代码，合法的、代码内容有变化的连续调用不会被中断。
---
**如果您觉得这个插件对您有帮助，请在 [GitHub](https://github.com/sl251/astrbot_plugin_e2b_sandbox) 上给它一个 Star 吧！这对我真的很重要！⭐**
---
## 许可证

AGPL-3.0
