# AIcarusCore - 自主 AI 代理核心框架

欢迎来到 AIcarusCore！这是一个为构建高级自主 AI 代理而设计的、高度模块化的核心决策框架。它不仅仅是一个聊天机器人后端，更是一个模拟 AI“意识”与“注意力”的完整系统。

AIcarusCore 的核心驱动力来自于两大创新设计理念：**思想链 (Thought Chain)** 和 **三层注意力模型 (Three-Layer Attention Model)**。

---

## 核心设计理念

### 1. 思想链 (Thought Chain)

传统的 AI 交互是无状态的、一问一答的。AIcarusCore 彻底颠覆了这一点。

**思想链** 是 AI 的意识流的持久化体现。每一次决策（思考）都会被记录成一个“思想点”并链接到前一个点，形成一条不断延伸的链条。每个思想点都包含了：

- **心境 (Mood)**: AI 当时的情绪状态。
- **想法 (Think)**: 对当前所有信息的详细分析、推理与内心独白。
- **目标 (Goal)**: 当前的主要任务或意图。
- **行动 (Action)**: 基于思考产生的具体行动指令。
- **结果 (Result)**: 行动执行后返回的结果。

这种设计使得 AI 的每一次决策都有迹可循，拥有了连贯的记忆和上下文，让 AI 真正地“活”了起来，而不是一个简单的请求/响应机器。

### 2. 三层注意力模型 (Three-Layer Attention Model)

为了模拟人类在不同情境下的注意力切换，AIcarusCore 设计了一个三层注意力模型，AI 的意识可以在这三层之间自由流动 (`focus`, `return`, `shift`)。

- #### **核心层 (Core Level) - “发呆/内省”**

  - **视野**: 最高层级。AI 会概览所有平台的连接状态和未读消息摘要，但不关心任何具体对话。
  - **决策**: 宏观、高层级的决策。例如：“`napcat_qq`平台似乎有高优先级消息，我应该去看看。”
  - **状态**: 这是 AI 的默认状态，类似于人类的发呆、放空或进行内心规划。

- #### **平台层 (Platform Level) - “检视平台”**

  - **视野**: 当 AI `focus` 到一个具体平台（如`napcat_qq`）后，它的注意力会集中在这个平台上。它能看到该平台下所有群聊/私聊的摘要列表、最新消息和未读状态。
  - **决策**: 更具针对性。例如：“‘AIcarus 交流群’的讨论很热烈，我应该`focus`进去参与。”
  - **状态**: 类似于我们打开微信或 QQ 后，在聊天列表界面浏览的状态。

- #### **细胞层 (Cellular Level) - “专注聊天模式”**

  - **视野**: 最深层级。AI `focus` 到一个具体会话后，它的全部注意力都在这个聊天窗口里。它能看到详细的聊天记录、成员信息、自己的群名片等一切细节。
  - **决策**: 实时、高情境化的交互。例如：“用户 U1 刚刚提到了一个问题，我应该`send_message`来回复他。”
  - **状态**: 这是 AI 作为一名真正的聊天参与者的状态。在此模式下，`SummarizationManager`会为它提供长期记忆（会话总结），`BehavioralGuidanceGenerator`会给予它社交技巧提示（如避免刷屏）。

---

## 项目结构导览

```
AIcarusCore/
├── 📄 run_core_logic.py         # 项目主入口
├── 📁 config/                   # 存放运行时配置文件 (config.toml)
├── 📁 data/                     # 存放数据模型 (如中断判断模型)
├── 📁 logs/                     # 日志输出目录
├── 📁 src/                      # 核心源码
│   ├── 📁 action/               # 动作处理中心：解析LLM决策并执行
│   ├── 📁 bootstrap/            # 项目启动与依赖注入
│   ├── 📁 common/               # 通用工具与服务模块
│   ├── 📁 config/               # 配置模型的定义与加载逻辑
│   ├── 📁 core_communication/   # 核心WebSocket通信层
│   ├── 📁 core_logic/           # AI的“大脑”：核心思考循环与状态管理
│   ├── 📁 database/             # 数据库模型与服务，AI的“记忆”
│   ├── 📁 focus_chat_mode/      # “专注聊天模式”的具体实现
│   ├── 📁 llmrequest/           # 强大、健壮的LLM请求客户端
│   ├── 📁 message_processing/   # 原始消息的预处理与分发
│   ├── 📁 platform_builders/    # 各平台“翻译官”，将标准动作转为平台指令
│   ├── 📁 prompt_templates/     # 存放所有核心Prompt模板
│   └── 📁 tools/                # 外部工具集（如网页搜索）
└── 📁 template/                 # 配置文件模板
```

---

## 启动与运行

### 1. 环境准备

- Python 3.10+
- 一个正在运行的 ArangoDB 数据库实例。
- 安装项目依赖：
  ```bash
  pip install -r requirements.txt
  ```

### 2. 配置

项目启动依赖两类配置：

- **环境变量 (`.env`文件)**: 在项目根目录 (`AIcarusCore/`) 创建一个 `.env` 文件，用于存放敏感信息。这是**必须**的步骤。

  **.env.example**:

  ```env
  # ArangoDB 数据库连接信息
  ARANGODB_HOST="http://localhost:8529"
  ARANGODB_USER="root"
  ARANGODB_PASSWORD="your_password"
  ARANGODB_DATABASE="aicarus_core_db"

  # LLM API Keys (以GEMINI和OPENAI为例，请根据您的provider修改)
  # 支持单个Key或JSON数组格式的多个Key
  GEMINI_API_KEYS='["your_gemini_key_1", "your_gemini_key_2"]'
  OPENAI_API_KEYS="your_openai_key"

  # LLM Base URLs
  GEMINI_BASE_URL="https://generativelanguage.googleapis.com/v1beta/models"
  OPENAI_BASE_URL="https://api.openai.com/v1"

  # 代理 (如果需要)
  PROXY_HOST="127.0.0.1"
  PROXY_PORT="7890"
  ```

- **项目配置文件 (`config.toml`)**:
  首次启动项目时，系统会自动从 `template/config_template.toml` 复制一份到 `config/config.toml`。您需要根据 `config_template.toml` 中的注释，编辑 `config/config.toml` 来配置 AI 的人格、模型使用偏好等。**请务必在首次启动后检查并修改此文件。**

### 3. 运行

完成配置后，直接运行 `run_core_logic.py` 即可启动 AI 核心。

```bash
python run_core_logic.py
```

AIcarusCore 将启动一个 WebSocket 服务器（默认监听 `ws://localhost:8077`），等待平台适配器前来连接。

---

## 主要依赖

- `websockets`: 核心通信基础。
- `arangoasync`: 用于与 ArangoDB 进行异步交互。
- `aiohttp`: 强大的异步 HTTP 客户端，用于 LLM 请求和网络工具。
- `loguru`: 提供优雅、强大的日志记录。
- `pydantic` / `dataclasses`: 用于数据建模和配置管理。
- `sentence-transformers`: 用于语义分析和中断判断。

## 🤝 贡献

欢迎任何形式的贡献！如果你有任何问题、功能建议或发现了 bug，请通过提交 Issue 或 Pull Request 的方式告知我们。

## 许可证

本项目基于 **GPL-3.0** 许可证开源。
