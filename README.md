# AstrBot E2B 云沙箱插件 (astrbot_plugin_e2b_sandbox)

![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20Docker-blue)
![License](https://img.shields.io/badge/License-AGPL--3.0-green)
![Python](https://img.shields.io/badge/Python-3.9+-blue)
![Status](https://img.shields.io/badge/Status-Active-brightgreen)

**使用 E2B 云沙箱安全执行 Python 代码，支持通过 LLM 自然语言调用。**

这是 AstrBot 的增强型代码执行工具，它将不安全的本地 Python 执行环境替换为隔离的云端沙箱。无需担心破坏宿主机环境，同时拥有完整的联网能力。

## ✨ 核心特性

### 🔒 安全隔离
- 代码在 **E2B 云端沙箱** 执行，完全隔离于宿主机。
- 无法访问宿主机文件系统和系统资源，杜绝 `rm -rf` 风险。
- 每次执行后沙箱自动销毁，不留任何痕迹。

### 🌐 完整功能
- **联网能力**：沙箱自带海外网络环境，可进行爬虫、API 调用等操作。
- **动态装库**：支持在代码中通过 `pip` 动态安装第三方库。
- **系统命令**：支持执行 Linux Shell 命令。

### 🤖 LLM 集成
- 完全兼容 AstrBot 的 LLM 工具调用系统。
- 自动识别 Markdown 代码块，防呆设计，解决 LLM 输出格式乱的问题。

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
📤 Output:
AstrBot - 多平台大模型机器人基础设施
```

### 2️⃣ 数学计算
> **用户**："计算 100 以内质数的和"

**机器人后台执行：**
```python
def is_prime(n):
    if n < 2: return False
    for i in range(2, int(n**0.5)+1):
        if n % i == 0: return False
    return True
print(sum(i for i in range(101) if is_prime(i)))
```
**返回结果：**
```text
📤 Output:
1060
```

## 🤝 遇到的问题 (Known Issues)

**关于 LLM 上下文中断的问题**
目前插件会将执行结果**直接发送给用户**，并停止事件传播，而不是返回给 LLM。

**为什么这样做？**
我也想把结果扔回给 LLM 让它润色一下，但是这会导致 **死循环**：
1. LLM 调用工具 -> 2. 获得结果 -> 3. LLM 思考如何回答 -> 4. LLM 觉得还需要再算一遍 -> 5. 再次调用工具...

为了防止你的额度和 Token 爆炸，我选择了最稳妥的方式：**算完直接发出来，别让 LLM 瞎琢磨了。** 🫡

## 🍪 
虽然现在只能跑代码，但我还有很多想法：
- [ ] **图片输出**：希望能把 Matplotlib 的图直接发出来。
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

