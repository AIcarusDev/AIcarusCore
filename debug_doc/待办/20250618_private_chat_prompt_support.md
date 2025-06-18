# 专注聊天模式增加私聊逻辑支持

**日期:** 2025年06月18日

## 1. 目标

当前专注聊天（`FocusChatCycler`）的逻辑流中，Prompt 构建过程是为群聊场景硬编码的。本次改造的目标是增加对私聊场景的支持，使其能够根据聊天类型（群聊 vs 私聊）加载并使用不同的 Prompt 模板。

## 2. 问题分析

经过代码审查，发现当前实现存在以下问题：

1.  **`ChatPromptBuilder` 未感知类型**: `ChatSession` 在初始化 `ChatPromptBuilder` 时，没有将 `conversation_type` 参数传递过去。导致 `ChatPromptBuilder` 无法知道当前的会话是群聊还是私聊。
2.  **硬编码的群聊模板**: `chat_prompt_builder.py` 文件中，`SYSTEM_PROMPT_TEMPLATE` 全局变量中硬编码了“你当前正在参与qq群聊”的文本，导致所有会话都被当作群聊处理。
3.  **私聊模板未被使用**: 根目录下的 `私聊prompt模板.md` 文件虽然存在，但没有任何代码逻辑去读取和使用它。

## 3. 改造方案

采纳用户建议，将 Prompt 模板与构建逻辑分离，以提高代码的可读性和可维护性。

### 步骤 1: 创建新的 Prompt 模板文件

-   **新文件**: `AIcarusCore/src/sub_consciousness/prompt_templates.py`
-   **操作**:
    -   创建一个新文件用于统一存放所有 Prompt 模板。
    -   将 `chat_prompt_builder.py` 中现有的 `SYSTEM_PROMPT_TEMPLATE` 和 `USER_PROMPT_TEMPLATE` 移动到此文件，并重命名为 `GROUP_SYSTEM_PROMPT` 和 `GROUP_USER_PROMPT`。
    -   将 `私聊prompt模板.md` 中的内容转换为 Python 字符串，也存入此文件，命名为 `PRIVATE_SYSTEM_PROMPT` 和 `PRIVATE_USER_PROMPT`。

### 步骤 2: 修改 `ChatSession`

-   **文件**: `AIcarusCore/src/sub_consciousness/chat_session.py`
-   **操作**: 在 `__init__` 方法中，修改 `ChatPromptBuilder` 的实例化过程，将 `self.conversation_type` 传递给其构造函数。这一步与原计划一致，是必要操作。

    ```python
    # 修改后
    self.prompt_builder = ChatPromptBuilder(
        event_storage=self.event_storage,
        bot_id=self.bot_id,
        conversation_id=self.conversation_id,
        conversation_type=self.conversation_type
    )
    ```

### 步骤 3: 修改 `ChatPromptBuilder`

-   **文件**: `AIcarusCore/src/sub_consciousness/chat_prompt_builder.py`
-   **操作**:
    1.  **移除旧模板**: 删除文件顶部的 `SYSTEM_PROMPT_TEMPLATE` 和 `USER_PROMPT_TEMPLATE` 全局变量。
    2.  **导入新模板**: 从新建的 `prompt_templates.py` 文件中导入所有模板常量。
        ```python
        from . import prompt_templates
        ```
    3.  **修改 `__init__`**:
        -   接收 `conversation_type` 参数并保存为实例属性 `self.conversation_type`。
    4.  **修改 `build_prompts`**:
        -   在方法开头，根据 `self.conversation_type` 的值来选择使用 `prompt_templates.PRIVATE_...` 还是 `prompt_templates.GROUP_...` 系列模板。
        -   后续的 `.format()` 调用将使用这个动态选择的模板。
        -   处理私聊模板中的 `{user_nick}` 占位符的逻辑保持不变。
        -   调整 `user_list_block` 的生成逻辑，使其在私聊时不包含 `title` 和 `perm` 字段。

## 4. 风险评估

## 4. 风险评估

-   **影响范围**: 修改主要集中在 `chat_prompt_builder.py` 和 `chat_session.py`，核心循环逻辑 `focus_chat_cycler.py` 不受影响。
-   **数据流**: 不改变 `Event` 的数据结构和存储方式。
-   **功能逻辑**: 渐进式总结、动作执行等依赖于 LLM 响应的 JSON 结构，而私聊和群聊的 JSON 输出要求一致，因此这些功能不会被破坏。

**结论**: 风险较低，可以安全实施。
