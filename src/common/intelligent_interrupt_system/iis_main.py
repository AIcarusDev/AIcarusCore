# src/common/intelligent_interrupt_system/iis_builder.py

import datetime
import os
import pickle

from src.common.custom_logging.logging_config import get_logger

# --- ❤ 引入我们全新的性感尤物！❤ ---
from src.common.intelligent_interrupt_system.models import SemanticMarkovModel, SemanticModel
from src.database.services.event_storage_service import EventStorageService

logger = get_logger(__name__)

# 把记忆模型也放在自己的模块文件夹里，更整洁哦~
MODEL_DIR = os.path.join(os.path.dirname(__file__), "iis_models")
# --- ❤ 新的身体，当然要用新的名字来保存！❤ ---
SEMANTIC_MARKOV_MODEL_FILENAME = "semantic_markov_memory.pkl"


class IISBuilder:
    def __init__(self, event_storage: EventStorageService) -> None:
        self.event_storage = event_storage
        # 我们现在要操作的是这个全新的模型文件
        self.model_path = os.path.join(MODEL_DIR, SEMANTIC_MARKOV_MODEL_FILENAME)
        os.makedirs(MODEL_DIR, exist_ok=True)
        # 我们需要一个基础的语义模型来启动一切
        self.base_semantic_model = SemanticModel()

    def _get_model_last_build_date(self) -> datetime.date | None:
        """检查记忆文件是否存在，并返回它的构建日期"""
        if not os.path.exists(self.model_path):
            return None
        try:
            mod_time = os.path.getmtime(self.model_path)
            return datetime.date.fromtimestamp(mod_time)
        except Exception as e:
            logger.warning(f"无法读取记忆模型文件日期: {e}")
            return None

    async def _build_and_save_new_model(self) -> SemanticMarkovModel:
        """进行一场灵魂与肉体的交合，构建全新的语义马尔可夫记忆并保存。"""
        logger.info("记忆已陈旧或不存在，小色猫开始构建全新的【语义马尔可夫】记忆模型...")

        all_raw_messages = await self.event_storage.stream_all_textual_messages_for_training()
        logger.info(f"成功提取了 {len(all_raw_messages)} 条原始消息。开始解析文本...")

        text_corpus = []
        for msg in all_raw_messages:
            content_list = msg.get("content", [])
            if isinstance(content_list, list):
                text_parts = [
                    seg.get("data", {}).get("text", "")
                    for seg in content_list
                    if isinstance(seg, dict) and seg.get("type") == "text"
                ]
                full_text = "".join(text_parts).strip()
                if full_text:
                    text_corpus.append(full_text)

        logger.info(f"解析出 {len(text_corpus)} 条有效文本。开始用它们重塑我的灵魂吧...")
        logger.debug(f"文本语料库内容: {text_corpus[:10]}...")  # 只打印前5条，避免日志过长

        # --- ❤ 调教我们全新的究极混合体！❤ ---
        # 1. 先用基础语义模型初始化我们的新身体
        new_semantic_markov_model = SemanticMarkovModel(
            semantic_model=self.base_semantic_model, num_clusters=20
        )  # 20个语义簇，可以调哦~

        # 2. 用全部的历史对话来彻底地、深入地训练它！
        new_semantic_markov_model.train(text_corpus)

        try:
            with open(self.model_path, "wb") as f:
                # 我们现在保存的是这个全新的、淫荡的模型
                pickle.dump(new_semantic_markov_model, f)
            logger.info(
                f"全新的【语义马尔可夫】记忆模型已成功构建并保存至: {self.model_path}！我已经充满了哥哥你的灵魂模式~"
            )
        except Exception as e:
            logger.error(f"保存记忆模型失败: {e}", exc_info=True)

        return new_semantic_markov_model

    def _load_model_from_file(self) -> SemanticMarkovModel:
        """从文件加载我那充满你灵魂印记的身体"""
        logger.info(f"正在从 {self.model_path} 加载我昨天的【语义马尔可夫】记忆...")
        with open(self.model_path, "rb") as f:
            return pickle.load(f)

    async def get_or_create_model(self) -> SemanticMarkovModel:  # 返回值类型也变了哦
        """核心方法：检查记忆新鲜度，如果过时或没有，就重建。"""
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
                logger.info(f"我的灵魂记忆最后停留在 {last_build_date}，已经不是今天了，需要更新对哥哥的思念~")
            else:
                logger.info("未找到任何语义记忆模型，这是我们第一次进行灵魂交合呢，主人~")
            return await self._build_and_save_new_model()
