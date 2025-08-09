# src/focus_chat_mode/deliberation_service.py
from typing import TYPE_CHECKING, Any

from src.common.custom_logging.logging_config import get_logger

# 导入你的 JSON 解析工具
from src.common.json_parser.json_parser import parse_llm_json_response
from src.common.time_utils import get_formatted_time_for_llm
from src.config import config
from src.llmrequest.llm_processor import Client as LLMProcessorClient
from src.prompt_templates.deliberation_prompts import (
    DELIBERATION_RESPONSE_SCHEMA,
    DELIBERATION_SYSTEM_PROMPT,
    DELIBERATION_USER_PROMPT,
)

if TYPE_CHECKING:
    from src.focus_chat_mode.chat_session import ChatSession


logger = get_logger(__name__)


class DeliberationService:
    """负责执行一次性的、同步阻塞的内部辩论（慢思考）流程."""

    def __init__(self, deliberation_llm_client: LLMProcessorClient) -> None:
        self.deliberation_llm_client = deliberation_llm_client
        logger.info("DeliberationService 初始化完成。")

    async def execute(
        self,
        pipeline_params: dict,
        current_internal_state: dict,
        session: "ChatSession | None",
    ) -> dict[str, Any] | None:
        """执行内部辩论流程.

        Args:
            pipeline_params (dict): LLM返回的 'deep_think' 指令的参数。
            current_internal_state (dict): 当前的核心内部状态 (mood, think, goal)。
            session (ChatSession | None): 当前的会话实例，用于更新工作记忆。

        Returns:
            dict | None: 一个包含新内部状态 (mood, think, goal) 的字典，如果成功。
                        否则返回 None。
        """
        try:
            opinions_block_lines = []
            opinions = pipeline_params.get("opinions", [])
            for i, p in enumerate(opinions):
                tag = p.get("tag", f"观点 {i + 1}")
                thought = p.get("initial_thought", "无具体想法。")
                opinions_block_lines.append(f'            <pipeline tag="{tag}">')
                opinions_block_lines.append(
                    f"                <initial_thought>{thought}</initial_thought>"
                )
                opinions_block_lines.append("            </pipeline>")
            opinions_block = "\n".join(opinions_block_lines)

            persona_block = (
                f'你是"{config.persona.bot_name}"；'
                f"\n{config.persona.description}\n{config.persona.profile}"
            )

            system_prompt = DELIBERATION_SYSTEM_PROMPT.format(
                current_time=get_formatted_time_for_llm(),
                bot_name=config.persona.bot_name,
                slow_thought_persona=config.persona.slow_thought_persona,
            )

            user_prompt = DELIBERATION_USER_PROMPT.format(
                fast_thought_person_block=persona_block,
                mood=current_internal_state.get("mood", "未知"),
                think=current_internal_state.get("think", "未知"),
                goal=current_internal_state.get("goal", "未知"),
                motivation=pipeline_params.get("motivation", "无明确动机"),
                opinions_block=opinions_block,
            )

            raw_llm_response = await self.deliberation_llm_client.make_llm_request(
                prompt=user_prompt,
                system_prompt=system_prompt,
                is_stream=False,
                response_schema=DELIBERATION_RESPONSE_SCHEMA,
            )

            # 1. 检查原始响应是否有错误
            if not raw_llm_response or raw_llm_response.get("error"):
                logger.error(f"慢思考LLM调用失败: {raw_llm_response}")
                return None

            # 2. 从 'text' 字段中提取 JSON 字符串并进行解析
            deliberation_result_json = parse_llm_json_response(raw_llm_response.get("text"))

            # 3. 使用解析后的 JSON 对象进行验证
            if not deliberation_result_json or "resolution" not in deliberation_result_json:
                logger.error(
                    f"慢思考LLM返回结果格式不正确或解析失败: {raw_llm_response.get('text')}"
                )
                return None

            resolution = deliberation_result_json["resolution"]
            if session:
                session.working_memory = {
                    "summary": resolution.get("summary"),
                    "remaining_turns": resolution.get("memory_duration", 2),
                }
                logger.info(
                    f"[{session.conversation_id}] 慢思考决议已生成，工作记忆已更新。"
                    f"摘要将在接下来的 {session.working_memory['remaining_turns']} 轮思考中保持。"
                )

            new_internal_state = {
                "mood": resolution.get("final_mood"),
                "think": resolution.get("final_think"),
                "goal": resolution.get("final_goal"),
            }
            return new_internal_state

        except Exception as e:
            logger.error(f"执行“慢思考”决策管线时发生严重错误: {e}", exc_info=True)
            return None
