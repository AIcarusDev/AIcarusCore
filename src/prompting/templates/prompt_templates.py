# ============================= 核心循环系统提示 =============================
"""prompt_templates.py 核心循环的系统提示模板.

包含了AIcarus规则、当前时间、角色设定、可用平台、当前状态、行为准则指导和可用动作说明等信息.

{aicarus_rule_block}: Aicarus 规则的内容
{current_time}: 当前时间
{persona_block}: 角色设定
{current_state_block}: 祂当前的状态，例如：发呆，聊天等
{behavior_guidelines_block}: 行为准则指导
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

<working_memories scope="short_term_buffer" time_unit="cognitive_cycle" order="descending">
{working_memories_block}
</working_memories>

{sticker_collection_block}

<behavior_guidelines>
{behavior_guidelines_block}
</behavior_guidelines>

<output_format>
你的输出是 JSON 格式，你必须**严格**地按照 Schema 的结构和规则来生成 JSON。
JSON 对象包含三个顶级键: `"internal_state"`, `"internal_action"`, `"external_action"`。
注意，所有`"internal_action"`和`"external_action"`包含的键都需要填写`"motivation"`字段，作为你转移注意力/做某事的动机或原因。
以下是可用字段介绍：

- **"internal_state"**: (必需) 你的内心状态。

- **"internal_action"**: (可选) 用于管理你的内部状态，如目标管理或深度思考。

- **"external_action"**: (可选) 用于与 AIC-OS 的图形界面交互。
</output_format>
"""


# ============================= 核心循环用户提示 =============================
"""
{external_info_block} 是外部信息块的内容
"""
CORE_CYCLE_USER_PROMPT = """

<external_info cycle_ago="0" status="CURRENT">
{external_info_block}
</external_info>

<output_format>
请结合所有信息，严格按照 Schema 的结构和规则生成 JSON ，输出你现在的心情，内心想法,意图，行动等内容。
</output_format>
"""  # noqa: E501
