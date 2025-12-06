# AstrBot E2B 云沙箱插件 (astrbot_plugin_e2b_sandbox)

![Platform](https://img.shields.io/badge/Platform-Windows%20|%20Linux%20|%20Docker-blue)
![License](https://img.shields.io/badge/License-AGPL--3.0-green)
![Python](https://img.shields.io/badge/Python-3.9+-blue)
![Status](https://img.shields.io/badge/Status-Active-brightgreen)

🚀 使用 E2B 云沙箱安全执行 Python 代码的 AstrBot 插件，支持 LLM 自然语言调用

这是 AstrBot 的增强型代码执行工具，它将不安全的本地 Python 执行环境替换为隔离的云端沙箱。无需担心破坏宿主机环境，同时内置多层防护机制，防止 LLM 在长对话中因上下文过长而陷入无限循环。

## ✨ 核心特性

### 🔒 安全隔离
- 代码在 E2B 云端沙箱执行，完全隔离于宿主机
- 无法访问宿主机文件系统和系统资源
- 每次执行后沙箱自动销毁，不留任何痕迹

### 🌐 完整功能
- **联网能力**：沙箱支持访问互联网，可进行爬虫、API 调用等操作
- **动态装库**：支持在代码中动态 `import` 或 `pip install` 第三方库
- **系统命令**：支持执行 shell 命令进行系统操作

### 🤖 LLM 集成
- 完全兼容 AstrBot 的 LLM 工具调用系统
- 支持 GPT、Claude、Gemini 等所有支持 Function Calling 的模型
- 自动识别和触发代码执行任务

### ⚡ 性能优化
- 异步非阻塞执行，不影响机器人响应其他消息
- 高效的沙箱池管理
- 智能的缓存机制

## ⚠️ 重要前置要求（必读）

### 1. 🤖 LLM 能力要求
您选用的语言模型**必须支持工具调用（Tool Calling / Function Calling）功能**。


### 2. 💻 环境兼容性说明
本插件仅在windows环境测试过，其他环境不保证可用


### 3. 🚫 必须禁用系统自带插件
AstrBot 自带的 `astrbot_plugin_python_interpreter`（本地代码执行器）与本插件功能重叠。

**必须禁用步骤**：
1. 打开 AstrBot 管理界面
2. 进入"插件管理" → "系统插件"
3. 找到 `astrbot_plugin_python_interpreter`
4. 点击"禁用"

⚠️ **不禁用会导致**：
- 两个工具同时被调用，造成资源浪费
- 可能导致 LLM 困惑而表现异常
- 增加循环调用的风险

### 4. 🌐 网络环境配置
E2B 服务器位于海外。在国内网络环境下必须配置代理。

   
## 🛠️ 安装指南

### 第一步：安装 Python 依赖
本插件依赖 `e2b-code-interpreter` 库。

```bash
# 方式 1：使用 pip
pip install e2b-code-interpreter>=1.0.0

# 方式 2：在 AstrBot web ui 中安装
# 进入 AstrBot web ui 输入 2b-code-interpreter 并安装
```

### 第二步：安装插件
选择以下任一方式：

**方式 A：从 AstrBot 插件市场安装（推荐）**
1. 打开 AstrBot 管理界面
2. 进入"插件市场"
3. 搜索 `astrbot_plugin_e2b_sandbox`
4. 点击"安装"

**方式 B：从 GitHub 安装**
1. 打开 AstrBot 管理界面
2. 进入"插件管理" → "添加插件"
3. 粘贴仓库地址：`https://github.com/sl251/astrbot_plugin_e2b_sandbox`
4.  点击"确认安装"


### 第三步：获取 E2B API Key
1. 访问 [E2B Dashboard](https://e2b.dev)
2. 使用 GitHub 或 Google 账号登录
3.  在仪表盘中找到 API Key 部分
4. 复制你的 API Key（格式通常为 `e2b_... `）
5. 妥善保管，不要分享给他人

## ⚙️ 配置指南

完成安装后，在 AstrBot 管理界面进行如下配置：

### 配置项说明表

| 配置项 | 类型 | 必填 | 默认值 | 说明 | 推荐值 |
|--------|------|------|--------|------|--------|
| `e2b_api_key` | 字符串 | ✅ 是 | 空 | E2B API Key（从 e2b. dev 获取） | - |
| `timeout` | 整数 | ❌ 否 | 30 | 单次代码执行超时时间（秒） | 30-60 |
| `max_output_length` | 整数 | ❌ 否 | 2000 | 单次返回的最大文本长度（字符） | 2000 |



## 💰 关于 E2B 成本（几乎免费！）

对于个人开发者和 AstrBot 聊天场景，**E2B 几乎是免费的**：

### 成本分析
- **免费额度**：注册即送 **$100** 一次性抵扣金
- **计费方式**：按沙箱**存活秒数**计费
- **价格水平**：基础沙箱约 **$0.05/小时**

### 实际算账
```
$100 额度 ÷ $0.05 per hour = 2000 小时运行时间

对于聊天机器人场景（用完即焚）：
- 平均每天使用 1 小时 → 可用 5. 5 年
- 平均每天使用 8 小时 → 可用 8 个月
- 平均每天使用 24 小时 → 可用 3 个月
```

**结论**：对绝大多数个人用户，$100 额度可以使用数年。

## 🚀 使用示例

### 基础示例
直接用自然语言与机器人对话，让它自动调用代码执行工具：

```
用户："帮我计算 1 到 100 的质数之和"

机器人自动执行：
def is_prime(n):
    if n < 2:
        return False
    for i in range(2, int(n ** 0.5) + 1):
        if n % i == 0:
            return False
    return True

print(sum(i for i in range(1, 101) if is_prime(i)))

返回：✅ 返回值: 1060
```

### 高级示例

#### 1️⃣ 联网获取数据
```
用户："帮我获取 example.com 的 HTTP 状态码"

机器人自动执行：
import requests
response = requests.get('http://example.com')
print(f"状态码: {response. status_code}")

返回：✅ 返回值: 状态码: 200
```

#### 2️⃣ 动态安装库并使用
```
用户："帮我生成 3 个中文名字，使用 faker 库"

机器人自动执行：
import subprocess
subprocess.run(['pip', 'install', 'faker'], capture_output=True)

from faker import Faker
fake = Faker('zh_CN')
for _ in range(3):
    print(fake.name())

返回：
📤 输出:
李明
王芳
张三
```



## 📚 FAQ - 常见问题



#### Q: 报错 `Connection timeout` 或 `Failed to connect to E2B`？
**A:** E2B 是海外服务，需要良好的代理配置。

**解决方案**：
- 切换代理节点，尝试更稳定的线路
- 检查代理的 HTTP 协议支持
- 如在公司/学校网络，可能需要配置企业代理
- 尝试使用 VPN 替代 HTTP 代理


#### Q: 为什么代码执行超时了？
**A:** 代码执行超过了配置的超时时间（默认 30 秒）。


## 📋  🍪
- [ ] **图片输出支持**：支持返回 Matplotlib/PIL 生成的图表或图片
- [ ] **文件上传支持**：允许用户上传文件供沙箱读取处理
- [ ] **执行历史记录**：在 UI 中查看执行历史和结果缓存
- [ ] **代码片段库**：预设常用的代码片段供快速调用
- [ ] **性能指标**：实时显示沙箱使用情况和成本统计


## 🤝 遇到的问题
由于工具不返回值给 LLM（只发送给用户），导致 LLM 无法将执行结果记录到对话历史中。虽然解决了重复调用问题，但 LLM 工具调用链条被中断。
需要找到在不触发重复调用的前提下，既能发送结果给用户又能返回结果给 LLM 的方案



## 📖 相关资源

- [AstrBot 官方文档](https://astrbot.readthedocs.io/)
- [E2B 官方网站](https://e2b.dev)
- [E2B 文档](https://docs.e2b.dev/)
- [AstrBot GitHub](https://github.com/Soulter/AstrBot)

---

**如果您觉得这个插件对您有帮助，请在 [GitHub](https://github.com/sl251/astrbot_plugin_e2b_sandbox) 上给它一个 Star ⭐！这对我真的很重要！**

## 许可证

AGPL-3.0 - 详见 [LICENSE](LICENSE) 文件
