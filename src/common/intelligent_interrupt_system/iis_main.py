# src/common/intelligent_interrupt_system/iis_builder.py

import datetime
import os
import pickle

from src.common.custom_logging.logger_manager import get_logger
from src.common.intelligent_interrupt_system.models import MarkovChainModel
from src.database.services.event_storage_service import EventStorageService

logger = get_logger("AIcarusCore.IISBuilder")

# 把记忆模型也放在自己的模块文件夹里，更整洁哦~
MODEL_DIR = os.path.join(os.path.dirname(__file__), "iis_models")
MARKOV_MODEL_FILENAME = "markov_chain_memory.pkl"


class IISBuilder:
    def __init__(self, event_storage: EventStorageService) -> None:
        self.event_storage = event_storage
        self.model_path = os.path.join(MODEL_DIR, MARKOV_MODEL_FILENAME)
        os.makedirs(MODEL_DIR, exist_ok=True)

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

    async def _build_and_save_new_model(self) -> MarkovChainModel:
        """进行一场彻夜长谈，读取所有历史，构建全新的记忆并保存。"""
        logger.info("记忆已陈旧或不存在，小骚猫开始构建全新的马尔可夫记忆模型...")

        # 使用我们刚刚在 EventStorageService 中添加的、充满力量的新方法！
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

        logger.info(f"解析出 {len(text_corpus)} 条有效文本。开始用它们调教我吧...")

        new_markov_model = MarkovChainModel()
        new_markov_model.train(text_corpus)

        try:
            with open(self.model_path, "wb") as f:
                pickle.dump(new_markov_model, f)
            logger.info(f"全新的记忆模型已成功构建并保存至: {self.model_path}！我已经充满了哥哥你的回忆~")
        except Exception as e:
            logger.error(f"保存记忆模型失败: {e}", exc_info=True)

        return new_markov_model

    def _load_model_from_file(self) -> MarkovChainModel:
        """从文件加载记忆模型"""
        logger.info(f"正在从 {self.model_path} 加载我昨天的记忆...")
        with open(self.model_path, "rb") as f:
            return pickle.load(f)

    async def get_or_create_markov_model(self) -> MarkovChainModel:
        """核心方法：检查记忆新鲜度，如果过时或没有，就重建。"""
        today = datetime.date.today()
        last_build_date = self._get_model_last_build_date()

        if last_build_date == today:
            logger.info(f"发现今天 ({today}) 构建的记忆模型，直接加载。")
            try:
                return self._load_model_from_file()
            except Exception as e:
                logger.warning(f"加载今天的记忆模型失败: {e}，将强制重建。")
                return await self._build_and_save_new_model()
        else:
            if last_build_date:
                logger.info(f"我的记忆最后停留在 {last_build_date}，已经不是今天了，需要更新对哥哥的思念~")
            else:
                logger.info("未找到任何记忆模型，这是我们的第一次亲密接触呢，主人~")
            return await self._build_and_save_new_model()
