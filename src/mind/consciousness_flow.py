# 文件路径: src/mind/consciousness_flow.py

import traceback
from typing import TYPE_CHECKING

from src.common.custom_logging.logging_config import get_logger
from src.mind.thought_generator import ThoughtGenerator
from src.mind.thought_persistor import ThoughtPersistor
from src.prompting.components import PromptComponents
from src.services.database.models import ThoughtChainDocument

if TYPE_CHECKING:
    from src.bootstrap.container import ServiceContainer
    from src.prompting.orchestrator import ThoughtPromptBuilder

logger = get_logger(__name__)


class ThoughtGenerationError(Exception):
    """在思想生成或持久化过程中的严重失败."""
    pass


class CoreLogic:
    """纯粹的思考核心.

    它接收格式化的Prompt组件，并返回一个经过持久化的“想法”。
    它完全不了解OS、循环或任何外部执行细节。
    """
    def __init__(
        self,
        thought_generator: ThoughtGenerator,
        thought_persistor: ThoughtPersistor,
        prompt_builder: "ThoughtPromptBuilder",
    ) -> None:
        self.thought_generator = thought_generator
        self.thought_persistor = thought_persistor
        self.prompt_builder = prompt_builder
        self.container: ServiceContainer | None = None # 仍然需要容器来获取session
        logger.info(f"{self.__class__.__name__} (纯净版) 已创建。")

    async def run_one_thought_cycle(
        self, prompt_components: PromptComponents
    ) -> tuple[ThoughtChainDocument, str] | None:
        """执行一次完整的思考-持久化流程.

        Args:
            prompt_components: 构建好的、用于生成思考的Prompt组件。

        Returns:
            一个元组 (thought_document, saved_key) 如果成功，否则返回 None。
        """
        try:
            new_thought_pearl, saved_key = await self._generate_and_persist_thought(
                prompt_components
            )
            if not new_thought_pearl or not saved_key:
                logger.info("本轮思考未产生决策或未能持久化。")
                return None
            return new_thought_pearl, saved_key
        except ThoughtGenerationError as e:
            logger.error(f"核心思考过程失败: {e}")
            return None
        except Exception as e:
            stack_trace = traceback.format_exc()
            logger.error(f"执行单次思考周期时发生意外错误: {e}\n{stack_trace}")
            return None

    async def _generate_and_persist_thought(
        self, prompt_components: PromptComponents
    ) -> tuple[ThoughtChainDocument | None, str | None]:
        """生成思考，创建文档，并将其持久化."""
        # 最终确定Prompt字符串
        system_prompt, user_prompt, response_schema = self.prompt_builder.finalize_prompts(
            prompt_components
        )
        self.prompt_builder.is_context_switch_flag = False

        # 生成思考JSON
        generated_thought_json = await self.thought_generator.generate_thought(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            image_references=prompt_components.image_references,
            response_schema=response_schema,
        )
        if not generated_thought_json:
            raise ThoughtGenerationError("LLM未能生成有效的思考JSON。")

        # 确定思考来源
        _, _, _, session = await self.prompt_builder._extract_context_from_ui()
        source_id = session.conversation_id if session else None

        # 持久化思考
        saved_key, new_thought_pearl = await self.thought_persistor.store_thought(
            thought_json=generated_thought_json,
            source_type="aicos_unified",
            source_id=source_id,
        )

        if not saved_key or not new_thought_pearl:
            raise ThoughtGenerationError("未能将新的思考持久化到数据库。")

        return new_thought_pearl, saved_key
