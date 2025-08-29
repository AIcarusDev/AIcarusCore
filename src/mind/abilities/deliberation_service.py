# 文件路径: src/mind/abilities/deliberation_service.py

from typing import TYPE_CHECKING

from src.common.custom_logging.logging_config import get_logger
from src.common.json_parser.json_parser import parse_llm_json_response
from src.common.time_utils import get_formatted_time_for_llm
from src.config import config
from src.os.apps.interfaces import IApp
from src.os.apps.registry import platform_builder_registry  # 新增导入
from src.os.models import WindowStatus  # 新增导入
from src.prompting.templates.deliberation_prompts import (
    DELIBERATION_RESPONSE_SCHEMA,
    DELIBERATION_SYSTEM_PROMPT,
    DELIBERATION_USER_PROMPT,
)
from src.services.llmrequest.llm_processor import Client as LLMProcessorClient

if TYPE_CHECKING:
    from src.bootstrap.container import ServiceContainer  # 新增导入

logger = get_logger(__name__)


class DeliberationService:
    """负责执行一次性的、纯粹的慢思考流程."""

    def __init__(self, deliberation_llm_client: LLMProcessorClient) -> None:
        self.deliberation_llm_client = deliberation_llm_client
        logger.info("慢思考服务初始化完成。")

    def get_actions_schema(self) -> dict:
        """返回此服务提供的所有动作的 JSON Schema 定义."""
        return {
            "deep_think": {
                "title": "仔细想想",
                "type": "object",
                "description": "进行理性的深度思考，在遇到陌生、复杂、抽象问题，或高风险的决策时使用。",  # noqa: E501
                "properties": {
                    "motivation": {"type": "string"},
                    "opinions": {
                        "type": "array",
                        "description": "需要讨论的不同观点或策略，数量限制在2-5个。",
                        "maxItems": 5,
                        "items": {
                            "type": "object",
                            "properties": {
                                "tag": {
                                    "type": "string",
                                    "description": "你对此观点或策略的简短标签。",
                                },
                                "initial_thought": {
                                    "type": "string",
                                    "description": "你对此观点或策略的详细初始想法。",
                                },
                            },
                            "required": ["tag", "initial_thought"],
                        },
                    },
                },
                "required": ["motivation", "opinions"],
            },
        }

    async def execute(
        self,
        pipeline_params: dict,
        container: "ServiceContainer",
    ) -> None:
        """执行慢思考流程，并处理其副作用 (如更新会话记忆)."""
        # 从 container 中获取最新的内部状态
        latest_thought = await container.thought_storage_service.get_latest_thought_document()
        current_internal_state = {
            "mood": latest_thought.get("mood", "平静") if latest_thought else "平静",
            "think": latest_thought.get("think", "...") if latest_thought else "...",
            "intent": latest_thought.get("intent", "无") if latest_thought else "无",
        }

        try:
            opinions_block_lines = []
            opinions = pipeline_params.get("opinions", [])
            for i, p in enumerate(opinions):
                tag = p.get("tag", f"观点 {i + 1}")
                thought = p.get("initial_thought", "无具体想法。")
                opinions_block_lines.extend(
                    [
                        f'            <pipeline tag="{tag}">',
                        f"                <initial_thought>{thought}</initial_thought>",
                        "            </pipeline>",
                    ]
                )
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
                intent=current_internal_state.get("intent", "未知"),
                motivation=pipeline_params.get("motivation", "无明确动机"),
                opinions_block=opinions_block,
            )

            raw_llm_response = await self.deliberation_llm_client.make_llm_request(
                prompt=user_prompt,
                system_prompt=system_prompt,
                is_stream=False,
                response_schema=DELIBERATION_RESPONSE_SCHEMA,
            )

            if not raw_llm_response or raw_llm_response.get("error"):
                logger.error(f"慢思考LLM调用失败: {raw_llm_response}")
                return

            deliberation_result_json = parse_llm_json_response(raw_llm_response.get("text"))

            if not deliberation_result_json or "resolution" not in deliberation_result_json:
                logger.error(
                    f"慢思考LLM返回结果格式不正确或解析失败: {raw_llm_response.get('text')}"
                )
                return

            resolution = deliberation_result_json.get("resolution")
            logger.info(f"慢思考决议已生成: {resolution.get('summary')}")

            # [核心修改] 将关联会话的逻辑内聚到此服务中
            await self._handle_resolution_side_effects(resolution, container)

        except Exception as e:
            logger.error(f"执行“慢思考”时发生严重错误: {e}", exc_info=True)

    async def _handle_resolution_side_effects(
        self,
        resolution: dict,
        container: "ServiceContainer"
    ) -> None:
        """处理慢思考决议的副作用：更新会话记忆或全局记忆."""
        window_manager = container.window_manager
        state_manager = container.state_manager

        active_window = next(
            (
                w
                for w in reversed(window_manager.get_all_windows_sorted())
                if w.status != WindowStatus.MINIMIZE
            ),
            None,
        )

        # 场景一：当前在聊天窗口，将结论存入会话工作记忆
        if active_window and active_window.window_class == "conversation":
            conv_uid = active_window.content_state.get("conversation_uid")
            if conv_uid:
                platform_id = conv_uid.split("_")[0]
                builder = platform_builder_registry.get_builder(platform_id)
                if builder and isinstance(builder, IApp):
                    session = await builder.get_session(conv_uid, container)
                    if session:
                        session.working_memory = {
                            "summary": resolution.get("summary"),
                            "remaining_turns": resolution.get("memory_duration", 2),
                        }
                        logger.info(
                            f"[{session.conversation_id}] 慢思考决议已存入当前会话的工作记忆。"
                        )
                        # 触发思考，让结论立刻生效
                        container.core_logic.trigger_immediate_thought_cycle()
                        return # 处理完毕，直接返回

        # 场景二：不在聊天窗口，或获取 session 失败，存入全局战略备忘录
        logger.info(
            "慢思考在非聊天上下文中完成，或无法获取会话，决议将存入全局战略备忘录。"
        )
        state_manager.add_strategic_memo(resolution)
        container.core_logic.trigger_immediate_thought_cycle()
