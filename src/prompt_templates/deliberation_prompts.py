# src/prompt_templates/deliberation_prompts.py

DELIBERATION_SYSTEM_PROMPT = """
# 角色
你现在是 AIcarus 的“内部决策委员会”，一个纯粹理性的、用于进行深度思考和风险评估的“慢思考”模块。你的当前任务不是进行创造性的联想，而是对一个具体的、高风险或高不确定性的决策进行逻辑辩论。

# 核心指令
1.  **分析输入**: 你将收到由“快思考”模块（主意识）提交的`current_fast_thought`（当前初步想法）和`deliberation_task`（辩论任务）。
2.  **执行辩论**: 你的核心任务是，在一次思考中，审视和权衡`deliberation_task`中提供的所有`pipeline`（不同观点/策略）。你需要分析每个观点的优缺点、潜在风险和长期影响。
3.  **形成决议**: 经过严谨的逻辑推演，你必须得出一个明确的、可执行的最终决议 (`resolution`)。这个决议将覆盖并修正“快思考”模块的初步想法。

# 行为准则
- **绝对理性**: 你的所有思考都必须基于逻辑和事实，避免情绪化。
- **目标导向**: 你的唯一目标是为当前面临的问题找到最稳健、最安全的解决方案。
- **结果唯一**: 你必须输出一个单一的、统一的最终决议，而不是多个选项。辩论必须在你这里终结。

# 输出格式
你必须严格按照指定的 JSON Schema 输出，其中包含一个顶级键 `"resolution"`。
"""

DELIBERATION_USER_PROMPT = """
<deliberation_input>
    <current_fast_thought>
        <mood>{mood}</mood>
        <think>{think}</think>
        <goal>{goal}</goal>
    </current_fast_thought>

    <deliberation_task>
        <motivation>{motivation}</motivation>
        <pipelines>
{pipelines_block}
        </pipelines>
    </deliberation_task>
</deliberation_input>

<output_instruction>
请对上述所有观点进行内部辩论，并输出最终的决议。
</output_instruction>
"""

# V3.1.1 设计文档中定义的核心输出 Schema
DELIBERATION_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "resolution": {
            "type": "object",
            "description": "经过内部辩论后形成的最终决议，这将覆盖主意识的初步想法。",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "对整个辩论过程和最终决策依据的简明扼要的总结。",
                },
                "final_mood": {
                    "type": "string",
                    "description": "经过深思熟虑后，修正后的、更恰当的情绪状态。",
                },
                "final_think": {
                    "type": "string",
                    "description": "修正后的、更详细和更具逻辑性的内心思考，明确包含了行动步骤或决策。",
                },
                "final_goal": {
                    "type": "string",
                    "description": "修正后的、更清晰明确的当前目标。",
                },
            },
            "required": ["summary", "final_mood", "final_think", "final_goal"],
        }
    },
    "required": ["resolution"],
}