# AIcarusCore 系统概览与数据流

本文档旨在提供 AIcarusCore 内部核心模块之间交互的高层视图，并详细描述一个典型事件从接收到响应的完整生命周期。

## 核心架构图

下图展示了 AIcarusCore 的主要组件以及它们之间的数据流和交互关系。

```mermaid
graph TD
    subgraph 外部适配器 (Platform Adapters)
        A[QQ Adapter]
    end

    subgraph 核心通信层 (Core Communication)
        B(CoreWebsocketServer)
        C(EventReceiver)
        D(ActionSender)
    end

    subgraph 消息与会话处理 (Message & Session Processing)
        E(DefaultMessageProcessor)
        F(ChatSessionManager)
        G[ChatSession]
    end

    subgraph AI意识核心 (Consciousness Core)
        H(CoreLogic / ConsciousnessFlow)
        I(PromptBuilder)
        J(ThoughtGenerator)
        K(ThoughtPersistor)
        L(ActionHandler)
    end

    subgraph 记忆与数据中心 (Database)
        M[Events Collection]
        N[Conversations Collection]
        O[Thought Chain Collection]
        P[Persons/Accounts Graph]
    end

    %% 定义连接关系
    A -- WebSocket Event --> B
    B -- 原始消息 --> C
    C -- ProtocolEvent --> E
    E -- 更新/创建会话 --> F
    F -- 管理 --> G

    E -- 触发思考(高优) --> H
    H -- 定期/被动思考 --> H

    H -- 构建Prompt --> I
    I -- 需上下文 --> F
    I -- 需历史事件 --> M
    I -- 需会话档案 --> N
    I -- 需上次思考 --> O
    I -- 需人物信息 --> P

    I -- 最终Prompt --> J
    J -- 调用LLM --> J
    J -- LLM决策(JSON) --> H

    H -- 存储思考 --> K
    K -- 写入 --> O

    H -- 分发动作 --> L
    L -- 发送指令 --> D
    D -- WebSocket Action --> B
    B -- 发送至 --> A

    F -- 需会话/人物信息 --> N & P
    L -- 需平台翻译 --> platform_builders(未在图中显示)
    L -- 需动作日志 --> Database(未在图中显示)
```

## 事件处理生命周期：一条 QQ 消息的旅程

让我们以“用户在 QQ 群里发送一条消息”为例，追踪数据在系统中的完整流转路径。

1.  **接收 (Adapter -> Core Communication)**

    - `QQ Adapter` 接收到消息，将其打包成符合 `aicarus-protocols` 规范的 `Event` JSON 字符串。
    - 通过 WebSocket 连接，将该 JSON 发送给 `CoreWebsocketServer`。
    - `CoreWebsocketServer` 将原始消息字符串交给 `EventReceiver`。

2.  **解析与预处理 (EventReceiver -> Message Processor)**

    - `EventReceiver` 解析 JSON，将其转换为一个 `ProtocolEvent` 对象。
    - `EventReceiver` 将 `ProtocolEvent` 对象分发给 `DefaultMessageProcessor`。
    - `DefaultMessageProcessor` 执行初步处理：
      - 为事件关联或创建 `Person` 和 `Account` 档案（存入 `Persons/Accounts Graph`）。
      - 更新或创建会话档案（存入 `Conversations Collection`）。
      - 为事件文本生成语义向量（Embedding）。
      - 将处理后的事件持久化到 `Events Collection`。

3.  **分发与唤醒 (Message Processor -> Consciousness Core)**

    - `DefaultMessageProcessor` 根据 AI 当前的**注意力焦点**（由 `ChatSessionManager` 维护）和消息的优先级（如是否被@），决定是否需要**立即唤醒** `CoreLogic` (即 `ConsciousnessFlow`)。
    - 如果 AI 正专注于当前会话，消息将被会话内的中断系统处理。如果 AI 在别处，高优先级消息会通过 `immediate_thought_trigger` 事件唤醒核心思考循环。

4.  **思考 (Consciousness Core)**

    - `CoreLogic` 的主思考循环被触发（无论是定时到达还是被动唤醒）。
    - 它请求 `PromptBuilder` 构建思考所需的上下文。
    - `PromptBuilder` 会像侦探一样，根据当前的**注意力层级**（核心/平台/细胞），从各个数据库服务中收集情报：
      - 从 `Thought Chain` 获取**上一次的思考内容** (`internal_info`)。
      - 如果处于“专注聊天模式”，从 `ChatSessionManager` 获取当前会话的**详细聊天记录和用户列表**。
      - 从 `EventStorageService` 和 `ConversationStorageService` 获取**未读消息摘要** (`external_info`)。
    - `PromptBuilder` 将所有信息组装成一个巨大的、结构化的 `System Prompt` 和 `User Prompt`。
    - `ThoughtGenerator` 接收到 Prompt，调用 `LLMRequest` 客户端向大语言模型发起请求。
    - LLM 返回一个包含内心活动 (`internal_state`) 和行动决策 (`action` / `consciousness_control`) 的 JSON 对象。

5.  **行动 (Consciousness Core -> Action -> Core Communication)**

    - `CoreLogic` 接收到 LLM 的决策 JSON。
    - 它首先调用 `ThoughtPersistor` 将本次的思考结果作为一个新的“思想点”存入 `Thought Chain Collection`，并链接到上一个点。
    - 然后，它将决策中的 `action` 部分分发给 `ActionHandler`。
    - `ActionHandler` 解析动作指令（例如 `send_message`）。
    - 它查找 `platform_builders` 注册中心，找到对应的平台“翻译官”（如 `QQBuilder`）。
    - `QQBuilder` 将通用的 `send_message` 指令翻译成 QQ 平台适配器能听懂的具体 `Event` 对象。
    - `ActionHandler` 将构建好的 `Event` 对象交给 `ActionSender`。
    - `ActionSender` 将 `Event` 对象序列化为 JSON，通过 `CoreWebsocketServer` 的 WebSocket 连接发送回 `QQ Adapter`。

6.  **执行与响应 (Adapter -> ...)**
    - `QQ Adapter` 收到动作指令，执行发送消息的操作。
    - 操作完成后，`QQ Adapter` 会发送一个 `action_response` 事件回传给 `CoreWebsocketServer`。
    - `EventReceiver` 捕获此响应，并交给 `ActionHandler` 内的 `PendingActionManager` 进行处理，后者会更新对应动作的状态日志，并将结果写入触发该动作的“思想点”中，完成闭环。

通过这个流程，AIcarusCore 实现了一个完整且自洽的“感知-思考-行动-反馈”循环。
