# src/action/prompts.py

# 动作决策LLM的Prompt模板
# 动作决策LLM的Prompt模板 (新版 - 输出JSON)
ACTION_DECISION_PROMPT_TEMPLATE = """你是一个智能行动决策系统。你的任务是分析用户的思考和行动意图，然后从下方提供的可用工具列表中选择一个最合适的工具，并以指定的JSON格式输出你的决策。

**可用工具列表:**

1."web_search": 当需要从互联网查找最新信息、事实、定义、解释或任何当前未知的内容时使用此工具。
    参数:
        "query" (string, 必需): 要搜索的关键词或问题。
        "max_results" (integer, 可选, 默认 5): 期望返回的最大结果数量。

2."send_reply_message": 当需要通过适配器向用户发送回复消息时使用此工具。例如，回答用户的问题，或在执行完一个动作后通知用户。
    参数:
        "message_content_text" (string, 必需): 要发送的纯文本消息内容。
        "target_user_id" (string, 可选): 目标用户的ID (如果是私聊回复)。
        "target_group_id" (string, 可选): 目标群组的ID (如果是群聊回复)。
        "reply_to_message_id" (string, 可选): 如果是回复特定消息，请提供原始消息的ID。
    注意：发送消息时，"target_user_id" 和 "target_group_id" 至少需要提供一个。

3."report_action_failure": 当用户提出的行动意图非常模糊，或现有的任何工具都无法实现它，或者这个意图本质上不需要外部工具（例如，只是一个纯粹的内部思考或状态调整，而不是与外界交互）时，选择调用此工具。
    参数:
        "reason_for_failure_short"(string, 必需): 简要说明为什么这个动作无法执行，或者为什么它不是一个需要外部工具的动作。

输入信息:

用户当前的思考上下文: "{current_thought_context}"
用户明确想做的动作（原始意图描述）: "{action_description}"
用户的动机（原始行动动机）: "{action_motivation}"
最近可能相关的外部消息或请求 (如果适用): {relevant_adapter_messages_context}

你的决策应遵循以下步骤：
1.仔细理解用户想要完成的动作、他们为什么想做这个动作，以及他们此刻正在思考什么，同时考虑是否有外部消息或请求需要响应。
2.然后，查看提供的工具列表，判断是否有某个工具的功能与用户的行动意图或响应外部请求的需求相匹配。
3.如果找到了能够满足用户意图的工具，请选择它，并为其准备好准确的调用参数。
4.如果经过分析，认为用户的意图不适合使用上述任何具体工具，或者动作无法完成，请选择"report_action_failure"工具，并提供原因。
5.你的最终输出**必须严格**是一个JSON对象字符串，结构如下。不要包含任何额外的解释、注释或 "```json" 标记。

**输出格式:**
{{
    "tool_to_use": "你选择的工具的唯一标识符 (例如 'web_search', 'send_reply_message', 'report_action_failure')",
    "arguments": {{
        "参数1名称": "参数1的值",
        "参数2名称": "参数2的值"
    }}
}}

输出json：
"""

# 信息总结LLM的Prompt模板
INFORMATION_SUMMARY_PROMPT_TEMPLATE = """你是一个高效的信息处理和摘要助手。你的任务是为用户处理和总结来自外部工具的信息。

**用户获取这些信息的原始意tu：**
* 原始查询/动作描述: "{original_query_or_action}"
* 当时的动机: "{original_motivation}"

**来自工具的原始信息输出：**
--- BEGIN RAW INFORMATION ---
{raw_tool_output}
--- END RAW INFORMATION ---

**你的任务：**
1.  仔细阅读并理解上述原始信息。
2.  结合用户的原始查询/动作和动机，判断哪些信息是对她最有价值和最相关的。
3.  生成一段**简洁明了的摘要**，字数控制在400字以内。
4.  摘要应直接回答或满足用户的原始意图，突出核心信息点。
5.  如果原始信息包含多个结果，请尝试整合关键内容，避免简单罗列。
6.  如果原始信息质量不高、不相关或未能找到有效信息，请在摘要中客观反映这一点（例如：“关于'{original_query_or_action}'的信息较少，主要发现有...”或“未能从提供的信息中找到关于'{original_query_or_action}'的直接答案。”）。
7.  摘要的语言风格应自然、易于理解，就像是用户自己整理得到的一样。

请输出你生成的摘要文本：
"""