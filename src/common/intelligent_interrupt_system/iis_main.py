# src/common/intelligent_interrupt_system/iis_main.py
import datetime
import os
import pickle
from pathlib import Path

from src.common.custom_logging.logging_config import get_logger
from src.common.intelligent_interrupt_system.models import SemanticMarkovModel, SemanticModel
from src.database import CoreDBCollections
from src.database.services.event_storage_service import EventStorageService

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MODEL_DIR = PROJECT_ROOT / "data" / "models"
SEMANTIC_MARKOV_MODEL_FILENAME = "iis_markov.pkl"


class IISBuilder:
    """无状态的智能中断系统构建器."""

    def __init__(self, event_storage: EventStorageService) -> None:
        self.event_storage = event_storage
        self.conn_manager = event_storage.conn_manager
        self.model_path = os.path.join(MODEL_DIR, SEMANTIC_MARKOV_MODEL_FILENAME)
        os.makedirs(MODEL_DIR, exist_ok=True)
        self.base_semantic_model = SemanticModel()

    def _get_model_last_build_date(self) -> datetime.date | None:
        if not os.path.exists(self.model_path):
            return None
        try:
            mod_time = os.path.getmtime(self.model_path)
            return datetime.date.fromtimestamp(mod_time)
        except Exception as e:
            logger.warning(f"无法读取记忆模型文件日期: {e}")
            return None

    async def _build_and_save_new_model(self) -> SemanticMarkovModel:
        """构建一个全新的语义马尔可夫模型，并保存到文件中."""
        logger.info("记忆已陈旧或不存在，开始基于【事件向量】重建全新的语义马尔可夫模型...")

        query = """
            FOR doc IN @@collection
                FILTER doc.event_type LIKE 'message.%'
                AND doc.embedding != null
                FILTER HAS(doc, 'conversation_id_extracted')
            COLLECT convId = doc.conversation_id_extracted INTO conversation_group
            FILTER COUNT(conversation_group) >= 2
            LET sorted_vectors = (
                FOR item IN conversation_group
                SORT item.doc.timestamp ASC
                RETURN item.doc.embedding
            )
            RETURN sorted_vectors
        """
        bind_vars = {"@collection": CoreDBCollections.EVENTS}

        logger.info("正在从事件存储中提取所有预计算的事件向量...")
        all_conversations_vectors: list[list[list[float]]] = await self.conn_manager.execute_query(
            query, bind_vars
        )

        if not all_conversations_vectors:
            logger.warning("未能从数据库中提取到足够的事件向量来训练IIS模型。将创建一个空模型。")
            new_semantic_markov_model = SemanticMarkovModel(
                semantic_model=self.base_semantic_model, num_clusters=20
            )
            new_semantic_markov_model.initialize_empty()
        else:
            total_vectors_count = sum(len(conv) for conv in all_conversations_vectors)
            logger.info(
                f"成功从 {len(all_conversations_vectors)} 场有效对话中，"
                f"提取出 {total_vectors_count} 个事件向量，"
                f"开始训练新的语义马尔可夫模型..."
            )
            new_semantic_markov_model = SemanticMarkovModel(
                semantic_model=self.base_semantic_model, num_clusters=20
            )
            new_semantic_markov_model.train_from_vectors(all_conversations_vectors)

        try:
            with open(self.model_path, "wb") as f:
                pickle.dump(new_semantic_markov_model, f)
            logger.info(f"全新的【语义马尔可夫】记忆模型已成功构建并保存至: {self.model_path}！")
        except Exception as e:
            logger.error(f"保存记忆模型失败: {e}", exc_info=True)

        return new_semantic_markov_model

    def _load_model_from_file(self) -> SemanticMarkovModel:
        logger.info(f"正在从 {self.model_path} 加载昨天的【语义马尔可夫】记忆...")
        with open(self.model_path, "rb") as f:
            return pickle.load(f)

    async def get_or_create_model(self) -> SemanticMarkovModel:
        """获取或创建语义马尔可夫模型的实例."""
        today = datetime.date.today()
        last_build_date = self._get_model_last_build_date()

        if last_build_date == today:
            logger.info(f"发现今天 ({today}) 构建的【语义马尔可夫】记忆模型，直接加载。")
            try:
                return self._load_model_from_file()
            except Exception as e:
                logger.warning(f"加载今天的记忆模型失败: {e}，将强制重建。")
                return await self._build_and_save_new_model()
        else:
            if last_build_date:
                logger.info(
                    f"发现上次构建的【语义马尔可夫】记忆模型是 {last_build_date}，"
                    f"已经过期，今天是 {today}。"
                    f" 将重建一个新的模型。"
                )
            else:
                logger.info("未找到任何语义记忆模型，将构建一个全新的模型。")
            return await self._build_and_save_new_model()
