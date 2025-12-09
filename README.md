# AstrBot E2B 云沙箱插件 (astrbot_plugin_e2b_sandbox)

![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20Docker-blue)
![License](https://img.shields.io/badge/License-AGPL--3.0-green)
![Python](https://img.shields.io/badge/Python-3.9+-blue)
![Status](https://img.shields.io/badge/Status-Active-brightgreen)

**使用 E2B 云沙箱安全执行 Python 代码，支持通过 LLM 自然语言调用。**

这是 AstrBot 的增强型代码执行工具，它将不安全的本地 Python 执行环境替换为隔离的云端沙箱。无需担心破坏宿主机环境，同时拥有完整的联网能力。

# 更新日志
- **v1.0.0**  在与ai评审员的深度交流中茅塞顿开,推出了一个能跑的版本
- **v1.0.1**  修复了沙箱中没有astrbot的bug (加了个图片logo)
- **v1.0.2**  买了一只笔，学会了画图，优化了结构
- **v1.0.3**  代码运行结果现在会回传给 LLM，Bot 终于知道自己算出了什么，并能对结果进行解释，引入代码哈希检测与系统指令注入，彻底解决了 LLM 反复调用工具导致的死循环问题。
- **v1.0.4**  🛡️ **核心修复与体验优化**：
  - **资源安全**：引入服务端自动销毁机制（timeout 参数），彻底杜绝僵尸沙箱扣费。
  - **依赖自动安装**：自动检测代码中的常用库（如 matplotlib）并执行 pip install，解决 ModuleNotFoundError。
  - **中文支持**：自动注入中文字体，解决画图乱码问题。
  - **SDK 适配**：修复了新版 E2B SDK 参数报错的问题。
  - **文档优化**：优化了插件文档的图片格式。
  - *感谢 @xboHodx 和 @IMAUZSA 的反馈建议以及 @LiYH2008 的鼓励！*

## ✨ 核心特性

### 🔒 安全隔离
- 代码在 **E2B 云端沙箱** 执行，完全隔离于宿主机。
- 无法访问宿主机文件系统和系统资源，杜绝 `rm -rf` 风险。
- 每次执行后沙箱自动销毁，不留任何痕迹。

### 🌐 完整功能
- **联网能力**：沙箱自带海外网络环境，可进行爬虫、API 调用等操作。
- **动态装库**：支持在代码中通过 `pip` 动态安装第三方库。
- **绘图支持**：支持 Matplotlib 等库生成图片，**自动异步发送**给用户，不阻塞对话。
- **系统命令**：支持执行 Linux Shell 命令。

### 🤖 深度 LLM 集成 (v1.0.3 增强)
- **上下文感知**：工具执行结果（Stdout/Stderr）会反馈给 LLM，LLM 可基于结果回答用户问题。
- **智能防呆**：自动识别 Markdown 代码块，自动处理 `plt.show()` 阻塞问题。
- **状态管理**：通过系统 Prompt 强制控制 LLM 的工具调用行为，防止 Token 浪费。

## ⚠️ 重要前置要求（必读）

> [!IMPORTANT]
> 请务必阅读以下内容，否则插件可能无法正常工作。

### 1. 🚫 必须禁用系统自带插件
AstrBot 自带的 `astrbot_plugin_python_interpreter`（本地代码执行器）与本插件功能冲突。

**操作步骤**：
1. 打开 AstrBot 管理界面。
2. 进入 **"插件管理"** → **"系统插件"**。
3. 找到 `astrbot_plugin_python_interpreter`。
4. 点击 **"禁用"**。

**如果不禁用**：可能导致两个工具同时抢答，甚至导致 LLM 逻辑混乱。

### 2. 🌐 网络环境配置
E2B 服务端位于海外。如果您在国内网络环境下运行 AstrBot，请确保您的网络环境可以访问 E2B API。

## 🛠️ 安装指南

### 第一步：安装依赖
本插件依赖 `e2b-code-interpreter` 库。

```bash
# 进入 AstrBot 的虚拟环境后执行
pip install e2b-code-interpreter>=1.0.0
```

### 第二步：安装插件

**方式 A：从 AstrBot 插件市场安装（推荐）**
1. 打开 AstrBot 管理界面 -> "插件市场"。
2. 搜索 `astrbot_plugin_e2b_sandbox`。
3. 点击"安装"。

**方式 B：从 GitHub 安装**
1. 打开 AstrBot 管理界面 -> "插件管理" -> "添加插件"。
2. 粘贴仓库地址：`https://github.com/sl251/astrbot_plugin_e2b_sandbox`
3. 点击"确认安装"。

### 第三步：获取 E2B API Key
1. 访问 [E2B Dashboard](https://e2b.dev)。
2. 使用 GitHub 或 Google 账号登录。
3. 在仪表盘中复制你的 **API Key**（格式为 `e2b_...`）。

## ⚙️ 配置指南

完成安装后，在 AstrBot 管理界面进行配置：

| 配置项 | 类型 | 必填 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `e2b_api_key` | 字符串 | ✅ 是 | 空 | E2B API Key |
| `timeout` | 整数 | ❌ 否 | 30 | 单次执行超时时间（秒） |
| `max_output_length` | 整数 | ❌ 否 | 2000 | 单次返回的最大文本长度（字符） |

## 💰 关于 E2B 成本

- **免费额度**：注册即送 **$100** 永久抵扣金。
- **计费方式**：仅按沙箱**存活秒数**计费（约 $0.05/小时）。
- **实际体验**：对于个人聊天机器人这种“用完即焚”的场景，这 $100 额度通常够用好几年，几乎等同于免费。

## 🚀 使用示例

### 1️⃣ 联网获取网页标题 
> **用户**："看看 astrbot.app 首页的 title 是什么，记得把 response 的 encoding 设置为 utf-8"

**机器人后台执行：**
```python
import requests
import re
res = requests.get("https://astrbot.app")
res.encoding = 'utf-8'
print(re.findall(r'<title>(.*?)</title>', res.text)[0])
```
**返回结果：**
```text
(LLM 会读取上述代码的输出，并回答你)
AstrBot 官网的标题是：AstrBot - 多平台大模型机器人基础设施
```

### 2️⃣ 进行 Python 代码计算
![代码计算示例](https://github.com/user-attachments/assets/7ecf12b4-d633-4edf-b260-5a7c5ea925f7)

### 3️⃣ 绘图能力 (v1.0.3 优化)
> **用户**："用 Python 画一个爱心函数的图像，并保存显示"

插件会自动生成代码、执行绘图、将图片发送给你，并由 LLM 告诉你“图片已生成”。

![绘图示例](https://github.com/user-attachments/assets/d73b55a5-c3c6-4752-96e7-7bf240949fb4)

## 📝 注意事项 (Limitations)

- **无状态环境**：为了节省成本和保证环境纯净，**每次对话（Tool Call）都会创建一个全新的沙箱**。这意味着你不能在第一句话定义 `x=1`，然后在第二句话打印 `print(x)`。如果需要使用之前的变量，请让 LLM 在一段代码中重新定义。
- **图片展示**：请告知 LLM 将图片保存为文件（如 `save('plot.png')`），插件会自动捕获并发送。不要使用 `plt.show()`（v1.0.3 已自动屏蔽此操作以防卡死）。

## 🍪 
虽然现在只能跑代码，但我还有很多想法：
- [x] **图片输出**：希望能把 Matplotlib 的图直接发出来。(v1.0.2 已实现)
- [x] **结果回传**：让 LLM 能看到运行结果并解释。(v1.0.3 已实现)
- [ ] **文件上传**：让沙箱能处理你发的 Excel 表格。
- [ ] **代码片段**：存一些常用的脚本，不用每次都让 LLM 现写。

## 📖 相关资源
- [AstrBot 官方文档](https://astrbot.readthedocs.io/)
- [E2B 官方网站](https://e2b.dev)

---

**如果您觉得这个插件好用，请给个 Star ⭐！这对我真的很重要！**

## 许可证
AGPL-3.0 - 详见 [LICENSE](LICENSE) 文件
```
