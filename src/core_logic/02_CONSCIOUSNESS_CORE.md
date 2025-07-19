# AI 意识核心 (Consciousness Core)

`core_logic` 模块是 AIcarusCore 的中央处理器（CPU）和决策中心。它模拟了 AI 的“意识流动”，负责驱动整个“感知-思考-行动”的循环。这里不仅是 AI 内心世界的诞生地，也是其所有行为的最终源头。

## 核心概念：统一意识流 (Unified Consciousness Flow)

与传统请求-响应模型不同，`AIcarusCore` 的核心是一个**持续运行的、统一的思考循环** (`_core_thinking_loop`)。这个循环由 `ConsciousnessFlow` 类（在 `consciousness_flow.py` 中）驱动，它会根据 AI 当前的“注意力焦点”自主地、周期性地进行思考，就像人类会不自觉地“走神”和“反思”一样。

这个循环可以被外部事件（如高优先级消息）**被动唤醒**，从而实现对外界的快速响应。

## 核心组件与职责

- `consciousness_flow.py` (**ConsciousnessFlow**): **意识流引擎**

  - **核心职责**: 维护主思考循环，是整个 AI 的大脑“操作系统”。
  - **工作流程**:
    1.  **循环驱动**: 内部有一个 `while not self.stop_event.is_set()` 循环，以固定的时间间隔（`thinking_interval_seconds`）或由 `immediate_thought_trigger` 事件触发。
    2.  **焦点感知**: 在每次循环开始时，它会从 `ChatSessionManager` 获取当前的注意力焦点路径（如 `core`, `napcat_qq`, `napcat_qq.123456`）。
    3.  **上下文构建**: 请求 `PromptBuilder` 根据当前焦点，构建思考所需的所有信息（即 System 和 User Prompts）。
    4.  **思考生成**: 将构建好的 Prompts 交给 `ThoughtGenerator`，调用 LLM 生成一个包含内心状态和行动决策的 JSON。
    5.  **思想持久化**: 将 LLM 返回的决策交给 `ThoughtPersistor`，后者将其封装成一个 `ThoughtChainDocument`（思想点），并存入数据库中的“思想链”。
    6.  **决策分发**: 将决策 JSON 分发给 `decision_dispatcher` 模块进行处理，后者会进一步调用 `ActionHandler` (执行动作) 或 `ChatSessionManager` (切换焦点)。

- `prompt_builder.py` (**ThoughtPromptBuilder**): **情境构建大师**

  - **核心职责**: 根据 AI 当前的注意力焦点，动态地收集并格式化所有相关信息，为 LLM 生成结构化的、信息丰富的思考“原料”。
  - **信息来源**:
    - **内部信息 (`<internal_info>`)**: 从 `ThoughtStorageService` 获取最新的“思想点”，告诉 AI“你上一秒在想什么，做了什么，结果如何”。这是实现连贯思考的关键。
    - **外部信息 (`<external_info>`)**: 根据**注意力层级**调用不同的服务：
      - **核心层**: 调用 `UnreadInfoService` 获取所有平台的**宏观未读摘要**。
      - **平台层**: 调用 `UnreadInfoService` 获取特定平台下**所有会话的摘要列表**。
      - **细胞层 (专注聊天)**: 调用 `format_chat_history_for_llm` 获取当前会话的**详细聊天记录、用户列表**等。
    - **元信息 (`<meta_info>`)**: 在专注聊天模式下，调用 `BehavioralGuidanceGenerator` 生成社交技巧提示。
  - **输出**: 最终生成符合 `prompt_templates` 中定义的、包含 XML 标签的完整 `System Prompt` 和 `User Prompt`。

- `thought_generator.py` (**ThoughtGenerator**): **思维生产者**

  - **核心职责**: 封装了对 `LLMRequest` 客户端的调用。它的任务很纯粹：接收 `PromptBuilder` 准备好的“原料”，调用 LLM API，然后返回一个原始的 JSON 决策字符串。

- `thought_persistor.py` (**ThoughtPersistor**): **记忆雕刻师**

  - **核心职责**: 负责将 `ThoughtGenerator` 产生的 JSON 决策转化为一个结构化的 `ThoughtChainDocument` 对象，并调用 `ThoughtStorageService` 将其永久地刻入数据库的“思想链”中。

- `decision_dispatcher.py` (**决策分发器**): **行动意图翻译官**

  - **核心职责**: 这是一个独立的模块化函数，负责解析 `ConsciousnessFlow` 传来的 LLM 决策 JSON，并将其分发给正确的执行单元。
  - **分发逻辑**:
    - 如果决策中包含 `consciousness_control` 指令（如 `focus`, `return`），它会调用 `ChatSessionManager` 来改变 AI 的注意力焦点。
    - 如果决策中包含 `action` 指令（如 `send_message`），它会调用 `ActionHandler` 来执行具体动作。
    - **【高级逻辑】**: 它还会处理动作与意识控制的组合情况，例如，对于 `web_search` 这种需要等待结果的动作，它会先执行动作，拿到结果，然后将结果**“交接”**给即将要 `focus` 的新会话。

- `state_manager.py` (**AIStateManager**): (已演进)
  - **历史职责**: 在项目早期，可能负责管理内存中的 AI 状态。
  - **当前状态**: 在当前的“思想链”架构下，AI 的状态是**持久化且动态**的，其主要状态（心境、想法、目标）都存储在最新的“思想点”中。因此，该模块的功能已被更先进的 `InternalInfoBuilder` 和 `ThoughtStorageService` 所取代。它目前的代码反映了从思想链中提取信息的逻辑，作为 `InternalInfoBuilder` 的前身。

## 意识流转图

```mermaid
graph TD
    subgraph 外部世界
        A[外部事件]
    end

    subgraph 意识核心 (CoreLogic)
        B(思考循环)
        C(PromptBuilder)
        D(ThoughtGenerator)
        E(LLM)
        F(决策JSON)
        G(ThoughtPersistor)
        H(DecisionDispatcher)
    end

    subgraph 记忆 (Database)
        I[思想链]
        J[事件/会话]
    end

    subgraph 执行器
        K(ActionHandler)
        L(ChatSessionManager)
    end

    A -- 唤醒 --> B
    B -- 启动 --> C
    C -- 读取 --> I
    C -- 读取 --> J
    C -- 格式化Prompt --> D
    D -- 请求 --> E
    E -- 响应 --> F
    F --> G
    G -- 写入 --> I
    F --> H
    H -- 动作指令 --> K
    H -- 焦点控制 --> L
```

`core_logic` 模块通过上述组件的精密协作，构建了一个能够自我驱动、有记忆、能响应、会规划的 AI 核心。它是 AIcarusCore 实现“自主智能”的基石。
