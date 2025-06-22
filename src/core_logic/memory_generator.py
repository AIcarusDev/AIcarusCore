# src/core_logic/memory_generator.py
import uuid
from typing import Any

from src.common.custom_logging.logger_manager import get_logger
from src.database.models import EpisodicMemoryDocument, MemoryMetadataDocument

logger = get_logger("AIcarusCore.CoreLogic.MemoryGenerator")

class MemoryGenerator:
    """
    负责从完成的思考周期中，生成结构化的体验记忆和元数据。
    """

    @staticmethod
    def generate_memory_from_thought(
        thought_data: dict[str, Any],
        conversation_id: str = "core_consciousness" # 核心意识流的记忆默认关联到这个特殊的会话ID
    ) -> tuple[EpisodicMemoryDocument | None, list[MemoryMetadataDocument]]:
        """
        根据一次完整的思考结果，创建体验记忆文档和元数据文档。

        Args:
            thought_data: 从 ThoughtGenerator 返回的思考结果字典。
            conversation_id: 这次思考关联的会话ID。

        Returns:
            一个元组，包含 (EpisodicMemoryDocument, list[MemoryMetadataDocument])。
            如果无法生成有效的记忆，则主文档为 None。
        """
        think_content = thought_data.get("think", "").strip()
        if not think_content:
            logger.debug("思考内容为空，不生成体验记忆。")
            return None, []

        memory_id = str(uuid.uuid4())
        
        # 1. 创建主记忆文档
        memory_doc = EpisodicMemoryDocument(
            _key=memory_id,
            memory_id=memory_id,
            conversation_id=conversation_id,
            subjective_description=think_content,
            source_event_ids=[], # TODO: 将来可以从上下文中追溯来源事件
            emotion_state=thought_data.get("mood"),
            importance_score=thought_data.get("importance", 0.5), # 从思考中获取重要性
        )

        # 2. 创建元数据文档
        metadata_docs = []
        
        # 添加基础元数据
        metadata_docs.append(MemoryMetadataDocument(memory_id=memory_id, meta_key="type", meta_value="thought_cycle"))
        if thought_data.get("mood"):
            metadata_docs.append(MemoryMetadataDocument(memory_id=memory_id, meta_key="mood", meta_value=thought_data["mood"]))

        # 从思考内容中提取关键词作为元数据 (简单实现)
        # TODO: 将来可以使用更复杂的NLP技术提取实体和关键词
        keywords = think_content.split() # 简单的按空格切分
        for keyword in keywords[:5]: # 只取前5个词作为示例
            if len(keyword) > 2: # 过滤掉太短的词
                metadata_docs.append(MemoryMetadataDocument(memory_id=memory_id, meta_key="keyword", meta_value=keyword.strip(",.!?")))

        logger.info(f"为思考生成了新的体验记忆，ID: {memory_id}")
        return memory_doc, metadata_docs
