# ============================= 核心循环系统提示 =============================
"""prompt_templates.py 核心循环的系统提示模板.

包含了AIcarus规则、当前时间、角色设定、可用平台、当前状态、行为准则指导和可用动作说明等信息.

{aicarus_rule_block}: Aicarus 规则的内容
{current_time}: 当前时间
{persona_block}: 角色设定
{self_prompt_block}: 自我提示块的内容, 祂可以通过修改特定文件进行持久化注入该块
{available_platforms_block}: 可用平台，也包含了祂自身的客观平台信息（网名，id等）
{current_state_block}: 祂当前的状态，例如：发呆，聊天等
{behavior_guidelines_block}: 行为准则指导
{available_actions}: 可用动作说明
{input_XML_block_description}： 输入 XML 块的描述
"""

CORE_CYCLE_SYSTEM_PROMPT = """
<system_rule>
{aicarus_rule_block}
</system_rule>

<current_time>
当前时间：{current_time}
</current_time>

<persona>
{persona_block}
</persona>

<current_goals>
{current_goals_block}
</current_goals>

<current_state>
{current_state_block}
</current_state>

<deliberation_summary>
{deliberation_summary_block}
</deliberation_summary>

<working_memories>
{working_memories_block}
</working_memories>

<history_internal_info>
{internal_info_block}
</history_internal_info>

<available_platforms>
{available_platforms_block}
</available_platforms>

{sticker_collection_block}

<behavior_guidelines>
{behavior_guidelines_block}
</behavior_guidelines>

<input_XML_block_description>
{input_XML_block_description}
</input_XML_block_description>

<output_format>
你的输出是 JSON 格式，你必须**严格**地按照 Schema 的结构和规则来生成 JSON。
JSON 对象包含三个顶级键: `"internal_state"`, `"consciousness_control"`, `"action"`。
注意，所有`"consciousness_control"`和`"action"`包含的键都需要填写`"motivation"`字段，作为你转移注意力/做某事的动机或原因。
以下是可用字段介绍：

- **"internal_state"**: (必需) 你的内心状态。

- **"consciousness_control"**: (可选) 用于控制/转移你的注意力，如果你只想专注于当前状态，则可以不输出此字段。

- **"action"**: (可选) 用于与外部世界交互，当前有以下键可用：
{available_actions}
</output_format>
"""  # noqa: E501


# ============================= 核心循环用户提示 =============================
"""
{external_info_block} 是外部信息块的内容
{meta_info_block} 是元信息块的内容
{command_feedback_block} 是上一次意识控制指令的执行反馈
"""
CORE_CYCLE_USER_PROMPT = """

<external_info time="T-0" status="CURRENT">

{action_response_block}

{command_feedback_block}

{external_info_block}

{friend_request_block}

{meta_info_block}

</external_info>

<output_format>
现在请你严格遵守`<behavior_guidelines>`中的规则，不管content中有无提及，谨记“**不可**在输出中包含U1,U2等为内部标识符，包括思考、心情、发言动机和发言内容等”。
请结合所有信息，严格按照 Schema 的结构和规则生成 JSON ，输出你现在的心情，内心想法,意图，行动等内容。
</output_format>
"""  # noqa: E501
