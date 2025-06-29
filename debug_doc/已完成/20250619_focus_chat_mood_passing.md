# 专注聊天模式 Mood 字段传递改造

**负责人:** 小懒猫

**日期:** 2025-06-19

## 需求

1.  在专注聊天模式的循环中，`mood` 字段需要像 `think` 字段一样，能够从一轮的输出传递到下一轮的输入中。
2.  实现主意识与专注模式之间 `mood` 字段的双向传递：
    *   进入专注模式时，能够继承主意识的 `mood`。
    *   退出专注模式时，能够将最后的 `mood` 交接回主意识。

## 改造方案

### 第一阶段：专注模式内部 Mood 传递 (已完成)

1.  **修改 `AIcarusCore/src/sub_consciousness/chat_prompt_builder.py`:**
    *   在 `build_prompts` 方法中，从 `last_llm_decision` 中提取 `mood` 字段。
    *   将 `mood` 字段的内容加入到 `{previous_thoughts_block}` 的 prompt 模板中。

2.  **修改 `AIcarusCore/src/sub_consciousness/prompt_templates.py`:**
    *   更新 `GROUP_USER_PROMPT` 和 `PRIVATE_USER_PROMPT` 中的 JSON 输出格式说明，提示 LLM 需要衔接上一轮的心情。

### 第二阶段：主意识与专注模式 Mood 衔接

1.  **`consciousness_flow.py` (主思维)**:
    *   **进入专注**：打包 `mood` 并通过 `activate_session_by_id` 传递。
    *   **接收交接**：修改 `trigger_immediate_thought_cycle` 以接收 `mood`。

2.  **`chat_session_manager.py` (管理员)**:
    *   修改 `activate_session_by_id` 方法，增加 `core_last_mood` 参数，并传递给 `session.activate`。

3.  **`chat_session.py` (专注模式)**:
    *   修改 `activate` 方法，接收并存储 `core_last_mood`。

4.  **`chat_prompt_builder.py` (Prompt生成器)**:
    *   在专注模式首次思考时，将继承的 `mood` 写入 prompt。

5.  **`focus_chat_cycler.py` (专注模式循环)**:
    *   在结束专注时，打包 `mood` 并通过 `trigger_immediate_thought_cycle` 传递。

6.  **`state_manager.py` (状态管理员)**:
    *   修改 `set_next_handover_info` 和 `get_current_state_for_prompt`，以处理 `mood` 的存储和读取。


## 影响评估

*   **数据库存储:** `mood` 字段作为中间状态在内存中传递，不直接存入事件数据库。本次修改不影响数据库结构。
*   **功能影响:** 修改范围涉及主意识与子意识的交互流程，需要谨慎操作，但核心逻辑封闭在状态传递中，对外部功能无直接影响。

## 状态

- [ ] 进行中
