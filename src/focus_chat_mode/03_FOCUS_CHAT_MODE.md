# 专注聊天模式 (Focus Chat Mode)

`focus_chat_mode` 模块是 AIcarusCore 实现“细胞层（Cellular Level）”注意力的核心。当 AI 的意识 `focus` 到一个具体的会话时，这个模块就会被激活，将 AI 从一个宏观的观察者，转变为一个完全沉浸在当前对话中的参与者。

该模式下的 AI 拥有更强的上下文感知能力、更精细的社交行为策略以及长短期记忆机制。

## 核心组件与职责

- `chat_session_manager.py` (**ChatSessionManager**): **会话总管家**

  - **核心职责**:
    1.  **管理会话实例**: 维护一个全局的、以 `conversation_id` 为键的 `sessions` 字典，存储所有当前处于“专注”状态的 `ChatSession` 实例。
    2.  **创建与销毁**: 当 AI `focus` 或 `shift` 到一个新会话时，负责创建对应的 `ChatSession` 实例；当 AI `return` 或 `shift` 离开时，负责销毁实例并触发最终总结。
    3.  **维护焦点路径**: 持有 `current_focus_path` 状态，这是整个三层注意力模型的核心状态变量。
    4.  **处理意识控制**: 作为 `decision_dispatcher` 的主要执行者，负责处理 `focus`, `return`, `shift` 等指令，并更新 `current_focus_path`。

- `chat_session.py` (**ChatSession**): **独立会话档案室**

  - **核心职责**: 封装和管理**单个**专注会话的所有状态和逻辑。每个进入专注模式的群聊或私聊都会拥有一个独立的 `ChatSession` 实例。
  - **关键属性与功能**:
    - **状态维护**: 记录 `last_processed_timestamp`（上次处理消息的时间点），用于区分新旧消息。
    - **行为计数器**: 维护 `no_action_count` (连续不发言次数) 和 `consecutive_bot_messages_count` (连续发言次数)，为社交行为指导提供数据。
    - **中断信号 (`interrupt_signal`)**: 拥有一个专属于本会话的 `asyncio.Event`。当后台中断检查器发现高优先级新消息时，会设置此信号，以中断当前正在进行的 LLM 思考或动作执行。
    - **中断上下文 (`interruption_context`)**: 当中断发生时，用于存储中断现场的详细信息（如是哪个事件打断的，是在思考时还是在行动时打断的），这些信息将用于构建下一轮思考的 `internal_info` 块。
    - **回声等待 (`_echo_events`)**: 管理一个 `action_id` 到 `asyncio.Event` 的字典，用于实现 `send_message` 后的“回声等待”机制，确保 AI 在自己消息成功发送后再进行下一步思考。
    - **缓存**: 缓存了 `bot_profile_cache` (AI 在本会话的档案，如群名片) 和 `conversation_details_cache` (会话详情)，减少不必要的数据库和 API 请求。

- `summarization_manager.py` (**SummarizationManager**): **记忆整理师 (长期记忆)**

  - **核心职责**: 负责会话的**阶段性总结**和**最终总结**，为 AI 提供长期记忆。
  - **工作流程**:
    1.  **阶段性总结**: 在会话进行中，当“已读” (`status='read'`) 的消息数量超过阈值 (`summary_interval`) 时，它会被触发。它会调用 `SummarizationService` 将这些消息连同之前的内存摘要 (`current_handover_summary`) 一起进行整合，生成一份更新后的摘要，并存回内存。
    2.  **最终总结**: 当会话结束时（AI `return` 或 `shift` 离开），它会执行最后一次总结，并将最终的、完整的会话回忆录保存到数据库的 `conversation_summaries` 集合中。

- `behavioral_guidance_generator.py` (**BehavioralGuidanceGenerator**): **社交礼仪教练**

  - **核心职责**: 根据 `ChatSession` 中的行为计数器，动态生成社交行为建议，并注入到 `PromptBuilder` 的 `<meta_info>` 块中。
  - **指导逻辑**:
    - 如果 AI 连续发言多次而无人回应，它会提示：“你已经连续发送了 N 条消息，对方没有回应，请注意观察是否需要暂停发言。”
    - 如果 AI 连续多轮决定不发言，它会提示：“你已连续 N 次保持沉默，请观察话题是否已结束，可以考虑 `return` 离开。”
    - 这些提示帮助 LLM 做出更符合社交直觉的决策，避免刷屏或陷入不必要的沉默。

- `components.py`: **数据容器**
  - 定义了 `PromptComponents` 这个 `dataclass`，用于在 `PromptBuilder` 和 `CoreLogic` 之间整洁地传递构建 Prompt 所需的所有零散组件，避免了混乱的函数参数。

## 专注模式下的特殊工作流

### 1. 中断处理 (Interruption Handling)

- **哨兵任务**: 一旦 `ChatSession` 被激活，`CoreLogic` 会为其启动一个常驻的后台中断检查任务 (`_check_for_interruptions_task`)。
- **持续监控**: 该任务会以高频率（如每 0.5 秒）检查数据库，看当前会话是否有时间戳晚于 `last_processed_timestamp` 的新消息。
- **智能判断**: 如果有新消息，它会调用 `IntelligentInterrupter` 服务来判断这条新消息的重要性是否足以“打断”AI 当前的思考或行动。
- **发出信号**: 如果判断需要中断，后台任务会立即设置 `session.interrupt_signal` 这个 `asyncio.Event`，并将会话的中断上下文 `interruption_context` 填满。
- **竞速与取消**: 在 `CoreLogic` 的主循环中，LLM 的思考任务 (`thought_generator.generate_thought`) 和 `session.interrupt_signal.wait()` 是在一个 `asyncio.wait` 中进行“竞速”的。如果中断信号先完成，LLM 的思考任务就会被 `cancel()`，主循环立即进入下一轮，并利用 `interruption_context` 中的信息构建新的思考 Prompt。

### 2. 回声等待 (Echo Waiting)

- **背景**: 当 AI 决定发送消息时，指令从核心发出，经过适配器，再到聊天服务器，这个过程需要时间。如果 AI 在消息还未真正在聊天窗口出现时就开始下一轮思考，它的上下文就是不完整的。
- **机制**:
  1. `MessageBuilder` 在发送每条消息时，会返回一个唯一的 `action_id`。
  2. `ChatSession` 会调用 `wait_for_echo(action_id)`，这会为该 ID 创建一个 `asyncio.Event` 并 `await` 它。
  3. 当平台适配器成功发送消息后，聊天服务器会把这条消息作为普通事件推回给核心。
  4. `DefaultMessageProcessor` 识别出这是“AI 自己发出的消息”（回声），并从事件元数据中提取出原始的 `action_id`。
  5. `DefaultMessageProcessor` 调用对应 `ChatSession` 的 `signal_echo_received(action_id)` 方法，这会 `set()` 之前创建的那个 `asyncio.Event`。
  6. `wait_for_echo` 被唤醒，流程继续。
- **效果**: 确保了 AI 的思考总是基于一个**已经包含了它自己上一轮发言**的、完全同步的聊天记录。

通过这些精巧的设计，`focus_chat_mode` 模块将 AI 从一个简单的命令执行者，提升为了一个具备社交意识、长短期记忆和中断响应能力的智能对话伙伴。
